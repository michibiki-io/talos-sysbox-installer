package main

import (
	"context"
	"errors"
	"net"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestSocketReadiness(t *testing.T) {
	p := filepath.Join(t.TempDir(), "ready.sock")
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Millisecond)
	defer cancel()
	if err := waitSocket(ctx, p); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("missing socket: %v", err)
	}
	// A regular path would fool a path-only dependency; require a Unix listener.
	listener, err := net.Listen("unix", p)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	if err := waitSocket(context.Background(), p); err != nil {
		t.Fatal(err)
	}
}

func TestBackendScope(t *testing.T) {
	for _, daemon := range []string{"sysbox-mgr", "sysbox-fs"} {
		s := supervisor{daemon: daemon}
		args := strings.Join(s.runArgs(), " ")
		for _, required := range []string{"--namespace=talos-sysbox", "--address=/system/run/containerd/containerd.sock", "--with-ns=pid:/proc/1/ns/pid", "--rootfs-propagation=shared", "options=rbind:rshared:rw", "/usr/local/lib/containers/" + daemon} {
			if !strings.Contains(args, required) {
				t.Errorf("missing %s", required)
			}
		}
		if strings.Contains(args, "--env=SYSBOX_PROC_TRANSFER_HELPER=/proc-mount-helper") != (daemon == "sysbox-fs") {
			t.Error("helper environment is not confined to the fs backend")
		}
		if strings.Contains(args, "--disable-shiftfs") != (daemon == "sysbox-mgr") {
			t.Error("wrong daemon flags")
		}
	}
	for _, invalid := range []string{"runc", "", "sysbox-mgr-other", "../sysbox-mgr"} {
		if err := (supervisor{daemon: invalid}).run(context.Background()); err == nil {
			t.Errorf("accepted %q", invalid)
		}
	}
}
