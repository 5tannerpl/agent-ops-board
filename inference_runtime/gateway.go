package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net"
	"net/http"
	"slices"
	"strings"
	"time"
	"unicode/utf8"
)

const (
	modelAlias  = "Qwen3-4B-Instruct-2507"
	chatPath    = "/v1/chat/completions"
	maxInput    = 64 << 10
	maxOutput   = 1 << 20
	chatTimeout = 120 * time.Second
)

func newGateway(ready func() bool, upstream string, inferenceKey string, events *telemetry) http.Handler {
	slot := make(chan struct{}, 1)
	key := sha256.Sum256([]byte(inferenceKey))
	client := &http.Client{
		Transport: &http.Transport{
			DialContext:        (&net.Dialer{Timeout: 2 * time.Second}).DialContext,
			DisableCompression: true, MaxResponseHeaderBytes: 16 << 10,
			IdleConnTimeout: 30 * time.Second,
		},
		Timeout:       chatTimeout,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		if r.URL.EscapedPath() != chatPath {
			gatewayError(w, 404)
			return
		}
		if r.Method != http.MethodPost {
			methodError(w, "POST")
			return
		}
		got := sha256.Sum256([]byte(r.Header.Get("X-Inference-Key")))
		if inferenceKey == "" || len(r.Header.Values("X-Inference-Key")) != 1 || subtle.ConstantTimeCompare(got[:], key[:]) != 1 {
			gatewayError(w, 401)
			return
		}
		start := time.Now()
		ev, trace := requestEvent(r.Header)
		w.Header().Set("X-Inference-Request-ID", ev.RequestID)
		defer func() { ev.DurationMS = time.Since(start).Milliseconds(); events.emit(ev) }()
		fail := func(code int) { ev.Status = code; ev.Outcome = "error"; gatewayError(w, code) }
		if !ready() {
			fail(503)
			return
		}
		if !hasType(r.Header, "application/json") || len(r.Header.Values("Content-Encoding")) != 0 {
			fail(415)
			return
		}
		if r.ContentLength > maxInput {
			fail(413)
			return
		}
		select {
		case slot <- struct{}{}:
			defer func() { <-slot }()
		default:
			fail(429)
			return
		}
		deadline := start.Add(chatTimeout)
		controller := http.NewResponseController(w)
		_ = controller.SetReadDeadline(start.Add(10 * time.Second))
		_ = controller.SetWriteDeadline(deadline)
		ctx, cancel := context.WithDeadline(r.Context(), deadline)
		defer cancel()
		raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxInput))
		if err != nil {
			var tooLarge *http.MaxBytesError
			var netErr net.Error
			switch {
			case errors.As(err, &tooLarge):
				fail(413)
			case errors.As(err, &netErr) && netErr.Timeout():
				fail(408)
			default:
				fail(400)
			}
			return
		}
		_ = controller.SetReadDeadline(time.Time{})
		body, stream, ok := chatBody(raw)
		if !ok {
			fail(400)
			return
		}
		ev.Stream = stream
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(upstream, "/")+chatPath, bytes.NewReader(body))
		if err != nil {
			fail(502)
			return
		}
		req.Header = trace
		req.Header.Set("Content-Type", "application/json")
		ev.Attempted = true
		resp, err := client.Do(req)
		if err != nil {
			fail(upstreamError(err))
			return
		}
		defer resp.Body.Close()
		if resp.StatusCode != 200 {
			if resp.StatusCode >= 400 && resp.StatusCode <= 599 {
				fail(resp.StatusCode)
			} else {
				fail(502)
			}
			return
		}
		if len(resp.Header.Values("Content-Encoding")) != 0 {
			fail(502)
			return
		}
		if stream {
			if !hasType(resp.Header, "text/event-stream") {
				fail(502)
				return
			}
			committed, err := streamChat(w, resp.Body, &ev, start)
			if err != nil {
				if !committed {
					fail(upstreamError(err))
				} else {
					ev.Status = 200
					ev.Outcome = "stream_incomplete"
				}
				return
			}
		} else {
			if !hasType(resp.Header, "application/json") || resp.ContentLength > maxOutput {
				fail(502)
				return
			}
			raw, err := io.ReadAll(io.LimitReader(resp.Body, maxOutput+1))
			if err != nil {
				fail(upstreamError(err))
				return
			}
			if len(raw) > maxOutput {
				fail(502)
				return
			}
			data, _, ok := completion(raw, false, false, &ev, start)
			if !ok || len(data) > maxOutput {
				fail(502)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			if _, err := w.Write(data); err != nil {
				ev.Status = 200
				ev.Outcome = "write_failed"
				return
			}
		}
		ev.Status = 200
		ev.Outcome = "ok"
	})
}

