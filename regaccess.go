// Package regaccess models a transactional register-access module for a
// small synthetic device.
//
// An Attempt is one instruction. Event reads record the event generations
// observed by that attempt, field and register writes are staged privately,
// and Commit applies every event acknowledgement and write atomically to the
// Device. Abort discards all staged state.
package regaccess

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
)

// Common sentinel errors. Callers can use errors.Is to classify failures.
var (
	ErrInvalidArgument      = errors.New("regaccess: invalid argument")
	ErrInstructionCommitted = errors.New("regaccess: instruction already committed")
	ErrUnknownInstruction   = errors.New("regaccess: unknown committed instruction")
	ErrAttemptCommitted     = errors.New("regaccess: attempt already committed")
	ErrAttemptAborted       = errors.New("regaccess: attempt aborted")
	ErrInvalidField         = errors.New("regaccess: invalid register field")
	ErrInvalidValue         = errors.New("regaccess: value does not fit field")
	ErrReplayMismatch       = errors.New("regaccess: replay does not match recorded read")
	ErrReplayExhausted      = errors.New("regaccess: no more recorded reads")
)

// EventMask is a bit set of synthetic event lines.
type EventMask uint32

// Synthetic event lines. Additional device-specific bits can be used directly
// as EventMask(1 << n).
const (
	EventCommandComplete EventMask = 1 << iota
	EventPeripheralError
	EventDataReady
)

// Has reports whether bit is present in the mask.
func (m EventMask) Has(bit EventMask) bool { return m&bit != 0 }

// IsEmpty reports whether the mask is zero.
func (m EventMask) IsEmpty() bool { return m == 0 }

// eventLine keeps a monotonic frontier. Every Raise advances latest. Reading
// observes latest and stages an acknowledgement through that generation.
// Acknowledging only an older frontier leaves newer raises visible.
type eventLine struct {
	latest  uint64
	ackedTo uint64
}

// EventRead is the immutable result of observing event lines.
//
// Generation returns the synthetic event generation observed for a line.
// Generations make an event that arrived after a read distinguishable from
// the older event observed by that read.
type EventRead struct {
	Mask        EventMask
	generations map[EventMask]uint64
}

// Has reports whether bit was present in the event read.
func (r EventRead) Has(bit EventMask) bool {
	return r.Mask.Has(bit)
}

// Generation returns the observed generation for bit. The boolean is false if
// the bit was not present in this read.
func (r EventRead) Generation(bit EventMask) (uint64, bool) {
	g, ok := r.generations[bit]
	return g, ok
}

// Bits returns the observed event bits in ascending bit order.
func (r EventRead) Bits() []EventMask {
	bits := make([]EventMask, 0, len(r.generations))
	for bit := range r.generations {
		bits = append(bits, bit)
	}
	sort.Slice(bits, func(i, j int) bool { return bits[i] < bits[j] })
	return bits
}

// Clone returns an independent copy of the read result.
func (r EventRead) Clone() EventRead {
	gens := make(map[EventMask]uint64, len(r.generations))
	for bit, gen := range r.generations {
		gens[bit] = gen
	}
	return EventRead{Mask: r.Mask, generations: gens}
}

// EventAck records that an attempt acknowledged all event generations through
// Through for Bit.
type EventAck struct {
	Bit     EventMask
	Through uint64
}

// Device is the externally visible synthetic register device.
//
// Its methods are safe for concurrent use. A transactional commit takes the
// device lock once, so all register writes and event acknowledgements in one
// instruction become visible together.
type Device struct {
	mu     sync.RWMutex
	regs   map[uint16]uint32
	events map[EventMask]*eventLine
}

// NewDevice creates an empty synthetic device. Unwritten registers read as
// zero.
func NewDevice() *Device {
	return &Device{
		regs:   make(map[uint16]uint32),
		events: make(map[EventMask]*eventLine),
	}
}

