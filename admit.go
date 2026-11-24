package admit

import (
	"fmt"
	"sort"
)

// Admit solves the admission problem with the safe cancellation barrier.
func Admit(p Problem) (*Result, error) {
	return AdmitWithOptions(p, Options{})
}

// AdmitWithOptions solves admission using explicit options.
func AdmitWithOptions(p Problem, opts Options) (*Result, error) {
	o := opts.withDefaults()
	e, err := newEngine(p, o)
	if err != nil {
		return nil, err
	}

	var committed uint8
	var candidates []int
	for i, r := range e.requests {
		if r.Committed {
			committed |= uint8(1) << uint(i)
		} else {
			candidates = append(candidates, i)
		}
	}

	committedRes, err := e.solveSet(committed)
	if err != nil {
		return nil, err
	}
	committedIDs := maskIDs(committed, e.requests)
	if committedRes.node == nil {
		w := convertWitness(e, committedRes.witness, committedIDs)
		return &Result{
			Feasible: false,
			Accepted: committedIDs,
			Witness:  w,
		}, nil
	}

	type bestState struct {
		mask   uint8
		profit int
		result *solveResult
	}
	best := bestState{mask: committed, profit: 0, result: committedRes}

	for subset := 0; subset < (1 << len(candidates)); subset++ {
		mask := committed
		profit := 0
		newIDs := []string{}
		for j, idx := range candidates {
			if subset&(1<<uint(j)) != 0 {
				mask |= uint8(1) << uint(idx)
				profit += e.requests[idx].Profit
				newIDs = append(newIDs, e.requests[idx].ID)
			}
		}
		rr, err := e.solveSet(mask)
		if err != nil {
			return nil, err
		}
		if rr.node == nil {
			continue
		}
		if profit > best.profit || (profit == best.profit && rr.margin > best.result.margin) {
			best = bestState{mask: mask, profit: profit, result: rr}
		}
	}

	accepted := maskIDs(best.mask, e.requests)
	newly := maskIDs(best.mask&^committed, e.requests)
	rejected := []string{}
	for _, idx := range candidates {
		if best.mask&(uint8(1)<<uint(idx)) == 0 {
			rejected = append(rejected, e.requests[idx].ID)
		}
	}
	plan := &Plan{Root: convertNode(e, best.result.node, best.mask), Accepted: maskIDs(best.mask, e.requests), WorstMargin: normalizeMargin(best.result.margin)}
	margins, err := planMargins(p, plan)
	if err != nil {
		return nil, err
	}
	plan.WorstMargin = margins.Worst
	worst := best.result.margin
	if margins != nil {
		worst = margins.Worst
	}
	return &Result{
		Feasible:           true,
		Accepted:           accepted,
		NewlyAccepted:      newly,
		RejectedCandidates: rejected,
		TotalProfit:        best.profit,
		OptimalMargin:      normalizeMargin(worst),
		Margins:            margins,
		Plan:               plan,
	}, nil
}

// EvaluateSet evaluates exactly the supplied fixed set of request IDs.
func EvaluateSet(p Problem, ids []string) (*Verdict, error) {
	return EvaluateSetWithOptions(p, ids, Options{})
}

// EvaluateSetWithOptions evaluates exactly the supplied set with options.
func EvaluateSetWithOptions(p Problem, ids []string, opts Options) (*Verdict, error) {
	o := opts.withDefaults()
	e, err := newEngine(p, o)
	if err != nil {
		return nil, err
	}
	mask, err := e.idMask(ids)
	if err != nil {
		return nil, err
	}
	rr, err := e.solveSet(mask)
	if err != nil {
		return nil, err
	}
	setIDs := maskIDs(mask, e.requests)
	if rr.node == nil {
		w := convertWitness(e, rr.witness, setIDs)
		return &Verdict{Set: setIDs, Feasible: false, Witness: w}, nil
	}
	plan := &Plan{Root: convertNode(e, rr.node, mask), Accepted: setIDs, WorstMargin: normalizeMargin(rr.margin)}
	margins, err := planMargins(p, plan)
	if err != nil {
		return nil, err
	}
	plan.WorstMargin = margins.Worst
	worst := normalizeMargin(rr.margin)
	if margins != nil {
		worst = margins.Worst
	}
	return &Verdict{
		Set:           setIDs,
		Feasible:      true,
		OptimalMargin: worst,
		Margins:       margins,
		Plan:          plan,
	}, nil
}

// EvaluateCandidates evaluates committed requests plus the supplied new IDs.
func EvaluateCandidates(p Problem, candidateIDs []string) (*Verdict, error) {
	e, err := newEngine(p, Options{})
	if err != nil {
		return nil, err
	}
	mask := uint8(0)
	for i, r := range e.requests {
		if r.Committed {
			mask |= uint8(1) << uint(i)
		}
	}
	add, err := e.idMask(candidateIDs)
	if err != nil {
		return nil, err
	}
	mask |= add
	ids := maskIDs(mask, e.requests)
	return EvaluateSet(p, ids)
}

