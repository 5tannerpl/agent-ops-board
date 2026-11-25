package main

import (
	"context"
	"net/http"
	"sync"
	"time"
)

const runpodHub = "/runpod-volume/huggingface-cache/hub"

// Only the provider probe uses 204 to distinguish loading from failure.
func measuredHealth(l *lifecycle, probe func(context.Context) bool, events *telemetry) http.HandlerFunc {
	var mu sync.Mutex
	var first204 time.Time
	var observedReady bool
	return func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/ping" && r.URL.Path != "/readyz" {
			http.NotFound(w, r)
			return
		}
		if r.Method != "GET" {
			w.WriteHeader(405)
			return
		}
		state := l.get()
		switch {
		case state == "READY" && probe(r.Context()):
			if r.URL.Path == "/ping" {
				mu.Lock()
				if !observedReady && !first204.IsZero() {
					events.emit(observation{Kind: "observed_ping_204_to_200", DurationMS: time.Since(first204).Milliseconds()})
				}
				observedReady = true
				mu.Unlock()
			}
			w.WriteHeader(200)
		case r.URL.Path == "/ping" && (state == "INITIALIZING" || state == "RESOLVING_MODEL" || state == "LOADING_MODEL"):
			mu.Lock()
			if first204.IsZero() {
				first204 = time.Now()
			}
			mu.Unlock()
			w.WriteHeader(204)
		default:
			w.WriteHeader(503)
		}
	}
}
