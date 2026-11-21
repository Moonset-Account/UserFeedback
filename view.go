// Package incview implements a transaction-aware incrementally maintained
// materialized view over a fact table and a device dimension table.
//
// Facts are keyed by row number and carry a device id and an integer
// duration. Devices are mapped to rooms. The view is the inner join of the
// two tables grouped by room, exposing row count, total duration and minimum
// duration per room.
//
// Each commit produces only the groups whose aggregate differs between the
// state immediately before and immediately after the commit. A group whose
// last row leaves the join disappears from the view. Facts whose device has
// no mapping stay in the fact table but do not contribute to the view.
//
// Maintenance only walks the fact rows of devices touched by the
// transaction; Commit reports those row numbers in CommitResult.VisitedRows.
package incview

import (
	"errors"
	"sort"
	"sync"
)

// Errors returned by transaction operations.
var (
	ErrInvalidRow   = errors.New("incview: row number must be positive")
	ErrEmptyDevice  = errors.New("incview: device id must not be empty")
	ErrEmptyRoom    = errors.New("incview: room must not be empty")
	ErrNoSuchFact   = errors.New("incview: no such fact row")
	ErrNoSuchDevice = errors.New("incview: no such device mapping")
	ErrTxnClosed    = errors.New("incview: transaction is already finished")
)

// Fact is one row of the fact table.
type Fact struct {
	Row      int
	Device   string
	Duration int
}

// Group is one aggregate bucket of the view.
type Group struct {
	Room  string
	Count int
	Total int64
	Min   int
}

// Delta is the net change of one room between the snapshot taken before a
// commit and the snapshot taken after it. The existence flags distinguish an
// absent group from a group whose aggregate happens to be the zero Group.
type Delta struct {
	Room         string
	Before       Group
	BeforeExists bool
	After        Group
	AfterExists  bool
}

// CommitResult is what a reader receives for one transaction.
type CommitResult struct {
	// Deltas holds only rooms with a net aggregate change. It is sorted by
	// room name.
	Deltas []Delta
	// VisitedRows lists, in ascending order and without duplicates, every
	// fact row number that maintenance read for this transaction.
	VisitedRows []int
}

// View owns the base tables and the materialized aggregates. It is safe for
// concurrent readers. Transactions are serialized: only one transaction may
// be open at a time.
type View struct {
	mu     sync.RWMutex
	writer sync.Mutex

	facts         map[int]Fact
	factsByDevice map[string]map[int]struct{}
	devices       map[string]string
	rooms         map[string]*roomState
}

// NewView returns an empty view.
func NewView() *View {
	return &View{
		facts:         make(map[int]Fact),
		factsByDevice: make(map[string]map[int]struct{}),
		devices:       make(map[string]string),
		rooms:         make(map[string]*roomState),
	}
}

// Begin opens a write transaction. The caller must finish it with Commit or
// Rollback; while it is open no other transaction can start.
func (v *View) Begin() *Txn {
	v.writer.Lock()
	return &Txn{
		v:         v,
		factOps:   make(map[int]*Fact),
		deviceOps: make(map[string]*string),
	}
}

// Group returns the aggregate of one room. The second result is false when
// the room is not present in the view.
func (v *View) Group(room string) (Group, bool) {
	v.mu.RLock()
	defer v.mu.RUnlock()
	rs, ok := v.rooms[room]
	if !ok || rs.count == 0 {
		return Group{}, false
	}
	return rs.group(room), true
}

// Groups returns a snapshot of every non-empty room, sorted by room name.
func (v *View) Groups() []Group {
	v.mu.RLock()
	defer v.mu.RUnlock()
	out := make([]Group, 0, len(v.rooms))
	for room, rs := range v.rooms {
		if rs.count == 0 {
			continue
		}
		out = append(out, rs.group(room))
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Room < out[j].Room })
	return out
}

// Fact returns one fact row from the fact table, mapped or not.
func (v *View) Fact(row int) (Fact, bool) {
	v.mu.RLock()
	defer v.mu.RUnlock()
	f, ok := v.facts[row]
	return f, ok
}

// Facts returns every fact row in ascending row order, mapped or not.
func (v *View) Facts() []Fact {
	v.mu.RLock()
	defer v.mu.RUnlock()
	out := make([]Fact, 0, len(v.facts))
	for _, f := range v.facts {
		out = append(out, f)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Row < out[j].Row })
	return out
}

