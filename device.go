// Package multipathwrite implements a client-side write coordinator for a
// block volume that is reachable over several paths to the same device.
//
// The coordinator owns stable write IDs, keeps overlapping writes in
// submission order, retries unknown outcomes through a fresh path to the
// same device, and blocks a range while its outcome is temporarily unknown.
package multipathwrite

import (
	"context"
	"errors"
	"sync"
)

// WriteID is a stable, coordinator-assigned identifier for a write. The
// same ID is reused when a write with an unknown outcome is re-sent.
type WriteID uint64

// WriteStatus is the outcome a device reports when asked about a write ID.
type WriteStatus int

const (
	// StatusNotStarted means the device has no durable record of the write.
	StatusNotStarted WriteStatus = iota
	// StatusWritten means the write was applied durably on the device.
	StatusWritten
	// StatusUnknown means the device cannot currently tell whether the
	// write landed; the query must be retried on another path.
	StatusUnknown
)

// String renders a status for diagnostics and tests.
func (s WriteStatus) String() string {
	switch s {
	case StatusNotStarted:
		return "not-started"
	case StatusWritten:
		return "written"
	case StatusUnknown:
		return "unknown"
	default:
		return "invalid"
	}
}

// PathInfo identifies which device/path produced a response. Every path
// response carries the device identity and the path generation so callers
// can prove a switch actually happened.
type PathInfo struct {
	DeviceID   string
	Generation int
}

// WriteResult is the response to a write attempt.
type WriteResult struct {
	ID   WriteID
	Path PathInfo
}

// QueryResult is the response to a write-status query.
type QueryResult struct {
	ID     WriteID
	Status WriteStatus
	Path   PathInfo
}

// ReadResult carries bytes plus the path that served them.
type ReadResult struct {
	Data []byte
	Path PathInfo
}

// FlushResult reports which path acknowledged the flush.
type FlushResult struct {
	Path PathInfo
}

// ErrPathDisconnected is returned by a path whose transport has gone away.
var ErrPathDisconnected = errors.New("multipathwrite: path disconnected")

// errNoHealthyPath is internal: the coordinator should wait and retry.
var errNoHealthyPath = errors.New("multipathwrite: no healthy path available")

// Path is one transport to a device. All methods are safe for concurrent
// use and may return ErrPathDisconnected.
type Path interface {
	// Info returns the device identity and this path's generation.
	Info() PathInfo
	// Broken reports whether the path transport is gone.
	Broken() bool
	// Write sends a write. A disconnected path returns
	// ErrPathDisconnected without touching the device.
	Write(ctx context.Context, id WriteID, offset int, data []byte) (WriteResult, error)
	// Query asks the device whether id has landed.
	Query(ctx context.Context, id WriteID) (QueryResult, error)
	// Read returns len(buf) bytes starting at offset.
	Read(ctx context.Context, offset int, buf []byte) (ReadResult, error)
	// Flush asks the device to make every accepted write durable.
	Flush(ctx context.Context) (FlushResult, error)
}

// SimDevice is an in-memory block device that all its paths share. It is
// the device-adapter layer used by tests; production code would implement
// the same Path interface over real transports.
//
// Hooks let tests inject faults:
//   - Gate(id) blocks an applying write until the gate is released.
//   - LostReceipts makes the device apply a write but make that path
//     return ErrPathDisconnected (the ack is lost after the landing).
//   - UnknownWrites puts an ID into a temporary-unknown state until
//     ResolveUnknown is called.
type SimDevice struct {
	id   string
	size int

	mu       sync.Mutex
	data     []byte
	applied  map[WriteID]bool
	unknown  map[WriteID]bool
	attempts map[WriteID]int
	order    []WriteID

	genMu sync.Mutex
	gen   int

	gatesMu sync.Mutex
	gates   map[WriteID]chan struct{}

	lostMu       sync.Mutex
	lostReceipts map[WriteID]bool

	unknownMu     sync.Mutex
	unknownWrites map[WriteID]bool

	failFirstMu   sync.Mutex
	failFirstPath map[WriteID]bool
}

