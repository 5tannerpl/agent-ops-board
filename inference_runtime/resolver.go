package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"regexp"
	"strconv"
	"strings"
	"syscall"
)

func dirPath(dir *os.File) string { return "/proc/self/fd/" + strconv.Itoa(int(dir.Fd())) }

// Model pins come only from the operator, never from an HTTP request.
type modelSpec struct {
	root, mode, repo, revision, filename, digest string
}

var (
	repoID     = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$`)
	revisionID = regexp.MustCompile(`^[a-f0-9]{40}$`)
	shaID      = regexp.MustCompile(`^[a-f0-9]{64}$`)
	ggufName   = regexp.MustCompile(`(?i)^[a-z0-9][a-z0-9_.-]*q8_0\.gguf$`)
)

// Pin each opened directory inode. Only recognize HF's snapshot->blob link;
// never follow an arbitrary symlink, including at a directory boundary.
func openAt(parent *os.File, name string, directory bool) (*os.File, error) {
	flags := syscall.O_RDONLY | syscall.O_NOFOLLOW | syscall.O_CLOEXEC | syscall.O_NONBLOCK
	if directory {
		flags |= syscall.O_DIRECTORY
	}
	fd, err := syscall.Openat(int(parent.Fd()), name, flags, 0)
	if err != nil {
		return nil, errors.New("model path unavailable")
	}
	return os.NewFile(uintptr(fd), name), nil
}

func resolveModel(ctx context.Context, s modelSpec) (*os.File, error) {
	if !strings.HasPrefix(s.root, "/") || !repoID.MatchString(s.repo) || strings.Contains(s.repo, "--") ||
		!revisionID.MatchString(s.revision) || !shaID.MatchString(s.digest) || !ggufName.MatchString(s.filename) {
		return nil, errors.New("invalid model pins")
	}
	// The absolute mount point and its ancestors are trusted operator configuration.
	root, err := os.Open(s.root)
	if err != nil {
		return nil, errors.New("model directory unavailable")
	}
	defer root.Close()
	dir := root
	switch s.mode {
	case "local":
		// A local export is staged as <approved root>/<full revision>/<filename>.
		dir, err = openAt(root, s.revision, true)
		if err != nil {
			return nil, err
		}
		defer dir.Close()
	case "cache":
		repo, err := openAt(root, "models--"+strings.ReplaceAll(s.repo, "/", "--"), true)
		if err != nil {
			return nil, err
		}
		defer repo.Close()
		snapshots, err := openAt(repo, "snapshots", true)
		if err != nil {
			return nil, err
		}
		defer snapshots.Close()
		dir, err = openAt(snapshots, s.revision, true)
		if err != nil {
			return nil, err
		}
		defer dir.Close()
		snapshot, err := os.OpenRoot(dirPath(dir))
		if err != nil {
			return nil, errors.New("snapshot unavailable")
		}
		defer snapshot.Close()
		if target, err := snapshot.Readlink(s.filename); err == nil {
			if target != "../../blobs/"+s.digest {
				return nil, errors.New("unapproved cache symlink")
			}
			dir, err = openAt(repo, "blobs", true)
			if err != nil {
				return nil, err
			}
			defer dir.Close()
			s.filename = s.digest
		}
	default:
		return nil, errors.New("invalid model source")
	}
	f, err := openAt(dir, s.filename, false)
	if err != nil {
		return nil, err
	}
	if err := verifyModel(ctx, f, s.digest); err != nil {
		f.Close()
		return nil, err
	}
	return f, nil
}

func verifyModel(ctx context.Context, f *os.File, expected string) error {
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() {
		return errors.New("not a regular model file")
	}
	var header [8]byte
	if _, err := io.ReadFull(f, header[:]); err != nil || string(header[:]) != "GGUF\x03\x00\x00\x00" {
		return errors.New("expected GGUF v3")
	}
	h := sha256.New()
	h.Write(header[:])
	buf := make([]byte, 1024*1024)
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		n, err := f.Read(buf)
		h.Write(buf[:n])
		if err == io.EOF {
			break
		}
		if err != nil {
			return errors.New("model read failed")
		}
	}
	if hex.EncodeToString(h.Sum(nil)) != expected {
		return errors.New("model checksum mismatch")
	}
	_, err = f.Seek(0, io.SeekStart)
	return err
}
