package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

// Local, opt-in JSONL observations. This is not an OTLP exporter or a
// provider metric. The worker instance is a random per-process identifier.
type telemetry struct {
	queue   chan observation
	worker  string
	dropped atomic.Uint64
}

type observation struct {
	VerifiedModel  *verifiedModel    `json:"verified_model,omitempty"`
	Kind           string            `json:"kind"`
	At             time.Time         `json:"observed_at"`
	Worker         string            `json:"inference_worker_instance"`
	Model          string            `json:"model,omitempty"`
	RequestID      string            `json:"inference_request_id,omitempty"`
	TraceID        string            `json:"trace_id,omitempty"`
	SpanID         string            `json:"span_id,omitempty"`
	ParentID       string            `json:"parent_span_id,omitempty"`
	Link           string            `json:"trace_context,omitempty"`
	References     map[string]string `json:"correlation_sha256,omitempty"`
	Attempt        int               `json:"attempt,omitempty"`
	Attempted      bool              `json:"upstream_attempted,omitempty"`
	Stream         bool              `json:"stream,omitempty"`
	Status         int               `json:"http_status,omitempty"`
	Outcome        string            `json:"outcome,omitempty"`
	DurationMS     int64             `json:"duration_ms"`
	FirstContentMS *int64            `json:"first_content_ms,omitempty"`
	Usage          *chatUsage        `json:"usage,omitempty"`
	GPU            *gpuObservation   `json:"gpu,omitempty"`
	Dropped        uint64            `json:"queue_dropped_total,omitempty"`
	SinkFailed     uint64            `json:"sink_failed_total,omitempty"`
	ReadyEvidence  string            `json:"ready_evidence,omitempty"`
	ElapsedState   string            `json:"elapsed_state,omitempty"`
}

// Only constructed after actual content verification, never from request data.
type verifiedModel struct {
	Repo     string `json:"repo"`
	Revision string `json:"revision"`
	File     string `json:"filename"`
	SHA256   string `json:"sha256"`
}

func newTelemetry() *telemetry {
	t := &telemetry{}
	if os.Getenv("AOB_INFERENCE_TELEMETRY") != "true" {
		return t
	}
	t.queue = make(chan observation, 64)
	t.worker = randomID(16)
	go t.export(os.Stdout, os.Stderr)
	if os.Getenv("AOB_INFERENCE_GPU_OBSERVATIONS") == "true" {
		go func() {
			ticker := time.NewTicker(30 * time.Second)
			defer ticker.Stop()
			for {
				at := time.Now()
				g := sampleGPU()
				t.emit(observation{Kind: "gpu_window_sample", At: at.UTC(), DurationMS: time.Since(at).Milliseconds(), GPU: &g})
				<-ticker.C
			}
		}()
	}
	return t
}

// Report an outage once on the independent diagnostic stream, without the
// sink's error text. Resume with cumulative loss evidence on recovery.
func (t *telemetry) export(sink, diagnostic io.Writer) {
	var failed uint64
	outage := false
	for ev := range t.queue {
		ev.SinkFailed = failed
		if err := json.NewEncoder(sink).Encode(ev); err != nil {
			failed++
			if !outage {
				_, _ = io.WriteString(diagnostic, "{\"kind\":\"telemetry_sink_failure\"}\n")
			}
			outage = true
		} else {
			outage = false
		}
	}
}

func (t *telemetry) emit(ev observation) {
	if t == nil || t.queue == nil {
		return
	}
	if ev.At.IsZero() {
		ev.At = time.Now().UTC()
	}
	ev.Worker = t.worker
	ev.Dropped = t.dropped.Load()
	select {
	case t.queue <- ev:
	default:
		t.dropped.Add(1)
	}
}

func (t *telemetry) lifecycle(state string, duration time.Duration) {
	ev := observation{Kind: "lifecycle", Model: modelAlias, DurationMS: max(0, duration.Milliseconds())}
	switch state {
	case "READY":
		// Supervisor requires both a healthy model and full offload log evidence.
		ev.ReadyEvidence = "model_health_and_full_layer_offload"
		ev.ElapsedState = "LOADING_MODEL"
	case "INITIALIZING", "RESOLVING_MODEL", "LOADING_MODEL", "FAILED", "DRAINING":
	default:
		return
	}
	ev.Outcome = state
	t.emit(ev)
}

func randomID(n int) string {
	buf := make([]byte, n)
	_, _ = rand.Read(buf)
	return hex.EncodeToString(buf)
}