// NewSimDevice builds an empty, zero-filled device of size bytes.
func NewSimDevice(id string, size int) *SimDevice {
	if size <= 0 {
		panic("multipathwrite: device size must be positive")
	}
	return &SimDevice{
		id:            id,
		size:          size,
		data:          make([]byte, size),
		applied:       make(map[WriteID]bool),
		unknown:       make(map[WriteID]bool),
		attempts:      make(map[WriteID]int),
		order:         nil,
		gates:         make(map[WriteID]chan struct{}),
		lostReceipts:  make(map[WriteID]bool),
		unknownWrites: make(map[WriteID]bool),
		failFirstPath: make(map[WriteID]bool),
	}
}

// ID returns the stable device identity shared by every path.
func (d *SimDevice) ID() string { return d.id }

// Size returns the device capacity in bytes.
func (d *SimDevice) Size() int { return d.size }

// NewPath attaches a fresh path with a new generation number.
func (d *SimDevice) NewPath() Path {
	d.genMu.Lock()
	d.gen++
	gen := d.gen
	d.genMu.Unlock()
	return &simPath{device: d, generation: gen}
}

// Gate installs (or returns an existing) gate that blocks the write with
// id while it is being applied. Release the gate by closing the channel.
func (d *SimDevice) Gate(id WriteID) chan struct{} {
	d.gatesMu.Lock()
	defer d.gatesMu.Unlock()
	g, ok := d.gates[id]
	if !ok {
		g = make(chan struct{})
		d.gates[id] = g
	}
	return g
}

// LoseReceipt makes the first path carrying id disconnect after the
// write lands but before the client receives an ack.
func (d *SimDevice) LoseReceipt(id WriteID) {
	d.lostMu.Lock()
	d.lostReceipts[id] = true
	d.lostMu.Unlock()
}

// MarkUnknown puts id into the temporary-unknown state: queries keep
// returning StatusUnknown until ResolveUnknown is called.
func (d *SimDevice) MarkUnknown(id WriteID) {
	d.unknownMu.Lock()
	d.unknownWrites[id] = true
	d.unknownMu.Unlock()
}

// ResolveUnknown clears the temporary-unknown flag. The next query then
// reports the real state (written or not-started).
func (d *SimDevice) ResolveUnknown(id WriteID) {
	d.unknownMu.Lock()
	delete(d.unknownWrites, id)
	d.unknownMu.Unlock()
}

// FailFirstPath makes the very first attempt to send id fail with a
// disconnected path before the device applies anything. A second attempt
// on a replacement path then applies normally.
func (d *SimDevice) FailFirstPath(id WriteID) {
	d.failFirstMu.Lock()
	d.failFirstPath[id] = true
	d.failFirstMu.Unlock()
}

// AttemptCount returns how many times the client sent id over a path
// (initial send plus every re-send).
func (d *SimDevice) AttemptCount(id WriteID) int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.attempts[id]
}

// HasStarted reports whether id has been applied at least once.
func (d *SimDevice) HasStarted(id WriteID) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.applied[id]
}

// ApplyOrder returns the IDs in the order the device applied them.
func (d *SimDevice) ApplyOrder() []WriteID {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]WriteID, len(d.order))
	copy(out, d.order)
	return out
}

// Snapshot returns a copy of the current device contents.
func (d *SimDevice) Snapshot() []byte {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]byte, len(d.data))
	copy(out, d.data)
	return out
}

// simPath is one generation of a transport to its SimDevice.
type simPath struct {
	device     *SimDevice
	generation int
	brokenMu   sync.Mutex
	broken     bool
}

func (p *simPath) Info() PathInfo {
	return PathInfo{DeviceID: p.device.id, Generation: p.generation}
}

func (p *simPath) markBroken() {
	p.brokenMu.Lock()
	p.broken = true
	p.brokenMu.Unlock()
}

func (p *simPath) Broken() bool {
	p.brokenMu.Lock()
	defer p.brokenMu.Unlock()
	return p.broken
}

