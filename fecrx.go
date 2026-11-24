// Package fecrx implements a sliding-window multicast receiver that
// recovers original packets from original packets and byte-wise XOR
// repair packets.
//
// Every repair packet is one affine equation over GF(2):
//
//	XOR of the packets in Cover = Payload
//
// (each byte position is XORed independently, but coefficient
// elimination is shared because coefficients are 0/1 per packet).
//
// The receiver keeps a reduced-row-echelon basis of the equations
// that involve only current-window variables. A basis row with a
// single coefficient is exactly a uniquely recovered packet; such
// packets are delivered contiguously and at most once. Solved packets
// behind a gap stay in the basis and keep participating in
// elimination.
//
// Retractions (revisions) are supported: every basis row carries the
// set of source packets whose XOR produces it, plus two auxiliary
// structures:
//
//   - z: formal zero dependencies between named sources (duplicates),
//     in RREF over source ids;
//   - w: facts whose coefficients on the current window are zero,
//     i.e. facts purely about discarded variables/constants, carrying
//     their exact discarded-variable support. These are created when
//     singleton rows slide out of the window.
//
// Confirmed (delivered) byte prefix is checked after every
// retraction; a revision that makes a confirmed byte unprovable or
// changes its value yields an ErrConflict with a minimal proof/source
// set and freezes the generation.
package fecrx

import (
	"bytes"
	"errors"
	"fmt"
	"sort"
)

// Public errors.
var (
	// ErrFrozen is returned by every mutating operation after a
	// conflicting revision has frozen the generation.
	ErrFrozen = errors.New("fecrx: generation frozen due to conflicting revision")
	// ErrUnknownSource is returned when retracting a source id that
	// is not currently live.
	ErrUnknownSource = errors.New("fecrx: unknown or already retracted source")
	// ErrStaleRevision is returned when an insert carries an older
	// revision than the one live for the same source.
	ErrStaleRevision = errors.New("fecrx: stale revision")
	// ErrRevisionConflict is returned when the same (source, revision)
	// pair is reused with a different equation that would increase
	// rank. A new revision number is required to replace an equation.
	ErrRevisionConflict = errors.New("fecrx: same source revision carries a different equation")
	// ErrOutOfWindow is returned when an equation covers a packet id
	// at or beyond Base+WindowSize.
	ErrOutOfWindow = errors.New("fecrx: cover references id beyond the window")
	// ErrVarDiscarded is returned when an equation covers a packet id
	// that has already slid out of the window; discarded bodies are
	// never re-received.
	ErrVarDiscarded = errors.New("fecrx: cover references discarded packet")
)

// ErrContradiction reports an insertion whose equation, XORed with
// already accepted equations, yields zero coefficients and a non-zero
// right-hand side. Sources is the set of source packet ids producing
// that contradiction (the new source is included). The insertion has
// failed atomically: receiver state is unchanged.
type ErrContradiction struct {
	Sources []uint64
}

func (e *ErrContradiction) Error() string {
	return fmt.Sprintf("fecrx: contradictory input; XOR of sources %v has zero coefficients and non-zero payload", e.Sources)
}

// ErrConflict reports that a retraction/revision contradicts bytes
// already confirmed to the caller. Sources is a minimal set of
// sources (including the retracted source) that proves the confirmed
// value which can no longer be maintained. The generation is frozen
// and the conflicting revision is not applied.
type ErrConflict struct {
	Sources []uint64
	// Packet is the confirmed packet id whose bytes conflict.
	Packet uint64
}

func (e *ErrConflict) Error() string {
	return fmt.Sprintf("fecrx: revision conflicts with confirmed packet %d; minimal conflicting sources %v; generation frozen", e.Packet, e.Sources)
}

// Hooks receives at-most-once deliveries and gap notifications.
// Hooks must not mutate the Receiver from inside a callback.
type Hooks struct {
	// OnDeliver is called once per contiguously recoverable packet,
	// in strictly increasing id order.
	OnDeliver func(id uint64, data []byte)
	// OnGap is called once per explicit abandon / forced slide-over
	// jump, with the half-open abandoned interval [from, to).
	OnGap func(from, to uint64)
}