var (
	parentRE   = regexp.MustCompile(`^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$`)
	stateKeyRE = regexp.MustCompile(`^(?:[a-z][a-z0-9_*/-]{0,255}|[a-z0-9][a-z0-9_*/-]{0,240}@[a-z][a-z0-9_*/-]{0,13})$`)
	refRE      = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$`)
)

func allowlistedState(s string) string {
	if s == "" || len(s) > 512 {
		return ""
	}
	seen := map[string]bool{}
	state := ""
	for _, member := range strings.Split(s, ",") {
		k, v, ok := strings.Cut(strings.Trim(member, " \t"), "=")
		if !ok || !stateKeyRE.MatchString(k) || seen[k] || len(seen) >= 32 || len(v) == 0 || len(v) > 256 {
			return ""
		}
		for i := range len(v) {
			if v[i] < 0x20 || v[i] > 0x7e || v[i] == '=' {
				return ""
			}
		}
		seen[k] = true
		if k == "aob" {
			state = "aob=" + v
		}
	}
	return state
}

func requestEvent(h http.Header) (observation, http.Header) {
	ev := observation{Kind: "chat_request", Model: modelAlias, RequestID: randomID(16), SpanID: randomID(8)}
	out := make(http.Header)
	flags := "00"
	p := parentRE.FindStringSubmatch(h.Get("Traceparent"))
	if len(h.Values("Traceparent")) == 1 && p != nil && strings.Trim(p[1], "0") != "" && strings.Trim(p[2], "0") != "" {
		ev.TraceID, ev.ParentID, flags = p[1], p[2], p[3]
		ev.Link = "received_parent"
		states := strings.Join(h.Values("Tracestate"), ",")
		if allowed := allowlistedState(states); allowed != "" {
			out.Set("Tracestate", allowed)
		}
	} else {
		ev.TraceID = randomID(16)
		ev.Link = "local_root"
	}
	out.Set("Traceparent", "00-"+ev.TraceID+"-"+ev.SpanID+"-"+flags)
	out.Set("X-Inference-Request-ID", ev.RequestID)
	for _, name := range []string{"Run", "Unit", "Worker", "Deployment", "Source", "Correlation"} {
		k := "X-AOB-" + name + "-ID"
		v := h.Get(k)
		if len(h.Values(k)) == 1 && refRE.MatchString(v) {
			out.Set(k, v)
			if ev.References == nil {
				ev.References = map[string]string{}
			}
			d := sha256.Sum256([]byte(v))
			ev.References[strings.ToLower(name)] = hex.EncodeToString(d[:])
		}
	}
	if a := h.Get("X-AOB-Attempt"); len(h.Values("X-AOB-Attempt")) == 1 && len(a) <= 2 {
		if n, err := strconv.Atoi(a); err == nil && n > 0 && strconv.Itoa(n) == a {
			ev.Attempt = n
			out.Set("X-AOB-Attempt", a)
		}
	}
	return ev, out
}

type gpuObservation struct {
	Provenance  string `json:"provenance"`
	UUID        string `json:"uuid,omitempty"`
	Name        string `json:"name,omitempty"`
	Utilization *int   `json:"utilization_percent,omitempty"`
	Used        *int   `json:"used_mib,omitempty"`
	Total       *int   `json:"total_mib,omitempty"`
	Temperature *int   `json:"temperature_c,omitempty"`
}

var (
	gpuUUID = regexp.MustCompile(`^GPU-[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$`)
	gpuName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,79}$`)
)

func gpuNumber(s string, limit int) *int {
	n, err := strconv.Atoi(s)
	if err != nil || n < 0 || n > limit {
		return nil
	}
	return &n
}

func parseGPU(raw string) gpuObservation {
	g := gpuObservation{Provenance: "unavailable"}
	if len(raw) > 1024 {
		return g
	}
	p := strings.Split(strings.TrimSpace(raw), ",")
	if len(p) != 6 {
		return g
	}
	for i := range p {
		p[i] = strings.TrimSpace(p[i])
	}
	if !gpuUUID.MatchString(p[0]) || !gpuName.MatchString(p[1]) {
		return g
	}
	g.Provenance = "measured_nvidia_smi"
	g.UUID, g.Name = p[0], p[1]
	g.Utilization = gpuNumber(p[2], 100)
	g.Used = gpuNumber(p[3], 1<<22)
	g.Total = gpuNumber(p[4], 1<<22)
	if g.Used != nil && g.Total != nil && *g.Used > *g.Total {
		g.Used, g.Total = nil, nil
	}
	g.Temperature = gpuNumber(p[5], 150)
	return g
}

type sampleBuffer struct{ buf bytes.Buffer }

func (b *sampleBuffer) Write(p []byte) (int, error) {
	if len(p) > 1024-b.buf.Len() {
		return 0, errors.New("sample too large")
	}
	return b.buf.Write(p)
}

func sampleGPU() gpuObservation {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "nvidia-smi", "--id=0", "--query-gpu=uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits")
	// No shell, arguments from requests, inherited credentials or stderr logs.
	cmd.Env = []string{"LC_ALL=C", "LD_LIBRARY_PATH=" + os.Getenv("LD_LIBRARY_PATH")}
	cmd.WaitDelay = 250 * time.Millisecond
	var out sampleBuffer
	cmd.Stdout = &out
	if cmd.Run() != nil {
		return gpuObservation{Provenance: "unavailable"}
	}
	return parseGPU(out.buf.String())
}
