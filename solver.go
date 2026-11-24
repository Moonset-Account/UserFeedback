package admit

import (
	"fmt"
	"sort"
	"strings"
)

const (
	stExcluded uint8 = iota
	stUnreleased
	stReady1
	stRunning1
	stReady2
	stRunning2
	stObs1
	stDone
	stCanceled
)

const vacuousMargin = 1_000_000_000

type state struct {
	now           int
	status        [8]uint8
	cancelPending [8]uint8
	busy          [2]uint8
	bStage        [2]uint8
	bShape        [2]string
	bMembers      [2]uint8
	bFinish       [2]int
	doneAt        [8]int
}

type memoKey struct {
	st     state
	worlds bits
}

type iAction struct {
	ch       int
	stage    Stage
	shape    string
	members  uint8
	start    int
	duration int
	finish   int
}

type iBranch struct {
	obs    obsKey
	worlds bits
	next   *iNode
	margin int
}

type iNode struct {
	at       int
	action   *iAction
	then     *iNode
	branches []iBranch
	done     bool
	doneMask uint8
	margin   int
	doneAt   [8]int
}

type iProof struct {
	desc   string
	action *iAction
	fail   *iWitness
}

type iWitness struct {
	at         int
	obs        *obsKey
	worlds     bits
	reason     string
	req        int
	deadline   int
	completion int
	proofs     []iProof
	child      *iWitness
}

type solveResult struct {
	node    *iNode
	margin  int
	witness *iWitness
}

func activeInitialState(e *engine, active uint8, now int) state {
	var st state
	st.now = now
	for i, r := range e.requests {
		if active&(uint8(1)<<uint(i)) == 0 {
			st.status[i] = stExcluded
			continue
		}
		if r.Release > now {
			st.status[i] = stUnreleased
		} else {
			st.status[i] = stReady1
		}
	}
	return st
}

func (e *engine) solveSet(active uint8) (*solveResult, error) {
	worlds := allBits(len(e.scenarios))
	st := activeInitialState(e, active, e.admissionTime)
	branches, wit := e.eventsAt(st, worlds, e.admissionTime)
	if wit != nil {
		return &solveResult{witness: wit}, nil
	}
	if len(branches) == 0 {
		return nil, fmt.Errorf("admit: internal error: initial event produced no branches")
	}
	margin := vacuousMargin
	for _, b := range branches {
		if b.margin < margin {
			margin = b.margin
		}
	}
	root := &iNode{at: e.admissionTime, branches: branches, margin: margin}
	return &solveResult{node: root, margin: margin}, nil
}

