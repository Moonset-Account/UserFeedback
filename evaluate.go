package egress

import (
	"fmt"
	"sort"
)

// ViolationKind classifies a checked candidate fragment list.
type ViolationKind string

const (
	VUnknownFrame  ViolationKind = "unknown_frame"
	VClassMismatch ViolationKind = "class_mismatch"
	VArrival       ViolationKind = "before_arrival"
	VOverlap       ViolationKind = "fragment_overlap"
	VFIFO          ViolationKind = "fifo_violation"
	VTooMuchData   ViolationKind = "too_much_data"
	VMinNonLast    ViolationKind = "small_non_last_fragment"
	VIncomplete    ViolationKind = "incomplete_frame"
	VGateClosed    ViolationKind = "gate_closed_at_start"
	VGateClose     ViolationKind = "fragment_crosses_gate_close"
	VBadOverhead   ViolationKind = "bad_overhead"
)

// Violation is one concrete discrepancy in a candidate schedule.
type Violation struct {
	Kind    ViolationKind `json:"kind"`
	FrameID string        `json:"frame_id,omitempty"`
	Tick    int           `json:"tick"`
	Message string        `json:"message"`
}

// GateMiss identifies the true phase/table gate missed by a candidate action.
type GateMiss struct {
	Scenario  string `json:"scenario,omitempty"`
	FrameID   string `json:"frame_id"`
	Start     int    `json:"start"`
	End       int    `json:"end"`
	GateClose int    `json:"gate_close"`
	At        int    `json:"at"`
	Reason    string `json:"reason"`
}

// Evaluation reports deadline results and all structural/gate violations.
type Evaluation struct {
	Plan         *Plan       `json:"plan,omitempty"`
	Violations   []Violation `json:"violations"`
	GateMisses   []GateMiss  `json:"gate_misses"`
	FastOnTime   int         `json:"fast_on_time"`
	NormalOnTime int         `json:"normal_on_time"`
}

// Valid reports whether the fragment list obeys queue, framing and gate rules.
func (e *Evaluation) Valid() bool { return len(e.Violations) == 0 }

// FirstGateMiss returns the earliest gate miss, if any.
func (e *Evaluation) FirstGateMiss() (GateMiss, bool) {
	if len(e.GateMisses) == 0 {
		return GateMiss{}, false
	}
	return e.GateMisses[0], true
}

