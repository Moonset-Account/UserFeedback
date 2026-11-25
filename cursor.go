package logcursor

import (
	"errors"
	"sort"
)

// Cursor level errors.
var (
	ErrInvalidLimit  = errors.New("logcursor: limit must not be negative")
	ErrInvalidCursor = errors.New("logcursor: cursor state is invalid")
)

// Cursor is an immutable, serialisable position in a multi-step log.
//
// A zero Cursor is unpinned. The first Read pins it to the snapshot the backend
// currently exposes; every later Read with the same Cursor value is answered
// from that pinned snapshot, so re-reading a fixed page returns the same data
// and the same next cursor even if the backend has grown in the meantime.
//
// Cursor fields are exported and JSON-tagged: the value can be stored and
// restored verbatim (for example after a reconnect) to resume at the exact
// byte where the previous page stopped.
type Cursor struct {
	// Pinned reports whether the cursor has captured a snapshot yet.
	Pinned bool `json:"pinned"`
	// Snapshot is the upstream-visible snapshot version the cursor reads from.
	Snapshot int64 `json:"snapshot"`
	// Steps holds one position per visible step, ordered by step name.
	Steps []StepState `json:"steps"`
}

// StepState is the position of a single step within a Cursor.
type StepState struct {
	Step string `json:"step"`
	// Attempt is the attempt number this position belongs to. Attempts are
	// numbered from zero per step and each attempt numbers its own bytes from
	// zero again.
	Attempt int `json:"attempt"`
	// Offset is the next byte this step will deliver. It is always the length
	// of the contiguous prefix already delivered, so resuming from it never
	// skips or repeats a byte.
	Offset int64 `json:"offset"`
	// GapCheck is the end offset of the most recently announced gap. It makes
	// a gap notice a one-time event for the life of the cursor, including
	// across serialisation and reconnects.
	GapCheck int64 `json:"gapCheck"`
	// Ended is true once the attempt's declared end offset has been reached
	// with no holes left before it.
	Ended bool `json:"ended"`
}

// GapNotice reports that the bytes [Start, End) of Step/Attempt are
// permanently lost. A cursor emits at most one notice per gap.
type GapNotice struct {
	Step    string `json:"step"`
	Attempt int    `json:"attempt"`
	Start   int64  `json:"start"`
	End     int64  `json:"end"`
}

// FragmentRef describes a slice of one numbered fragment delivered in a page.
// Seq is scoped to Step/Attempt: numbering restarts at zero for each attempt.
type FragmentRef struct {
	Seq   int   `json:"seq"`
	Start int64 `json:"start"`
	End   int64 `json:"end"`
}

// StepData is the page contribution of one step.
type StepData struct {
	Step      string        `json:"step"`
	Attempt   int           `json:"attempt"`
	Offset    int64         `json:"offset"`
	Data      []byte        `json:"data,omitempty"`
	Fragments []FragmentRef `json:"fragments,omitempty"`
	Gaps      []GapNotice   `json:"gaps,omitempty"`
	Ended     bool          `json:"ended"`
	// Waiting is true when the step cannot make progress because bytes are
	// missing at WaitingAt and no gap covers them. Other steps are unaffected.
	Waiting   bool  `json:"waiting"`
	WaitingAt int64 `json:"waitingAt"`
}

// Page is the result of one Read. Cursor is the position that resumes exactly
// after the last delivered byte of every step.
type Page struct {
	Cursor Cursor     `json:"cursor"`
	Steps  []StepData `json:"steps"`
	Bytes  int        `json:"bytes"`
	Done   bool       `json:"done"`
}

// Step returns the page contribution for name.
func (p Page) Step(name string) (StepData, bool) {
	for _, sd := range p.Steps {
		if sd.Step == name {
			return sd, true
		}
	}
	return StepData{}, false
}

// StepState returns the cursor position for name.
func (c Cursor) StepState(name string) (StepState, bool) {
	for _, st := range c.Steps {
		if st.Step == name {
			return st, true
		}
	}
	return StepState{}, false
}