// Raise sets every event line in events and creates a new generation on each
// line, including a line that was already active. This models an edge or
// re-armed event arriving while an older observed event is awaiting commit.
func (d *Device) Raise(events EventMask) {
	if events == 0 {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	for i := uint(0); i < 32; i++ {
		bit := EventMask(1) << i
		if events&bit == 0 {
			continue
		}
		line := d.events[bit]
		if line == nil {
			line = &eventLine{}
			d.events[bit] = line
		}
		line.latest++
	}
}

// RaiseEvent is a singular convenience wrapper for Raise.
func (d *Device) RaiseEvent(bit EventMask) { d.Raise(bit) }

// PeekEvents returns the current externally visible active events without
// acknowledging them.
func (d *Device) PeekEvents() EventRead {
	d.mu.RLock()
	defer d.mu.RUnlock()

	mask := EventMask(0)
	gens := make(map[EventMask]uint64)
	for bit, line := range d.events {
		if line.latest > line.ackedTo {
			mask |= bit
			gens[bit] = line.latest
		}
	}
	return EventRead{Mask: mask, generations: gens}
}

// EventProgress returns the acknowledged and newest generations for bit.
// ok is false if the synthetic line has never been raised.
func (d *Device) EventProgress(bit EventMask) (ackedTo, latest uint64, ok bool) {
	d.mu.RLock()
	defer d.mu.RUnlock()
	line, exists := d.events[bit]
	if !exists {
		return 0, 0, false
	}
	return line.ackedTo, line.latest, true
}

// ReadRegister reads one externally committed register value.
func (d *Device) ReadRegister(address uint16) uint32 {
	d.mu.RLock()
	defer d.mu.RUnlock()
	return d.regs[address]
}

// SetRegister directly loads a register. It is intended for synthetic test
// setup or external device mutation in replay tests, not for instruction
// writes; instruction writes should go through an Attempt.
func (d *Device) SetRegister(address uint16, value uint32) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.regs[address] = value
}

// RegisterSnapshot reads several registers under one device lock. The returned
// values therefore represent one externally visible device state and cannot
// contain a partial multi-register commit.
func (d *Device) RegisterSnapshot(addresses ...uint16) map[uint16]uint32 {
	d.mu.RLock()
	defer d.mu.RUnlock()
	out := make(map[uint16]uint32, len(addresses))
	for _, address := range addresses {
		out[address] = d.regs[address]
	}
	return out
}

// apply commits all acknowledgements and writes while holding the device lock.
func (d *Device) apply(writes []WriteOp, acks []EventAck) {
	d.mu.Lock()
	defer d.mu.Unlock()

	for _, ack := range acks {
		line := d.events[ack.Bit]
		if line == nil {
			line = &eventLine{}
			d.events[ack.Bit] = line
		}
		// A later attempt may already have acknowledged farther. Never move
		// an acknowledgement frontier backwards.
		if ack.Through > line.ackedTo {
			line.ackedTo = ack.Through
		}
	}

	for _, op := range writes {
		switch op.Kind {
		case WriteRegister:
			d.regs[op.Address] = op.Value
		case WriteField:
			current := d.regs[op.Field.Register]
			next, err := op.Field.Encode(current, op.Value)
			if err != nil {
				// Values were validated before staging. Keep apply total.
				panic(fmt.Sprintf("regaccess: validated field write became invalid: %v", err))
			}
			d.regs[op.Field.Register] = next
		}
	}
}

// Standard synthetic configuration registers.
const (
	RegControl uint16 = 0x04
	RegTag     uint16 = 0x08
)

// Field describes an unsigned bit field in a 32-bit register.
type Field struct {
	Name     string
	Register uint16
	LSB      uint
	Width    uint
}

// Validate checks that the field fits in a 32-bit register.
func (f Field) Validate() error {
	if f.Width == 0 || f.Width > 32 {
		return fmt.Errorf("%w: field %q has invalid width %d", ErrInvalidField, f.Name, f.Width)
	}
	if f.LSB >= 32 || uint64(f.LSB)+uint64(f.Width) > 32 {
		return fmt.Errorf("%w: field %q bits [%d:%d] exceed uint32",
			ErrInvalidField, f.Name, f.LSB, f.LSB+f.Width)
	}
	return nil
}

