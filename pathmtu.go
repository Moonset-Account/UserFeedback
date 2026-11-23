// Package pathmtu implements sender-side Path MTU discovery for a packet
// connection that migrates between paths (generations).
//
// The sender keeps, for the active generation, two bounds on the usable
// payload:
//
//   - a verified lower bound (payload sizes that have been acknowledged),
//   - an upper bound or ceiling (sizes the path has not rejected).
//
// Ordinary traffic is always sent at the verified lower bound. Probes are
// numbered; success, timeout and "too big" feedback are routed back by probe
// number and are only allowed to change the generation that issued the probe.
//
// Policy implemented by this package:
//
//  1. A successful probe only raises the verified lower bound of its own
//     generation, never above that generation's ceiling.
//  2. A "too big" report lowers the ceiling of its own generation.
//  3. If a new limit is below the verified lower bound, the verified bound is
//     dropped to the limit immediately and the sender re-verifies it.
//  4. A probe timeout with no ordinary-packet acknowledgment inside the same
//     window (since the probe was sent) holds both bounds: loss cannot be
//     attributed to the path.
//  5. Only two consecutive probe timeouts, each with ordinary packets still
//     acknowledged in the window, lower the ceiling (to one byte below the
//     failed probe size, never below the verified lower bound or the known
//     minimum).
//  6. Migrating to a new generation starts from fresh bounds; feedback for an
//     older generation is recorded against that generation and never leaks
//     into the active one.
//  7. Returning to a previously used generation reuses its evidence only if it
//     is unexpired (per EvidenceTTL) and the generation number matches.
//
// Every event returns a Snapshot containing the ordinary send size, the next
// probe suggestion, the current bounds and the reason the bounds changed (or
// were held). The same snapshots are appended to a trace via Trace.
package pathmtu

import (
	"errors"
	"fmt"
	"time"
)

// ErrInvalidConfig is returned by New when the configuration is unusable.
var ErrInvalidConfig = errors.New("pathmtu: invalid config")

// ChangeKind classifies why bounds changed (or did not change) after an event.
type ChangeKind string

const (
	// ChangeNone means the event did not touch the bounds.
	ChangeNone ChangeKind = "none"
	// ChangeVerifiedRaised means a successful probe raised the lower bound.
	ChangeVerifiedRaised ChangeKind = "verified_raised"
	// ChangeCeilingLowered means a size limit or repeated timeouts lowered the ceiling.
	ChangeCeilingLowered ChangeKind = "ceiling_lowered"
	// ChangeRevalidating means the limit fell below the verified bound and the
	// sender dropped to the limit and scheduled a confirmation probe.
	ChangeRevalidating ChangeKind = "revalidating"
	// ChangeRevalidated means a pending re-verification succeeded.
	ChangeRevalidated ChangeKind = "revalidated"
	// ChangeHeld means bounds were deliberately left unchanged.
	ChangeHeld ChangeKind = "held"
	// ChangeMigratedFresh means a path migration started from fresh bounds.
	ChangeMigratedFresh ChangeKind = "migrated_fresh"
	// ChangeMigratedReuse means a path migration reused unexpired, matching evidence.
	ChangeMigratedReuse ChangeKind = "migrated_reuse"
	// ChangeAppliedElsewhere means feedback was valid but targeted a generation
	// that is not active; the active generation was not touched.
	ChangeAppliedElsewhere ChangeKind = "applied_elsewhere"
	// ChangeIgnored means the input was stale, unknown or inconsistent.
	ChangeIgnored ChangeKind = "ignored"
)

// Config configures a Discoverer.
type Config struct {
	// MinPayload is the floor: the payload size known to be sendable on any
	// path even before probing. It must be at least 1.
	MinPayload int
	// MaxPayload is the initial ceiling used for a fresh generation. It must be
	// at least MinPayload.
	MaxPayload int
	// EvidenceTTL bounds how long stored bounds may be reused when returning to
	// a generation. Zero disables reuse entirely. Negative values are invalid.
	EvidenceTTL time.Duration
	// Now supplies the clock. When nil, time.Now is used. Injecting a clock
	// makes expiry and window behaviour deterministic in tests.
	Now func() time.Time
}

// Probe identifies one numbered probe.
type Probe struct {
	ID         uint64
	Generation uint64
	Size       int
}

