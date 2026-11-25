package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"sync/atomic"
	"syscall"
	"time"
)

const privateURL = "http://127.0.0.1:18081"

var offloadLine = regexp.MustCompile(`offloaded ([1-9][0-9]*)/([1-9][0-9]*) layers to GPU`)

// Retain only the full-offload observation, never output arbitrary llama logs.
type offloadEvidence struct {
	ok  atomic.Bool
	buf []byte
}

func (s *offloadEvidence) Write(p []byte) (int, error) {
	n := len(p)
	for len(p) > 0 {
		i := bytes.IndexByte(p, '\n')
		part := p
		if i >= 0 {
			part = p[:i]
		}
		if room := 4096 - len(s.buf); room > 0 {
			s.buf = append(s.buf, part[:min(room, len(part))]...)
		}
		if i < 0 {
			break
		}
		m := offloadLine.FindSubmatch(s.buf)
		if len(m) > 0 && bytes.Equal(m[1], m[2]) {
			s.ok.Store(true)
		}
		s.buf = s.buf[:0]
		p = p[i+1:]
	}
	return n, nil
}

func startLlama(f *os.File, proof *offloadEvidence) (*exec.Cmd, error) {
	cmd := exec.Command("/app/llama-server", "-m", "/proc/self/fd/3", "--alias", modelAlias,
		"--host", "127.0.0.1", "--port", "18081", "--device", "CUDA0", "-ngl", "99", "-c", "4096",
		"--parallel", "1", "--fit", "off", "--offline", "--no-webui", "--no-mmproj")
	cmd.ExtraFiles = []*os.File{f}
	// No inherited inference secret, provider token, proxy, or LLAMA_ARG overrides.
	cmd.Env = []string{"PATH=/usr/bin:/bin", "CUDA_VISIBLE_DEVICES=0", "LD_LIBRARY_PATH=/app:" + os.Getenv("LD_LIBRARY_PATH")}
	cmd.Stderr = proof
	cmd.SysProcAttr = &syscall.SysProcAttr{Pdeathsig: syscall.SIGKILL}
	return cmd, cmd.Start()
}

func llamaHealthy(ctx context.Context, client *http.Client, url string) bool {
	req, err := http.NewRequestWithContext(ctx, "GET", url+"/health", nil)
	if err != nil {
		return false
	}
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return false
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1025))
	var status struct{ Status string }
	return err == nil && len(body) <= 1024 && json.Unmarshal(body, &status) == nil && status.Status == "ok"
}

type supervisor struct {
	resolve           func(context.Context) (*os.File, error)
	launch            func(*os.File, *offloadEvidence) (*exec.Cmd, error)
	probe             func(context.Context) bool
	set               func(string)
	timeout, interval time.Duration
}

func (s supervisor) run(ctx context.Context) error {
	startup, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()
	s.set("RESOLVING_MODEL")
	f, err := s.resolve(startup)
	if err != nil {
		return err
	}
	defer f.Close()
	if err := startup.Err(); err != nil {
		return err
	}
	s.set("LOADING_MODEL")
	proof := new(offloadEvidence)
	cmd, err := s.launch(f, proof)
	if err != nil {
		return errors.New("llama launch failed")
	}
	done := make(chan struct{})
	go func() { _ = cmd.Wait(); close(done) }()
	defer func() {
		_ = cmd.Process.Signal(syscall.SIGTERM)
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			_ = cmd.Process.Kill()
			<-done
		}
	}()
	tick := time.NewTicker(s.interval)
	defer tick.Stop()
	for {
		select {
		case <-startup.Done():
			return errors.New("model startup deadline")
		case <-done:
			return errors.New("llama exited during initialization")
		case <-tick.C:
			if proof.ok.Load() && s.probe(startup) && startup.Err() == nil {
				s.set("READY")
				select {
				case <-ctx.Done():
					return nil
				case <-done:
					return errors.New("llama exited after readiness")
				}
			}
		}
	}
}

func runtimePort(name string) (int, error) {
	v := os.Getenv(name)
	if v == "" {
		return 8080, nil
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < 1024 || n > 65535 || n == 18081 {
		return 0, errors.New("invalid listener port")
	}
	return n, nil
}