func (f Field) valueMask() uint64 {
	return uint64(1)<<f.Width - 1
}

// Decode extracts the field value from a full register value.
func (f Field) Decode(registerValue uint32) (uint32, error) {
	if err := f.Validate(); err != nil {
		return 0, err
	}
	mask := uint32(f.valueMask() << f.LSB)
	return (registerValue & mask) >> f.LSB, nil
}

// Encode returns registerValue with this field replaced by value.
func (f Field) Encode(registerValue, value uint32) (uint32, error) {
	if err := f.Validate(); err != nil {
		return 0, err
	}
	maxValue := f.valueMask()
	if uint64(value) > maxValue {
		return 0, fmt.Errorf("%w: %s=%d exceeds %d bits",
			ErrInvalidValue, f.Name, value, f.Width)
	}
	mask := uint32(maxValue << f.LSB)
	return (registerValue &^ mask) | uint32((uint64(value)<<f.LSB)&uint64(mask)), nil
}

// Standard fields forming one synthetic configuration instruction.
var (
	FieldEnable = Field{Name: "ENABLE", Register: RegControl, LSB: 0, Width: 1}
	FieldMode   = Field{Name: "MODE", Register: RegControl, LSB: 1, Width: 3}
	FieldSpeed  = Field{Name: "SPEED", Register: RegControl, LSB: 4, Width: 4}
	FieldTag    = Field{Name: "TAG", Register: RegTag, LSB: 0, Width: 8}
)

// Config is the complete set of canonical configuration fields submitted by
// one instruction.
type Config struct {
	Enabled bool
	Mode    uint32
	Speed   uint32
	Tag     uint32
}

// ReadKind identifies a recorded read operation.
type ReadKind string

const (
	ReadEvents   ReadKind = "events"
	ReadRegister ReadKind = "register"
)

// RecordedRead is one original read result retained for replay.
type RecordedRead struct {
	Kind    ReadKind
	Address uint16
	Value   uint32
	Events  EventRead
}

// WriteKind identifies a staged write operation.
type WriteKind string

const (
	WriteField    WriteKind = "field"
	WriteRegister WriteKind = "register"
)

// WriteOp records a staged write. For WriteField, Value is the unshifted
// field value; for WriteRegister, Value is the full register value.
type WriteOp struct {
	Kind    WriteKind
	Field   Field
	Address uint16
	Value   uint32
}

// Session owns instruction idempotency records and the shared device.
type Session struct {
	device *Device

	mu       sync.Mutex
	receipts map[string]*Receipt
	nextSeq  uint64
}

// NewSession creates an instruction session. If device is nil, a new device
// is created.
func NewSession(device *Device) *Session {
	if device == nil {
		device = NewDevice()
	}
	return &Session{
		device:   device,
		receipts: make(map[string]*Receipt),
	}
}

// Device returns the session's synthetic device.
func (s *Session) Device() *Device { return s.device }

// Begin starts a new instruction attempt. An already committed ID cannot be
// started again; use Replay to retrieve its original results. An ID whose
// attempt was aborted may be started again.
func (s *Session) Begin(instructionID string) (*Attempt, error) {
	if strings.TrimSpace(instructionID) == "" {
		return nil, fmt.Errorf("%w: instruction id must not be empty", ErrInvalidArgument)
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	if _, committed := s.receipts[instructionID]; committed {
		return nil, fmt.Errorf("%w: %q", ErrInstructionCommitted, instructionID)
	}

	return &Attempt{
		session: s,
		id:      instructionID,
		staged:  make(map[uint16]uint32),
		acks:    make(map[EventMask]uint64),
	}, nil
}

// Receipt returns the immutable record for a committed instruction.
func (s *Session) Receipt(instructionID string) (*Receipt, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	receipt, ok := s.receipts[instructionID]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrUnknownInstruction, instructionID)
	}
	return receipt, nil
}

