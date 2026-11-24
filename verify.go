package egress

import (
	"fmt"
	"sort"
)

// Verify checks the returned policy tree:
//   - every shared action is legal in every candidate true table,
//   - observation branches partition their candidate scenarios,
//   - branch timing is not before the hardware latch,
//   - no root-to-leaf path uses more than two timestamp observations.
func (p *Policy) Verify() error {
	if p == nil || p.Root == nil {
		return fmt.Errorf("nil policy")
	}
	scenarioByName := map[string]Scenario{}
	for _, sc := range p.Scenarios {
		scenarioByName[sc.Name] = sc
	}
	return p.verifyNode(p.Root, scenarioByName, map[string]bool{}, 0)
}

func (p *Policy) verifyNode(n *PolicyNode, scs map[string]Scenario, seen map[string]bool, observations int) error {
	if n == nil {
		return nil
	}
	candidates := map[string]bool{}
	for _, name := range n.Candidates {
		if _, ok := scs[name]; !ok {
			return fmt.Errorf("node %s references unknown scenario %q", n.ID, name)
		}
		candidates[name] = true
	}

	switch n.Kind {
	case "action":
		if n.Action == nil {
			return fmt.Errorf("node %s action has nil fragment", n.ID)
		}
		for _, name := range n.Candidates {
			sc := scs[name]
			a := n.Action
			if !sc.Table.FitsAt(a.Start, a.Data, a.Overhead, a.Class) {
				return fmt.Errorf("shared action %s [%d,%d) is not legal in candidate %q", a.FrameID, a.Start, a.End, name)
			}
		}
		return p.verifyNode(n.Next, scs, seen, observations)
	case "wait":
		if n.WaitUntil < n.At {
			return fmt.Errorf("node %s waits backwards", n.ID)
		}
		return p.verifyNode(n.Next, scs, seen, observations)
	case "observe":
		if n.Observation == nil {
			return fmt.Errorf("node %s observation lacks event", n.ID)
		}
		if n.Observation.BranchAt < n.Observation.LatchedAt {
			return fmt.Errorf("node %s branches before observation latch", n.ID)
		}
		if observations+1 > 2 {
			return fmt.Errorf("node %s exceeds two observations on one path", n.ID)
		}
		union := map[string]bool{}
		stamps := map[int]bool{}
		for _, b := range n.Branches {
			if stamps[b.Stamp] {
				return fmt.Errorf("node %s has duplicate stamp %d", n.ID, b.Stamp)
			}
			stamps[b.Stamp] = true
			for _, name := range b.Candidates {
				if !candidates[name] {
					return fmt.Errorf("branch stamp %d contains %q outside node candidates", b.Stamp, name)
				}
				if union[name] {
					return fmt.Errorf("scenario %q appears in multiple observation branches", name)
				}
				union[name] = true
			}
			if err := p.verifyNode(b.Next, scs, seen, observations+1); err != nil {
				return err
			}
		}
		if len(union) != len(candidates) {
			return fmt.Errorf("node %s branches do not partition candidates", n.ID)
		}
		return nil
	case "leaf", "terminal":
		return nil
	case "infeasible":
		return fmt.Errorf("node %s is infeasible: %s", n.ID, n.Reason)
	default:
		return fmt.Errorf("node %s has unknown kind %q", n.ID, n.Kind)
	}
}

// SharedActionTable returns a deterministic description of nodes at which the
// same observation history must use one action. Tests and reviewers can use it
// without walking unexported tree details.
func (p *Policy) SharedActionTable() []string {
	var out []string
	var walk func(*PolicyNode, string)
	walk = func(n *PolicyNode, prefix string) {
		if n == nil {
			return
		}
		switch n.Kind {
		case "action":
			a := n.Action
			out = append(out, fmt.Sprintf("%snode=%s at=%d candidates=%v action=%s/%d data=%d [%d,%d)",
				prefix, n.ID, n.At, n.Candidates, a.FrameID, a.Index, a.Data, a.Start, a.End))
			walk(n.Next, prefix+"  ")
		case "wait":
			out = append(out, fmt.Sprintf("%snode=%s wait until=%d candidates=%v", prefix, n.ID, n.WaitUntil, n.Candidates))
			walk(n.Next, prefix+"  ")
		case "observe":
			out = append(out, fmt.Sprintf("%snode=%s observe probe=%d latch=%d branch=%d branches=%d",
				prefix, n.ID, n.Observation.ProbeIndex, n.Observation.LatchedAt, n.Observation.BranchAt, len(n.Branches)))
			branches := append([]PolicyBranch{}, n.Branches...)
			sort.Slice(branches, func(i, j int) bool { return branches[i].Stamp < branches[j].Stamp })
			for _, b := range branches {
				walk(b.Next, prefix+fmt.Sprintf("stamp=%d ", b.Stamp))
			}
		default:
			out = append(out, fmt.Sprintf("%snode=%s kind=%s candidates=%v", prefix, n.ID, n.Kind, n.Candidates))
		}
	}
	walk(p.Root, "")
	return out
}
