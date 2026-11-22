// Package temporaljoin implements a keyed streaming temporal join operator.
//
// Semantics (all times are int64 logical event times):
//
//   - A left event L (key K, time tL) joins the right-side version of key K
//     whose [effective time] is the largest value <= tL. Ties on effective
//     time are broken by revision number (larger wins), then by arrival
//     sequence (later insertion wins). Retracted versions never match.
//   - A left event emits an immediate (possibly empty) pairing when it
//     arrives; later legal right-side changes retract the previous pairing
//     by left event ID and emit a replacement pairing.
//   - Right-side records may arrive late: a record is late when the version's
//     effective time is already strictly behind the right watermark.
//     Late records (upserts and retracts alike) go to the late side output
//     and never modify sealed or unsealed join results.
//   - Left events whose event time is strictly behind the left watermark are
//     likewise diverted to the late side output.
//   - A left event is sealed only once BOTH watermarks have advanced past its
//     event time: then no legal right-side revision can ever affect it again.
//     Sealed pairings are immutable.
//   - Garbage collection removes right versions that can no longer affect any
//     unsealed left event; see collectUnused for the exact rules.
package temporaljoin

import (
	"encoding/json"
	"sort"
	"sync"
)

// RightOp is the kind of a right-side record.
type RightOp int

const (
	RightUpsert  RightOp = iota // insert or replace a version
	RightRetract                // retract an existing version by version ID
)

// LeftEvent is a left-side stream record.
type LeftEvent struct {
	ID      string
	Key     string
	Time    int64
	Payload string // carried opaquely
}

// RightRecord is a right-side stream record.
//
// For RightRetract, EffectiveTime is optional: when the version ID is known,
// the stored effective time is used for lateness decisions, matching the
// statement that retraction is judged late by the retracted version's
// effective time.
type RightRecord struct {
	VersionID     string
	Key           string
	EffectiveTime int64
	Revision      int64
	Op            RightOp
	Value         string
}

// ResultKind distinguishes pairing additions from pairing retractions.
type ResultKind int

const (
	ResultAdd     ResultKind = iota // a (possibly empty) pairing becomes current
	ResultRetract                   // the previous pairing of a left event is withdrawn
)

// Pairing is one emitted join result for a left event.
//
// Right is nil for an empty pairing (no active version at the event time).
type Pairing struct {
	LeftID string
	Key    string
	LeftT  int64
	Right  *RightRecord
}

// Result is an emitted stream result.
type Result struct {
	Kind ResultKind
	Pair Pairing
}

// Sealed is a finalized pairing. Once sealed it can never be retracted or
// replaced because both watermarks have passed the left event time.
type Sealed struct {
	Pair Pairing
}

// LateRecord is a record that arrived behind the corresponding watermark and
// was therefore excluded from normal processing.
type LateRecord struct {
	Source string // "left" or "right"
	Left   *LeftEvent
	Right  *RightRecord
}

// GCEntry reports a right version removed by garbage collection.
type GCEntry struct {
	Key       string
	VersionID string
	Reason    string
}

type rightEntry struct {
	rec    RightRecord
	active bool
	seq    int64
}

type leftEntry struct {
	ev  LeftEvent
	seq int64
	cur Pairing // current emitted pairing; Right == nil means empty pairing
}

// Joiner is the streaming temporal join operator. It is safe for concurrent
// use: every public method serializes on an internal mutex.
type Joiner struct {
	mu sync.Mutex

	leftWm  int64
	rightWm int64
	nextSeq int64

	// key -> versionID -> entry (inactive/retracted entries are kept until GC)
	right map[string]map[string]*rightEntry
	// left event ID -> entry
	left map[string]*leftEntry
	// insertion order of left IDs, used for deterministic emission
	leftOrder []string

	results []Result
	sealed  []Sealed
	late    []LateRecord
	gc      []GCEntry
}

// NewJoiner creates an empty operator. Both watermarks start at MinInt64 so
// that every finite event time is initially on time.
func NewJoiner() *Joiner {
	return &Joiner{
		leftWm:  minInt64(),
		rightWm: minInt64(),
		right:   make(map[string]map[string]*rightEntry),
		left:    make(map[string]*leftEntry),
	}
}

