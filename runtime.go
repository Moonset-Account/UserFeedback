// Package procruntime implements an in-memory model of a small part of a
// process runtime: per-process file descriptor tables built on anonymous
// pipes.
//
// The model covers the operations needed to reason about descriptor
// inheritance across fork(2)/execve(2) style boundaries:
//
//   - creating an anonymous pipe (two endpoints, one read end, one write end),
//   - duplicating a descriptor onto a chosen descriptor number (dup2 semantics),
//   - toggling the close-on-exec (O_CLOEXEC/FD_CLOEXEC) marker,
//   - forking a child process whose descriptor table is a copy of the
//     parent's, while the underlying pipe endpoints stay shared,
//   - replacing the running program, which closes every marked descriptor,
//   - closing descriptors, and reading from / writing to pipe endpoints.
//
// Pipe endpoints are reference counted across every process in the runtime.
// A read from an empty pipe returns ErrNoDataAvailable while at least one
// write end is open, and io.EOF once every write end has been closed.
package procruntime

import (
	"errors"
	"io"
	"sort"
	"sync"
)

// Endpoint kind strings returned by Runtime.FDEnd.
const (
	// ReadEnd identifies the read endpoint of a pipe.
	ReadEnd = "read"
	// WriteEnd identifies the write endpoint of a pipe.
	WriteEnd = "write"
)

// Sentinel errors. Callers should compare with errors.Is.
var (
	// ErrNoSuchProcess is returned when a process id does not name a
	// process known to the runtime.
	ErrNoSuchProcess = errors.New("procruntime: no such process")

	// ErrBadFDNumber is returned for an impossible descriptor number
	// (for example a negative number).
	ErrBadFDNumber = errors.New("procruntime: invalid file descriptor number")

	// ErrBadFD is returned when the descriptor number is not currently
	// open in the target process.
	ErrBadFD = errors.New("procruntime: bad file descriptor")

	// ErrWrongMode is returned when a read targets a write end or a
	// write targets a read end.
	ErrWrongMode = errors.New("procruntime: descriptor is not open for the requested operation")

	// ErrBrokenPipe is returned by Write when every read end of the
	// pipe has been closed.
	ErrBrokenPipe = errors.New("procruntime: broken pipe")

	// ErrNoDataAvailable is returned by Read on an empty pipe that still
	// has at least one open write end. It is deliberately distinct from
	// io.EOF, which signals that every write end has been closed.
	ErrNoDataAvailable = errors.New("procruntime: no data available at the moment")
)

// endKind selects which endpoint of a pipe an fdEntry references.
type endKind int

const (
	readEnd endKind = iota
	writeEnd
)

// pipeObject is the shared, reference-counted endpoint pair of one anonymous
// pipe. Every fdEntry pointing at the pipe contributes one reference to the
// corresponding endpoint counter. The mutex protects the byte buffer and the
// endpoint counters.
type pipeObject struct {
	mu      sync.Mutex
	buf     []byte
	readers int
	writers int
}

// fdEntry is one slot of a process descriptor table.
type fdEntry struct {
	pipe    *pipeObject
	end     endKind
	cloexec bool
}

// Process is a modeled process with its own descriptor table.
type Process struct {
	pid  int
	prog string
	fds  map[int]*fdEntry
}

// PID returns the process identifier assigned by the runtime.
func (p *Process) PID() int { return p.pid }

// Runtime owns every modeled process and their pipes. Its zero value is not
// usable; create one with NewRuntime.
type Runtime struct {
	mu      sync.Mutex
	procs   map[int]*Process
	nextPID int
}

// NewRuntime creates an empty runtime with no processes.
func NewRuntime() *Runtime {
	return &Runtime{procs: make(map[int]*Process)}
}

// startLocked creates a process. The caller must hold rt.mu.
func (rt *Runtime) startLocked(program string) *Process {
	proc := &Process{
		pid:  rt.nextPID,
		prog: program,
		fds:  make(map[int]*fdEntry),
	}
	rt.nextPID++
	rt.procs[proc.pid] = proc
	return proc
}

// Start creates a process running the named program with an empty descriptor
// table and returns it.
func (rt *Runtime) Start(program string) *Process {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	return rt.startLocked(program)
}

// getProcess looks up a process. The caller must hold rt.mu.
func (rt *Runtime) getProcess(pid int) (*Process, error) {
	proc, ok := rt.procs[pid]
	if !ok {
		return nil, ErrNoSuchProcess
	}
	return proc, nil
}