func upstreamError(err error) int {
	var netErr net.Error
	if errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &netErr) && netErr.Timeout()) {
		return 504
	}
	return 502
}

func methodError(w http.ResponseWriter, allow string) {
	w.Header().Set("Allow", allow)
	gatewayError(w, 405)
}

func gatewayError(w http.ResponseWriter, code int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"message": http.StatusText(code), "type": "gateway_error"}})
}

func hasType(h http.Header, want string) bool {
	if len(h.Values("Content-Type")) != 1 {
		return false
	}
	t, _, err := mime.ParseMediaType(h.Get("Content-Type"))
	return err == nil && t == want
}

// Exact, non-null, non-duplicate keys. Never forward the original request body.
func fields(raw []byte, keys ...string) (map[string]json.RawMessage, bool) {
	d := json.NewDecoder(bytes.NewReader(raw))
	t, err := d.Token()
	if err != nil || t != json.Delim('{') {
		return nil, false
	}
	m := make(map[string]json.RawMessage)
	for d.More() {
		t, err = d.Token()
		key, ok := t.(string)
		if err != nil || !ok || !slices.Contains(keys, key) || m[key] != nil {
			return nil, false
		}
		var v json.RawMessage
		if d.Decode(&v) != nil || string(v) == "null" {
			return nil, false
		}
		m[key] = v
	}
	t, err = d.Token()
	if err != nil || t != json.Delim('}') {
		return nil, false
	}
	_, err = d.Token()
	return m, err == io.EOF
}

