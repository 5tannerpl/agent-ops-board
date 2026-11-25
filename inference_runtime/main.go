package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"
)

type lifecycle struct {
	mu      sync.Mutex
	state   string
	since   time.Time
	observe func(string, time.Duration)
}

func (l *lifecycle) set(state string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.state == "DRAINING" {
		return
	}
	if l.observe != nil {
		l.observe(state, time.Since(l.since))
	}
	l.state, l.since = state, time.Now()
}
func (l *lifecycle) get() string { l.mu.Lock(); defer l.mu.Unlock(); return l.state }

func serve(signals context.Context) error {
	port, err := runtimePort("PORT")
	if err != nil {
		return err
	}
	healthPort, err := runtimePort("PORT_HEALTH")
	if err != nil {
		return err
	}
	key := os.Getenv("INFERENCE_KEY")
	if len(key) < 32 || len(key) > 256 {
		return errors.New("missing or invalid inference credential")
	}
	tel := newTelemetry()
	l := &lifecycle{state: "INITIALIZING", since: time.Now(), observe: tel.lifecycle}
	transport := &http.Transport{MaxConnsPerHost: 2, IdleConnTimeout: 5 * time.Second}
	defer transport.CloseIdleConnections()
	client := &http.Client{Timeout: time.Second, Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	probe := func(c context.Context) bool { return llamaHealthy(c, client, privateURL) }
	health := measuredHealth(l, probe, tel)
	mux := http.NewServeMux()
	mux.Handle("/ping", health)
	mux.Handle("/readyz", health)
	mux.Handle("/", newGateway(func() bool { return l.get() == "READY" && probe(signals) }, privateURL, key, tel))
	workers, stop := context.WithCancel(context.Background())
	defer stop()
	ended := make(chan error, 3)
	var servers []*http.Server
	defer func() {
		for _, srv := range servers {
			_ = srv.Close()
		}
	}()
	listen := func(port int, h http.Handler) error {
		ln, e := net.Listen("tcp", ":"+strconv.Itoa(port))
		if e != nil {
			return errors.New("listener failed")
		}
		srv := &http.Server{Handler: h, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 130 * time.Second, IdleTimeout: 15 * time.Second, MaxHeaderBytes: 8192, ErrorLog: log.New(io.Discard, "", 0)}
		servers = append(servers, srv)
		go func() { ended <- srv.Serve(ln) }()
		return nil
	}
	if err := listen(port, mux); err != nil {
		return err
	}
	if healthPort != port {
		if err := listen(healthPort, health); err != nil {
			return err
		}
	}
	mode := os.Getenv("MODEL_SOURCE")
	root := os.Getenv("MODEL_DIR")
	if mode == "cache" {
		root = runpodHub
	}
	spec := modelSpec{root, mode, os.Getenv("MODEL_REPO"), os.Getenv("MODEL_REVISION"), os.Getenv("MODEL_FILENAME"), os.Getenv("MODEL_SHA256")}
	resolve := func(c context.Context) (*os.File, error) {
		start := time.Now()
		f, err := resolveModel(c, spec)
		if err == nil {
			tel.emit(observation{Kind: "model_verified", Model: modelAlias, DurationMS: time.Since(start).Milliseconds(),
				VerifiedModel: &verifiedModel{spec.repo, spec.revision, spec.filename, spec.digest}})
		}
		return f, err
	}
	s := supervisor{resolve, startLlama, probe, l.set, 180 * time.Second, 250 * time.Millisecond}
	completed := make(chan error, 1)
	go func() { completed <- s.run(workers) }()
	// Independent watchdog also bounds a stalled mount read (which context
	// cancellation alone cannot interrupt). Once ready the startup clock ends.
	timer := time.NewTimer(s.timeout + time.Second)
	defer timer.Stop()
	for {
		select {
		case err := <-completed:
			l.set("FAILED")
			return err
		case <-timer.C:
			if l.get() != "READY" {
				l.set("FAILED")
				return errors.New("startup watchdog expired")
			}
		case <-ended:
			l.set("FAILED")
			return errors.New("listener stopped")
		case <-signals.Done():
			l.set("DRAINING")
			drain, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			for _, srv := range servers {
				_ = srv.Shutdown(drain)
			}
			cancel()
			stop()
			select {
			case <-completed:
			case <-time.After(3 * time.Second):
			}
			return nil
		}
	}
}

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	if err := serve(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "FAILED:", err)
		os.Exit(1)
	}
}