// lookup returns the descriptor entry for fd. The caller must hold rt.mu.
func (proc *Process) lookup(fd int) (*fdEntry, error) {
	if fd < 0 {
		return nil, ErrBadFDNumber
	}
	entry, ok := proc.fds[fd]
	if !ok {
		return nil, ErrBadFD
	}
	return entry, nil
}

// lowestFree returns the smallest descriptor number not currently open.
// The caller must hold rt.mu.
func (proc *Process) lowestFree() int {
	fd := 0
	for {
		if _, ok := proc.fds[fd]; !ok {
			return fd
		}
		fd++
	}
}

// release drops one endpoint reference. The caller must hold rt.mu and must
// not already hold po.mu.
func (po *pipeObject) release(kind endKind) {
	po.mu.Lock()
	defer po.mu.Unlock()
	if kind == readEnd {
		po.readers--
	} else {
		po.writers--
	}
}

// retain adds one endpoint reference. The caller must hold rt.mu.
func (po *pipeObject) retain(kind endKind) {
	po.mu.Lock()
	defer po.mu.Unlock()
	if kind == readEnd {
		po.readers++
	} else {
		po.writers++
	}
}

// Pipe creates an anonymous pipe in the process pid. Two new descriptors are
// allocated at the lowest free numbers; the first returned descriptor is the
// read end and the second is the write end. Both start unmarked.
func (rt *Runtime) Pipe(pid int) (readFD, writeFD int, err error) {
	rt.mu.Lock()
	defer rt.mu.Unlock()

	proc, err := rt.getProcess(pid)
	if err != nil {
		return 0, 0, err
	}

	po := &pipeObject{readers: 1, writers: 1}
	readFD = proc.lowestFree()
	proc.fds[readFD] = &fdEntry{pipe: po, end: readEnd}
	writeFD = proc.lowestFree()
	proc.fds[writeFD] = &fdEntry{pipe: po, end: writeEnd}
	return readFD, writeFD, nil
}

// SetCloseOnExec sets or clears the close-on-exec marker on an open
// descriptor.
func (rt *Runtime) SetCloseOnExec(pid, fd int, closeOnExec bool) error {
	rt.mu.Lock()
	defer rt.mu.Unlock()

	proc, err := rt.getProcess(pid)
	if err != nil {
		return err
	}
	entry, err := proc.lookup(fd)
	if err != nil {
		return err
	}
	entry.cloexec = closeOnExec
	return nil
}

// Dup2 duplicates the descriptor oldfd onto newfd, following dup2(2) rules:
//
//   - if newfd is already open it is closed first, dropping its endpoint
//     reference;
//   - the new descriptor starts with a cleared close-on-exec marker;
//   - duplicating a descriptor onto itself is a no-op and keeps its marker.
func (rt *Runtime) Dup2(pid, oldfd, newfd int) error {
	rt.mu.Lock()
	defer rt.mu.Unlock()

	if newfd < 0 {
		return ErrBadFDNumber
	}
	proc, err := rt.getProcess(pid)
	if err != nil {
		return err
	}
	src, err := proc.lookup(oldfd)
	if err != nil {
		return err
	}
	if oldfd == newfd {
		// dup2 onto itself leaves every marker untouched.
		return nil
	}
	if dst, ok := proc.fds[newfd]; ok {
		delete(proc.fds, newfd)
		dst.pipe.release(dst.end)
	}
	src.pipe.retain(src.end)
	proc.fds[newfd] = &fdEntry{pipe: src.pipe, end: src.end, cloexec: false}
	return nil
}

// Fork creates a child process whose descriptor table is a copy of the
// parent's. Every descriptor number, endpoint and close-on-exec marker is
// copied, while the underlying pipe endpoints remain shared and gain one
// reference per copied descriptor. The child starts running the same program.
func (rt *Runtime) Fork(parentPID int) (childPID int, err error) {
	rt.mu.Lock()
	defer rt.mu.Unlock()

	parent, err := rt.getProcess(parentPID)
	if err != nil {
		return 0, err
	}
	child := rt.startLocked(parent.prog)

	numbers := make([]int, 0, len(parent.fds))
	for fd := range parent.fds {
		numbers = append(numbers, fd)
	}
	sort.Ints(numbers)
	for _, fd := range numbers {
		src := parent.fds[fd]
		child.fds[fd] = &fdEntry{pipe: src.pipe, end: src.end, cloexec: src.cloexec}
		src.pipe.retain(src.end)
	}
	return child.pid, nil
}