// Read returns at most limit bytes in total across all steps of the cursor's
// pinned snapshot.
//
// limit bounds the sum of delivered bytes, not the number of steps or
// fragments. A page may stop inside a fragment; the returned cursor then
// resumes at exactly the next unseen byte. Steps advance independently: a step
// blocked on a hole contributes nothing while the remaining steps continue.
//
// A step's end marker only takes effect after every hole before the declared
// end offset has been filled by a fragment or declared as a gap. Page.Done is
// true only when every step has reached that point.
//
// Read does not mutate the receiver or the cursor: the returned cursor is the
// only new state.
func (l *Log) Read(c Cursor, limit int) (Page, error) {
	if limit < 0 {
		return Page{}, ErrInvalidLimit
	}
	cur := l.Version()
	if !c.Pinned {
		c = l.pin(c, cur)
	}
	if err := c.validate(); err != nil {
		return Page{}, err
	}
	if c.Snapshot > cur {
		// A cursor can only pin a version this backend has produced.
		return Page{}, ErrInvalidCursor
	}
	snap := l.freezeAt(c.Snapshot)
	for _, st := range c.Steps {
		ss := snap.steps[st.Step]
		if ss == nil || ss.attempts[st.Attempt] == nil {
			return Page{}, ErrInvalidCursor
		}
	}

	states := make([]StepState, len(c.Steps))
	copy(states, c.Steps)
	accs := make([]stepAccum, len(states))

	remaining := limit
	for remaining > 0 {
		progressed := false
		for i := range states {
			as := snap.attempt(states[i].Step, states[i].Attempt)
			if as == nil {
				// The pinned snapshot no longer knows this attempt; nothing
				// can be delivered for it.
				continue
			}
			if deliverOne(&states[i], as, &accs[i], &remaining) {
				progressed = true
			}
			if remaining == 0 {
				break
			}
		}
		if !progressed {
			break
		}
	}

	page := Page{
		Cursor: Cursor{Pinned: true, Snapshot: c.Snapshot, Steps: states},
		Bytes:  limit - remaining,
		Steps:  make([]StepData, 0, len(states)),
		Done:   true,
	}
	for i := range states {
		st := states[i]
		as := snap.attempt(st.Step, st.Attempt)
		blocked := as != nil && isBlocked(as, &st)
		sd := StepData{
			Step:      st.Step,
			Attempt:   st.Attempt,
			Offset:    st.Offset,
			Data:      accs[i].data,
			Fragments: accs[i].refs,
			Gaps:      accs[i].gaps,
			Ended:     st.Ended,
			Waiting:   blocked,
			WaitingAt: st.Offset,
		}
		if !blocked {
			sd.WaitingAt = 0
		}
		if !st.Ended {
			page.Done = false
		}
		page.Steps = append(page.Steps, sd)
	}
	return page, nil
}

// Advance re-pins c to the backend's current snapshot. Positions of steps are
// preserved, so the caller resumes at the exact byte where it stopped. If a
// step has started a newer attempt, that step restarts from offset zero of the
// new attempt; content still arriving for the superseded attempt can never be
// mixed into the retry. Steps that became visible only now are appended.
//
// Advance is explicit: without it a cursor never observes data added after its
// pin, which is what makes page re-reads reproducible.
func (l *Log) Advance(c Cursor) (Cursor, error) {
	v := l.Version()
	if !c.Pinned {
		return l.pin(c, v), nil
	}
	if err := c.validate(); err != nil {
		return Cursor{}, err
	}
	snap := l.freezeAt(v)

	out := Cursor{Pinned: true, Snapshot: v, Steps: make([]StepState, 0, len(c.Steps))}
	seen := make(map[string]bool, len(c.Steps))
	for _, st := range c.Steps {
		seen[st.Step] = true
		if ss := snap.steps[st.Step]; ss != nil && ss.maxAttempt > st.Attempt {
			st.Attempt = ss.maxAttempt
			st.Offset = 0
			st.GapCheck = 0
			st.Ended = false
		}
		out.Steps = append(out.Steps, st)
	}
	for _, name := range snap.order {
		if seen[name] {
			continue
		}
		out.Steps = append(out.Steps, StepState{
			Step:    name,
			Attempt: snap.steps[name].maxAttempt,
		})
	}
	sort.Slice(out.Steps, func(i, j int) bool { return out.Steps[i].Step < out.Steps[j].Step })
	return out, nil
}