func (p *simPath) Write(ctx context.Context, id WriteID, offset int, data []byte) (WriteResult, error) {
	if p.Broken() {
		return WriteResult{}, ErrPathDisconnected
	}
	d := p.device
	d.mu.Lock()
	d.attempts[id]++
	d.mu.Unlock()

	// Injected failure of the first transport: nothing reaches the
	// device, the path goes away.
	d.failFirstMu.Lock()
	failFirst := d.failFirstPath[id]
	delete(d.failFirstPath, id)
	d.failFirstMu.Unlock()
	if failFirst {
		p.markBroken()
		return WriteResult{}, ErrPathDisconnected
	}

	applied, err := d.apply(ctx, id, offset, data)
	if err != nil {
		return WriteResult{}, err
	}

	// Injected lost receipt: the bytes landed but the ack does not.
	d.lostMu.Lock()
	lost := d.lostReceipts[id]
	delete(d.lostReceipts, id)
	d.lostMu.Unlock()
	if lost {
		p.markBroken()
		return WriteResult{}, ErrPathDisconnected
	}

	if !applied {
		// Idempotent re-send of an already-written ID: the device just
		// confirms it without changing anything.
	}
	return WriteResult{ID: id, Path: p.Info()}, nil
}

func (p *simPath) Query(ctx context.Context, id WriteID) (QueryResult, error) {
	if p.Broken() {
		return QueryResult{}, ErrPathDisconnected
	}
	d := p.device

	d.unknownMu.Lock()
	unknown := d.unknownWrites[id]
	d.unknownMu.Unlock()
	if unknown {
		return QueryResult{ID: id, Status: StatusUnknown, Path: p.Info()}, nil
	}

	d.mu.Lock()
	switch {
	case d.unknown[id]:
		// Unknown state held inside the device itself (not currently
		// produced, but keeps the adapter honest).
	case d.applied[id]:
		d.mu.Unlock()
		return QueryResult{ID: id, Status: StatusWritten, Path: p.Info()}, nil
	default:
		d.mu.Unlock()
		return QueryResult{ID: id, Status: StatusNotStarted, Path: p.Info()}, nil
	}
	d.mu.Unlock()
	return QueryResult{ID: id, Status: StatusUnknown, Path: p.Info()}, nil
}

func (p *simPath) Read(ctx context.Context, offset int, buf []byte) (ReadResult, error) {
	if p.Broken() {
		return ReadResult{}, ErrPathDisconnected
	}
	d := p.device
	d.mu.Lock()
	defer d.mu.Unlock()
	if offset < 0 || len(buf) == 0 || offset+len(buf) > d.size {
		return ReadResult{}, errors.New("multipathwrite: read out of range")
	}
	copy(buf, d.data[offset:offset+len(buf)])
	return ReadResult{Data: buf, Path: p.Info()}, nil
}

func (p *simPath) Flush(ctx context.Context) (FlushResult, error) {
	if p.Broken() {
		return FlushResult{}, ErrPathDisconnected
	}
	d := p.device
	// The simulated store is already synchronous; flush on a live path
	// is the durable barrier required by the protocol.
	d.mu.Lock()
	d.mu.Unlock()
	return FlushResult{Path: p.Info()}, nil
}

// apply is the shared device-side write logic reached through any path.
// It returns applied=false when an idempotent re-send found the write
// already present.
func (d *SimDevice) apply(ctx context.Context, id WriteID, offset int, data []byte) (bool, error) {
	if offset < 0 || len(data) == 0 || offset+len(data) > d.size {
		return false, errors.New("multipathwrite: write out of range")
	}

	// Honor an installed ordering gate while respecting cancellation.
	d.gatesMu.Lock()
	gate := d.gates[id]
	d.gatesMu.Unlock()
	if gate != nil {
		select {
		case <-gate:
		case <-ctx.Done():
			return false, ctx.Err()
		}
	}

	d.mu.Lock()
	defer d.mu.Unlock()
	if d.applied[id] {
		return false, nil
	}
	copy(d.data[offset:offset+len(data)], data)
	d.applied[id] = true
	d.order = append(d.order, id)
	return true, nil
}