func minInt64() int64 {
	return -1 << 63 // math.MinInt64
}

func maxInt64() int64 {
	return 1<<63 - 1 // math.MaxInt64
}

// Snapshot is an opaque, serializable full operator state.
type Snapshot struct {
	LeftWm    int64                               `json:"left_wm"`
	RightWm   int64                               `json:"right_wm"`
	NextSeq   int64                               `json:"next_seq"`
	Right     map[string]map[string]rightEntryDTO `json:"right"`
	Left      map[string]leftEntryDTO             `json:"left"`
	LeftOrder []string                            `json:"left_order"`
	Results   []Result                            `json:"results"`
	Sealed    []Sealed                            `json:"sealed"`
	Late      []LateRecord                        `json:"late"`
	GC        []GCEntry                           `json:"gc"`
}

type rightEntryDTO struct {
	Rec    RightRecord `json:"rec"`
	Active bool        `json:"active"`
	Seq    int64       `json:"seq"`
}

type leftEntryDTO struct {
	Ev  LeftEvent `json:"ev"`
	Seq int64     `json:"seq"`
	Cur Pairing   `json:"cur"`
}

// Snapshot returns a deep copy of the operator state as serializable JSON
// bytes (a checkpoint).
func (j *Joiner) Snapshot() ([]byte, error) {
	j.mu.Lock()
	defer j.mu.Unlock()
	snap := j.toSnapshot()
	return json.Marshal(snap)
}

func (j *Joiner) toSnapshot() Snapshot {
	rightCopy := make(map[string]map[string]rightEntryDTO, len(j.right))
	for k, vs := range j.right {
		m := make(map[string]rightEntryDTO, len(vs))
		for vid, e := range vs {
			m[vid] = rightEntryDTO{Rec: e.rec, Active: e.active, Seq: e.seq}
		}
		rightCopy[k] = m
	}
	leftCopy := make(map[string]leftEntryDTO, len(j.left))
	for id, e := range j.left {
		leftCopy[id] = leftEntryDTO{Ev: e.ev, Seq: e.seq, Cur: copyPair(e.cur)}
	}
	return Snapshot{
		LeftWm:    j.leftWm,
		RightWm:   j.rightWm,
		NextSeq:   j.nextSeq,
		Right:     rightCopy,
		Left:      leftCopy,
		LeftOrder: append([]string(nil), j.leftOrder...),
		Results:   append([]Result(nil), j.results...),
		Sealed:    append([]Sealed(nil), j.sealed...),
		Late:      append([]LateRecord(nil), j.late...),
		GC:        append([]GCEntry(nil), j.gc...),
	}
}

func copyPair(p Pairing) Pairing {
	cp := Pairing{LeftID: p.LeftID, Key: p.Key, LeftT: p.LeftT}
	if p.Right != nil {
		r := *p.Right
		cp.Right = &r
	}
	return cp
}

// Restore replaces the operator state with a checkpoint previously produced
// by Snapshot.
func (j *Joiner) Restore(data []byte) error {
	j.mu.Lock()
	defer j.mu.Unlock()

	var s Snapshot
	if err := json.Unmarshal(data, &s); err != nil {
		return err
	}
	return j.loadSnapshot(s)
}

func (j *Joiner) loadSnapshot(s Snapshot) error {
	right := make(map[string]map[string]*rightEntry, len(s.Right))
	for k, vs := range s.Right {
		m := make(map[string]*rightEntry, len(vs))
		for vid, dto := range vs {
			m[vid] = &rightEntry{rec: dto.Rec, active: dto.Active, seq: dto.Seq}
		}
		right[k] = m
	}
	left := make(map[string]*leftEntry, len(s.Left))
	for id, dto := range s.Left {
		left[id] = &leftEntry{ev: dto.Ev, seq: dto.Seq, cur: copyPair(dto.Cur)}
	}
	j.leftWm = s.LeftWm
	j.rightWm = s.RightWm
	j.nextSeq = s.NextSeq
	j.right = right
	j.left = left
	j.leftOrder = append([]string(nil), s.LeftOrder...)
	j.results = append([]Result(nil), s.Results...)
	j.sealed = append([]Sealed(nil), s.Sealed...)
	j.late = append([]LateRecord(nil), s.Late...)
	j.gc = append([]GCEntry(nil), s.GC...)
	return nil
}