// row is one RREF basis equation over current-window variables.
type row struct {
	coeffs set    // variable ids with coefficient 1
	rhs    []byte // right-hand side (byte vector)
	label  set    // source ids whose XOR equals this row
}

func (r *row) pivot() uint64 { return r.coeffs.min() }

func (r *row) xorRow(o *row) {
	r.coeffs.xor(o.coeffs)
	r.rhs = xorBytes(r.rhs, o.rhs)
	r.label.xor(o.label)
}

func (r *row) clone() *row {
	return &row{
		coeffs: r.coeffs.clone(),
		rhs:    append([]byte(nil), r.rhs...),
		label:  r.label.clone(),
	}
}

// coord identifies a pivot coordinate of a wfact: either a discarded
// variable (ordered before every constant bit) or one bit of the
// constant vector.
type coord struct {
	isConst bool
	idx     uint64
}

func coordLess(a, b coord) bool {
	if a.isConst != b.isConst {
		return !a.isConst // variable coordinates sort first
	}
	return a.idx < b.idx
}

// wfact is an affine fact whose coefficients on the current window
// are zero. support records its exact coefficients on discarded
// variables; konst is its right-hand side.
type wfact struct {
	support set
	konst   []byte
	sources set
}

func (w *wfact) pivot() coord {
	if len(w.support) > 0 {
		return coord{isConst: false, idx: w.support.min()}
	}
	return coord{isConst: true, idx: firstSetBit(w.konst)}
}

func (w *wfact) hasCoord(c coord) bool {
	if c.isConst {
		return bitSet(w.konst, c.idx)
	}
	return w.support.has(c.idx)
}

func (w *wfact) xorVec(o *wfact) {
	w.support.xor(o.support)
	w.konst = xorBytes(w.konst, o.konst)
}

func (w *wfact) isZeroVector() bool {
	return len(w.support) == 0 && isZeroBytes(w.konst)
}

func (w *wfact) clone() *wfact {
	return &wfact{
		support: w.support.clone(),
		konst:   append([]byte(nil), w.konst...),
		sources: w.sources.clone(),
	}
}

// Receiver is a sliding-window XOR repair receiver. The zero value is
// not usable; construct with New.
type Receiver struct {
	base       uint64
	size       uint64
	deliverPtr uint64

	rows []*row   // RREF basis, sorted by pivot
	w    []*wfact // RREF-ish basis of discarded-variable/constant facts
	z    []set    // RREF over source ids: formal zero dependencies

	// confirmed maps delivered packet id -> delivered (aligned) bytes.
	confirmed map[uint64][]byte
	// meta maps packet id -> original payload length in bytes.
	meta map[uint64]int
	// sources maps live source id -> last accepted revision.
	sources map[uint64]uint64

	frozen bool
	hooks  Hooks
}

// New creates a Receiver for the half-open window [base, base+size).
func New(base, size uint64, hooks Hooks) *Receiver {
	return &Receiver{
		base:       base,
		size:       size,
		deliverPtr: base,
		confirmed:  map[uint64][]byte{},
		meta:       map[uint64]int{},
		sources:    map[uint64]uint64{},
		hooks:      hooks,
	}
}

// Base returns the current window base (smallest live id).
func (r *Receiver) Base() uint64 { return r.base }

// WindowSize returns the configured window size.
func (r *Receiver) WindowSize() uint64 { return r.size }

// DeliveredUpTo returns the exclusive upper bound of the contiguous
// delivered/abandoned prefix.
func (r *Receiver) DeliveredUpTo() uint64 { return r.deliverPtr }

// Frozen reports whether a conflicting revision froze this generation.
func (r *Receiver) Frozen() bool { return r.frozen }

// SetMeta records the original payload length of packet id. Recovered
// payloads are zero-padded and truncated to exactly this length.
func (r *Receiver) SetMeta(id uint64, originalLength int) {
	r.meta[id] = originalLength
}