// Snapshot is the observable sender state after an event.
type Snapshot struct {
	// At is the clock reading associated with the snapshot.
	At time.Time
	// Generation is the active path generation (0 when no path is active).
	Generation uint64
	// Floor is the known minimum sendable payload for the generation.
	Floor int
	// Verified is the verified lower bound.
	Verified int
	// Ceiling is the upper bound on payload size.
	Ceiling int
	// OrdinarySendSize is the payload size used for ordinary traffic. It is the
	// verified lower bound.
	OrdinarySendSize int
	// NextProbe is the payload size the next probe should use. Only meaningful
	// when NextProbeValid is true.
	NextProbe int
	// NextProbeValid reports whether a probe is currently useful.
	NextProbeValid bool
	// Revalidating reports whether a confirmation probe is pending after the
	// verified bound was forced down to a new limit.
	Revalidating bool
	// Kind classifies the boundary change caused by the event.
	Kind ChangeKind
	// Reason is a human-readable explanation of the event and its effect.
	Reason string
}

// String renders the per-instant summary: ordinary send size, next probe and
// the reason for the boundary state.
func (s Snapshot) String() string {
	next := "none"
	if s.NextProbeValid {
		next = fmt.Sprintf("%d", s.NextProbe)
	}
	reval := ""
	if s.Revalidating {
		reval = " revalidating"
	}
	return fmt.Sprintf(
		"gen=%d ordinary=%d nextProbe=%s floor=%d verified=%d ceiling=%d%s kind=%s | %s",
		s.Generation, s.OrdinarySendSize, next, s.Floor, s.Verified, s.Ceiling, reval, s.Kind, s.Reason,
	)
}

// Result is returned by every event. Applied is true when the input matched a
// known, live object (an active or retired probe, or the active generation).
// Snapshot always describes the active generation.
type Result struct {
	Applied  bool
	Snapshot Snapshot
}

// Evidence is a copy of the stored knowledge about one generation.
type Evidence struct {
	Generation uint64
	Floor      int
	Verified   int
	Ceiling    int
	UpdatedAt  time.Time
	Active     bool
}

type genState struct {
	gen                             uint64
	floor                           int
	verified                        int
	ceiling                         int
	revalidate                      bool
	activation                      uint64
	ackSeq                          uint64
	consecutiveTimeoutsWithLiveness int
	evidenceAt                      time.Time
	everActivated                   bool
	active                          bool
}

type probeRecord struct {
	gen          uint64
	activation   uint64
	size         int
	ackSeqAtSend uint64
}

// Discoverer is a sender-side path MTU state machine. It is not safe for
// concurrent use; serialise calls at the transport.
type Discoverer struct {
	cfg         Config
	clock       func() time.Time
	gens        map[uint64]*genState
	probes      map[uint64]*probeRecord
	nextProbeID uint64
	activation  uint64
	current     *genState
	trace       []Snapshot
}

// New validates cfg and returns an inactive Discoverer. Call MigrateTo to
// activate the first path generation.
func New(cfg Config) (*Discoverer, error) {
	if cfg.MinPayload < 1 {
		return nil, fmt.Errorf("%w: MinPayload must be >= 1, got %d", ErrInvalidConfig, cfg.MinPayload)
	}
	if cfg.MaxPayload < cfg.MinPayload {
		return nil, fmt.Errorf("%w: MaxPayload (%d) must be >= MinPayload (%d)", ErrInvalidConfig, cfg.MaxPayload, cfg.MinPayload)
	}
	if cfg.EvidenceTTL < 0 {
		return nil, fmt.Errorf("%w: EvidenceTTL must not be negative", ErrInvalidConfig)
	}
	clock := cfg.Now
	if clock == nil {
		clock = time.Now
	}
	return &Discoverer{
		cfg:    cfg,
		clock:  clock,
		gens:   make(map[uint64]*genState),
		probes: make(map[uint64]*probeRecord),
	}, nil
}