// CheckFragments validates an externally supplied concrete fragment sequence.
// It is used by the ambiguity checker to evaluate a shared decision against a
// true table/phase, including decisions that would be illegal under that phase.
func CheckFragments(cfg Config, fragments []Fragment) (*Evaluation, error) {
	frames, err := validateConfig(cfg)
	if err != nil {
		return nil, err
	}
	byID := map[string]Frame{}
	var fastQ, normalQ []string
	for _, f := range frames {
		byID[f.ID] = f
		if f.Class == Fast {
			fastQ = append(fastQ, f.ID)
		} else {
			normalQ = append(normalQ, f.ID)
		}
	}

	fs := make([]Fragment, len(fragments))
	copy(fs, fragments)
	sort.SliceStable(fs, func(i, j int) bool {
		if fs[i].Start != fs[j].Start {
			return fs[i].Start < fs[j].Start
		}
		return fs[i].End < fs[j].End
	})

	ev := &Evaluation{}
	rem := map[string]int{}
	for id, f := range byID {
		rem[id] = f.Length
	}
	fi, ni := 0, 0
	prevEnd := cfg.StartAt
	normalized := make([]Fragment, 0, len(fs))

	addViolation := func(v Violation) { ev.Violations = append(ev.Violations, v) }
	addGateMiss := func(gm GateMiss) { ev.GateMisses = append(ev.GateMisses, gm) }

	for i := range fs {
		fr := &fs[i]
		f, ok := byID[fr.FrameID]
		if !ok {
			addViolation(Violation{Kind: VUnknownFrame, FrameID: fr.FrameID, Tick: fr.Start,
				Message: "fragment references unknown frame"})
			continue
		}
		if fr.Class != f.Class {
			addViolation(Violation{Kind: VClassMismatch, FrameID: f.ID, Tick: fr.Start,
				Message: fmt.Sprintf("fragment class %s != frame class %s", fr.Class, f.Class)})
		}
		if fr.Overhead != cfg.Overhead {
			addViolation(Violation{Kind: VBadOverhead, FrameID: f.ID, Tick: fr.Start,
				Message: fmt.Sprintf("overhead %d != configured %d", fr.Overhead, cfg.Overhead)})
		}
		if fr.Start < f.Arrival {
			addViolation(Violation{Kind: VArrival, FrameID: f.ID, Tick: fr.Start,
				Message: fmt.Sprintf("start %d before arrival %d", fr.Start, f.Arrival)})
		}
		if fr.Start < prevEnd {
			addViolation(Violation{Kind: VOverlap, FrameID: f.ID, Tick: fr.Start,
				Message: fmt.Sprintf("fragment starts at %d before previous end %d", fr.Start, prevEnd)})
		}
		if fr.End != fr.Start+fr.Overhead+fr.Data {
			fr.End = fr.Start + fr.Overhead + fr.Data
		}
		if fr.Bytes == 0 {
			fr.Bytes = fr.Overhead + fr.Data
		}
		prevEnd = maxInt(prevEnd, fr.End)

		// FIFO check against the current class head.
		queue, idx := fastQ, &fi
		if f.Class == Normal {
			queue, idx = normalQ, &ni
		}
		if *idx < len(queue) && queue[*idx] != f.ID {
			addViolation(Violation{Kind: VFIFO, FrameID: f.ID, Tick: fr.Start,
				Message: fmt.Sprintf("frame %s sent before FIFO head %s", f.ID, queue[*idx])})
		}

		if fr.Data <= 0 {
			addViolation(Violation{Kind: VTooMuchData, FrameID: f.ID, Tick: fr.Start, Message: "non-positive data"})
			continue
		}
		if fr.Data > rem[f.ID] {
			addViolation(Violation{Kind: VTooMuchData, FrameID: f.ID, Tick: fr.Start,
				Message: fmt.Sprintf("data %d exceeds remaining %d", fr.Data, rem[f.ID])})
		} else {
			before := rem[f.ID]
			rem[f.ID] -= fr.Data
			fr.Final = rem[f.ID] == 0
			if !fr.Final && fr.Data < cfg.MinNonLast {
				addViolation(Violation{Kind: VMinNonLast, FrameID: f.ID, Tick: fr.Start,
					Message: fmt.Sprintf("non-last data %d < minimum %d", fr.Data, cfg.MinNonLast)})
			}
			if fr.Final && *idx < len(queue) && queue[*idx] == f.ID {
				*idx++
			}
			_ = before
		}

		if !cfg.Table.FitsAt(fr.Start, fr.Data, fr.Overhead, f.Class) {
			gm := GateMiss{FrameID: f.ID, Start: fr.Start, End: fr.End}
			if !cfg.Table.GateOpenAt(fr.Start, f.Class) {
				gm.At = fr.Start
				gm.Reason = fmt.Sprintf("%s gate closed at start tick %d", f.Class, fr.Start)
				addViolation(Violation{Kind: VGateClosed, FrameID: f.ID, Tick: fr.Start, Message: gm.Reason})
			} else {
				for _, w := range cfg.Table.absoluteWindows(f.Class, fr.Start) {
					if fr.Start >= w.start && fr.Start < w.end {
						gm.GateClose = w.end
						gm.At = w.end
						gm.Reason = fmt.Sprintf("fragment ends %d (guard %d) after %s gate close %d", fr.End, cfg.Table.Guard, f.Class, w.end)
						break
					}
				}
				addViolation(Violation{Kind: VGateClose, FrameID: f.ID, Tick: gm.At, Message: gm.Reason})
			}
			addGateMiss(gm)
		}
		normalized = append(normalized, *fr)
	}

	for _, f := range frames {
		if rem[f.ID] != 0 {
			addViolation(Violation{Kind: VIncomplete, FrameID: f.ID,
				Message: fmt.Sprintf("%d bytes remain unsent", rem[f.ID])})
		}
	}

	// Build accounting even when an expected counterfactual misses a gate; the
	// caller can still inspect deadline outcomes. Structural FIFO/data errors
	// may make this accounting approximate, hence Plan is for display only.
	s := &solver{cfg: cfg, fast: nil, normal: nil}
	for _, f := range frames {
		if f.Class == Fast {
			s.fast = append(s.fast, f)
		} else {
			s.normal = append(s.normal, f)
		}
	}
	ev.Plan = s.buildPlan(normalized)
	ev.FastOnTime = ev.Plan.FastOnTime
	ev.NormalOnTime = ev.Plan.NormalOnTime

	sort.Slice(ev.GateMisses, func(i, j int) bool { return ev.GateMisses[i].Start < ev.GateMisses[j].Start })
	return ev, nil
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
