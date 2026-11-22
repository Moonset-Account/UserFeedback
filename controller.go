package capacitycontroller

import (
	"errors"
	"fmt"
	"sort"
	"sync"
)

type obsKey struct {
	instance string
	gen      int
	start    int64
	end      int64
}

type genKey struct {
	instance string
	gen      int
}

type span struct{ start, end int64 }

// Controller ingests observations and lifecycle events, verifies fixed-size
// windows against the actual online periods, and decides scaling actions.
//
// All exported methods are safe for concurrent use.
type Controller struct {
	mu sync.Mutex

	cfg Config

	// Latest revision record keyed by (instance, gen, start, end).
	observations map[obsKey]Observation
	// Ordered keys per (instance, gen) for deterministic sweeps.
	byGen map[genKey][]obsKey
	// Online spans per (instance, gen), already union-merged.
	online map[genKey][]span
	// Pending up timestamp per generation, closed by the matching down.
	pendingUp map[genKey]int64
	// Monotonic lifecycle order for mismatch reporting.
	lifecycle []Lifecycle

	frozen   map[int]Verdict
	order    int // next window index to evaluate
	actions  []*Action
	actionID int64

	pendingActionID int64
	cooldownUntil   int64

	highStreak int
	lowStreak  int

	// A decision that was due but could not be published yet.
	blocked          *Decision
	blockedEmittedAs int64 // action id once the blocked decision was flushed
}

// New constructs a controller with the given configuration.
func New(cfg Config) (*Controller, error) {
	if err := cfg.validate(); err != nil {
		return nil, err
	}
	return &Controller{
		cfg:          cfg,
		observations: make(map[obsKey]Observation),
		byGen:        make(map[genKey][]obsKey),
		online:       make(map[genKey][]span),
		pendingUp:    make(map[genKey]int64),
		frozen:       make(map[int]Verdict),
	}, nil
}

// IngestObservation stores or revises one observation. A higher revision for
// the same (instance, gen, start, end) replaces the old record. Equal or
// lower revisions are rejected.
//
// The observation is kept even if it currently contradicts the lifecycle
// (for example an old-generation record arriving after a restart): window
// verification marks the contradiction explicitly instead of silently
// letting it fill a new-generation gap.
func (c *Controller) IngestObservation(o Observation) error {
	if err := o.validate(); err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()

	k := obsKey{o.Instance, o.Gen, o.Start, o.End}
	gk := genKey{o.Instance, o.Gen}
	if prev, ok := c.observations[k]; ok {
		if o.Rev < prev.Rev {
			return fmt.Errorf("stale revision %d for %s gen %d [%d,%d), latest is %d",
				o.Rev, o.Instance, o.Gen, o.Start, o.End, prev.Rev)
		}
		if o.Rev == prev.Rev {
			return fmt.Errorf("conflicting revision %d for %s gen %d [%d,%d)",
				o.Rev, o.Instance, o.Gen, o.Start, o.End)
		}
		c.observations[k] = o
		return nil
	}
	c.observations[k] = o
	c.byGen[gk] = append(c.byGen[gk], k)
	sort.Slice(c.byGen[gk], func(i, j int) bool {
		a, b := c.byGen[gk][i], c.byGen[gk][j]
		if a.start != b.start {
			return a.start < b.start
		}
		return a.end < b.end
	})
	return nil
}

// RegisterLifecycle records an up/down transition. An up opens a pending
// online slice and the matching down closes it as [upAt, downAt). Spans for
// the same (instance, gen) are union-merged so out-of-order or duplicate
// delivery is tolerated.
func (c *Controller) RegisterLifecycle(e Lifecycle) error {
	if err := e.validate(); err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.lifecycle = append(c.lifecycle, e)

	gk := genKey{e.Instance, e.Gen}
	if e.Up {
		if up, ok := c.pendingUp[gk]; !ok || up != e.At {
			c.pendingUp[gk] = e.At
		}
		return nil
	}
	if up, ok := c.pendingUp[gk]; ok {
		if e.At > up {
			c.online[gk] = mergeSpan(c.online[gk], span{up, e.At})
		}
		delete(c.pendingUp, gk)
		return nil
	}
	// A down with no matching up carries no online duration by itself.
	if _, known := c.online[gk]; !known {
		c.online[gk] = nil
	}
	return nil
}