func (r *Receiver) metaLen(id uint64, fallback int) int {
	if n, ok := r.meta[id]; ok && n > 0 {
		return n
	}
	return fallback
}

func (r *Receiver) rowPivotedAt(id uint64) *row {
	for _, rw := range r.rows {
		if rw.pivot() == id {
			return rw
		}
	}
	return nil
}

// Solved reports whether id is currently uniquely determined, and
// returns its payload aligned to metadata length. Solved packets may
// still be behind an undelivered gap and may become unsolved again
// after a retraction.
func (r *Receiver) Solved(id uint64) ([]byte, bool) {
	if id < r.base || id >= r.base+r.size {
		return nil, false
	}
	rw := r.rowPivotedAt(id)
	if rw == nil || len(rw.coeffs) != 1 {
		return nil, false
	}
	return align(rw.rhs, r.metaLen(id, len(rw.rhs))), true
}

// insertRaw reduces and commits one equation. On contradiction it
// returns ErrContradiction without mutating anything (the new row is
// built from fresh sets and reduction only touches that row until the
// commit point).
func (r *Receiver) insertRaw(src uint64, cover []uint64, payload []byte) error {
	for _, v := range cover {
		if v < r.base {
			return ErrVarDiscarded
		}
		if v >= r.base+r.size {
			return ErrOutOfWindow
		}
	}

	nr := &row{
		coeffs: newSet(cover...),
		rhs:    append([]byte(nil), payload...),
		label:  newSet(src),
	}

	// Forward reduction against the RREF basis, ascending pivots.
	for _, o := range r.rows {
		p := o.pivot()
		if nr.coeffs.has(p) {
			nr.xorRow(o)
		}
	}

	if len(nr.coeffs) == 0 {
		if isZeroBytes(nr.rhs) {
			// Duplicate / dependent equation: formal zero dependency.
			r.zAdd(nr.label)
			return nil
		}
		// Contradiction: atomic failure, receiver untouched.
		return &ErrContradiction{Sources: nr.label.sorted()}
	}

	p := nr.pivot()
	// Back-substitute the new pivot into all existing rows.
	for _, o := range r.rows {
		if o.coeffs.has(p) {
			o.xorRow(nr)
		}
	}
	r.rows = append(r.rows, nr)
	sortRows(r.rows)
	return nil
}

// AddEquation accepts one equation from source src at revision rev.
//
//   - A new source is inserted.
//   - A newer revision of a live source atomically replaces the old
//     equation (retract then insert); a conflict with confirmed bytes
//     freezes the generation.
//   - The same revision must carry the same equation; if it would
//     increase rank, ErrRevisionConflict is returned.
//   - Older revisions yield ErrStaleRevision.
func (r *Receiver) AddEquation(src, rev uint64, cover []uint64, payload []byte) error {
	if r.frozen {
		return ErrFrozen
	}

	if oldRev, live := r.sources[src]; live {
		switch {
		case rev < oldRev:
			return ErrStaleRevision
		case rev == oldRev:
			trial := r.clone()
			if err := trial.insertRaw(src, cover, payload); err != nil {
				return err
			}
			if len(trial.rows) != len(r.rows) {
				return ErrRevisionConflict
			}
			// Commit any new formal dependency discovered by the
			// retransmission.
			*r = *trial
			return nil
		default:
			// Newer revision: atomic replace on a trial copy.
			trial := r.clone()
			trial.applyRetraction(src)
			if bad, conflict := trial.firstConflict(); conflict {
				proof := r.proofOf(bad)
				r.frozen = true
				return &ErrConflict{Packet: bad, Sources: unionSorted(proof, src)}
			}
			if err := trial.insertRaw(src, cover, payload); err != nil {
				// Contradiction/out-of-window: whole replacement fails
				// atomically, old revision stays live.
				return err
			}
			trial.sources[src] = rev
			*r = *trial
			r.deliver()
			return nil
		}
	}

	if err := r.insertRaw(src, cover, payload); err != nil {
		return err
	}
	r.sources[src] = rev
	r.deliver()
	return nil
}

