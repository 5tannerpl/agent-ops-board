package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestTraceContextAndCorrelation(t *testing.T) {
	valid := "00-" + strings.Repeat("a", 32) + "-" + strings.Repeat("b", 16) + "-01"
	for _, p := range []string{"", strings.Replace(valid, "00-", "ff-", 1), strings.ReplaceAll(valid, "a", "0"), strings.ReplaceAll(valid, "b", "0"), strings.ToUpper(valid), valid + "," + valid} {
		h := make(http.Header)
		h.Set("traceparent", p)
		h.Set("tracestate", "aob=private")
		h.Set("X-AOB-Run-ID", "untrusted data")
		h.Set("X-AOB-Worker-ID", "worker-0")
		h.Set("X-AOB-Attempt", "2")
		ev, out := requestEvent(h)
		if ev.Link != "local_root" || ev.ParentID != "" || !parentRE.MatchString(out.Get("Traceparent")) || out.Get("Tracestate") != "" || out.Get("X-AOB-Run-ID") != "" || ev.Attempt != 2 || len(ev.References["worker"]) != 64 {
			t.Fatal("invalid context handling")
		}
	}
	for _, state := range []string{"vendor=private", "aob=ok,Key=value", "aob=1,aob=2", "aob=b=c", "aob=" + strings.Repeat("z", 257), "aob=bad\tvalue", "=value"} {
		h := make(http.Header)
		h.Set("Traceparent", valid)
		h.Set("Tracestate", state)
		ev, out := requestEvent(h)
		if ev.TraceID != strings.Repeat("a", 32) || out.Get("Traceparent") == valid || out.Get("Tracestate") != "" {
			t.Fatal("state not independently validated")
		}
	}
}

func TestOptInAndBoundedObservations(t *testing.T) {
	t.Setenv("AOB_INFERENCE_TELEMETRY", "false")
	t.Setenv("AOB_INFERENCE_GPU_OBSERVATIONS", "true")
	if newTelemetry().queue != nil {
		t.Fatal("export must be disabled")
	}
	events := &telemetry{queue: make(chan observation, 1)}
	events.lifecycle("private-file-path", time.Second)
	events.lifecycle("READY", time.Second)
	events.lifecycle("FAILED", time.Second)
	ev := <-events.queue
	if ev.Outcome != "READY" || ev.ElapsedState != "LOADING_MODEL" || ev.ReadyEvidence != "model_health_and_full_layer_offload" || events.dropped.Load() != 1 {
		t.Fatal("lifecycle leak or unbounded queue")
	}
	for _, state := range []string{"INITIALIZING", "RESOLVING_MODEL", "LOADING_MODEL", "FAILED", "DRAINING"} {
		events.lifecycle(state, time.Second)
		if e := <-events.queue; e.Outcome != state || e.Dropped != 1 || e.DurationMS != 1000 || e.ReadyEvidence != "" {
			t.Fatal("incorrect lifecycle metadata")
		}
	}
}

type recoveringSink struct {
	strings.Builder
	failures int
}

func (s *recoveringSink) Write(p []byte) (int, error) {
	if s.failures > 0 {
		s.failures--
		return 0, errors.New("private sink details")
	}
	return s.Builder.Write(p)
}

func TestSinkFailuresAndRecovery(t *testing.T) {
	events := &telemetry{queue: make(chan observation, 3)}
	for range 3 {
		events.lifecycle("FAILED", 0)
	}
	close(events.queue)
	sink := &recoveringSink{failures: 2}
	var diagnostic strings.Builder
	events.export(sink, &diagnostic)
	var ev observation
	if diagnostic.String() != "{\"kind\":\"telemetry_sink_failure\"}\n" || json.Unmarshal([]byte(sink.String()), &ev) != nil || ev.SinkFailed != 2 || ev.Dropped != 0 {
		t.Fatal("missing sanitized failure or recovery evidence")
	}
}

func TestNVMLUnavailableIsNotZero(t *testing.T) {
	prefix := "GPU-12345678-1234-1234-1234-123456789abc, NVIDIA Test GPU, "
	g := parseGPU(prefix + "0, 0, 16384, [N/A]\n")
	if g.Provenance != "measured_nvidia_smi" || g.Used == nil || *g.Used != 0 || g.Utilization == nil || g.Temperature != nil {
		t.Fatal(g)
	}
	g = parseGPU(prefix + "101, 20000, 16384, 40")
	if g.Utilization != nil || g.Used != nil || g.Total != nil || g.Temperature == nil {
		t.Fatal(g)
	}
	for _, bad := range []string{"", "private-driver-error", prefix + "0,0,1,1\n" + prefix + "1,1,1,1", strings.Repeat("z", 1050)} {
		if g := parseGPU(bad); g.Provenance != "unavailable" || g.Used != nil {
			t.Fatal("fabricated or unsanitized GPU data")
		}
	}
	t.Setenv("PATH", "")
	if g := sampleGPU(); g.Provenance != "unavailable" {
		t.Fatal("missing binary must not yield metrics")
	}
}