func (e *engine) solve(st state, worlds bits) *solveResult {
	key := memoKey{st: st, worlds: worlds}
	if r, ok := e.memo[key]; ok {
		return r
	}

	fail := func(w *iWitness) *solveResult {
		r := &solveResult{witness: w}
		e.memo[key] = r
		return r
	}

	// Hard deadline/pruning checks.
	allTerminal := true
	for i, r := range e.requests {
		switch st.status[i] {
		case stExcluded, stDone, stCanceled:
			continue
		default:
			allTerminal = false
		}
		if st.cancelPending[i] != 0 {
			continue
		}
		if st.status[i] != stExcluded && st.now > r.Deadline {
			return fail(&iWitness{
				at:         st.now,
				reason:     fmt.Sprintf("current virtual time %d is after deadline", st.now),
				req:        i,
				deadline:   r.Deadline,
				completion: st.now,
			})
		}
	}
	for ch := 0; ch < 2; ch++ {
		if st.busy[ch] == 0 {
			continue
		}
		for i := 0; i < len(e.requests); i++ {
			if st.bMembers[ch]&(uint8(1)<<uint(i)) == 0 || st.cancelPending[i] != 0 {
				continue
			}
			if st.bFinish[ch] > e.requests[i].Deadline {
				return fail(&iWitness{
					at:         st.now,
					reason:     "non-preemptible running batch cannot finish before deadline",
					req:        i,
					deadline:   e.requests[i].Deadline,
					completion: st.bFinish[ch],
				})
			}
		}
	}

	if allTerminal {
		margin := vacuousMargin
		for i, r := range e.requests {
			if st.status[i] == stDone {
				m := r.Deadline - st.doneAt[i]
				if m < margin {
					margin = m
				}
			}
		}
		if margin < 0 {
			return fail(&iWitness{at: st.now, reason: "terminal completion misses a deadline"})
		}
		node := &iNode{at: st.now, done: true, doneMask: doneMask(st), margin: margin, doneAt: st.doneAt}
		res := &solveResult{node: node, margin: margin}
		e.memo[key] = res
		return res
	}

	type candidate struct {
		node   *iNode
		margin int
	}
	var best *candidate
	var proofs []iProof

	considerSuccess := func(node *iNode, margin int) {
		if best == nil || margin > best.margin || (margin == best.margin && lessNode(node, best.node)) {
			best = &candidate{node: node, margin: margin}
		}
	}

	// Wait/idle decision.
	if t, ok := e.nextEvent(st, worlds); ok {
		branches, ew := e.eventsAt(st, worlds, t)
		if ew != nil {
			proofs = append(proofs, iProof{
				desc: fmt.Sprintf("idle until boundary t=%d", t),
				fail: ew,
			})
		} else {
			margin := vacuousMargin
			for _, b := range branches {
				if b.margin < margin {
					margin = b.margin
				}
			}
			node := &iNode{at: st.now, branches: branches, margin: margin}
			considerSuccess(node, margin)
		}
	}

	// Exhaust every legal nonempty same-stage/same-shape batch on the first
	// free channel. Channel indices are symmetric, so enumerating both channel
	// identities would only duplicate states.
	ch := -1
	for c := 0; c < 2; c++ {
		if st.busy[c] == 0 {
			ch = c
			break
		}
	}
	if ch >= 0 {
		groups := map[string]uint8{}
		var groupKeys []string
		for i := 0; i < len(e.requests); i++ {
			var stage Stage
			switch st.status[i] {
			case stReady1:
				stage = Stage1
			case stReady2:
				stage = Stage2
			default:
				continue
			}
			k := fmt.Sprintf("%d|%s", stage, e.requests[i].Shape)
			if groups[k] == 0 {
				groupKeys = append(groupKeys, k)
			}
			groups[k] |= uint8(1) << uint(i)
		}
		sort.Strings(groupKeys)
		for _, gk := range groupKeys {
			groupMask := groups[gk]
			parts := strings.SplitN(gk, "|", 2)
			var stage Stage
			fmt.Sscanf(parts[0], "%d", &stage)
			shape := parts[1]
			for sub := groupMask; sub > 0; sub = (sub - 1) & groupMask {
				size := popcount8(sub)
				dur, ok := e.duration(stage, shape, size)
				if !ok {
					continue
				}
				ns := st
				ns.busy[ch] = 1
				ns.bStage[ch] = uint8(stage)
				ns.bShape[ch] = shape
				ns.bMembers[ch] = sub
				ns.bFinish[ch] = st.now + dur
				for i := 0; i < len(e.requests); i++ {
					if sub&(uint8(1)<<uint(i)) != 0 {
						if stage == Stage1 {
							ns.status[i] = stRunning1
						} else {
							ns.status[i] = stRunning2
						}
					}
				}
				action := &iAction{
					ch:       ch,
					stage:    stage,
					shape:    shape,
					members:  sub,
					start:    st.now,
					duration: dur,
					finish:   st.now + dur,
				}
				rr := e.solve(ns, worlds)
				if rr.node != nil {
					node := &iNode{at: st.now, action: action, then: rr.node, margin: rr.margin}
					considerSuccess(node, rr.margin)
				} else {
					proofs = append(proofs, iProof{
						desc:   e.actionDescription(action),
						action: action,
						fail:   rr.witness,
					})
				}
			}
		}
	}

	if best != nil {
		res := &solveResult{node: best.node, margin: best.margin}
		e.memo[key] = res
		return res
	}
	w := &iWitness{
		at:     st.now,
		reason: "every legal batch start and the idle/wait decision leads to a missed deadline on some allowed branch",
		proofs: proofs,
	}
	return fail(w)
}

func (e *engine) nextEvent(st state, worlds bits) (int, bool) {
	best := int(^uint(0) >> 1)
	found := false
	for ch := 0; ch < 2; ch++ {
		if st.busy[ch] != 0 && st.bFinish[ch] > st.now && st.bFinish[ch] < best {
			best = st.bFinish[ch]
			found = true
		}
	}
	for i, r := range e.requests {
		if st.status[i] == stUnreleased && r.Release > st.now && r.Release < best {
			best = r.Release
			found = true
		}
	}
	worlds.each(func(w int) {
		for _, c := range e.scenarios[w].cancels {
			if c.at > st.now && c.at < best {
				best = c.at
				found = true
			}
		}
	})
	return best, found
}