// Exec replaces the program run by pid. Every descriptor marked
// close-on-exec is closed (dropping its endpoint reference); unmarked
// descriptors, including unmarked duplicates of marked ones, survive.
func (rt *Runtime) Exec(pid int, program string) error {
	rt.mu.Lock()
	defer rt.mu.Unlock()

	proc, err := rt.getProcess(pid)
	if err != nil {
		return err
	}
	proc.prog = program
	for fd, entry := range proc.fds {
		if entry.cloexec {
			delete(proc.fds, fd)
			entry.pipe.release(entry.end)
		}
	}
	return nil
}

// Close closes one descriptor and drops its reference to the underlying
// endpoint. Closing a descriptor that is not open returns ErrBadFD.
func (rt *Runtime) Close(pid, fd int) error {
	rt.mu.Lock()
	defer rt.mu.Unlock()

	proc, err := rt.getProcess(pid)
	if err != nil {
		return err
	}
	entry, err := proc.lookup(fd)
	if err != nil {
		return err
	}
	delete(proc.fds, fd)
	entry.pipe.release(entry.end)
	return nil
}

// Read reads from a pipe read end:
//
//   - buffered bytes are taken first, up to len(p), in FIFO order;
//   - on an empty buffer with an open write end it returns
//     ErrNoDataAvailable;
//   - on an empty buffer with every write end closed it returns io.EOF.
func (rt *Runtime) Read(pid, fd int, p []byte) (int, error) {
	rt.mu.Lock()
	defer rt.mu.Unlock()

	proc, err := rt.getProcess(pid)
	if err != nil {
		return 0, err
	}
	entry, err := proc.lookup(fd)
	if err != nil {
		return 0, err
	}
	if entry.end != readEnd {
		return 0, ErrWrongMode
	}

	po := entry.pipe
	po.mu.Lock()
	defer po.mu.Unlock()

	if len(po.buf) > 0 {
		n := copy(p, po.buf)
		po.buf = po.buf[n:]
		return n, nil
	}
	if po.writers == 0 {
		return 0, io.EOF
	}
	return 0, ErrNoDataAvailable
}

// Write appends p to a pipe write end. It returns ErrBrokenPipe once every
// read end of the pipe has been closed. On success the whole slice is
// buffered and its full length is returned.
func (rt *Runtime) Write(pid, fd int, p []byte) (int, error) {
	rt.mu.Lock()
	defer rt.mu.Unlock()

	proc, err := rt.getProcess(pid)
	if err != nil {
		return 0, err
	}
	entry, err := proc.lookup(fd)
	if err != nil {
		return 0, err
	}
	if entry.end != writeEnd {
		return 0, ErrWrongMode
	}

	po := entry.pipe
	po.mu.Lock()
	defer po.mu.Unlock()

	if po.readers == 0 {
		return 0, ErrBrokenPipe
	}
	po.buf = append(po.buf, p...)
	return len(p), nil
}

// Program returns the program currently run by pid.
func (rt *Runtime) Program(pid int) (string, error) {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	proc, err := rt.getProcess(pid)
	if err != nil {
		return "", err
	}
	return proc.prog, nil
}

// FDs returns the open descriptor numbers of pid in ascending order.
func (rt *Runtime) FDs(pid int) ([]int, error) {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	proc, err := rt.getProcess(pid)
	if err != nil {
		return nil, err
	}
	out := make([]int, 0, len(proc.fds))
	for fd := range proc.fds {
		out = append(out, fd)
	}
	sort.Ints(out)
	return out, nil
}

// IsOpen reports whether fd is currently open in pid.
func (rt *Runtime) IsOpen(pid, fd int) (bool, error) {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	proc, err := rt.getProcess(pid)
	if err != nil {
		return false, err
	}
	if fd < 0 {
		return false, ErrBadFDNumber
	}
	_, ok := proc.fds[fd]
	return ok, nil
}

// IsCloseOnExec reports the close-on-exec marker of an open descriptor.
func (rt *Runtime) IsCloseOnExec(pid, fd int) (bool, error) {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	proc, err := rt.getProcess(pid)
	if err != nil {
		return false, err
	}
	entry, err := proc.lookup(fd)
	if err != nil {
		return false, err
	}
	return entry.cloexec, nil
}

// FDEnd returns ReadEnd or WriteEnd for an open descriptor.
func (rt *Runtime) FDEnd(pid, fd int) (string, error) {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	proc, err := rt.getProcess(pid)
	if err != nil {
		return "", err
	}
	entry, err := proc.lookup(fd)
	if err != nil {
		return "", err
	}
	if entry.end == readEnd {
		return ReadEnd, nil
	}
	return WriteEnd, nil
}