func normalizeMargin(m int) int {
	if m >= vacuousMargin/2 {
		return 0
	}
	return m
}

func convertNode(e *engine, in *iNode, active uint8) *PlanNode {
	if in == nil {
		return nil
	}
	out := &PlanNode{At: in.at}
	if in.action != nil {
		out.Start = convertAction(e, in.action)
		out.Then = convertNode(e, in.then, active)
	}
	if in.done {
		completion := map[string]int{}
		for i := 0; i < len(e.requests); i++ {
			if in.doneMask&(uint8(1)<<uint(i)) != 0 {
				completion[e.requests[i].ID] = in.doneAt[i]
			}
		}
		margin := normalizeMargin(in.margin)
		out.Leaf = &Leaf{CompletionAt: completion, Margin: margin}
	}
	for _, b := range in.branches {
		obs := obsToPublic(e, b.obs)
		scenarioNames := worldsNames(e, b.worlds)
		pb := PlanBranch{Observation: obs, Scenarios: scenarioNames, Next: convertNode(e, b.next, active)}
		out.Branches = append(out.Branches, pb)
	}
	return out
}

func convertAction(e *engine, a *iAction) *BatchAction {
	return &BatchAction{
		Channel:    a.ch,
		Stage:      a.stage,
		Shape:      a.shape,
		RequestIDs: maskIDs(a.members, e.requests),
		Start:      a.start,
		Duration:   a.duration,
		Finish:     a.finish,
	}
}

func obsToPublic(e *engine, k obsKey) Observation {
	completed := maskIDs(k.completedMask, e.requests)
	continueIDs := maskIDs(k.continueMask&^k.cancellationMask, e.requests)
	earlyMask := k.completedMask &^ k.continueMask &^ k.cancellationMask
	early := maskIDs(earlyMask, e.requests)
	canceled := maskIDs(k.cancellationMask, e.requests)
	sort.Strings(completed)
	sort.Strings(continueIDs)
	sort.Strings(early)
	sort.Strings(canceled)
	return Observation{
		Time:                  k.time,
		Stage1Completed:       completed,
		ContinueToStage2:      continueIDs,
		FinishAfterStage1:     early,
		RevealedCancellations: canceled,
	}
}

func worldsNames(e *engine, ws bits) []string {
	var names []string
	ws.each(func(i int) {
		if i < len(e.scenarios) {
			names = append(names, e.scenarios[i].name)
		}
	})
	sort.Strings(names)
	return names
}

func convertWitness(e *engine, iw *iWitness, set []string) *Witness {
	if iw == nil {
		return &Witness{Set: set, Summary: "infeasible", Root: &WitnessNode{Reason: "no witness details"}}
	}
	root := convertWitnessNode(e, iw)
	return &Witness{
		Set:     set,
		Summary: iw.reason,
		Root:    root,
	}
}

func convertWitnessNode(e *engine, iw *iWitness) *WitnessNode {
	if iw == nil {
		return nil
	}
	w := &WitnessNode{
		At:         iw.at,
		Reason:     iw.reason,
		Scenarios:  worldsNames(e, iw.worlds),
		Deadline:   iw.deadline,
		Completion: iw.completion,
		Child:      convertWitnessNode(e, iw.child),
	}
	if iw.req >= 0 && iw.req < len(e.requests) {
		w.RequestID = e.requests[iw.req].ID
	}
	if iw.obs != nil {
		o := obsToPublic(e, *iw.obs)
		w.Observation = &o
	}
	for _, p := range iw.proofs {
		dp := DecisionProof{Decision: p.desc, Failure: convertWitnessNode(e, p.fail)}
		if p.action != nil {
			dp.Action = convertAction(e, p.action)
		}
		w.DecisionProofs = append(w.DecisionProofs, dp)
	}
	return w
}

// VerifyWitness checks structural witness completeness. Because witnesses are
// produced by the exhaustive solver, a non-nil root with either a terminal
// deadline conflict or decision proofs is considered a well-formed forcing
// proof. Tests additionally replay known branches through VirtualDriver.
func VerifyWitness(w *Witness) error {
	if w == nil || w.Root == nil {
		return fmt.Errorf("missing witness")
	}
	return verifyWitnessNode(w.Root)
}

func verifyWitnessNode(n *WitnessNode) error {
	if n == nil {
		return fmt.Errorf("nil witness node")
	}
	if n.RequestID != "" && n.Completion > n.Deadline {
		return nil
	}
	if len(n.DecisionProofs) > 0 {
		for _, dp := range n.DecisionProofs {
			if dp.Failure == nil {
				return fmt.Errorf("decision %q has no failure branch", dp.Decision)
			}
			if err := verifyWitnessNode(dp.Failure); err != nil {
				return err
			}
		}
		return nil
	}
	if n.Child != nil {
		return verifyWitnessNode(n.Child)
	}
	// A reason-only leaf is acceptable when it describes an exhaustive state
	// such as missing legal duration or a passed deadline.
	if n.Reason != "" {
		return nil
	}
	return fmt.Errorf("witness node at %d has no proof content", n.At)
}