// MigrateTo activates generation gen with the supplied known minimum sendable
// payload. A non-positive minPayload falls back to Config.MinPayload. The floor
// is never allowed below Config.MinPayload.
//
// If this generation was used before, its evidence is reused only when
// EvidenceTTL is positive, the stored evidence has not expired and the stored
// generation number matches. Otherwise the generation starts fresh at the
// floor with Config.MaxPayload as the ceiling.
//
// Re-activating the generation that is already active is a no-op.
func (d *Discoverer) MigrateTo(gen uint64, minPayload int) Result {
	now := d.clock()
	if gen == 0 {
		return Result{Applied: false, Snapshot: d.record(d.buildSnapshot(now, ChangeIgnored, "generation 0 is invalid"))}
	}
	if d.current != nil && d.current.gen == gen {
		return Result{Applied: false, Snapshot: d.buildSnapshot(now, ChangeNone, fmt.Sprintf("generation %d is already active", gen))}
	}

	floor := minPayload
	if floor <= 0 {
		floor = d.cfg.MinPayload
	}
	if floor < d.cfg.MinPayload {
		floor = d.cfg.MinPayload
	}

	if d.current != nil {
		d.current.active = false
	}
	d.activation++

	st, existed := d.gens[gen]
	reuse := existed && st.everActivated && st.gen == gen &&
		d.cfg.EvidenceTTL > 0 && !st.evidenceAt.IsZero() &&
		now.Sub(st.evidenceAt) <= d.cfg.EvidenceTTL

	var kind ChangeKind
	var reason string

	if reuse {
		st.floor = floor
		if st.verified < floor {
			st.verified = floor
		}
		if st.ceiling < floor {
			st.ceiling = floor
		}
		if st.verified > st.ceiling {
			st.verified = st.ceiling
		}
		// A new activation starts a fresh ordinary-ack window and a fresh
		// timeout streak. The stored bounds and their timestamp are preserved.
		st.ackSeq = 0
		st.consecutiveTimeoutsWithLiveness = 0
		st.activation = d.activation
		st.active = true
		d.current = st
		kind = ChangeMigratedReuse
		reason = fmt.Sprintf("reused unexpired evidence for generation %d (age %s <= ttl %s): floor=%d verified=%d ceiling=%d",
			gen, now.Sub(st.evidenceAt), d.cfg.EvidenceTTL, st.floor, st.verified, st.ceiling)
	} else {
		if !existed {
			st = &genState{gen: gen}
			d.gens[gen] = st
		}
		previousEvidenceAt := st.evidenceAt
		st.gen = gen
		st.floor = floor
		st.verified = floor
		st.ceiling = d.cfg.MaxPayload
		if st.ceiling < floor {
			st.ceiling = floor
		}
		st.revalidate = false
		st.ackSeq = 0
		st.consecutiveTimeoutsWithLiveness = 0
		st.activation = d.activation
		st.evidenceAt = now
		st.everActivated = true
		st.active = true
		d.current = st
		kind = ChangeMigratedFresh
		switch {
		case !existed:
			reason = fmt.Sprintf("new generation %d: fresh bounds at floor %d, ceiling %d", gen, st.floor, st.ceiling)
		case d.cfg.EvidenceTTL <= 0:
			reason = fmt.Sprintf("generation %d: evidence reuse disabled (ttl=0), fresh bounds at floor %d, ceiling %d", gen, st.floor, st.ceiling)
		default:
			reason = fmt.Sprintf("generation %d: stored evidence expired (age %s > ttl %s), fresh bounds at floor %d, ceiling %d",
				gen, now.Sub(previousEvidenceAt), d.cfg.EvidenceTTL, st.floor, st.ceiling)
		}
	}

	return Result{Applied: true, Snapshot: d.record(d.buildSnapshot(now, kind, reason))}
}

// OnOrdinaryAck records that an ordinary packet was acknowledged on generation
// gen. It is the liveness signal used by probe-timeout handling. Acknowledgments
// for a non-active generation are ignored.
func (d *Discoverer) OnOrdinaryAck(gen uint64) Result {
	now := d.clock()
	if d.current == nil || gen != d.current.gen {
		return Result{Applied: false, Snapshot: d.record(d.buildSnapshot(now, ChangeIgnored,
			fmt.Sprintf("ordinary acknowledgment for generation %d while generation %d is active: ignored", gen, d.currentGen())))}
	}
	d.current.ackSeq++
	return Result{Applied: true, Snapshot: d.record(d.buildSnapshot(now, ChangeNone,
		fmt.Sprintf("ordinary packet acknowledged on generation %d (liveness window seq=%d)", gen, d.current.ackSeq)))}
}

