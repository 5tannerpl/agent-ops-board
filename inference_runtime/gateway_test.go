package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const (
	testKey    = "unit-test-inference-secret"
	baseInput  = `{"model":"` + modelAlias + `","messages":[{"role":"user","content":"private-prompt"}]`
	baseResult = `{"model":"local/path","prompt":"private-prompt","choices":[{"index":0,"message":{"role":"assistant","content":"private-answer"},"finish_reason":"stop"}],"usage":{"completion_tokens":2,"prompt_tokens":3,"total_tokens":5}}`
)

func chatReq(body string) *http.Request {
	r := httptest.NewRequest("POST", chatPath, strings.NewReader(body))
	r.Header.Set("X-Inference-Key", testKey)
	r.Header.Set("Content-Type", "application/json")
	return r
}
func serveTestChat(h http.Handler, r *http.Request) *httptest.ResponseRecorder {
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}
func awake() bool { return true }

func TestChatWorkerContract(t *testing.T) {
	events := &telemetry{queue: make(chan observation, 1)}
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.URL.RequestURI() != chatPath {
			t.Errorf("wrong target %s", r.URL)
		}
		for _, k := range []string{"Authorization", "X-Inference-Key", "Cookie", "Baggage", "X-Forwarded-For"} {
			if r.Header.Get(k) != "" {
				t.Errorf("leaked %s", k)
			}
		}
		if !strings.HasPrefix(r.Header.Get("Traceparent"), "00-"+strings.Repeat("a", 32)+"-") || r.Header.Get("Tracestate") != "aob=test" || r.Header.Get("X-AOB-Unit-ID") != "unit-01" {
			t.Error("missing correlation")
		}
		var m map[string]any
		if json.NewDecoder(r.Body).Decode(&m) != nil || m["model"] != modelAlias || m["max_tokens"] != float64(600) || m["temperature"] != 0.0 || m["response_format"].(map[string]any)["type"] != "json_object" {
			t.Error("wrong worker request")
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Set-Cookie", "private-cookie")
		io.WriteString(w, baseResult)
	}))
	defer up.Close()
	h := newGateway(awake, up.URL, testKey, events)
	r := chatReq(baseInput + `,"temperature":0,"response_format":{"type":"json_object"}}`)
	r.URL.RawQuery = "private-query"
	for k, v := range map[string]string{"Authorization": "Bearer private-bearer", "Cookie": "private-cookie", "Baggage": "private-baggage", "Traceparent": "00-" + strings.Repeat("a", 32) + "-" + strings.Repeat("b", 16) + "-01", "Tracestate": "vendor=private-tracestate,aob=test", "X-AOB-Unit-ID": "unit-01"} {
		r.Header.Set(k, v)
	}
	w := serveTestChat(h, r)
	if w.Code != 200 || !strings.Contains(w.Body.String(), "private-answer") || strings.Contains(w.Body.String(), "private-prompt") || strings.Contains(w.Body.String(), "local/path") || w.Header().Get("Set-Cookie") != "" {
		t.Fatal("incorrect response projection", w.Code)
	}
	ev := <-events.queue
	if !ev.Attempted || ev.Status != 200 || ev.ParentID != strings.Repeat("b", 16) || ev.Usage == nil || *ev.Usage.Total != 5 || ev.RequestID != w.Header().Get("X-Inference-Request-ID") {
		t.Fatal("missing measurement")
	}
	log, _ := json.Marshal(ev)
	for _, secret := range []string{"private-", testKey, "unit-01"} {
		if strings.Contains(string(log), secret) {
			t.Errorf("leaked %q", secret)
		}
	}
}

func TestOnlyAuthorizedReadyChat(t *testing.T) {
	var ready atomic.Bool
	h := newGateway(ready.Load, "", testKey, nil)
	r := chatReq("")
	r.Header.Del("X-Inference-Key")
	r.Header.Set("Authorization", "Bearer "+testKey)
	if w := serveTestChat(h, r); w.Code != 401 {
		t.Fatal(w.Code)
	}
	if w := serveTestChat(h, chatReq("")); w.Code != 503 {
		t.Fatal(w.Code)
	}
	ready.Store(true)
	for _, path := range []string{"/ping", "/props", "/metrics", "/v1/models", "/health", "/v1/chat%2fcompletions"} {
		if w := serveTestChat(h, httptest.NewRequest("GET", path, nil)); w.Code != 404 {
			t.Errorf("%s returned %d", path, w.Code)
		}
	}
	if w := serveTestChat(h, httptest.NewRequest("GET", chatPath, nil)); w.Code != 405 || w.Header().Get("Allow") != "POST" {
		t.Fatal(w.Code)
	}
	if w := serveTestChat(newGateway(awake, "", "", nil), chatReq("")); w.Code != 401 {
		t.Fatal(w.Code)
	}
	r = chatReq("")
	r.Header.Add("X-Inference-Key", testKey)
	if w := serveTestChat(h, r); w.Code != 401 {
		t.Fatal(w.Code)
	}
}

