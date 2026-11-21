// Package zoned simulates a device made of equal-length address zones with
// append-only writes.
//
// Each zone has a fixed address length (ZoneBlocks) and a writable capacity
// that may be smaller than or equal to that length. Appends are made in whole
// blocks and the device reports the actual starting block of every accepted
// append. A device-wide mutex makes each append atomic, so concurrent appends
// to the same zone each receive one contiguous, non-overlapping range.
//
// Two device-wide limits are enforced:
//
//   - MaxOpenZones: how many zones may be open at the same time.
//   - MaxActiveZones: how many zones may be active at the same time. Open
//     zones and closed-but-not-full zones are both active. Once a zone becomes
//     full it leaves both the open and the active count.
//
// An append to an empty or closed zone must first acquire an opening credit,
// even when that single append would fill the zone. If the opening credit or
// the active-zone slot is unavailable, the append fails as a whole: no write
// pointer movement and no state change. An append that does not fit in the
// remaining writable range fails in the same atomic way.
package zoned

import (
	"errors"
	"fmt"
	"sync"
)

// BlockSize is the size in bytes of one addressable block expected by Append.
// Only the block count is simulated; the data bytes are validated but not stored.
const BlockSize = 512

// Sentinel errors. Methods wrap them with fmt.Errorf so callers can match with
// errors.Is while still receiving contextual detail.
var (
	ErrInvalidConfig     = errors.New("zoned: invalid config")
	ErrZoneOutOfRange    = errors.New("zoned: zone out of range")
	ErrInvalidAppendSize = errors.New("zoned: append data must be a positive whole number of blocks")
	ErrZoneFull          = errors.New("zoned: zone is full")
	ErrNoSpace           = errors.New("zoned: append does not fit in remaining zone capacity")
	ErrNoOpenQuota       = errors.New("zoned: open-zone quota exhausted")
	ErrActiveLimit       = errors.New("zoned: active-zone limit reached")
	ErrAlreadyOpen       = errors.New("zoned: zone already open")
	ErrNotOpen           = errors.New("zoned: zone is not open")
	ErrNotActive         = errors.New("zoned: zone is not active")
	ErrAlreadyFull       = errors.New("zoned: zone already full")
)

type phase uint8

const (
	phaseEmpty phase = iota
	phaseOpen
	phaseClosed
	phaseFull
)

func (p phase) String() string {
	switch p {
	case phaseEmpty:
		return "empty"
	case phaseOpen:
		return "open"
	case phaseClosed:
		return "closed"
	case phaseFull:
		return "full"
	default:
		return "unknown"
	}
}

type zone struct {
	capacity int
	written  int
	phase    phase
}

// Config describes a simulated zoned device.
type Config struct {
	// ZoneCount is the number of equal-length address zones. Must be > 0.
	ZoneCount int
	// ZoneBlocks is the address length of every zone in blocks. Must be > 0.
	ZoneBlocks int
	// Capacities optionally gives the writable capacity in blocks for each
	// zone, in zone order. Each value must be in [1, ZoneBlocks]. When nil,
	// every zone gets a writable capacity equal to ZoneBlocks.
	Capacities []int
	// MaxOpenZones is the maximum number of zones open at once. Must be in
	// [1, ZoneCount].
	MaxOpenZones int
	// MaxActiveZones is the maximum number of active zones. Open zones and
	// closed-but-not-full zones are active. Must be in [1, ZoneCount].
	MaxActiveZones int
}

// Result reports where an accepted append was placed.
type Result struct {
	// Zone is the zone index that accepted the data.
	Zone int
	// StartBlock is the device-wide block number of the first written block.
	// It equals Zone*ZoneBlocks + local write pointer at the time of append.
	StartBlock int
	// Blocks is the number of whole blocks written by this append.
	Blocks int
}

// ZoneState is a snapshot of one zone. It is available after failed appends so
// callers can inspect the unchanged write pointer and remaining writable range.
type ZoneState struct {
	Zone int
	// ZoneBlocks is the address length of the zone in blocks.
	ZoneBlocks int
	// CapacityBlocks is the writable capacity in blocks (<= ZoneBlocks).
	CapacityBlocks int
	// WritePointer is the next zone-local block that would be written.
	WritePointer int
	// RemainingBlocks is the number of blocks that may still be written. It is
	// 0 for a full zone, including a zone marked full by Finish before its
	// writable capacity was exhausted.
	RemainingBlocks int
	Open            bool
	Closed          bool
	Full            bool
	Active          bool
}

// Stats is a device-wide snapshot of zone counters.
type Stats struct {
	TotalZones  int
	OpenZones   int
	ActiveZones int
	FullZones   int
}

// Device is a simulated zoned append device. It is safe for concurrent use.
type Device struct {
	mu          sync.Mutex
	zoneBlocks  int
	maxOpen     int
	maxActive   int
	zones       []zone
	openCount   int
	activeCount int
	fullCount   int
}