// PushLeft ingests one left event. On-time events produce an immediate
// ResultAdd (the pairing may be empty). Events behind the left watermark are
// diverted to the late side output and produce no join result.
func (j *Joiner) PushLeft(ev LeftEvent) {
	j.mu.Lock()
	defer j.mu.Unlock()

	if ev.Time < j.leftWm {
		e := ev
		j.late = append(j.late, LateRecord{Source: "left", Left: &e})
		return
	}

	seq := j.nextSeq
	j.nextSeq++

	le := &leftEntry{
		ev:  ev,
		seq: seq,
		cur: Pairing{LeftID: ev.ID, Key: ev.Key, LeftT: ev.Time, Right: nil},
	}
	if _, exists := j.left[ev.ID]; !exists {
		j.leftOrder = append(j.leftOrder, ev.ID)
	}
	j.left[ev.ID] = le

	if best := j.bestVersion(ev.Key, ev.Time); best != nil {
		r := best.rec
		le.cur.Right = &r
	}
	j.results = append(j.results, Result{Kind: ResultAdd, Pair: copyPair(le.cur)})
}

// PushRight ingests one right-side upsert or retract. Late records (judged by
// the version's effective time against the right watermark) go to the late
// side output and never alter results. On-time records are applied and every
// unsealed left event of the same key that could be affected is re-evaluated,
// emitting a retraction of the old pairing followed by the replacement add.
func (j *Joiner) PushRight(rec RightRecord) {
	j.mu.Lock()
	defer j.mu.Unlock()

	effTime := rec.EffectiveTime
	if existing, ok := j.version(rec.Key, rec.VersionID); ok {
		effTime = existing.rec.EffectiveTime
	}
	if effTime < j.rightWm {
		r := rec
		j.late = append(j.late, LateRecord{Source: "right", Right: &r})
		return
	}

	j.applyRight(rec)
	j.recomputeKey(rec.Key)
}

func (j *Joiner) version(key, vid string) (*rightEntry, bool) {
	if vs, ok := j.right[key]; ok {
		e, ok := vs[vid]
		return e, ok
	}
	return nil, false
}

func (j *Joiner) applyRight(rec RightRecord) {
	vs, ok := j.right[rec.Key]
	if !ok {
		vs = make(map[string]*rightEntry)
		j.right[rec.Key] = vs
	}
	switch rec.Op {
	case RightRetract:
		if e, exists := vs[rec.VersionID]; exists {
			e.active = false
		} else {
			// Retract of a never-seen version: retain an inactive tombstone
			// so a later upsert with the same ID is ordered correctly.
			vs[rec.VersionID] = &rightEntry{rec: rec, active: false, seq: j.nextSeq}
			j.nextSeq++
		}
	default: // upsert (insert or replace by version ID)
		if e, exists := vs[rec.VersionID]; exists {
			e.rec = rec
			e.active = true
			e.seq = j.nextSeq
		} else {
			vs[rec.VersionID] = &rightEntry{rec: rec, active: true, seq: j.nextSeq}
		}
		j.nextSeq++
	}
}

// bestVersion returns the active version of key k effective at or before t,
// choosing the greatest effective time, then greatest revision, then greatest
// arrival sequence.
func (j *Joiner) bestVersion(k string, t int64) *rightEntry {
	var best *rightEntry
	for _, e := range j.right[k] {
		if !e.active || e.rec.EffectiveTime > t {
			continue
		}
		if best == nil ||
			e.rec.EffectiveTime > best.rec.EffectiveTime ||
			(e.rec.EffectiveTime == best.rec.EffectiveTime &&
				(e.rec.Revision > best.rec.Revision ||
					(e.rec.Revision == best.rec.Revision && e.seq > best.seq))) {
			best = e
		}
	}
	return best
}

func sameVersion(a *RightRecord, b *RightRecord) bool {
	if a == nil || b == nil {
		return a == b
	}
	return a.VersionID == b.VersionID
}