func TestRejectBadChatBeforeDispatch(t *testing.T) {
	h := newGateway(awake, "", testKey, nil)
	for _, bad := range []string{"{}", baseInput + `,"temperature":null}`, baseInput + `,"stream":true,"stream":false}`, baseInput + `,"Model":"bad"}`, baseInput + `,"n":2}`, baseInput + `,"max_tokens":1025}`, baseInput + `,"max_tokens":0}`, baseInput + `,"temperature":2.1}`, baseInput + `,"stream_options":{"include_usage":true}}`, baseInput + `,"response_format":{"type":"json_schema"}}`, baseInput + `} {}`, strings.Replace(baseInput, "user", "tool", 1) + `}`, strings.Replace(baseInput, modelAlias, "other", 1) + `}`, strings.Replace(baseInput, `"private-prompt"`, `[{}]`, 1) + `}`} {
		if w := serveTestChat(h, chatReq(bad)); w.Code != 400 || strings.Contains(w.Body.String(), "private-prompt") {
			t.Fatalf("bad input: %d", w.Code)
		}
	}
	r := chatReq(strings.Repeat("a", maxInput+1))
	r.ContentLength = -1
	if w := serveTestChat(h, r); w.Code != 413 {
		t.Fatal(w.Code)
	}
	r = chatReq("{}")
	r.Header.Set("Content-Encoding", "gzip")
	if w := serveTestChat(h, r); w.Code != 415 {
		t.Fatal(w.Code)
	}
}

func TestBusyAndCancellation(t *testing.T) {
	started := make(chan struct{})
	cancelled := make(chan struct{})
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		close(started)
		<-r.Context().Done()
		close(cancelled)
	}))
	defer up.Close()
	h := newGateway(awake, up.URL, testKey, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	done := make(chan struct{})
	go func() { defer close(done); serveTestChat(h, chatReq(baseInput+`}`).WithContext(ctx)) }()
	select {
	case <-started:
	case <-ctx.Done():
		t.Fatal("no upstream request")
	}
	if w := serveTestChat(h, chatReq(baseInput+`}`)); w.Code != 429 {
		t.Fatal(w.Code)
	}
	cancel()
	select {
	case <-cancelled:
	case <-time.After(time.Second):
		t.Fatal("upstream not cancelled")
	}
	<-done
}

func TestBadUpstreamResponses(t *testing.T) {
	for _, tc := range []struct {
		status     int
		mime, body string
		want       int
	}{
		{429, "application/json", "private-upstream-error", 429},
		{307, "text/plain", "private-redirect", 502},
		{200, "text/plain", baseResult, 502},
		{200, "application/json", strings.Repeat("a", maxOutput+1), 502},
		{200, "application/json", baseResult + `{}`, 502},
		{200, "application/json", `{"choices":[{"message":{"role":"user","content":"private-error"}}]}`, 502},
		{200, "application/json", `{"error":{"message":"private-error"}}`, 502},
	} {
		up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Location", "/do-not-follow")
			w.Header().Set("Content-Type", tc.mime)
			w.WriteHeader(tc.status)
			io.WriteString(w, tc.body)
		}))
		w := serveTestChat(newGateway(awake, up.URL, testKey, nil), chatReq(baseInput+`}`))
		up.Close()
		if w.Code != tc.want || strings.Contains(w.Body.String(), "private-") || w.Header().Get("Location") != "" {
			t.Errorf("upstream %d => %d", tc.status, w.Code)
		}
	}
}

func TestStreamBoundariesAndRedaction(t *testing.T) {
	first := `data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"answer"}}],"extra":"private-hidden"}` + "\n\n"
	last := `data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}` + "\n\n"
	usage := `data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}` + "\n\n"
	for _, tc := range []struct {
		body    string
		status  int
		outcome string
	}{
		{": ignore\n\n" + first + last + usage + "data: [DONE]\n\n", 200, "ok"},
		{first, 200, "stream_incomplete"},
		{first + "data: [DONE]\n\n", 200, "stream_incomplete"},
		{first + last + first + "data: [DONE]\n\n", 200, "stream_incomplete"},
		{first + strings.Repeat(": ignored\n", maxOutput/8), 200, "stream_incomplete"},
		{"data: " + strings.Repeat("x", 70000), 502, "error"},
		{"data: {\"error\":\"private-oops\"}\n\n", 502, "error"},
	} {
		up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
			io.WriteString(w, tc.body)
		}))
		events := &telemetry{queue: make(chan observation, 1)}
		w := serveTestChat(newGateway(awake, up.URL, testKey, events), chatReq(baseInput+`,"stream":true,"stream_options":{"include_usage":true}}`))
		up.Close()
		ev := <-events.queue
		if w.Code != tc.status || ev.Outcome != tc.outcome || strings.Contains(w.Body.String(), "private-") || strings.Contains(w.Body.String(), "[DONE]") != (ev.Outcome == "ok") {
			t.Fatalf("stream: status %d outcome %s", w.Code, ev.Outcome)
		}
		if ev.Outcome == "ok" && (ev.FirstContentMS == nil || ev.Usage == nil || !strings.Contains(w.Body.String(), "answer")) {
			t.Fatal("missing stream data")
		}
	}
}