// New validates cfg and constructs a device.
func New(cfg Config) (*Device, error) {
	if cfg.ZoneCount <= 0 {
		return nil, fmt.Errorf("%w: ZoneCount must be > 0, got %d", ErrInvalidConfig, cfg.ZoneCount)
	}
	if cfg.ZoneBlocks <= 0 {
		return nil, fmt.Errorf("%w: ZoneBlocks must be > 0, got %d", ErrInvalidConfig, cfg.ZoneBlocks)
	}
	if cfg.MaxOpenZones <= 0 || cfg.MaxOpenZones > cfg.ZoneCount {
		return nil, fmt.Errorf("%w: MaxOpenZones must be in [1, %d], got %d", ErrInvalidConfig, cfg.ZoneCount, cfg.MaxOpenZones)
	}
	if cfg.MaxActiveZones <= 0 || cfg.MaxActiveZones > cfg.ZoneCount {
		return nil, fmt.Errorf("%w: MaxActiveZones must be in [1, %d], got %d", ErrInvalidConfig, cfg.ZoneCount, cfg.MaxActiveZones)
	}

	caps := cfg.Capacities
	if caps == nil {
		caps = make([]int, cfg.ZoneCount)
		for i := range caps {
			caps[i] = cfg.ZoneBlocks
		}
	}
	if len(caps) != cfg.ZoneCount {
		return nil, fmt.Errorf("%w: Capacities length %d does not match ZoneCount %d", ErrInvalidConfig, len(caps), cfg.ZoneCount)
	}

	zones := make([]zone, cfg.ZoneCount)
	for i, c := range caps {
		if c <= 0 || c > cfg.ZoneBlocks {
			return nil, fmt.Errorf("%w: capacity of zone %d must be in [1, %d], got %d", ErrInvalidConfig, i, cfg.ZoneBlocks, c)
		}
		zones[i] = zone{capacity: c, phase: phaseEmpty}
	}

	return &Device{
		zoneBlocks: cfg.ZoneBlocks,
		maxOpen:    cfg.MaxOpenZones,
		maxActive:  cfg.MaxActiveZones,
		zones:      zones,
	}, nil
}

// ZoneCount returns the number of zones in the device.
func (d *Device) ZoneCount() int {
	return len(d.zones)
}

// ZoneBlocks returns the address length of every zone in blocks.
func (d *Device) ZoneBlocks() int {
	return d.zoneBlocks
}

func (d *Device) checkZone(idx int) error {
	if idx < 0 || idx >= len(d.zones) {
		return fmt.Errorf("%w: %d (device has %d zones)", ErrZoneOutOfRange, idx, len(d.zones))
	}
	return nil
}

// Open acquires an opening credit for a zone that is empty or closed-but-not-
// full. An empty zone also needs an active-zone slot; a closed zone is already
// active. A full zone must be Reset before it can be opened again.
func (d *Device) Open(idx int) error {
	if err := d.checkZone(idx); err != nil {
		return err
	}
	d.mu.Lock()
	defer d.mu.Unlock()

	z := &d.zones[idx]
	switch z.phase {
	case phaseOpen:
		return fmt.Errorf("%w: zone %d", ErrAlreadyOpen, idx)
	case phaseFull:
		return fmt.Errorf("%w: zone %d", ErrZoneFull, idx)
	}
	if d.openCount >= d.maxOpen {
		return fmt.Errorf("%w: zone %d (open %d/%d)", ErrNoOpenQuota, idx, d.openCount, d.maxOpen)
	}
	if z.phase == phaseEmpty && d.activeCount >= d.maxActive {
		return fmt.Errorf("%w: zone %d (active %d/%d)", ErrActiveLimit, idx, d.activeCount, d.maxActive)
	}
	if z.phase == phaseEmpty {
		d.activeCount++
	}
	d.openCount++
	z.phase = phaseOpen
	return nil
}

// Close closes an open, non-full zone. The write pointer is preserved so a
// later Open or Append resumes at the same position. Closing releases the open
// credit but the zone remains active because it is not full.
func (d *Device) Close(idx int) error {
	if err := d.checkZone(idx); err != nil {
		return err
	}
	d.mu.Lock()
	defer d.mu.Unlock()

	z := &d.zones[idx]
	if z.phase != phaseOpen {
		return fmt.Errorf("%w: zone %d is %s", ErrNotOpen, idx, z.phase)
	}
	z.phase = phaseClosed
	d.openCount--
	return nil
}

