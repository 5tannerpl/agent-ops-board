package main

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"sync/atomic"
	"testing"
	"time"
)

func TestFakeChild(t *testing.T) {
	switch os.Getenv("AOB_TEST_CHILD") {
	case "exit":
		os.Exit(1)
	case "wait", "partial", "serve":
		ln, err := net.FileListener(os.NewFile(3, "test-listener"))
		if err != nil {
			os.Exit(2)
		}
		if os.Getenv("AOB_TEST_CHILD") == "serve" {
			fmt.Fprintln(os.Stderr, "load: offloaded 37/37 layers to GPU")
		}
		if os.Getenv("AOB_TEST_CHILD") == "partial" {
			fmt.Fprintln(os.Stderr, "load: offloaded 1/37 layers to GPU")
		}
		_ = http.Serve(ln, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, `{"status":"ok"}`) }))
		os.Exit(0)
	}
}

func TestSupervise(t *testing.T) {
	for _, mode := range []string{"exit", "wait", "partial", "serve"} {
		t.Run(mode, func(t *testing.T) {
			ln, err := net.ListenTCP("tcp", &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)})
			if err != nil {
				t.Fatal(err)
			}
			fd, err := ln.File()
			ln.Close()
			if err != nil {
				t.Fatal(err)
			}
			defer fd.Close()
			base := "http://" + ln.Addr().String()
			ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
			defer cancel()
			var ready atomic.Bool
			var child *exec.Cmd
			s := supervisor{
				resolve: func(context.Context) (*os.File, error) { return os.Open(os.DevNull) },
				launch: func(_ *os.File, p *offloadEvidence) (*exec.Cmd, error) {
					child = exec.Command(os.Args[0], "-test.run=^TestFakeChild$")
					child.Env = []string{"AOB_TEST_CHILD=" + mode}
					child.Stderr = p
					child.ExtraFiles = []*os.File{fd}
					return child, child.Start()
				},
				probe: func(c context.Context) bool {
					return llamaHealthy(c, &http.Client{Timeout: 100 * time.Millisecond}, base)
				},
				set: func(state string) {
					if state == "READY" {
						ready.Store(true)
						cancel()
					}
				},
				timeout: 500 * time.Millisecond, interval: 10 * time.Millisecond,
			}
			err = s.run(ctx)
			if mode == "serve" {
				if err != nil || !ready.Load() {
					t.Fatal("did not become ready", err)
				}
			} else if err == nil || ready.Load() {
				t.Fatal("unexpected success")
			}
			if child.ProcessState == nil {
				t.Fatal("child was not reaped")
			}
		})
	}
}

func TestProviderHealth(t *testing.T) {
	l := &lifecycle{state: "INITIALIZING", since: time.Now()}
	var healthy bool
	tel := &telemetry{queue: make(chan observation, 2)}
	h := measuredHealth(l, func(context.Context) bool { return healthy }, tel)
	check := func(path string, status int) {
		t.Helper()
		r := httptest.NewRecorder()
		h.ServeHTTP(r, httptest.NewRequest("GET", path, nil))
		if r.Code != status {
			t.Errorf("%s in %s: %d != %d", path, l.get(), r.Code, status)
		}
	}
	for _, s := range []string{"INITIALIZING", "RESOLVING_MODEL", "LOADING_MODEL"} {
		l.set(s)
		check("/ping", 204)
		check("/readyz", 503)
	}
	l.set("READY")
	check("/ping", 503)
	healthy = true
	check("/readyz", 200)
	check("/ping", 200)
	check("/ping", 200)
	if len(tel.queue) != 1 || (<-tel.queue).Kind != "observed_ping_204_to_200" {
		t.Fatal("expected one observed probe transition")
	}
	l.set("FAILED")
	check("/ping", 503)
	l.set("DRAINING")
	l.set("READY")
	check("/ping", 503)
}