// Replay returns a cursor that yields the instruction's original read results
// in their original order. Replay never touches the device and has no side
// effects.
func (s *Session) Replay(instructionID string) (*Replayer, error) {
	receipt, err := s.Receipt(instructionID)
	if err != nil {
		return nil, err
	}
	return receipt.NewReplayer(), nil
}

type attemptState int

const (
	attemptOpen attemptState = iota
	attemptCommitted
	attemptAborted
)

// Attempt is one transactional instruction attempt.
type Attempt struct {
	session *Session
	id      string

	mu      sync.Mutex
	state   attemptState
	staged  map[uint16]uint32
	reads   []RecordedRead
	writes  []WriteOp
	acks    map[EventMask]uint64
	receipt *Receipt
}

// ID returns the instruction identifier.
func (a *Attempt) ID() string { return a.id }

func (a *Attempt) usableLocked() error {
	switch a.state {
	case attemptCommitted:
		return fmt.Errorf("%w: %q", ErrAttemptCommitted, a.id)
	case attemptAborted:
		return fmt.Errorf("%w: %q", ErrAttemptAborted, a.id)
	default:
		return nil
	}
}

// ReadEvents observes currently unread event generations for this attempt and
// stages an acknowledgement through each observed generation.
//
// The attempt's own staged acknowledgements hide an already observed line from
// later attempt-local reads, while Device.PeekEvents continues to show the
// line until Commit. A newer generation arriving after a read is not hidden.
func (a *Attempt) ReadEvents() (EventRead, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if err := a.usableLocked(); err != nil {
		return EventRead{}, err
	}
	return a.readEventsLocked(), nil
}

func (a *Attempt) readEventsLocked() EventRead {
	external := a.session.device.PeekEvents()
	result := EventRead{Mask: 0, generations: make(map[EventMask]uint64)}

	for _, bit := range external.Bits() {
		latest, _ := external.Generation(bit)
		if latest <= a.acks[bit] {
			// Already observed by this attempt and therefore staged for clear.
			continue
		}
		result.Mask |= bit
		result.generations[bit] = latest
		a.acks[bit] = latest
	}

	a.reads = append(a.reads, RecordedRead{
		Kind:   ReadEvents,
		Events: result.Clone(),
	})
	return result
}

// ReadRegister returns the attempt-local value: a staged value when present,
// otherwise the current externally committed device value.
func (a *Attempt) ReadRegister(address uint16) (uint32, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if err := a.usableLocked(); err != nil {
		return 0, err
	}
	return a.readRegisterLocked(address), nil
}

func (a *Attempt) readRegisterLocked(address uint16) uint32 {
	value, staged := a.staged[address]
	if !staged {
		value = a.session.device.ReadRegister(address)
	}
	a.reads = append(a.reads, RecordedRead{
		Kind:    ReadRegister,
		Address: address,
		Value:   value,
	})
	return value
}

// ReadField reads and decodes a field using attempt-local state.
func (a *Attempt) ReadField(field Field) (uint32, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if err := a.usableLocked(); err != nil {
		return 0, err
	}
	if err := field.Validate(); err != nil {
		return 0, err
	}
	registerValue := a.readRegisterLocked(field.Register)
	return field.Decode(registerValue)
}

// StagedRegister returns the currently staged full value for address. The
// boolean is false if no write has been staged for that register.
func (a *Attempt) StagedRegister(address uint16) (uint32, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	value, ok := a.staged[address]
	return value, ok
}

// PendingEventAcks returns the event generations this attempt will acknowledge
// if Commit succeeds.
func (a *Attempt) PendingEventAcks() []EventAck {
	a.mu.Lock()
	defer a.mu.Unlock()
	return sortedAcks(a.acks)
}

// StageRegister stages a whole-register write. It is not externally visible
// until Commit.
func (a *Attempt) StageRegister(address uint16, value uint32) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if err := a.usableLocked(); err != nil {
		return err
	}
	a.staged[address] = value
	a.writes = append(a.writes, WriteOp{
		Kind:    WriteRegister,
		Address: address,
		Value:   value,
	})
	return nil
}

