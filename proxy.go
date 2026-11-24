package migrationplanner

import (
	"errors"
	"fmt"
	"sort"
)

// Proxy synthesizes a continuation/rollback mapping for one broken-up old
// session during a transition. It may only observe frames that the old
// session already confirmed.
//
// The mapping has three obligations:
//
//   - every frame sequence still allowed by the old FSM remains allowed after
//     mapping into the new FSM;
//   - every business effect already produced is retained and exposed;
//   - a confirmed resendable tail frame is never processed twice.
type Proxy struct {
	oldP *StreamProtocol
	newP *StreamProtocol

	oldState string
	newState string

	confirmedTail map[int]bool   // tail frame Seq values already confirmed
	tailEffect    map[int]string // business effect recorded per confirmed tail
	effects       []string       // effects in production order, replays excluded
	frames        []Frame        // confirmed observation trace
}

// NewProxy builds a mapping from an observed old-FSM state into the new FSM.
// It searches the reachable new-FSM states for one whose continuation covers
// every frame the old FSM can still accept.
func NewProxy(oldP, newP *StreamProtocol, oldState string) (*Proxy, error) {
	if oldP == nil || newP == nil {
		return nil, errors.New("proxy requires two compiled stream protocols")
	}
	known := map[string]bool{}
	for _, s := range oldP.States {
		known[s] = true
	}
	if !known[oldState] {
		return nil, fmt.Errorf("observed old state %q is not part of %q", oldState, oldP.Name)
	}

	target, err := findCoveringState(oldP, newP, oldState)
	if err != nil {
		return nil, err
	}
	return &Proxy{
		oldP:          oldP,
		newP:          newP,
		oldState:      oldState,
		newState:      target,
		confirmedTail: map[int]bool{},
		tailEffect:    map[int]string{},
	}, nil
}

// findCoveringState BFSes the new FSM from its start and returns the first
// reachable state (shortest depth, then name) whose enabled continuations
// cover the old state's.
func findCoveringState(oldP, newP *StreamProtocol, oldState string) (string, error) {
	need := oldP.labelsAt(oldState)
	oldTerminal := oldP.Terminal[oldState]

	type cand struct {
		state string
		depth int
	}
	visited := map[string]bool{newP.Start: true}
	queue := []cand{{newP.Start, 0}}
	var candidates []cand

	for len(queue) > 0 {
		cur := queue[0]
		queue = queue[1:]
		have := newP.labelsAt(cur.state)
		if covers(have, need) && newP.Terminal[cur.state] == oldTerminal {
			candidates = append(candidates, cur)
		}
		if cur.depth >= len(newP.States) {
			continue
		}
		for _, l := range have {
			next := newP.next[cur.state][l].To
			if !visited[next] {
				visited[next] = true
				queue = append(queue, cand{next, cur.depth + 1})
			}
		}
	}
	if len(candidates) == 0 {
		return "", fmt.Errorf("no state of %q covers the continuations of %q in state %q",
			newP.Name, oldP.Name, oldState)
	}
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].depth != candidates[j].depth {
			return candidates[i].depth < candidates[j].depth
		}
		return candidates[i].state < candidates[j].state
	})
	return candidates[0].state, nil
}

func covers(have, need []string) bool {
	set := map[string]bool{}
	for _, l := range have {
		set[l] = true
	}
	for _, l := range need {
		if !set[l] {
			return false
		}
	}
	return true
}

// Accept feeds one confirmed frame observed from the old session.
//
// replayed is true when a resendable tail frame had already been confirmed:
// the stored business effect is returned and the new FSM is not driven again,
// so confirmed tails are processed exactly once.
func (px *Proxy) Accept(f Frame) (effect string, replayed bool, err error) {
	if f.Kind == KindTail && px.confirmedTail[f.Seq] {
		return px.tailEffect[f.Seq], true, nil
	}

	nextOld, oldEffect, ok := px.oldP.Step(px.oldState, f)
	if !ok {
		return "", false, fmt.Errorf("frame %s is not allowed by old FSM in state %q", f.label(), px.oldState)
	}
	nextNew, newEffect, ok := px.newP.Step(px.newState, f)
	if !ok {
		return "", false, fmt.Errorf("frame %s cannot be mapped into new FSM state %q", f.label(), px.newState)
	}
	if oldEffect != newEffect {
		return "", false, fmt.Errorf("frame %s changes business effect under mapping: %q vs %q",
			f.label(), oldEffect, newEffect)
	}

	px.oldState = nextOld
	px.newState = nextNew
	px.frames = append(px.frames, f)
	if f.Effect != "" {
		px.effects = append(px.effects, f.Effect)
	}
	if f.Kind == KindTail {
		px.confirmedTail[f.Seq] = true
		px.tailEffect[f.Seq] = f.Effect
	}
	return f.Effect, false, nil
}

// MappedState returns the current new-FSM state (for continuation).
func (px *Proxy) MappedState() string { return px.newState }

// RollbackState returns the current old-FSM state, sufficient to rebuild the
// old session if the transition is abandoned.
func (px *Proxy) RollbackState() string { return px.oldState }

// AppliedEffects returns business effects in production order, without any
// duplicates caused by tail retransmission.
func (px *Proxy) AppliedEffects() []string {
	out := make([]string, len(px.effects))
	copy(out, px.effects)
	return out
}

// ConfirmedFrames returns the observed confirmed trace.
func (px *Proxy) ConfirmedFrames() []Frame {
	out := make([]Frame, len(px.frames))
	copy(out, px.frames)
	return out
}