// Device returns the room currently assigned to a device.
func (v *View) Device(id string) (string, bool) {
	v.mu.RLock()
	defer v.mu.RUnlock()
	room, ok := v.devices[id]
	return room, ok
}

// FactRowsForDevice returns the committed fact rows of one device in
// ascending order. It is a diagnostic helper; the maintenance path uses the
// same index internally.
func (v *View) FactRowsForDevice(id string) []int {
	v.mu.RLock()
	defer v.mu.RUnlock()
	set := v.factsByDevice[id]
	out := make([]int, 0, len(set))
	for row := range set {
		out = append(out, row)
	}
	sort.Ints(out)
	return out
}

// Txn is a write transaction. Operations are buffered and become visible in
// one atomic commit. Operation order inside the transaction does not matter:
// Commit evaluates the final projected state.
type Txn struct {
	v         *View
	factOps   map[int]*Fact
	deviceOps map[string]*string
	done      bool
}

// PutFact inserts or replaces a fact row, including moving it to another
// device. The row number is the identity of the fact.
func (t *Txn) PutFact(row int, device string, duration int) error {
	if t.done {
		return ErrTxnClosed
	}
	if row <= 0 {
		return ErrInvalidRow
	}
	if device == "" {
		return ErrEmptyDevice
	}
	f := Fact{Row: row, Device: device, Duration: duration}
	t.factOps[row] = &f
	return nil
}

// DeleteFact removes a fact row. Inside the same transaction the check is
// made against the projected state, so a row inserted earlier in the
// transaction can be deleted again.
func (t *Txn) DeleteFact(row int) error {
	if t.done {
		return ErrTxnClosed
	}
	if row <= 0 {
		return ErrInvalidRow
	}
	if _, ok := t.projectedFact(row); !ok {
		return ErrNoSuchFact
	}
	if _, committed := t.v.facts[row]; committed {
		t.factOps[row] = nil
	} else {
		// The row only existed as a buffered insert; cancelling the insert
		// leaves the committed state untouched.
		delete(t.factOps, row)
	}
	return nil
}

// SetDevice assigns a device to a room, creating or moving the mapping.
func (t *Txn) SetDevice(id, room string) error {
	if t.done {
		return ErrTxnClosed
	}
	if id == "" {
		return ErrEmptyDevice
	}
	if room == "" {
		return ErrEmptyRoom
	}
	r := room
	t.deviceOps[id] = &r
	return nil
}

// DeleteDevice removes a device mapping. Its facts remain in the fact table
// but leave the view because the view is an inner join.
func (t *Txn) DeleteDevice(id string) error {
	if t.done {
		return ErrTxnClosed
	}
	if id == "" {
		return ErrEmptyDevice
	}
	if _, ok := t.projectedDevice(id); !ok {
		return ErrNoSuchDevice
	}
	if _, committed := t.v.devices[id]; committed {
		t.deviceOps[id] = nil
	} else {
		delete(t.deviceOps, id)
	}
	return nil
}

// Rollback discards the transaction. It is also needed when a transaction is
// opened and never committed.
func (t *Txn) Rollback() {
	if t.done {
		return
	}
	t.done = true
	t.v.writer.Unlock()
}

func (t *Txn) projectedFact(row int) (Fact, bool) {
	if f, ok := t.factOps[row]; ok {
		if f == nil {
			return Fact{}, false
		}
		return *f, true
	}
	f, ok := t.v.facts[row]
	return f, ok
}

func (t *Txn) projectedDevice(id string) (string, bool) {
	if r, ok := t.deviceOps[id]; ok {
		if r == nil {
			return "", false
		}
		return *r, true
	}
	r, ok := t.v.devices[id]
	return r, ok
}

type devicePlan struct {
	device     string
	oldRoom    string
	hasOldRoom bool
	newRoom    string
	hasNewRoom bool
	oldRows    []int
	newRows    []int
}