// Retract removes the equation currently live for src. If the
// retraction invalidates bytes already delivered, an ErrConflict is
// returned and the generation is frozen without applying the
// retraction.
func (r *Receiver) Retract(src uint64) error {
	if r.frozen {
		return ErrFrozen
	}
	if _, live := r.sources[src]; !live {
		return ErrUnknownSource
	}

	trial := r.clone()
	trial.applyRetraction(src)
	if bad, conflict := trial.firstConflict(); conflict {
		proof := r.proofOf(bad)
		r.frozen = true
		return &ErrConflict{Packet: bad, Sources: unionSorted(proof, src)}
	}

	*r = *trial
	r.deliver()
	return nil
}

// applyRetraction removes src from the algebraic state. It must not be
// called on the authoritative receiver before the conflict check;
// callers run it on a clone.
func (r *Receiver) applyRetraction(src uint64) {
	delete(r.sources, src)

	if zi := r.zIndexContaining(src); zi >= 0 {
		// src is formally redundant: a zero dependency zeta containing
		// src lets every label be rewritten without src, so no
		// knowledge dimension is lost.
		zeta := r.z[zi].clone()
		for _, rw := range r.rows {
			if rw.label.has(src) {
				rw.label.xor(zeta)
			}
		}
		for _, wf := range r.w {
			if wf.sources.has(src) {
				wf.sources.xor(zeta)
			}
		}
		rest := make([]set, 0, len(r.z)-1)
		for i, zr := range r.z {
			if i == zi {
				continue
			}
			if zr.has(src) {
				zr.xor(zeta)
			}
			rest = append(rest, zr)
		}
		r.z = nil
		for _, zr := range rest {
			r.zAdd(zr)
		}
		return
	}

	// src is essential to the formal system. Eliminate it
	// independently inside the window basis K and the discarded
	// facts W; dimensions lost on each side are independent (cross
	// combinations become forgotten projection-only information,
	// which is exactly what sliding columns has already discarded).
	if pi := r.rowIndexWithLabel(src); pi >= 0 {
		pivot := r.rows[pi]
		for i, rw := range r.rows {
			if i != pi && rw.label.has(src) {
				rw.xorRow(pivot)
			}
		}
		r.rows = append(r.rows[:pi], r.rows[pi+1:]...)
		r.normalizeRows()
	}

	if wi := r.wIndexWithSource(src); wi >= 0 {
		wp := r.w[wi]
		for i, wf := range r.w {
			if i != wi && wf.sources.has(src) {
				wf.xorVec(wp)
				wf.sources.xor(wp.sources)
			}
		}
		r.w = append(r.w[:wi], r.w[wi+1:]...)
		r.renormalizeW()
	}
}

func (r *Receiver) zIndexContaining(src uint64) int {
	for i, zr := range r.z {
		if zr.has(src) {
			return i
		}
	}
	return -1
}

func (r *Receiver) rowIndexWithLabel(src uint64) int {
	for i, rw := range r.rows {
		if rw.label.has(src) {
			return i
		}
	}
	return -1
}

func (r *Receiver) wIndexWithSource(src uint64) int {
	for i, wf := range r.w {
		if wf.sources.has(src) {
			return i
		}
	}
	return -1
}

// zAdd inserts a formal zero dependency into the RREF over sources.
func (r *Receiver) zAdd(s set) {
	if len(s) == 0 {
		return
	}
	for _, zr := range r.z {
		p := zr.min()
		if s.has(p) {
			s.xor(zr)
		}
	}
	if len(s) == 0 {
		return
	}
	p := s.min()
	for _, zr := range r.z {
		if zr.has(p) {
			zr.xor(s)
		}
	}
	r.z = append(r.z, s)
	sortZ(r.z)
}

func sortZ(z []set) {
	sort.Slice(z, func(i, j int) bool { return z[i].min() < z[j].min() })
}

