package main

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestResolve(t *testing.T) {
	data := []byte("GGUF\x03\x00\x00\x00synthetic bytes, not a model")
	for _, name := range []string{"local", "cache", "link", "missing", "revision", "checksum", "traversal", "foreign link", "snapshot symlink", "layout", "format", "unsupported quant", "cancel"} {
		t.Run(name, func(t *testing.T) {
			s := modelSpec{t.TempDir(), "cache", "test/example", strings.Repeat("a", 40), "test-Q8_0.gguf", fmt.Sprintf("%x", sha256.Sum256(data))}
			repo := filepath.Join(s.root, "models--test--example")
			snap := filepath.Join(repo, "snapshots", s.revision)
			dir := snap
			if name == "local" {
				s.mode = "local"
				dir = s.root
			}
			if err := os.MkdirAll(dir, 0700); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(dir, s.filename)
			write := func(path string, data []byte) {
				t.Helper()
				if err := os.WriteFile(path, data, 0600); err != nil {
					t.Fatal(err)
				}
			}
			link := func(from, to string) {
				t.Helper()
				if err := os.Symlink(from, to); err != nil {
					t.Fatal(err)
				}
			}
			write(filepath.Join(dir, "aaa-Q4.gguf"), []byte("wrong"))
			ctx := context.Background()
			switch name {
			case "link":
				if err := os.Mkdir(filepath.Join(repo, "blobs"), 0700); err != nil {
					t.Fatal(err)
				}
				write(filepath.Join(repo, "blobs", s.digest), data)
				link("../../blobs/"+s.digest, path)
			case "foreign link":
				link("/etc/hosts", path)
			case "snapshot symlink":
				write(path, data)
				s.revision = strings.Repeat("c", 40)
				link(snap, filepath.Join(filepath.Dir(snap), s.revision))
			case "format":
				write(path, []byte("not GGUF"))
			case "missing":
			default:
				write(path, data)
			}
			switch name {
			case "layout":
				s.repo = "another/repo"
			case "revision":
				s.revision = strings.Repeat("b", 40)
			case "checksum":
				s.digest = strings.Repeat("0", 64)
			case "traversal":
				s.filename = "../" + s.filename
			case "unsupported quant":
				s.filename = "aaa-Q4.gguf"
			case "cancel":
				var cancel context.CancelFunc
				ctx, cancel = context.WithCancel(ctx)
				cancel()
			}
			f, err := resolveModel(ctx, s)
			if f != nil {
				defer f.Close()
			}
			ok := name == "local" || name == "cache" || name == "link"
			if (err == nil) != ok {
				t.Fatalf("expected success %v, got %v", ok, err)
			}
		})
	}
}