// recomputeKey re-evaluates all unsealed left events of key k and emits
// retract+add pairs wherever the current pairing changes. Left events are
// processed in arrival order for deterministic output.
func (j *Joiner) recomputeKey(k string) {
	for _, id := range j.leftOrder {
		le, ok := j.left[id]
		if !ok || le.ev.Key != k {
			continue
		}
		var newRight *RightRecord
		if best := j.bestVersion(k, le.ev.Time); best != nil {
			r := best.rec
			newRight = &r
		}
		if sameVersion(le.cur.Right, newRight) {
			continue
		}
		// Retract the previous pairing (including an empty one) by left ID,
		// then emit the replacement pairing.
		j.results = append(j.results, Result{Kind: ResultRetract, Pair: copyPair(le.cur)})
		le.cur.Right = newRight
		j.results = append(j.results, Result{Kind: ResultAdd, Pair: copyPair(le.cur)})
	}
}

// AdvanceLeftWatermark advances the left watermark (monotonic) and seals any
// left events now strictly older than both watermarks, followed by GC.
func (j *Joiner) AdvanceLeftWatermark(wm int64) {
	j.mu.Lock()
	defer j.mu.Unlock()
	if wm > j.leftWm {
		j.leftWm = wm
	}
	j.sealCycle()
}

// AdvanceRightWatermark advances the right watermark (monotonic) and seals any
// left events now strictly older than both watermarks, followed by GC.
func (j *Joiner) AdvanceRightWatermark(wm int64) {
	j.mu.Lock()
	defer j.mu.Unlock()
	if wm > j.rightWm {
		j.rightWm = wm
	}
	j.sealCycle()
}

func (j *Joiner) sealCycle() {
	ready := make([]*leftEntry, 0)
	for _, le := range j.left {
		if le.ev.Time < j.leftWm && le.ev.Time < j.rightWm {
			ready = append(ready, le)
		}
	}
	if len(ready) == 0 {
		return
	}
	sort.Slice(ready, func(a, b int) bool {
		if ready[a].ev.Time != ready[b].ev.Time {
			return ready[a].ev.Time < ready[b].ev.Time
		}
		return ready[a].seq < ready[b].seq
	})

	sealedIDs := make(map[string]bool, len(ready))
	for _, le := range ready {
		j.sealed = append(j.sealed, Sealed{Pair: copyPair(le.cur)})
		delete(j.left, le.ev.ID)
		sealedIDs[le.ev.ID] = true
	}
	filtered := j.leftOrder[:0]
	for _, id := range j.leftOrder {
		if !sealedIDs[id] {
			filtered = append(filtered, id)
		}
	}
	j.leftOrder = filtered

	j.collectUnused()
}