// pin locks c to snapshot version v and adopts every step visible there.
func (l *Log) pin(c Cursor, v int64) Cursor {
	snap := l.freezeAt(v)
	out := Cursor{Pinned: true, Snapshot: v, Steps: make([]StepState, 0, len(snap.order))}
	for _, name := range snap.order {
		out.Steps = append(out.Steps, StepState{
			Step:    name,
			Attempt: snap.steps[name].maxAttempt,
		})
	}
	return out
}

func (c Cursor) validate() error {
	if c.Snapshot < 0 {
		return ErrInvalidCursor
	}
	prev := ""
	for i, st := range c.Steps {
		if st.Step == "" || st.Attempt < 0 || st.Offset < 0 || st.GapCheck < 0 {
			return ErrInvalidCursor
		}
		if i > 0 && st.Step <= prev {
			return ErrInvalidCursor
		}
		prev = st.Step
	}
	return nil
}

type stepAccum struct {
	data []byte
	refs []FragmentRef
	gaps []GapNotice
}

// deliverOne delivers a single contiguous run for one step: either the bytes of
// one fragment or the remainder of one declared gap. It reports whether it made
// progress; a blocked or exhausted step returns false.
func deliverOne(st *StepState, as *attemptSnapshot, acc *stepAccum, budget *int) bool {
	if *budget <= 0 || st.Ended {
		return false
	}
	if g := gapAt(as, st.Offset); g != nil {
		n := g.end - st.Offset
		if int64(*budget) < n {
			n = int64(*budget)
		}
		if st.GapCheck < g.end {
			acc.gaps = append(acc.gaps, GapNotice{
				Step: st.Step, Attempt: st.Attempt, Start: g.start, End: g.end,
			})
			st.GapCheck = g.end
		}
		st.Offset += n
		*budget -= int(n)
		settle(st, as)
		return true
	}
	if f := fragAt(as, st.Offset); f != nil {
		n := f.end() - st.Offset
		if int64(*budget) < n {
			n = int64(*budget)
		}
		in := st.Offset - f.start
		acc.data = append(acc.data, f.data[in:in+n]...)
		acc.refs = append(acc.refs, FragmentRef{Seq: f.seq, Start: st.Offset, End: st.Offset + n})
		st.Offset += n
		*budget -= int(n)
		settle(st, as)
		return true
	}
	settle(st, as)
	return false
}

// settle marks the step ended once its delivered prefix covers the declared
// end offset. Because the backend rejects data or gaps past the end offset,
// Offset never overshoots it.
func settle(st *StepState, as *attemptSnapshot) {
	if as.endSet && st.Offset >= as.endOff {
		st.Ended = true
	}
}

// isBlocked reports whether the step is stuck at a hole: no fragment and no gap
// starts at Offset, and the declared end (if any) is still ahead.
func isBlocked(as *attemptSnapshot, st *StepState) bool {
	if st.Ended {
		return false
	}
	if as.endSet && st.Offset >= as.endOff {
		return false
	}
	return fragAt(as, st.Offset) == nil && gapAt(as, st.Offset) == nil
}

func fragAt(as *attemptSnapshot, off int64) *fragSnapshot {
	i := sort.Search(len(as.frags), func(i int) bool { return as.frags[i].end() > off })
	if i < len(as.frags) && as.frags[i].start <= off {
		return &as.frags[i]
	}
	return nil
}

func gapAt(as *attemptSnapshot, off int64) *gapSnapshot {
	i := sort.Search(len(as.gaps), func(i int) bool { return as.gaps[i].end > off })
	if i < len(as.gaps) && as.gaps[i].start <= off {
		return &as.gaps[i]
	}
	return nil
}
