// The official imager and its formatter are unchanged. This launcher supplies
// only owned loop nodes in Docker's private /dev, synchronously at mkfs.vfat.
package main

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const raw = "/out/oracle-arm64.raw"
const journal = "/out/loop.json"

type Record struct {
	Loop       int      `json:"loop"`
	Before     string   `json:"before"`
	After      string   `json:"after"`
	Partitions []string `json:"partitions"`
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
func ioctl(fd uintptr, op uintptr, arg uintptr) (uintptr, error) {
	v, _, e := syscall.Syscall(syscall.SYS_IOCTL, fd, op, arg)
	if e != 0 {
		return v, e
	}
	return v, nil
}
func node(path string, major, minor uint64, char bool) error {
	mode := uint32(syscall.S_IFBLK | 0600)
	if char {
		mode = syscall.S_IFCHR | 0600
	}
	dev := (major&0xfff)<<8 | (major & ^uint64(0xfff))<<32 | minor&0xff | (minor & ^uint64(0xff))<<12
	return syscall.Mknod(path, mode, int(dev)) // Existing nodes are never replaced.
}
func save(r Record) {
	b, e := json.MarshalIndent(r, "", "  ")
	must(e)
	must(os.WriteFile(journal, b, 0644))
}
func load() Record {
	b, e := os.ReadFile(journal)
	must(e)
	var r Record
	must(json.Unmarshal(b, &r))
	if r.Loop < 0 || r.Loop > 1048575 {
		panic("invalid loop journal")
	}
	return r
}
func loopPath(r Record) string { return fmt.Sprintf("/dev/loop%d", r.Loop) }
func status(r Record) ([]byte, error) {
	f, e := os.OpenFile(loopPath(r), os.O_RDONLY, 0)
	if e != nil {
		return nil, e
	}
	defer f.Close()
	b := make([]byte, 232)
	_, e = ioctl(f.Fd(), 0x4c05, uintptr(unsafe.Pointer(&b[0])))
	return b, e
}
func owned(r Record) bool {
	b, e := status(r)
	if e != nil {
		return false
	}
	var s syscall.Stat_t
	if syscall.Stat(raw, &s) != nil {
		return false
	}
	return binary.LittleEndian.Uint64(b[0:8]) == uint64(s.Dev) && binary.LittleEndian.Uint64(b[8:16]) == s.Ino
}
func cleanup(r Record) error {
	_, e := status(r)
	if errors.Is(e, syscall.ENXIO) {
		r.After = "unbound"
		save(r)
		return nil
	}
	if e != nil {
		return e
	}
	if !owned(r) {
		r.After = "foreign mapping untouched"
		save(r)
		return fmt.Errorf("selected loop is no longer owned; not detaching")
	}
	f, e := os.OpenFile(loopPath(r), os.O_RDONLY, 0)
	if e != nil {
		return e
	}
	_, e = ioctl(f.Fd(), 0x4c01, 0)
	f.Close()
	if e != nil {
		return e
	}
	_, e = status(r)
	if !errors.Is(e, syscall.ENXIO) {
		return fmt.Errorf("loop detach not confirmed: %v", e)
	}
	r.After = "owned mapping detached"
	save(r)
	return nil
}
func gate() {
	r := load()
	if !owned(r) {
		panic("formatter loop is not backed by owned scratch")
	}
	if len(os.Args) < 2 || os.Args[len(os.Args)-1] != loopPath(r)+"p1" {
		panic("unexpected formatter target")
	}
	// Never predict extended device numbers. Both nodes must be registered by
	// the kernel and belong to the verified loop before the real formatter runs.
	deadline := time.Now().Add(30 * time.Second)
	for _, part := range []int{1, 2} {
		name := fmt.Sprintf("loop%dp%d", r.Loop, part)
		for {
			b, e := os.ReadFile("/sys/class/block/" + name + "/dev")
			if e == nil {
				parent, e := filepath.EvalSymlinks("/sys/class/block/" + name + "/..")
				must(e)
				if filepath.Base(parent) != fmt.Sprintf("loop%d", r.Loop) || !owned(r) {
					panic("partition parent ownership mismatch")
				}
				v := strings.Split(strings.TrimSpace(string(b)), ":")
				if len(v) != 2 {
					panic("invalid kernel device")
				}
				ma, e := strconv.ParseUint(v[0], 10, 32)
				must(e)
				mi, e := strconv.ParseUint(v[1], 10, 32)
				must(e)
				must(node("/dev/"+name, ma, mi, false))
				r.Partitions = append(r.Partitions, name)
				break
			}
			if time.Now().After(deadline) {
				panic("owned partition registration timed out")
			}
			time.Sleep(10 * time.Millisecond)
		}
	}
	save(r)
	real := os.Getenv("ORACLE_REAL_MKFS")
	if real == "" || strings.HasPrefix(real, "/oracle/") {
		panic("missing official formatter")
	}
	must(syscall.Exec(real, append([]string{real}, os.Args[1:]...), os.Environ()))
}
func main() {
	if filepath.Base(os.Args[0]) == "mkfs.vfat" {
		gate()
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "audit" {
		r := load()
		must(node(loopPath(r), 7, uint64(r.Loop), false))
		must(cleanup(r))
		return
	}
	// A normal, non-privileged Docker /dev has no host block nodes.
	entries, e := os.ReadDir("/dev")
	must(e)
	for _, entry := range entries {
		info, e := entry.Info()
		must(e)
		if info.Mode()&os.ModeDevice != 0 && info.Mode()&os.ModeCharDevice == 0 {
			panic("pre-existing block node in private /dev")
		}
	}
	real, e := exec.LookPath("mkfs.vfat")
	must(e)
	must(os.Setenv("ORACLE_REAL_MKFS", real))
	must(os.Setenv("PATH", "/oracle:"+os.Getenv("PATH")))
	must(node("/dev/loop-control", 10, 237, true))
	f, e := os.OpenFile("/dev/loop-control", os.O_RDWR, 0)
	must(e)
	n, e := ioctl(f.Fd(), 0x4c82, 0)
	f.Close()
	must(e)
	r := Record{Loop: int(n), Before: "verified unused"}
	must(node(loopPath(r), 7, uint64(n), false))
	_, e = status(r)
	if !errors.Is(e, syscall.ENXIO) {
		panic("selected loop is already bound")
	}
	save(r)
	cmd := exec.Command("/bin/imager", "-")
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	must(cmd.Start())
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	go func() { s := <-sig; _ = cmd.Process.Signal(s) }()
	err := cmd.Wait()
	r = load()
	cleanErr := cleanup(r)
	if cleanErr != nil {
		fmt.Fprintln(os.Stderr, cleanErr)
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