// StageField stages one bit-field update. Repeated fields and multiple
// registers remain private until the single Commit.
func (a *Attempt) StageField(field Field, value uint32) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if err := a.usableLocked(); err != nil {
		return err
	}
	return a.stageFieldLocked(field, value)
}

func (a *Attempt) stageFieldLocked(field Field, value uint32) error {
	if err := field.Validate(); err != nil {
		return err
	}
	if _, err := field.Encode(0, value); err != nil {
		return err
	}

	current, staged := a.staged[field.Register]
	if !staged {
		current = a.session.device.ReadRegister(field.Register)
	}
	next, err := field.Encode(current, value)
	if err != nil {
		return err
	}
	a.staged[field.Register] = next
	a.writes = append(a.writes, WriteOp{
		Kind:    WriteField,
		Field:   field,
		Address: field.Register,
		Value:   value,
	})
	return nil
}

// StageConfig stages all canonical configuration fields as part of this same
// instruction. Validation happens before any field is modified, so an invalid
// Config leaves the attempt unchanged.
func (a *Attempt) StageConfig(config Config) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if err := a.usableLocked(); err != nil {
		return err
	}

	values := []struct {
		field Field
		value uint32
	}{
		{FieldEnable, boolToUint(config.Enabled)},
		{FieldMode, config.Mode},
		{FieldSpeed, config.Speed},
		{FieldTag, config.Tag},
	}

	// Validate the complete group before staging any of it.
	for _, item := range values {
		if err := item.field.Validate(); err != nil {
			return err
		}
		if _, err := item.field.Encode(0, item.value); err != nil {
			return err
		}
	}
	for _, item := range values {
		if err := a.stageFieldLocked(item.field, item.value); err != nil {
			return err
		}
	}
	return nil
}

func boolToUint(v bool) uint32 {
	if v {
		return 1
	}
	return 0
}

// Commit atomically applies event acknowledgements and staged writes.
//
// Commit is idempotent for the same Attempt: repeated calls return the same
// receipt and do not apply the instruction twice. Attempts sharing an ID with
// an already committed instruction receive ErrInstructionCommitted.
func (a *Attempt) Commit() (*Receipt, error) {
	a.mu.Lock()
	defer a.mu.Unlock()

	if a.state == attemptCommitted {
		return a.receipt, nil
	}
	if a.state == attemptAborted {
		return nil, fmt.Errorf("%w: %q", ErrAttemptAborted, a.id)
	}

	receipt, err := a.session.commit(a)
	if err != nil {
		return nil, err
	}
	a.state = attemptCommitted
	a.receipt = receipt
	return receipt, nil
}

// Abort discards all staged writes, reads and pending event acknowledgements.
// It is idempotent. Aborting a committed attempt returns
// ErrAttemptCommitted.
func (a *Attempt) Abort() error {
	a.mu.Lock()
	defer a.mu.Unlock()

	if a.state == attemptCommitted {
		return fmt.Errorf("%w: %q", ErrAttemptCommitted, a.id)
	}
	if a.state == attemptAborted {
		return nil
	}

	a.state = attemptAborted
	a.staged = nil
	a.writes = nil
	a.reads = nil
	a.acks = nil
	return nil
}

// Rollback is the database-style alias for Abort.
func (a *Attempt) Rollback() error { return a.Abort() }

func (s *Session) commit(a *Attempt) (*Receipt, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if _, exists := s.receipts[a.id]; exists {
		return nil, fmt.Errorf("%w: %q", ErrInstructionCommitted, a.id)
	}

	s.nextSeq++
	receipt := &Receipt{
		id:       a.id,
		sequence: s.nextSeq,
		reads:    cloneReads(a.reads),
		writes:   append([]WriteOp(nil), a.writes...),
		acks:     sortedAcks(a.acks),
	}

	// The device lock makes the complete instruction visible atomically.
	s.device.apply(receipt.writes, receipt.acks)
	s.receipts[a.id] = receipt
	return receipt, nil
}

func sortedAcks(acks map[EventMask]uint64) []EventAck {
	out := make([]EventAck, 0, len(acks))
	for bit, through := range acks {
		if through == 0 {
			continue
		}
		out = append(out, EventAck{Bit: bit, Through: through})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Bit < out[j].Bit })
	return out
}

