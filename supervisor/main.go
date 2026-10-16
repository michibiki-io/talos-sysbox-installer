// SPDX-License-Identifier: MIT
// The extension-service PID namespace cannot supply node PID identities to
// Sysbox. Launch only our owned backends through the system containerd socket.
package main

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"syscall"
	"time"
)

const namespace = "talos-sysbox"
const socket = "/system/run/containerd/containerd.sock"

type supervisor struct {
	daemon         string
	ctr            string
	commandTimeout time.Duration
	startupTimeout time.Duration
	stopTimeout    time.Duration
}

func validDaemon(name string) bool { return name == "sysbox-mgr" || name == "sysbox-fs" }

func (s supervisor) command(ctx context.Context, args ...string) error {
	ctx, cancel := context.WithTimeout(ctx, s.commandTimeout)
	defer cancel()
	c := exec.CommandContext(ctx, s.ctr, append([]string{"--address=" + socket, "--namespace=" + namespace}, args...)...)
	c.Stdout, c.Stderr = os.Stdout, os.Stderr
	err := c.Run()
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return err
}

func (s supervisor) cleanup(ctx context.Context) error {
	// Missing tasks/containers are expected after a clean shutdown. Timeout is
	// not missing: fail instead of attempting a second overlapping launch.
	for _, args := range [][]string{{"tasks", "kill", "--signal", "SIGKILL", s.daemon}, {"tasks", "rm", "--force", s.daemon}, {"containers", "rm", s.daemon}} {
		err := s.command(ctx, args...)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if err != nil {
			var e *exec.ExitError
			if !errors.As(err, &e) {
				return err
			}
			// ctr run below independently rejects an ID still in use.
		}
	}
	return nil
}

func waitSocket(ctx context.Context, path string) error {
	for {
		c, err := net.DialTimeout("unix", path, 200*time.Millisecond)
		if err == nil {
			c.Close()
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(200 * time.Millisecond):
		}
	}
}

func (s supervisor) runArgs() []string {
	a := []string{"--address=" + socket, "--namespace=" + namespace, "run", "--rm", "--rootfs", "--privileged", "--net-host", "--with-ns=pid:/proc/1/ns/pid", "--rootfs-propagation=shared", "--env=PATH=/bin:/usr/bin:/usr/local/bin"}
	if s.daemon == "sysbox-fs" {
		a = append(a, "--env=SYSBOX_PROC_TRANSFER_HELPER=/proc-mount-helper", "--env=SYSBOX_SYSFS_TRANSFER_HELPER=/sysfs-mount-helper")
	}
	for _, path := range []string{"/run", "/var", "/dev"} {
		a = append(a, "--mount", "type=bind,src="+path+",dst="+path+",options=rbind:rshared:rw")
	}
	a = append(a, "/usr/local/lib/containers/"+s.daemon, s.daemon, "/"+s.daemon)
	if s.daemon == "sysbox-mgr" {
		a = append(a, "--disable-shiftfs")
	}
	return a
}

func (s supervisor) run(ctx context.Context) error {
	if !validDaemon(s.daemon) {
		return fmt.Errorf("invalid owned daemon")
	}
	startup, cancel := context.WithTimeout(ctx, s.startupTimeout)
	defer cancel()
	if err := waitSocket(startup, socket); err != nil {
		return fmt.Errorf("system containerd unavailable: %w", err)
	}
	if err := s.cleanup(startup); err != nil {
		return err
	}
	if s.daemon == "sysbox-fs" {
		if err := waitSocket(startup, "/run/sysbox/sysmgr.sock"); err != nil {
			return fmt.Errorf("manager unavailable: %w", err)
		}
	}
	c := exec.Command(s.ctr, s.runArgs()...)
	c.Stdout, c.Stderr = os.Stdout, os.Stderr
	if err := c.Start(); err != nil {
		return err
	}
	done := make(chan error, 1)
	go func() { done <- c.Wait() }()
	ready := make(chan error, 1)
	path := "/run/sysbox/sysmgr.sock"
	if s.daemon == "sysbox-fs" {
		path = "/run/sysbox/sysfs.sock"
	}
	go func() { ready <- waitSocket(startup, path) }()
	var result error
	select {
	case result = <-done:
		return fmt.Errorf("backend exited before readiness: %v", result)
	case result = <-ready:
		if result == nil {
			cancel()
			fmt.Fprintln(os.Stdout, "backend ready:", s.daemon)
			select {
			case result = <-done:
				return fmt.Errorf("backend exited: %v", result)
			case <-ctx.Done():
			}
		}
	case <-ctx.Done():
		result = ctx.Err()
	}
	// Forward TERM to the backend, not only to the ctr client. Each operation
	// and the grace period is bounded, even if the system daemon has failed.
	stop, stopCancel := context.WithTimeout(context.Background(), s.stopTimeout)
	defer stopCancel()
	_ = s.command(stop, "tasks", "kill", "--signal", "SIGTERM", s.daemon)
	select {
	case <-done:
	case <-stop.Done():
		kill, kc := context.WithTimeout(context.Background(), s.commandTimeout)
		_ = s.command(kill, "tasks", "kill", "--signal", "SIGKILL", s.daemon)
		kc()
		_ = c.Process.Kill()
		<-done
	}
	clean, cc := context.WithTimeout(context.Background(), 3*s.commandTimeout)
	defer cc()
	if err := s.cleanup(clean); err != nil {
		return err
	}
	return result
}

func main() {
	if len(os.Args) != 2 || !validDaemon(os.Args[1]) {
		fmt.Fprintln(os.Stderr, "usage: supervisor sysbox-mgr|sysbox-fs")
		os.Exit(2)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	s := supervisor{os.Args[1], "/ctr", 5 * time.Second, 60 * time.Second, 10 * time.Second}
	if err := s.run(ctx); err != nil && ctx.Err() == nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