func (e *engine) eventsAt(st state, worlds bits, t int) ([]iBranch, *iWitness) {
	base := st
	base.now = t

	var obs1Mask uint8
	for ch := 0; ch < 2; ch++ {
		if base.busy[ch] == 0 || base.bFinish[ch] != t {
			continue
		}
		mask := base.bMembers[ch]
		stage := Stage(base.bStage[ch])
		for i := 0; i < len(e.requests); i++ {
			if mask&(uint8(1)<<uint(i)) == 0 {
				continue
			}
			if base.cancelPending[i] != 0 {
				base.status[i] = stCanceled
				continue
			}
			if stage == Stage2 {
				base.status[i] = stDone
				base.doneAt[i] = t
			} else {
				base.status[i] = stObs1
				obs1Mask |= uint8(1) << uint(i)
			}
		}
		base.busy[ch] = 0
		base.bStage[ch] = 0
		base.bShape[ch] = ""
		base.bMembers[ch] = 0
		base.bFinish[ch] = 0
	}

	for i, r := range e.requests {
		if base.status[i] == stUnreleased && r.Release <= t {
			base.status[i] = stReady1
		}
	}

	groups := map[obsKey]bits{}
	worlds.each(func(w int) {
		sc := e.scenarios[w]
		var cancelMask uint8
		for _, c := range sc.cancels {
			if c.at == t {
				cancelMask |= uint8(1) << uint(c.req)
			}
		}
		k := obsKey{
			time:             t,
			completedMask:    obs1Mask,
			continueMask:     obs1Mask & sc.continueMask &^ cancelMask,
			cancellationMask: cancelMask,
		}
		groups[k] = groups[k].with(w)
	})
	if len(groups) == 0 {
		// Defensive: an empty belief set should never be constructed.
		k := obsKey{time: t}
		groups[k] = worlds
	}

	keys := make([]obsKey, 0, len(groups))
	for k := range groups {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].time != keys[j].time {
			return keys[i].time < keys[j].time
		}
		if keys[i].completedMask != keys[j].completedMask {
			return keys[i].completedMask < keys[j].completedMask
		}
		if keys[i].continueMask != keys[j].continueMask {
			return keys[i].continueMask < keys[j].continueMask
		}
		return keys[i].cancellationMask < keys[j].cancellationMask
	})

	var branches []iBranch
	for _, k := range keys {
		ws := groups[k]
		child := base
		for i := 0; i < len(e.requests); i++ {
			bit := uint8(1) << uint(i)
			if k.completedMask&bit != 0 {
				if k.cancellationMask&bit != 0 {
					child.status[i] = stCanceled
					child.cancelPending[i] = 1
				} else if k.continueMask&bit != 0 {
					child.status[i] = stReady2
				} else {
					child.status[i] = stDone
					child.doneAt[i] = t
				}
			}
			if k.cancellationMask&bit != 0 && k.completedMask&bit == 0 {
				switch child.status[i] {
				case stReady1, stReady2, stUnreleased:
					child.status[i] = stCanceled
				case stRunning1, stRunning2:
					child.cancelPending[i] = 1
				case stObs1:
					child.status[i] = stCanceled
				}
			}
		}
		rr := e.solve(child, ws)
		if rr.witness != nil {
			wit := rr.witness
			if wit.obs == nil {
				kk := k
				wit.obs = &kk
				wit.worlds = ws
			}
			return nil, &iWitness{
				at:     t,
				obs:    &k,
				worlds: ws,
				reason: "an allowed joint observation at this boundary defeats this decision",
				child:  rr.witness,
			}
		}
		margin := rr.margin
		branches = append(branches, iBranch{obs: k, worlds: ws, next: rr.node, margin: margin})
	}
	return branches, nil
}

func popcount8(m uint8) int {
	n := 0
	for m != 0 {
		n++
		m &= m - 1
	}
	return n
}

func doneMask(st state) uint8 {
	var m uint8
	for i := 0; i < 8; i++ {
		if st.status[i] == stDone {
			m |= uint8(1) << uint(i)
		}
	}
	return m
}

func (e *engine) actionDescription(a *iAction) string {
	ids := maskIDs(a.members, e.requests)
	return fmt.Sprintf("start ch=%d stage=%d shape=%s ids=%v at=%d duration=%d finish=%d",
		a.ch, a.stage, a.shape, ids, a.start, a.duration, a.finish)
}

// lessNode provides a deterministic tie-break for equal maximin margin. It only
// needs to be a stable partial order; nil safety is handled by callers.
func lessNode(a, b *iNode) bool {
	if a == nil || b == nil {
		return a != nil
	}
	if a.at != b.at {
		return a.at < b.at
	}
	if (a.action == nil) != (b.action == nil) {
		return a.action != nil
	}
	if a.action != nil && b.action != nil && a.action.members != b.action.members {
		return a.action.members < b.action.members
	}
	return false
}