// collectUnused removes right versions that cannot affect any unsealed left
// event. Three sound classes:
//
//  1. Inactive (retracted) versions: they can never match again; an on-time
//     re-upsert of the same version ID re-inserts the entry fresh.
//  2. Dominated active versions: an active version X of key k with effective
//     time tX is collectable when another active version D of the same key is
//     strictly later by (effective time, revision), BOTH watermarks are
//     already past tX, and no unsealed left event can still select X (its
//     time is > tX). No future on-time revision can make X relevant again:
//     any such revision has effective time >= current watermark > tD > tX.
//  3. When no unsealed left event remains for a key (end-of-input /
//     key-bounded stream), every retained version of that key is collectable.
func (j *Joiner) collectUnused() {
	// Class 3 is sound only at end-of-input: in an open stream a key with no
	// pending left events may simply not have received them yet.
	terminalInput := j.leftWm == maxInt64() && j.rightWm == maxInt64()

	keys := make([]string, 0, len(j.right))
	for k := range j.right {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	for _, k := range keys {
		vs := j.right[k]
		pending := j.pendingTimes(k)
		terminal := len(pending) == 0 && terminalInput
		var minPending int64
		hasPending := false
		for _, t := range pending {
			if !hasPending || t < minPending {
				minPending, hasPending = t, true
			}
		}

		// Decide removals first (deterministic version order), then apply them.
		vids := make([]string, 0, len(vs))
		for vid := range vs {
			vids = append(vids, vid)
		}
		sort.Strings(vids)

		for _, vid := range vids {
			e := vs[vid]
			switch {
			case !e.active:
				delete(vs, vid)
				j.gc = append(j.gc, GCEntry{Key: k, VersionID: vid, Reason: "retracted"})
			case terminal:
				delete(vs, vid)
				j.gc = append(j.gc, GCEntry{Key: k, VersionID: vid, Reason: "no-pending-left"})
			case j.isDominated(e, vs) &&
				e.rec.EffectiveTime < j.leftWm && e.rec.EffectiveTime < j.rightWm &&
				hasPending && e.rec.EffectiveTime < minPending:
				delete(vs, vid)
				j.gc = append(j.gc, GCEntry{Key: k, VersionID: vid, Reason: "dominated"})
			}
		}
		if len(vs) == 0 {
			delete(j.right, k)
		}
	}
}

func (j *Joiner) pendingTimes(k string) []int64 {
	var out []int64
	for _, id := range j.leftOrder {
		if le, ok := j.left[id]; ok && le.ev.Key == k {
			out = append(out, le.ev.Time)
		}
	}
	return out
}

// isDominated reports whether e can never win bestVersion again because
// another active version d is strictly later by (effective time, revision).
func (j *Joiner) isDominated(e *rightEntry, vs map[string]*rightEntry) bool {
	for _, d := range vs {
		if d == e || !d.active {
			continue
		}
		if d.rec.EffectiveTime > e.rec.EffectiveTime ||
			(d.rec.EffectiveTime == e.rec.EffectiveTime && d.rec.Revision > e.rec.Revision) {
			return true
		}
	}
	return false
}

// Results returns a copy of all emitted retract/add results so far.
func (j *Joiner) Results() []Result {
	j.mu.Lock()
	defer j.mu.Unlock()
	out := make([]Result, len(j.results))
	for i, r := range j.results {
		r.Pair = copyPair(r.Pair)
		out[i] = r
	}
	return out
}

// Sealed returns a copy of all finalized pairings, in sealing order.
func (j *Joiner) Sealed() []Sealed {
	j.mu.Lock()
	defer j.mu.Unlock()
	out := make([]Sealed, len(j.sealed))
	for i, s := range j.sealed {
		s.Pair = copyPair(s.Pair)
		out[i] = s
	}
	return out
}

// Late returns a copy of the late side output.
func (j *Joiner) Late() []LateRecord {
	j.mu.Lock()
	defer j.mu.Unlock()
	out := make([]LateRecord, len(j.late))
	for i, l := range j.late {
		out[i] = l
		if l.Left != nil {
			e := *l.Left
			out[i].Left = &e
		}
		if l.Right != nil {
			r := *l.Right
			out[i].Right = &r
		}
	}
	return out
}

// GCLog returns a copy of garbage-collection entries in removal order.
func (j *Joiner) GCLog() []GCEntry {
	j.mu.Lock()
	defer j.mu.Unlock()
	return append([]GCEntry(nil), j.gc...)
}

// LeftWatermark reports the current left watermark.
func (j *Joiner) LeftWatermark() int64 {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.leftWm
}

// RightWatermark reports the current right watermark.
func (j *Joiner) RightWatermark() int64 {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.rightWm
}

// PendingLeftIDs reports the IDs of left events not yet sealed, in arrival
// order. It is mainly used by tests to verify state cleanup.
func (j *Joiner) PendingLeftIDs() []string {
	j.mu.Lock()
	defer j.mu.Unlock()
	return append([]string(nil), j.leftOrder...)
}

// RetainedRightVersions reports the right version IDs still retained for a
// key, sorted for deterministic test assertions. Inactive entries are
// suffixed with "(inactive)".
func (j *Joiner) RetainedRightVersions(key string) []string {
	j.mu.Lock()
	defer j.mu.Unlock()
	vs := j.right[key]
	out := make([]string, 0, len(vs))
	for vid, e := range vs {
		if e.active {
			out = append(out, vid)
		} else {
			out = append(out, vid+"(inactive)")
		}
	}
	sort.Strings(out)
	return out
}

// Finish advances both watermarks to the maximum time, sealing everything and
// collecting all retained right state.
func (j *Joiner) Finish() {
	j.mu.Lock()
	defer j.mu.Unlock()
	j.leftWm = maxInt64()
	j.rightWm = maxInt64()
	j.sealCycle()
	// sealCycle skips GC when nothing was ready to seal; run it explicitly so
	// right-only keys are also collected at end-of-input.
	j.collectUnused()
}