func chatBody(raw []byte) ([]byte, bool, bool) {
	if !utf8.Valid(raw) {
		return nil, false, false
	}
	m, ok := fields(raw, "model", "messages", "max_tokens", "temperature", "stream", "response_format", "stream_options")
	var model string
	if !ok || json.Unmarshal(m["model"], &model) != nil || model != modelAlias {
		return nil, false, false
	}
	var messages []json.RawMessage
	if json.Unmarshal(m["messages"], &messages) != nil || len(messages) < 1 || len(messages) > 64 {
		return nil, false, false
	}
	for _, msg := range messages {
		f, ok := fields(msg, "role", "content")
		var role, content string
		if !ok || json.Unmarshal(f["role"], &role) != nil || !slices.Contains([]string{"system", "user", "assistant"}, role) || json.Unmarshal(f["content"], &content) != nil || strings.TrimSpace(content) == "" {
			return nil, false, false
		}
	}
	if v := m["max_tokens"]; v != nil {
		var n int
		if json.Unmarshal(v, &n) != nil || n < 1 || n > 1024 {
			return nil, false, false
		}
	} else {
		m["max_tokens"] = json.RawMessage("600")
	}
	if v := m["temperature"]; v != nil {
		var n float64
		if json.Unmarshal(v, &n) != nil || n < 0 || n > 2 {
			return nil, false, false
		}
	}
	var stream bool
	if v := m["stream"]; v != nil && json.Unmarshal(v, &stream) != nil {
		return nil, false, false
	}
	if v := m["stream_options"]; v != nil {
		f, ok := fields(v, "include_usage")
		var b bool
		if !stream || !ok || json.Unmarshal(f["include_usage"], &b) != nil {
			return nil, false, false
		}
	}
	if v := m["response_format"]; v != nil {
		f, ok := fields(v, "type")
		var kind string
		if !ok || json.Unmarshal(f["type"], &kind) != nil || (kind != "text" && kind != "json_object") {
			return nil, false, false
		}
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	err := enc.Encode(m)
	return buf.Bytes(), stream, err == nil && buf.Len() <= maxInput
}

type chatMessage struct {
	Role    string  `json:"role,omitempty"`
	Content *string `json:"content,omitempty"`
}
type chatChoice struct {
	Index   int          `json:"index"`
	Message *chatMessage `json:"message,omitempty"`
	Delta   *chatMessage `json:"delta,omitempty"`
	Finish  *string      `json:"finish_reason"`
}
type chatUsage struct {
	Prompt     *int `json:"prompt_tokens"`
	Completion *int `json:"completion_tokens"`
	Total      *int `json:"total_tokens"`
}

// Project only supported result fields. In particular llama.cpp can also return
// the original prompt, server paths and implementation-specific timing fields.
func completion(raw []byte, stream, afterFinish bool, ev *observation, start time.Time) ([]byte, bool, bool) {
	var v struct {
		Choices []chatChoice    `json:"choices"`
		Usage   *chatUsage      `json:"usage"`
		Error   json.RawMessage `json:"error"`
	}
	if !utf8.Valid(raw) || json.Unmarshal(raw, &v) != nil || v.Error != nil || v.Choices == nil || len(v.Choices) > 1 {
		return nil, false, false
	}
	finished := false
	if len(v.Choices) == 0 {
		if !afterFinish || v.Usage == nil {
			return nil, false, false
		}
	} else {
		if afterFinish {
			return nil, false, false
		}
		c := &v.Choices[0]
		if c.Index != 0 || (c.Finish != nil && !slices.Contains([]string{"stop", "length", "content_filter"}, *c.Finish)) {
			return nil, false, false
		}
		finished = c.Finish != nil
		if stream {
			if c.Message != nil || c.Delta == nil || (c.Delta.Role != "" && c.Delta.Role != "assistant") {
				return nil, false, false
			}
			if c.Delta.Content != nil && *c.Delta.Content != "" && ev.FirstContentMS == nil {
				ms := time.Since(start).Milliseconds()
				ev.FirstContentMS = &ms
			}
		} else if c.Delta != nil || c.Message == nil || c.Message.Role != "assistant" || c.Message.Content == nil || !finished {
			return nil, false, false
		}
	}
	obj := "chat.completion"
	if stream {
		obj += ".chunk"
	}
	out := map[string]any{"id": "chatcmpl-" + ev.RequestID, "model": modelAlias, "object": obj, "created": start.Unix(), "choices": v.Choices}
	if u := v.Usage; u != nil {
		if u.Prompt == nil || u.Completion == nil || u.Total == nil || *u.Prompt < 0 || *u.Prompt > 1<<20 || *u.Completion < 0 || *u.Completion > 1024 || *u.Total != *u.Prompt+*u.Completion {
			return nil, false, false
		}
		ev.Usage = u
		out["usage"] = u
	}
	b, err := json.Marshal(out)
	return b, finished, err == nil
}

var errStream = errors.New("invalid or oversized completion stream")

// Only single-line JSON data events are supported. Truncation after headers
// have been sent deliberately omits [DONE]; never convert it into success.
func streamChat(w http.ResponseWriter, r io.Reader, ev *observation, start time.Time) (bool, error) {
	lr := &io.LimitedReader{R: r, N: maxOutput + 1}
	sc := bufio.NewScanner(lr)
	sc.Buffer(make([]byte, 4096), 64<<10)
	committed, finished := false, false
	total := 0
	controller := http.NewResponseController(w)
	var payload []byte
	for sc.Scan() {
		if lr.N == 0 {
			return committed, errStream
		}
		line := sc.Bytes()
		if len(line) > 0 {
			if line[0] == ':' {
				continue
			}
			data, ok := bytes.CutPrefix(line, []byte("data:"))
			if !ok || payload != nil {
				return committed, errStream
			}
			payload = bytes.Clone(bytes.TrimSpace(data))
			continue
		}
		if payload == nil {
			continue
		}
		p := payload
		payload = nil
		done := bytes.Equal(p, []byte("[DONE]"))
		if done {
			if !finished {
				return committed, errStream
			}
		} else {
			var stop, valid bool
			p, stop, valid = completion(p, true, finished, ev, start)
			if !valid {
				return committed, errStream
			}
			finished = finished || stop
		}
		total += len(p) + 8
		if total > maxOutput {
			return committed, errStream
		}
		w.Header().Set("Content-Type", "text/event-stream")
		committed = true
		if _, err := w.Write(append(append([]byte("data: "), p...), '\n', '\n')); err != nil {
			return committed, err
		}
		if err := controller.Flush(); err != nil {
			return committed, err
		}
		if done {
			return committed, nil
		}
	}
	if err := sc.Err(); err != nil {
		return committed, err
	}
	return committed, errStream
}