// Commit applies the transaction atomically and returns the net group deltas
// plus the fact rows maintenance visited. A second Commit is a no-op.
func (t *Txn) Commit() CommitResult {
	if t.done {
		return CommitResult{}
	}
	t.done = true
	defer t.v.writer.Unlock()

	v := t.v
	v.mu.Lock()
	defer v.mu.Unlock()

	plans := t.buildPlans(v)

	// Snapshot affected rooms before changing the aggregate state.
	affectedRooms := make(map[string]struct{})
	for _, p := range plans {
		if p.hasOldRoom {
			affectedRooms[p.oldRoom] = struct{}{}
		}
		if p.hasNewRoom {
			affectedRooms[p.newRoom] = struct{}{}
		}
	}
	before := make(map[string]Group, len(affectedRooms))
	existsBefore := make(map[string]bool, len(affectedRooms))
	for room := range affectedRooms {
		if rs, ok := v.rooms[room]; ok && rs.count > 0 {
			before[room] = rs.group(room)
			existsBefore[room] = true
		}
	}

	// Retract old contributions, then add new ones. The split makes moves
	// and deletes in the same transaction settle both sides exactly once.
	for _, p := range plans {
		if !p.hasOldRoom {
			continue
		}
		rs := v.getOrCreateRoom(p.oldRoom)
		for _, row := range p.oldRows {
			rs.remove(v.facts[row].Duration)
		}
	}
	for _, p := range plans {
		if !p.hasNewRoom {
			continue
		}
		rs := v.getOrCreateRoom(p.newRoom)
		for _, row := range p.newRows {
			f, ok := t.projectedFact(row)
			if !ok {
				continue
			}
			rs.add(f.Duration)
		}
	}

	// Compare with the pre-commit snapshot and emit only net changes.
	var deltas []Delta
	for room := range affectedRooms {
		b, hadBefore := before[room], existsBefore[room]
		rs, ok := v.rooms[room]
		hasAfter := ok && rs.count > 0
		var a Group
		if hasAfter {
			a = rs.group(room)
		}
		if hadBefore == hasAfter && (!hadBefore || b == a) {
			continue
		}
		deltas = append(deltas, Delta{
			Room:         room,
			Before:       b,
			BeforeExists: hadBefore,
			After:        a,
			AfterExists:  hasAfter,
		})
	}
	sort.Slice(deltas, func(i, j int) bool { return deltas[i].Room < deltas[j].Room })

	// Empty groups exit the view.
	for room := range affectedRooms {
		if rs, ok := v.rooms[room]; ok && rs.count == 0 {
			delete(v.rooms, room)
		}
	}

	visited := visitedRows(plans)

	// Publish the base-table changes.
	for row, f := range t.factOps {
		if old, ok := v.facts[row]; ok {
			v.removeFromDeviceIndex(old.Device, row)
		}
		if f == nil {
			delete(v.facts, row)
			continue
		}
		v.facts[row] = *f
		v.addToDeviceIndex(f.Device, row)
	}
	for id, room := range t.deviceOps {
		if room == nil {
			delete(v.devices, id)
			continue
		}
		v.devices[id] = *room
	}

	return CommitResult{Deltas: deltas, VisitedRows: visited}
}