// SendProbe allocates the next numbered probe for the active generation. It
// returns false when no probe is useful (bounds settled or no active path).
func (d *Discoverer) SendProbe() (Probe, bool) {
	st := d.current
	if st == nil {
		return Probe{}, false
	}
	size, ok := d.nextProbeSize()
	if !ok {
		return Probe{}, false
	}
	d.nextProbeID++
	id := d.nextProbeID
	d.probes[id] = &probeRecord{gen: st.gen, activation: st.activation, size: size, ackSeqAtSend: st.ackSeq}
	return Probe{ID: id, Generation: st.gen, Size: size}, true
}

// OnProbeSuccess reports that probe id succeeded. It raises the verified lower
// bound of the probe's generation to the probe size (capped by the ceiling).
// Success for a retired generation updates that generation's stored evidence
// only; the active generation is untouched.
func (d *Discoverer) OnProbeSuccess(id uint64) Result {
	now := d.clock()
	rec, gst, found := d.resolveProbe(id)
	if !found || gst == nil {
		return Result{Applied: false, Snapshot: d.record(d.buildSnapshot(now, ChangeIgnored,
			fmt.Sprintf("probe %d is unknown or already resolved: ignored", id)))}
	}
	if rec.activation != gst.activation {
		return Result{Applied: false, Snapshot: d.record(d.buildSnapshot(now, ChangeIgnored,
			fmt.Sprintf("probe %d belongs to an earlier activation of generation %d (activation %d, current %d): stale feedback ignored",
				id, gst.gen, rec.activation, gst.activation)))}
	}

	wasRevalidating := gst.revalidate
	target := rec.size
	if target > gst.ceiling {
		target = gst.ceiling
	}
	raised := false
	if target > gst.verified {
		gst.verified = target
		raised = true
	}
	if gst.revalidate && target >= gst.verified {
		gst.revalidate = false
	}
	gst.consecutiveTimeoutsWithLiveness = 0
	gst.evidenceAt = now

	if gst != d.current {
		return Result{Applied: true, Snapshot: d.record(d.buildSnapshot(now, ChangeAppliedElsewhere,
			fmt.Sprintf("probe %d (%d bytes) succeeded on retired generation %d: evidence recorded there, active generation %d unaffected",
				id, rec.size, gst.gen, d.currentGen())))}
	}

	kind := ChangeHeld
	var reason string
	switch {
	case raised:
		kind = ChangeVerifiedRaised
		reason = fmt.Sprintf("probe %d (%d bytes) succeeded: verified lower bound raised to %d", id, rec.size, gst.verified)
	case wasRevalidating:
		kind = ChangeRevalidated
		reason = fmt.Sprintf("probe %d (%d bytes) succeeded: limit %d re-verified, ordinary send size confirmed", id, rec.size, gst.verified)
	default:
		reason = fmt.Sprintf("probe %d (%d bytes) succeeded but verified lower bound already %d: no change", id, rec.size, gst.verified)
	}
	return Result{Applied: true, Snapshot: d.record(d.buildSnapshot(now, kind, reason))}
}