// OnlineSpan is a convenience for tests/feeders: it registers one complete
// online period [start, end) for an instance generation.
func (c *Controller) OnlineSpan(instance string, gen int, start, end int64) error {
	if end <= start {
		return fmt.Errorf("online span: non-positive period [%d,%d)", start, end)
	}
	if err := c.RegisterLifecycle(Lifecycle{Instance: instance, Gen: gen, At: start, Up: true}); err != nil {
		return err
	}
	return c.RegisterLifecycle(Lifecycle{Instance: instance, Gen: gen, At: end, Up: false})
}

// Acknowledge marks an action receipt at time at. Cooldown starts from that
// instant, not from issue time. Unknown or duplicate receipts are rejected.
func (c *Controller) Acknowledge(actionID int64, at int64) error {
	if at < 0 {
		return fmt.Errorf("acknowledge: negative timestamp %d", at)
	}
	c.mu.Lock()
	defer c.mu.Unlock()

	var act *Action
	for _, a := range c.actions {
		if a.ID == actionID {
			act = a
			break
		}
	}
	if act == nil {
		return fmt.Errorf("acknowledge: unknown action id %d", actionID)
	}
	if act.Acknowledged() {
		return fmt.Errorf("acknowledge: action %d already acknowledged at %d", actionID, act.AckedAt)
	}
	if at < act.IssuedAt {
		return fmt.Errorf("acknowledge: receipt time %d before issue time %d", at, act.IssuedAt)
	}
	act.AckedAt = at
	if c.pendingActionID == actionID {
		c.pendingActionID = 0
	}
	if until := at + c.cfg.Cooldown; until > c.cooldownUntil {
		c.cooldownUntil = until
	}
	return nil
}

// PendingActionID returns the unacknowledged action, or 0 when none exists.
func (c *Controller) PendingActionID() int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.pendingActionID
}

// CooldownUntil returns the earliest time at which the next action may be
// issued given the latest acknowledged receipt.
func (c *Controller) CooldownUntil() int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.cooldownUntil
}

// Streaks returns the current verified high/low window streaks.
func (c *Controller) Streaks() (high, low int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.highStreak, c.lowStreak
}

// Actions returns a copy of every published action in issue order.
func (c *Controller) Actions() []Action {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]Action, 0, len(c.actions))
	for _, a := range c.actions {
		out = append(out, *a)
	}
	return out
}

// FrozenVerdicts returns copies of all published window verdicts, ordered.
func (c *Controller) FrozenVerdicts() []Verdict {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]Verdict, 0, len(c.frozen))
	for i := 0; i < c.order; i++ {
		if v, ok := c.frozen[i]; ok {
			out = append(out, cloneVerdict(v))
		}
	}
	return out
}

// BlockedDecision reports a due-but-withheld scaling decision, if any.
func (c *Controller) BlockedDecision() *Decision {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.blocked == nil {
		return nil
	}
	d := *c.blocked
	return &d
}

// ErrNoWindow is returned when no window is currently evaluable.
var ErrNoWindow = errors.New("no closed window available")

// Preview evaluates the next not-yet-published window without freezing it.
// Late corrections can therefore still fill its gaps before Pump.
func (c *Controller) Preview(now int64) (Verdict, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	idx := c.order
	start := int64(idx) * c.cfg.WindowSize
	end := start + c.cfg.WindowSize
	if now < end {
		return Verdict{}, fmt.Errorf("%w: window %d closes at %d, now=%d", ErrNoWindow, idx, end, now)
	}
	return c.evaluateWindow(idx, start, end), nil
}

func mergeSpan(spans []span, add span) []span {
	spans = append(spans, add)
	sort.Slice(spans, func(i, j int) bool {
		if spans[i].start != spans[j].start {
			return spans[i].start < spans[j].start
		}
		return spans[i].end < spans[j].end
	})
	merged := spans[:0]
	for _, s := range spans {
		if len(merged) == 0 || s.start > merged[len(merged)-1].end {
			merged = append(merged, s)
			continue
		}
		if s.end > merged[len(merged)-1].end {
			merged[len(merged)-1].end = s.end
		}
	}
	return merged
}

func cloneVerdict(v Verdict) Verdict {
	if v.Problems != nil {
		v.Problems = append([]Problem(nil), v.Problems...)
	}
	return v
}