// buildPlans decides which devices a transaction can possibly affect and
// collects the fact rows whose view contribution changes. Unrelated devices
// are never enumerated.
//
// For a device whose room mapping changes, every fact row of that device
// changes sides, so all of its committed and projected rows are visited. For
// a device that keeps its room, only the rows touched by the transaction need
// maintenance; the rest of the device's rows are provably unchanged.
func (t *Txn) buildPlans(v *View) []*devicePlan {
	affected := make(map[string]struct{})

	for row, newF := range t.factOps {
		if oldF, ok := v.facts[row]; ok {
			affected[oldF.Device] = struct{}{}
		}
		if newF != nil {
			affected[newF.Device] = struct{}{}
		}
	}
	for id := range t.deviceOps {
		oldRoom, hadOld := v.devices[id]
		newRoom, hasNew := t.projectedDevice(id)
		if hadOld != hasNew || oldRoom != newRoom {
			affected[id] = struct{}{}
		}
	}

	devices := make([]string, 0, len(affected))
	for id := range affected {
		devices = append(devices, id)
	}
	sort.Strings(devices)

	plans := make([]*devicePlan, 0, len(devices))
	for _, id := range devices {
		p := &devicePlan{device: id}
		p.oldRoom, p.hasOldRoom = v.devices[id]
		p.newRoom, p.hasNewRoom = t.projectedDevice(id)
		mappingChanged := p.hasOldRoom != p.hasNewRoom || p.oldRoom != p.newRoom

		oldSeen := make(map[int]struct{})
		newSeen := make(map[int]struct{})
		addOld := func(row int) {
			if _, ok := oldSeen[row]; ok {
				return
			}
			oldSeen[row] = struct{}{}
			p.oldRows = append(p.oldRows, row)
		}
		addNew := func(row int) {
			if _, ok := newSeen[row]; ok {
				return
			}
			newSeen[row] = struct{}{}
			p.newRows = append(p.newRows, row)
		}

		if mappingChanged {
			// Every committed row of the device leaves its old room and every
			// surviving projected row arrives in the new one.
			for row := range v.factsByDevice[id] {
				addOld(row)
				if f, ok := t.projectedFact(row); ok && f.Device == id {
					addNew(row)
				}
			}
		} else {
			// Only rows the transaction rewrote can differ.
			for row := range t.factOps {
				if f, ok := v.facts[row]; ok && f.Device == id {
					addOld(row)
				}
				if f, ok := t.projectedFact(row); ok && f.Device == id {
					addNew(row)
				}
			}
		}

		// Rows that changed device sides inside the transaction are not in the
		// committed index of their new device, so fold them in explicitly.
		for row, f := range t.factOps {
			if f != nil && f.Device == id {
				addNew(row)
			}
		}

		sort.Ints(p.oldRows)
		sort.Ints(p.newRows)
		plans = append(plans, p)
	}
	return plans
}

func (v *View) addToDeviceIndex(device string, row int) {
	set := v.factsByDevice[device]
	if set == nil {
		set = make(map[int]struct{})
		v.factsByDevice[device] = set
	}
	set[row] = struct{}{}
}

func (v *View) removeFromDeviceIndex(device string, row int) {
	set := v.factsByDevice[device]
	if set == nil {
		return
	}
	delete(set, row)
	if len(set) == 0 {
		delete(v.factsByDevice, device)
	}
}

func (v *View) getOrCreateRoom(room string) *roomState {
	if rs, ok := v.rooms[room]; ok {
		return rs
	}
	rs := newRoomState()
	v.rooms[room] = rs
	return rs
}

func visitedRows(plans []*devicePlan) []int {
	set := make(map[int]struct{})
	for _, p := range plans {
		for _, row := range p.oldRows {
			set[row] = struct{}{}
		}
		for _, row := range p.newRows {
			set[row] = struct{}{}
		}
	}
	if len(set) == 0 {
		return nil
	}
	out := make([]int, 0, len(set))
	for row := range set {
		out = append(out, row)
	}
	sort.Ints(out)
	return out
}

// roomState keeps the aggregate plus a duration multiset. The multiset makes
// minimum maintenance independent of how many fact rows are still in the
// room: removing one occurrence of the minimum keeps that minimum while
// another occurrence remains. The minimum is only recomputed, in O(distinct
// durations), when the last occurrence of the current minimum leaves.
type roomState struct {
	count     int
	total     int64
	durations map[int]int
	minVal    int
	hasMin    bool
}

func newRoomState() *roomState {
	return &roomState{durations: make(map[int]int)}
}

func (rs *roomState) add(d int) {
	rs.count++
	rs.total += int64(d)
	if !rs.hasMin || d < rs.minVal {
		rs.minVal, rs.hasMin = d, true
	}
	rs.durations[d]++
}

func (rs *roomState) remove(d int) {
	c := rs.durations[d]
	if c == 0 {
		return
	}
	if c == 1 {
		delete(rs.durations, d)
	} else {
		rs.durations[d] = c - 1
	}
	rs.count--
	rs.total -= int64(d)
	if c == 1 && d == rs.minVal {
		rs.hasMin = false
		for v := range rs.durations {
			if !rs.hasMin || v < rs.minVal {
				rs.minVal, rs.hasMin = v, true
			}
		}
		if !rs.hasMin {
			rs.minVal = 0
		}
	}
}

func (rs *roomState) group(room string) Group {
	return Group{Room: room, Count: rs.count, Total: rs.total, Min: rs.minVal}
}
