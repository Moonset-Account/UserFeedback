package procruntime

import (
	"bytes"
	"errors"
	"io"
	"reflect"
	"sync"
	"testing"
)

func mustOK(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func mustErr(t *testing.T, err, want error) {
	t.Helper()
	if !errors.Is(err, want) {
		t.Fatalf("got error %v, want %v", err, want)
	}
}

func mustWrite(t *testing.T, rt *Runtime, pid, fd int, p []byte) {
	t.Helper()
	n, err := rt.Write(pid, fd, p)
	mustOK(t, err)
	if n != len(p) {
		t.Fatalf("write count = %d, want %d", n, len(p))
	}
}

func mustFDs(t *testing.T, rt *Runtime, pid int, want []int) {
	t.Helper()
	got, err := rt.FDs(pid)
	mustOK(t, err)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("open fds = %v, want %v", got, want)
	}
}

// TestPipeBufferedReadAndEmptyStates covers the normal read/write path plus
// the boundary distinction on an empty pipe: transient "no data" versus
// permanent EOF once all write ends are closed.
func TestPipeBufferedReadAndEmptyStates(t *testing.T) {
	rt := NewRuntime()
	pid := rt.Start("sh").PID()
	r, w, err := rt.Pipe(pid)
	mustOK(t, err)
	if r != 0 || w != 1 {
		t.Fatalf("pipe fds = (%d,%d), want (0,1)", r, w)
	}

	n, err := rt.Write(pid, w, []byte("hello pipe"))
	mustOK(t, err)
	if n != len("hello pipe") {
		t.Fatalf("write count = %d, want %d", n, len("hello pipe"))
	}

	buf := make([]byte, 5)
	n, err = rt.Read(pid, r, buf)
	mustOK(t, err)
	if n != 5 || !bytes.Equal(buf, []byte("hello")) {
		t.Fatalf("first read = (%d,%q), want (5,\"hello\")", n, buf)
	}

	buf = make([]byte, 64)
	n, err = rt.Read(pid, r, buf)
	mustOK(t, err)
	if n != 5 || !bytes.Equal(buf[:n], []byte(" pipe")) {
		t.Fatalf("second read = (%d,%q), want (5,\" pipe\")", n, buf[:n])
	}

	// Empty buffer, write end still open: transient condition, not EOF.
	n, err = rt.Read(pid, r, make([]byte, 4))
	if n != 0 || !errors.Is(err, ErrNoDataAvailable) {
		t.Fatalf("empty read with writer open = (%d,%v), want (0,ErrNoDataAvailable)", n, err)
	}

	mustOK(t, rt.Close(pid, w))
	// Empty buffer, no writer left: real end of file.
	n, err = rt.Read(pid, r, make([]byte, 4))
	if n != 0 || !errors.Is(err, io.EOF) {
		t.Fatalf("empty read after last close = (%d,%v), want (0,io.EOF)", n, err)
	}
}

// TestBufferedBytesDeliveredBeforeEOF checks that closing the write end never
// discards already-buffered bytes: they drain first, EOF comes afterwards.
func TestBufferedBytesDeliveredBeforeEOF(t *testing.T) {
	rt := NewRuntime()
	pid := rt.Start("sh").PID()
	r, w, err := rt.Pipe(pid)
	mustOK(t, err)

	mustWrite(t, rt, pid, w, []byte("xy"))
	mustOK(t, rt.Close(pid, w))

	one := make([]byte, 1)
	n, err := rt.Read(pid, r, one)
	mustOK(t, err)
	if n != 1 || one[0] != 'x' {
		t.Fatalf("read = (%d,%q), want (1,\"x\")", n, one)
	}
	n, err = rt.Read(pid, r, one)
	mustOK(t, err)
	if n != 1 || one[0] != 'y' {
		t.Fatalf("read = (%d,%q), want (1,\"y\")", n, one)
	}
	_, err = rt.Read(pid, r, one)
	mustErr(t, err, io.EOF)
}