// wAdd inserts a discarded-variable/constant fact. If it is linearly
// dependent on existing facts, the dependency is recorded in z.
func (r *Receiver) wAdd(wf *wfact) {
	srcs := wf.sources.clone()
	for _, o := range r.w {
		p := o.pivot()
		if wf.hasCoord(p) {
			wf.xorVec(o)
			srcs.xor(o.sources)
		}
	}
	if wf.isZeroVector() {
		// Dependent fact: its source expression is a formal zero.
		r.zAdd(srcs)
		return
	}
	p := wf.pivot()
	for _, o := range r.w {
		if o.hasCoord(p) {
			o.xorVec(wf)
			o.sources.xor(srcs)
		}
	}
	wf.sources = srcs
	r.w = append(r.w, wf)
	sortW(r.w)
}

func (r *Receiver) renormalizeW() {
	old := r.w
	r.w = nil
	for _, wf := range old {
		r.wAdd(wf)
	}
}

func sortW(w []*wfact) {
	sort.Slice(w, func(i, j int) bool { return coordLess(w[i].pivot(), w[j].pivot()) })
}

// normalizeRows rebuilds the RREF basis after retraction
// elimination.
func (r *Receiver) normalizeRows() {
	old := r.rows
	r.rows = nil
	for _, rw := range old {
		for _, o := range r.rows {
			p := o.pivot()
			if rw.coeffs.has(p) {
				rw.xorRow(o)
			}
		}
		if len(rw.coeffs) == 0 {
			if !isZeroBytes(rw.rhs) {
				// Unreachable in a correctly maintained system: a
				// zero-coefficient non-zero RHS would mean accepted
				// equations were contradictory.
				panic("fecrx: inconsistent basis after normalization")
			}
			continue
		}
		p := rw.pivot()
		for _, o := range r.rows {
			if o.coeffs.has(p) {
				o.xorRow(rw)
			}
		}
		r.rows = append(r.rows, rw)
		sortRows(r.rows)
	}
}

func sortRows(rows []*row) {
	sort.Slice(rows, func(i, j int) bool { return rows[i].pivot() < rows[j].pivot() })
}

// deliver emits the contiguous solved prefix exactly once.
func (r *Receiver) deliver() {
	if r.frozen {
		return
	}
	for r.deliverPtr < r.base+r.size {
		rw := r.rowPivotedAt(r.deliverPtr)
		if rw == nil || len(rw.coeffs) != 1 {
			return
		}
		val := align(rw.rhs, r.metaLen(r.deliverPtr, len(rw.rhs)))
		r.confirmed[r.deliverPtr] = val
		if r.hooks.OnDeliver != nil {
			r.hooks.OnDeliver(r.deliverPtr, val)
		}
		r.deliverPtr++
	}
}

// Abandon explicitly gives up the undelivered packets in
// [DeliveredUpTo(), upTo) and jumps over the gap. The caller receives
// exactly one OnGap notification for the whole interval. Solved
// packets in the abandoned range are never delivered, but their
// equations remain in the basis until the window slides.
func (r *Receiver) Abandon(upTo uint64) {
	if r.frozen {
		return
	}
	if upTo > r.base+r.size {
		upTo = r.base + r.size
	}
	if upTo <= r.deliverPtr {
		return
	}
	if r.hooks.OnGap != nil {
		r.hooks.OnGap(r.deliverPtr, upTo)
	}
	r.deliverPtr = upTo
	r.deliver()
}