// Finish ends an active zone and marks it full directly, even if its writable
// capacity was not exhausted. A full zone leaves both the open and active
// counts. The write pointer is preserved; only Reset allows writing from the
// start again. Finishing an empty zone or a full zone is an error.
func (d *Device) Finish(idx int) error {
	if err := d.checkZone(idx); err != nil {
		return err
	}
	d.mu.Lock()
	defer d.mu.Unlock()

	z := &d.zones[idx]
	switch z.phase {
	case phaseFull:
		return fmt.Errorf("%w: zone %d", ErrAlreadyFull, idx)
	case phaseEmpty:
		return fmt.Errorf("%w: zone %d is empty", ErrNotActive, idx)
	case phaseOpen:
		d.openCount--
		d.activeCount--
	case phaseClosed:
		d.activeCount--
	}
	z.phase = phaseFull
	d.fullCount++
	return nil
}

// Reset returns a zone to the empty state and resets its write pointer to zero.
// Reset is the only operation that allows a full (or partially written) zone
// to be written from the start again. Resetting an empty zone is an error.
func (d *Device) Reset(idx int) error {
	if err := d.checkZone(idx); err != nil {
		return err
	}
	d.mu.Lock()
	defer d.mu.Unlock()

	z := &d.zones[idx]
	switch z.phase {
	case phaseEmpty:
		return fmt.Errorf("%w: zone %d is already empty", ErrNotActive, idx)
	case phaseOpen:
		d.openCount--
		d.activeCount--
	case phaseClosed:
		d.activeCount--
	case phaseFull:
		d.fullCount--
	}
	z.phase = phaseEmpty
	z.written = 0
	return nil
}

// Append writes len(data)/BlockSize whole blocks to the zone and returns the
// actual starting block. len(data) must be a positive multiple of BlockSize.
//
// The append is atomic: it succeeds completely or changes nothing. It fails
// when the zone is full, when the blocks do not fit in the remaining writable
// capacity, or when the zone is empty/closed and an opening credit or active
// slot cannot be acquired. The opening requirement applies even if the append
// would fill the zone.
func (d *Device) Append(idx int, data []byte) (Result, error) {
	if err := d.checkZone(idx); err != nil {
		return Result{}, err
	}
	if len(data) == 0 || len(data)%BlockSize != 0 {
		return Result{}, fmt.Errorf("%w: got %d bytes", ErrInvalidAppendSize, len(data))
	}
	blocks := len(data) / BlockSize

	d.mu.Lock()
	defer d.mu.Unlock()

	z := &d.zones[idx]
	if z.phase == phaseFull {
		return Result{}, fmt.Errorf("%w: zone %d", ErrZoneFull, idx)
	}
	remaining := z.capacity - z.written
	if blocks > remaining {
		return Result{}, fmt.Errorf("%w: zone %d needs %d blocks, %d remain", ErrNoSpace, idx, blocks, remaining)
	}

	if z.phase != phaseOpen {
		if d.openCount >= d.maxOpen {
			return Result{}, fmt.Errorf("%w: zone %d (open %d/%d)", ErrNoOpenQuota, idx, d.openCount, d.maxOpen)
		}
		if z.phase == phaseEmpty && d.activeCount >= d.maxActive {
			return Result{}, fmt.Errorf("%w: zone %d (active %d/%d)", ErrActiveLimit, idx, d.activeCount, d.maxActive)
		}
		if z.phase == phaseEmpty {
			d.activeCount++
		}
		d.openCount++
		z.phase = phaseOpen
	}

	start := z.written
	z.written += blocks
	if z.written == z.capacity {
		// A full zone exits both counters.
		z.phase = phaseFull
		d.openCount--
		d.activeCount--
		d.fullCount++
	}

	return Result{
		Zone:       idx,
		StartBlock: idx*d.zoneBlocks + start,
		Blocks:     blocks,
	}, nil
}

// State returns a snapshot of one zone. It is safe to call after a failed
// append to inspect the unchanged write pointer and remaining writable range.
func (d *Device) State(idx int) (ZoneState, error) {
	if err := d.checkZone(idx); err != nil {
		return ZoneState{}, err
	}
	d.mu.Lock()
	defer d.mu.Unlock()

	z := &d.zones[idx]
	remaining := z.capacity - z.written
	if z.phase == phaseFull {
		remaining = 0
	}
	return ZoneState{
		Zone:            idx,
		ZoneBlocks:      d.zoneBlocks,
		CapacityBlocks:  z.capacity,
		WritePointer:    z.written,
		RemainingBlocks: remaining,
		Open:            z.phase == phaseOpen,
		Closed:          z.phase == phaseClosed,
		Full:            z.phase == phaseFull,
		Active:          z.phase == phaseOpen || z.phase == phaseClosed,
	}, nil
}

// Stats returns a device-wide counter snapshot.
func (d *Device) Stats() Stats {
	d.mu.Lock()
	defer d.mu.Unlock()
	return Stats{
		TotalZones:  len(d.zones),
		OpenZones:   d.openCount,
		ActiveZones: d.activeCount,
		FullZones:   d.fullCount,
	}
}