// TestDup2ReplacesTargetAndClearsMarker verifies that copying onto an open
// number closes the old descriptor first and that the new descriptor starts
// with a cleared close-on-exec marker.
func TestDup2ReplacesTargetAndClearsMarker(t *testing.T) {
	rt := NewRuntime()
	pid := rt.Start("sh").PID()
	r1, w1, err := rt.Pipe(pid)
	mustOK(t, err)
	r2, w2, err := rt.Pipe(pid)
	mustOK(t, err)

	mustOK(t, rt.SetCloseOnExec(pid, r2, true))
	marked, err := rt.IsCloseOnExec(pid, r2)
	mustOK(t, err)
	if !marked {
		t.Fatal("precondition: r2 should be marked")
	}

	// Overwrite r2 (read end of pipe 2) with a copy of w1 (write end of pipe 1).
	mustOK(t, rt.Dup2(pid, w1, r2))

	end, err := rt.FDEnd(pid, r2)
	mustOK(t, err)
	if end != WriteEnd {
		t.Fatalf("fd %d endpoint = %q, want %q", r2, end, WriteEnd)
	}
	marked, err = rt.IsCloseOnExec(pid, r2)
	mustOK(t, err)
	if marked {
		t.Fatal("dup2 target must start with the close-on-exec marker cleared")
	}
	mustFDs(t, rt, pid, []int{r1, w1, r2, w2})

	// The replaced read end of pipe 2 is gone: its only write end now sees
	// a broken pipe, proving the old reference was dropped.
	_, err = rt.Write(pid, w2, []byte("z"))
	mustErr(t, err, ErrBrokenPipe)

	// The duplicated write end shares the pipe with w1: bytes written via
	// r2 are readable through r1.
	mustWrite(t, rt, pid, r2, []byte("shared"))
	buf := make([]byte, 32)
	n, err := rt.Read(pid, r1, buf)
	mustOK(t, err)
	if string(buf[:n]) != "shared" {
		t.Fatalf("read through shared pipe = %q, want \"shared\"", buf[:n])
	}
}

// TestDup2OntoSelfKeepsMarker verifies the self-duplication edge case: the
// descriptor number and its close-on-exec marker are left untouched.
func TestDup2OntoSelfKeepsMarker(t *testing.T) {
	rt := NewRuntime()
	pid := rt.Start("sh").PID()
	_, w, err := rt.Pipe(pid)
	mustOK(t, err)
	mustOK(t, rt.SetCloseOnExec(pid, w, true))

	mustOK(t, rt.Dup2(pid, w, w))

	marked, err := rt.IsCloseOnExec(pid, w)
	mustOK(t, err)
	if !marked {
		t.Fatal("dup2 onto itself must preserve the close-on-exec marker")
	}
}

// TestForkCopiesDescriptorsAndSharesPipe verifies that fork copies every
// descriptor number, endpoint and marker, while the pipe itself stays shared:
// a write performed by the parent is visible to the child's read end.
func TestForkCopiesDescriptorsAndSharesPipe(t *testing.T) {
	rt := NewRuntime()
	parent := rt.Start("parent")
	pid := parent.PID()
	r, w, err := rt.Pipe(pid)
	mustOK(t, err)
	mustOK(t, rt.SetCloseOnExec(pid, w, true))
	mustOK(t, rt.Dup2(pid, w, 5))

	childPID, err := rt.Fork(pid)
	mustOK(t, err)

	mustFDs(t, rt, childPID, []int{r, w, 5})
	childMarked, err := rt.IsCloseOnExec(childPID, w)
	mustOK(t, err)
	if !childMarked {
		t.Fatal("fork must copy the close-on-exec marker")
	}
	childDupMarked, err := rt.IsCloseOnExec(childPID, 5)
	mustOK(t, err)
	if childDupMarked {
		t.Fatal("unmarked parent descriptor must stay unmarked in the child")
	}
	end, err := rt.FDEnd(childPID, 5)
	mustOK(t, err)
	if end != WriteEnd {
		t.Fatalf("child fd 5 endpoint = %q, want write", end)
	}

	// Shared underlying endpoint: parent writes, child reads.
	mustWrite(t, rt, pid, w, []byte("across fork"))
	buf := make([]byte, 32)
	n, err := rt.Read(childPID, r, buf)
	mustOK(t, err)
	if string(buf[:n]) != "across fork" {
		t.Fatalf("child read = %q, want \"across fork\"", buf[:n])
	}
}