// SlideTo moves the window base forward to newBase, eliminating old
// columns. For every slid variable v, one row containing v is used as
// the eliminator: v is XORed out of all other rows and the eliminator
// is dropped. If the eliminator is the singleton row v=rhs, its fact
// is preserved in w (so its proof survives retractions); otherwise
// the row is discarded, while every derived row that no longer
// contains v remains — this is exactly how A⊕B and A⊕C leave B⊕C
// after A is slid away.
//
// Undelivered packets crossed by the slide produce one OnGap
// notification.
func (r *Receiver) SlideTo(newBase uint64) error {
	if r.frozen {
		return ErrFrozen
	}
	if newBase <= r.base {
		return nil
	}

	r.deliver()

	if r.deliverPtr < newBase {
		if r.hooks.OnGap != nil {
			r.hooks.OnGap(r.deliverPtr, newBase)
		}
		r.deliverPtr = newBase
	}

	for v := r.base; v < newBase; v++ {
		ei := -1
		for i, rw := range r.rows {
			if rw.coeffs.has(v) {
				ei = i
				break
			}
		}
		if ei < 0 {
			continue // free variable: no row carries it
		}
		elim := r.rows[ei]
		if len(elim.coeffs) == 1 {
			// Preserve the uniquely determined fact as a
			// discarded-variable fact.
			r.wAdd(&wfact{
				support: newSet(v),
				konst:   append([]byte(nil), elim.rhs...),
				sources: elim.label.clone(),
			})
		}
		for i, rw := range r.rows {
			if i != ei && rw.coeffs.has(v) {
				rw.xorRow(elim)
			}
		}
		r.rows = append(r.rows[:ei], r.rows[ei+1:]...)
	}
	r.normalizeRows()

	r.base = newBase
	r.deliver()
	return nil
}

// wSpan reports whether the target vector (support, konst) lies in the
// span of w facts.
func (r *Receiver) wSpan(support set, konst []byte) bool {
	t := &wfact{support: support.clone(), konst: append([]byte(nil), konst...)}
	for _, o := range r.w {
		p := o.pivot()
		if t.hasCoord(p) {
			t.xorVec(o)
		}
	}
	return t.isZeroVector()
}

// firstConflict returns the smallest confirmed packet id whose
// delivered value is no longer provable in the trial state.
func (r *Receiver) firstConflict() (uint64, bool) {
	for _, id := range sortedKeys(r.confirmed) {
		want := r.confirmed[id]
		if id >= r.base {
			rw := r.rowPivotedAt(id)
			if rw == nil || len(rw.coeffs) != 1 {
				return id, true
			}
			got := align(rw.rhs, r.metaLen(id, len(want)))
			if !bytes.Equal(got, want) {
				return id, true
			}
		} else {
			if !r.wSpan(newSet(id), want) {
				return id, true
			}
		}
	}
	return 0, false
}

// proofOf returns a minimal-ish source set proving the confirmed
// value of id in the current (pre-retraction) state. For in-window
// packets the singleton label reduced by z is used; for slid packets
// the unique combination of the independent w basis is computed.
func (r *Receiver) proofOf(id uint64) []uint64 {
	want := r.confirmed[id]
	if id >= r.base {
		rw := r.rowPivotedAt(id)
		if rw == nil {
			return nil
		}
		proof := rw.label.clone()
		for _, zr := range r.z {
			p := zr.min()
			if proof.has(p) {
				proof.xor(zr)
			}
		}
		return proof.sorted()
	}

	t := &wfact{support: newSet(id), konst: append([]byte(nil), want...)}
	srcs := set{}
	for _, o := range r.w {
		p := o.pivot()
		if t.hasCoord(p) {
			t.xorVec(o)
			srcs.xor(o.sources)
		}
	}
	return srcs.sorted()
}

func (r *Receiver) clone() *Receiver {
	c := &Receiver{
		base:       r.base,
		size:       r.size,
		deliverPtr: r.deliverPtr,
		frozen:     r.frozen,
		hooks:      r.hooks,
		confirmed:  map[uint64][]byte{},
		meta:       map[uint64]int{},
		sources:    map[uint64]uint64{},
	}
	for _, rw := range r.rows {
		c.rows = append(c.rows, rw.clone())
	}
	for _, wf := range r.w {
		c.w = append(c.w, wf.clone())
	}
	for _, zr := range r.z {
		c.z = append(c.z, zr.clone())
	}
	for k, v := range r.confirmed {
		c.confirmed[k] = append([]byte(nil), v...)
	}
	for k, v := range r.meta {
		c.meta[k] = v
	}
	for k, v := range r.sources {
		c.sources[k] = v
	}
	return c
}