// OnProbeTimeout reports that probe id timed out. If no ordinary packet was
// acknowledged since the probe was sent, both bounds are held. Otherwise the
// timeout is counted; only two consecutive such timeouts lower the ceiling, to
// one byte below the failed probe size and never below the floor. Timeouts for
// a retired generation are ignored.
func (d *Discoverer) OnProbeTimeout(id uint64) Result {
	now := d.clock()
	rec, gst, found := d.resolveProbe(id)
	if !found || gst == nil {
		return Result{Applied: false, Snapshot: d.record(d.buildSnapshot(now, ChangeIgnored,
			fmt.Sprintf("probe %d is unknown or already resolved: ignored", id)))}
	}
	if d.current == nil || gst != d.current {
		return Result{Applied: false, Snapshot: d.record(d.buildSnapshot(now, ChangeIgnored,
			fmt.Sprintf("probe %d timeout belongs to generation %d while generation %d is active: ignored",
				id, rec.gen, d.currentGen())))}
	}
	if rec.activation != gst.activation {
		return Result{Applied: false, Snapshot: d.record(d.buildSnapshot(now, ChangeIgnored,
			fmt.Sprintf("probe %d belongs to an earlier activation of generation %d (activation %d, current %d): stale timeout ignored",
				id, gst.gen, rec.activation, gst.activation)))}
	}

	if gst.ackSeq <= rec.ackSeqAtSend {
		// No liveness in the same window: keep the original bounds and break
		// the consecutive-timeout streak.
		gst.consecutiveTimeoutsWithLiveness = 0
		return Result{Applied: true, Snapshot: d.record(d.buildSnapshot(now, ChangeHeld,
			fmt.Sprintf("probe %d (%d bytes) timed out with no ordinary acknowledgment in the window: bounds held (verified=%d ceiling=%d)",
				id, rec.size, gst.verified, gst.ceiling)))}
	}

	gst.consecutiveTimeoutsWithLiveness++

	// A pending re-verification probe is sent at exactly the verified size. If
	// ordinary packets at that size were acknowledged in this window, the size
	// is proven by ordinary traffic even though the probe itself was lost:
	// finish the re-verification instead of waiting on a probe that keeps
	// failing in isolation.
	if gst.revalidate && rec.size == gst.verified {
		gst.revalidate = false
		gst.consecutiveTimeoutsWithLiveness = 0
		return Result{Applied: true, Snapshot: d.record(d.buildSnapshot(now, ChangeRevalidated,
			fmt.Sprintf("probe %d (%d bytes) timed out, but ordinary packets at %d were acknowledged in the window: limit re-verified",
				id, rec.size, gst.verified)))}
	}

	if gst.consecutiveTimeoutsWithLiveness < 2 {
		return Result{Applied: true, Snapshot: d.record(d.buildSnapshot(now, ChangeHeld,
			fmt.Sprintf("probe %d (%d bytes) timed out with ordinary liveness (1/2): bounds held pending confirmation",
				id, rec.size)))}
	}

	gst.consecutiveTimeoutsWithLiveness = 0
	oldCeiling := gst.ceiling
	newCeiling := rec.size - 1
	if newCeiling < gst.floor {
		newCeiling = gst.floor
	}
	// Ordinary packets at the verified size were acknowledged in this window,
	// so the verified lower bound stays proven even though the larger probe was
	// lost. Never push the ceiling below it.
	if newCeiling < gst.verified {
		newCeiling = gst.verified
	}
	changed := false
	if newCeiling < gst.ceiling {
		gst.ceiling = newCeiling
		changed = true
	}

	kind := ChangeHeld
	var reason string
	if changed {
		kind = ChangeCeilingLowered
		gst.evidenceAt = now
		reason = fmt.Sprintf("two consecutive probe timeouts with ordinary liveness: ceiling lowered %d -> %d (failed probe %d bytes)",
			oldCeiling, gst.ceiling, rec.size)
	} else {
		reason = fmt.Sprintf("two consecutive probe timeouts with ordinary liveness, but ceiling %d is already at the verified bound %d (floor %d): bounds held",
			gst.ceiling, gst.verified, gst.floor)
	}
	return Result{Applied: true, Snapshot: d.record(d.buildSnapshot(now, kind, reason))}
}

// OnProbeTooBig reports that probe id was rejected with a size limit of
// maxPayload bytes. The ceiling of the probe's generation is lowered to the
// limit. If the limit is below the verified lower bound, the verified bound is
// dropped to the limit immediately and a confirmation probe is scheduled so the
// new limit is re-verified before ordinary traffic settles on it. Reports for a
// retired generation update only that generation's evidence.
func (d *Discoverer) OnProbeTooBig(id uint64, maxPayload int) Result {
	now := d.clock()
	if maxPayload <= 0 {
		return Result{Applied: false, Snapshot: d.record(d.buildSnapshot(now, ChangeIgnored,
			fmt.Sprintf("size limit %d is invalid: ignored (probe %d left pending)", maxPayload, id)))}
	}
	rec, gst, found := d.resolveProbe(id)
	if !found || gst == nil {
		return Result{Applied: false, Snapshot: d.record(d.buildSnapshot(now, ChangeIgnored,
			fmt.Sprintf("probe %d is unknown or already resolved: ignored", id)))}
	}
	if rec.activation != gst.activation {
		return Result{Applied: false, Snapshot: d.record(d.buildSnapshot(now, ChangeIgnored,
			fmt.Sprintf("probe %d belongs to an earlier activation of generation %d (activation %d, current %d): stale too-big feedback ignored",
				id, gst.gen, rec.activation, gst.activation)))}
	}

	oldVerified := gst.verified
	oldCeiling := gst.ceiling
	limit := maxPayload
	if limit < gst.floor {
		limit = gst.floor
	}
	ceilingLowered := false
	if limit < gst.ceiling {
		gst.ceiling = limit
		ceilingLowered = true
	}

	kind := ChangeHeld
	reason := fmt.Sprintf("probe %d too big with limit %d, but current ceiling %d is already lower: no change", id, maxPayload, oldCeiling)
	dropped := false
	if gst.ceiling < gst.verified {
		gst.verified = gst.ceiling
		dropped = true
		// The size the sender now trusts was inferred from a limit, not from an
		// acknowledgment. Drop to it immediately and verify it before treating
		// it as settled. This also applies at the floor: the confirmation probe
		// is cheap and keeps the state machine's invariant simple.
		gst.revalidate = true
		kind = ChangeRevalidating
		reason = fmt.Sprintf("probe %d too big: limit %d is below verified %d, immediately dropped to %d and scheduling re-verification",
			id, maxPayload, oldVerified, gst.verified)
	} else if ceilingLowered {
		kind = ChangeCeilingLowered
		reason = fmt.Sprintf("probe %d too big: ceiling lowered %d -> %d by reported limit %d", id, oldCeiling, gst.ceiling, maxPayload)
	}

	gst.consecutiveTimeoutsWithLiveness = 0
	if ceilingLowered || dropped {
		gst.evidenceAt = now
	}

	if gst != d.current {
		return Result{Applied: true, Snapshot: d.record(d.buildSnapshot(now, ChangeAppliedElsewhere,
			fmt.Sprintf("too-big feedback for probe %d recorded on retired generation %d: active generation %d unaffected",
				id, gst.gen, d.currentGen())))}
	}
	return Result{Applied: true, Snapshot: d.record(d.buildSnapshot(now, kind, reason))}
}