// TestForkMarkersAreIndependentPerProcess verifies that changing a marker in
// the child does not touch the parent's copied descriptor.
func TestForkMarkersAreIndependentPerProcess(t *testing.T) {
	rt := NewRuntime()
	pid := rt.Start("parent").PID()
	_, w, err := rt.Pipe(pid)
	mustOK(t, err)
	mustOK(t, rt.SetCloseOnExec(pid, w, true))

	childPID, err := rt.Fork(pid)
	mustOK(t, err)
	mustOK(t, rt.SetCloseOnExec(childPID, w, false))

	childMarked, err := rt.IsCloseOnExec(childPID, w)
	mustOK(t, err)
	parentMarked, err := rt.IsCloseOnExec(pid, w)
	mustOK(t, err)
	if childMarked {
		t.Fatal("child marker should have been cleared")
	}
	if !parentMarked {
		t.Fatal("parent marker must be unaffected by the child's change")
	}
}

// TestExecClosesOnlyMarkedDescriptors verifies program replacement closes
// exactly the marked descriptors and leaves unmarked ones (including
// unmarked duplicates of a marked descriptor) open.
func TestExecClosesOnlyMarkedDescriptors(t *testing.T) {
	rt := NewRuntime()
	pid := rt.Start("oldprog").PID()
	r, w, err := rt.Pipe(pid)
	mustOK(t, err)
	mustOK(t, rt.SetCloseOnExec(pid, w, true))
	mustOK(t, rt.Dup2(pid, w, 5)) // unmarked second write end

	mustOK(t, rt.Exec(pid, "newprog"))

	prog, err := rt.Program(pid)
	mustOK(t, err)
	if prog != "newprog" {
		t.Fatalf("program = %q, want \"newprog\"", prog)
	}
	mustFDs(t, rt, pid, []int{r, 5})
	if open, _ := rt.IsOpen(pid, w); open {
		t.Fatal("marked descriptor must be closed by exec")
	}
	if open, _ := rt.IsOpen(pid, 5); !open {
		t.Fatal("unmarked duplicate must survive exec")
	}
}

// TestInterleavedForkDupExecCloseEOF is the headline interleaved scenario:
// after the parent closes every write end, one exec in the child may still
// leave an unmarked duplicate open, so an empty read stays "no data" until
// that duplicate is also closed; only then does the read return EOF.
func TestInterleavedForkDupExecCloseEOF(t *testing.T) {
	rt := NewRuntime()
	parent := rt.Start("parent")
	pid := parent.PID()
	r, w, err := rt.Pipe(pid)
	mustOK(t, err)

	// Mark the original write end, then keep an unmarked duplicate.
	mustOK(t, rt.SetCloseOnExec(pid, w, true))
	mustOK(t, rt.Dup2(pid, w, 5))

	childPID, err := rt.Fork(pid)
	mustOK(t, err)

	// Parent closes both of its write ends. The child still holds two.
	mustOK(t, rt.Close(pid, w))
	mustOK(t, rt.Close(pid, 5))

	// Empty pipe with child writers still open: no data, not EOF.
	_, err = rt.Read(pid, r, make([]byte, 8))
	mustErr(t, err, ErrNoDataAvailable)

	// Child replaces its program: the marked write end goes, the unmarked
	// duplicate survives.
	mustOK(t, rt.Exec(childPID, "childprog"))
	mustFDs(t, rt, childPID, []int{r, 5})

	// One writer remains, so an empty read still cannot report EOF.
	_, err = rt.Read(childPID, r, make([]byte, 8))
	mustErr(t, err, ErrNoDataAvailable)

	// The surviving duplicate is a working write end.
	mustWrite(t, rt, childPID, 5, []byte("late"))
	buf := make([]byte, 8)
	n, err := rt.Read(pid, r, buf)
	mustOK(t, err)
	if string(buf[:n]) != "late" {
		t.Fatalf("read = %q, want \"late\"", buf[:n])
	}

	// Once the last unmarked copy is closed, empty reads reach EOF.
	mustOK(t, rt.Close(childPID, 5))
	_, err = rt.Read(childPID, r, make([]byte, 8))
	mustErr(t, err, io.EOF)
}

