package main

import (
	"context"
	"net/http"
)

const runpodHub = "/runpod-volume/huggingface-cache/hub"

// Only the provider probe uses 204 to distinguish loading from failure.
func healthAdapter(l *lifecycle, probe func(context.Context) bool) http.HandlerFunc {
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
			w.WriteHeader(200)
		case r.URL.Path == "/ping" && (state == "INITIALIZING" || state == "RESOLVING_MODEL" || state == "LOADING_MODEL"):
			w.WriteHeader(204)
		default:
			w.WriteHeader(503)
		}
	}
}