func cloneReads(reads []RecordedRead) []RecordedRead {
	out := make([]RecordedRead, len(reads))
	for i, read := range reads {
		out[i] = read
		if read.Kind == ReadEvents {
			out[i].Events = read.Events.Clone()
		}
	}
	return out
}

// Receipt is the immutable commit record used for idempotent replay.
type Receipt struct {
	id       string
	sequence uint64
	reads    []RecordedRead
	writes   []WriteOp
	acks     []EventAck
}

// ID returns the instruction ID.
func (r *Receipt) ID() string { return r.id }

// Sequence returns the session-local commit sequence number.
func (r *Receipt) Sequence() uint64 { return r.sequence }

// Reads returns a copy of all recorded reads.
func (r *Receipt) Reads() []RecordedRead { return cloneReads(r.reads) }

// Writes returns a copy of all staged write operations.
func (r *Receipt) Writes() []WriteOp { return append([]WriteOp(nil), r.writes...) }

// EventAcks returns a copy of the committed event acknowledgements.
func (r *Receipt) EventAcks() []EventAck { return append([]EventAck(nil), r.acks...) }

// EventReads returns cloned event-read results in recording order.
func (r *Receipt) EventReads() []EventRead {
	out := make([]EventRead, 0, len(r.reads))
	for _, read := range r.reads {
		if read.Kind == ReadEvents {
			out = append(out, read.Events.Clone())
		}
	}
	return out
}

// NewReplayer creates a read cursor over this receipt's original results.
func (r *Receipt) NewReplayer() *Replayer {
	return &Replayer{receipt: r}
}

// Replayer yields recorded read results in the exact order in which the
// instruction observed them.
type Replayer struct {
	receipt *Receipt
	cursor  int
}

// Receipt returns the receipt being replayed.
func (rp *Replayer) Receipt() *Receipt { return rp.receipt }

// Cursor returns the number of reads already consumed.
func (rp *Replayer) Cursor() int { return rp.cursor }

// Remaining returns the number of recorded reads not yet consumed.
func (rp *Replayer) Remaining() int { return len(rp.receipt.reads) - rp.cursor }

// ReadEvents returns the next recorded event read.
func (rp *Replayer) ReadEvents() (EventRead, error) {
	if rp.cursor >= len(rp.receipt.reads) {
		return EventRead{}, fmt.Errorf("%w: instruction %q", ErrReplayExhausted, rp.receipt.id)
	}
	record := rp.receipt.reads[rp.cursor]
	if record.Kind != ReadEvents {
		return EventRead{}, fmt.Errorf(
			"%w: read #%d is %s, not events",
			ErrReplayMismatch, rp.cursor+1, record.Kind)
	}
	rp.cursor++
	return record.Events.Clone(), nil
}

// ReadRegister returns the next recorded register read and verifies address.
// A mismatch does not consume the current record.
func (rp *Replayer) ReadRegister(address uint16) (uint32, error) {
	if rp.cursor >= len(rp.receipt.reads) {
		return 0, fmt.Errorf("%w: instruction %q", ErrReplayExhausted, rp.receipt.id)
	}
	record := rp.receipt.reads[rp.cursor]
	if record.Kind != ReadRegister {
		return 0, fmt.Errorf(
			"%w: read #%d is %s, not register",
			ErrReplayMismatch, rp.cursor+1, record.Kind)
	}
	if record.Address != address {
		return 0, fmt.Errorf(
			"%w: read #%d address = 0x%x, want 0x%x",
			ErrReplayMismatch, rp.cursor+1, address, record.Address)
	}
	rp.cursor++
	return record.Value, nil
}

// ReadField replays the full register read backing a field and decodes the
// recorded value.
func (rp *Replayer) ReadField(field Field) (uint32, error) {
	if err := field.Validate(); err != nil {
		return 0, err
	}
	value, err := rp.ReadRegister(field.Register)
	if err != nil {
		return 0, err
	}
	return field.Decode(value)
}