// TestWriteBrokenPipeAndModeErrors covers endpoint boundary conditions:
// writing after all read ends close, and using an endpoint in the wrong mode.
func TestWriteBrokenPipeAndModeErrors(t *testing.T) {
	rt := NewRuntime()
	pid := rt.Start("sh").PID()
	r, w, err := rt.Pipe(pid)
	mustOK(t, err)

	_, err = rt.Read(pid, w, make([]byte, 4))
	mustErr(t, err, ErrWrongMode)
	_, err = rt.Write(pid, r, []byte("x"))
	mustErr(t, err, ErrWrongMode)

	mustOK(t, rt.Close(pid, r))
	_, err = rt.Write(pid, w, []byte("x"))
	mustErr(t, err, ErrBrokenPipe)
}

// TestDescriptorAndProcessErrors checks boundary error reporting for unknown
// processes, closed/impossible descriptor numbers and repeated closes.
func TestDescriptorAndProcessErrors(t *testing.T) {
	rt := NewRuntime()
	pid := rt.Start("sh").PID()
	r, _, err := rt.Pipe(pid)
	mustOK(t, err)

	_, _, err = rt.Pipe(4242)
	mustErr(t, err, ErrNoSuchProcess)
	_, err = rt.Read(4242, 0, make([]byte, 1))
	mustErr(t, err, ErrNoSuchProcess)

	_, err = rt.Read(pid, 42, make([]byte, 1))
	mustErr(t, err, ErrBadFD)
	mustErr(t, rt.SetCloseOnExec(pid, 42, true), ErrBadFD)
	mustErr(t, rt.Dup2(pid, r, -1), ErrBadFDNumber)
	mustErr(t, rt.Dup2(pid, 42, 9), ErrBadFD)

	mustOK(t, rt.Close(pid, r))
	mustErr(t, rt.Close(pid, r), ErrBadFD)
}

// TestCloseReleasesLowestFreeNumber verifies descriptor numbers are reused
// from the lowest free slot after a close.
func TestCloseReleasesLowestFreeNumber(t *testing.T) {
	rt := NewRuntime()
	pid := rt.Start("sh").PID()
	r, w, err := rt.Pipe(pid)
	mustOK(t, err)
	mustOK(t, rt.Close(pid, r))

	nr, nw, err := rt.Pipe(pid)
	mustOK(t, err)
	if nr != 0 || nw != 2 {
		t.Fatalf("reallocated fds = (%d,%d), want (0,2) (fd %d stays open)", nr, nw, w)
	}
}

// TestConcurrentAccessSmoke exercises the locking under concurrent writers
// and marker toggles. Run with `go test -race` for race detection.
func TestConcurrentAccessSmoke(t *testing.T) {
	rt := NewRuntime()
	pid := rt.Start("sh").PID()
	r, w, err := rt.Pipe(pid)
	mustOK(t, err)

	const writers = 8
	const perWriter = 50
	var wg sync.WaitGroup
	for i := 0; i < writers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < perWriter; j++ {
				if _, err := rt.Write(pid, w, []byte("a")); err != nil {
					t.Errorf("concurrent write: %v", err)
					return
				}
			}
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		for j := 0; j < 200; j++ {
			_ = rt.SetCloseOnExec(pid, r, j%2 == 0)
		}
	}()
	wg.Wait()

	total := writers * perWriter
	got := 0
	buf := make([]byte, 16)
	for {
		n, err := rt.Read(pid, r, buf)
		got += n
		if errors.Is(err, ErrNoDataAvailable) {
			break
		}
		if err != nil {
			t.Fatalf("drain read: %v", err)
		}
	}
	if got != total {
		t.Fatalf("drained %d bytes, want %d", got, total)
	}
}