// Current returns the active state without appending to the trace.
func (d *Discoverer) Current() Snapshot {
	return d.buildSnapshot(d.clock(), ChangeNone, "current state")
}

// NextProbe returns the payload size the next probe should use.
func (d *Discoverer) NextProbe() (int, bool) {
	return d.nextProbeSize()
}

// Trace returns a copy of every snapshot recorded by events so far.
func (d *Discoverer) Trace() []Snapshot {
	out := make([]Snapshot, len(d.trace))
	copy(out, d.trace)
	return out
}

// PendingProbes returns the number of probes whose feedback has not arrived.
func (d *Discoverer) PendingProbes() int {
	return len(d.probes)
}

// EvidenceFor returns the stored knowledge about gen, if any.
func (d *Discoverer) EvidenceFor(gen uint64) (Evidence, bool) {
	st, ok := d.gens[gen]
	if !ok || !st.everActivated {
		return Evidence{}, false
	}
	return Evidence{
		Generation: st.gen,
		Floor:      st.floor,
		Verified:   st.verified,
		Ceiling:    st.ceiling,
		UpdatedAt:  st.evidenceAt,
		Active:     st.active,
	}, true
}

func (d *Discoverer) resolveProbe(id uint64) (*probeRecord, *genState, bool) {
	rec, ok := d.probes[id]
	if !ok {
		return nil, nil, false
	}
	delete(d.probes, id)
	gst := d.gens[rec.gen]
	if gst == nil || gst.gen != rec.gen {
		return rec, nil, true
	}
	return rec, gst, true
}

func (d *Discoverer) currentGen() uint64 {
	if d.current == nil {
		return 0
	}
	return d.current.gen
}

func (d *Discoverer) nextProbeSize() (int, bool) {
	st := d.current
	if st == nil {
		return 0, false
	}
	if st.revalidate {
		return st.verified, true
	}
	if st.verified >= st.ceiling {
		return 0, false
	}
	size := st.verified + (st.ceiling-st.verified)/2
	if size <= st.verified {
		size = st.verified + 1
	}
	if size > st.ceiling {
		size = st.ceiling
	}
	return size, true
}

func (d *Discoverer) buildSnapshot(now time.Time, kind ChangeKind, reason string) Snapshot {
	s := Snapshot{At: now, Kind: kind, Reason: reason}
	if d.current == nil {
		return s
	}
	s.Generation = d.current.gen
	s.Floor = d.current.floor
	s.Verified = d.current.verified
	s.Ceiling = d.current.ceiling
	s.OrdinarySendSize = d.current.verified
	s.Revalidating = d.current.revalidate
	if size, ok := d.nextProbeSize(); ok {
		s.NextProbe = size
		s.NextProbeValid = true
	}
	return s
}

func (d *Discoverer) record(s Snapshot) Snapshot {
	d.trace = append(d.trace, s)
	return s
}
