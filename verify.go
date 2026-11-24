package dataflow

import (
	"fmt"
)

// DeadlockReport is the shortest trajectory found to a state with no
// executable action before all operators finished, together with what every
// blocked operator is waiting for.
type DeadlockReport struct {
	Config   Config    `json:"config"`
	Length   int       `json:"length"`
	Trace    []Record  `json:"trace"`
	Blockers []Blocker `json:"blockers"`
	Terminal Snapshot  `json:"terminal"`
}

// Verdict is the result of Verify for one configuration.
type Verdict struct {
	Config        Config          `json:"config"`
	Robust        bool            `json:"robust"`
	StatesVisited int             `json:"states_visited"`
	TotalSteps    int             `json:"total_steps"`
	Deadlock      *DeadlockReport `json:"deadlock,omitempty"`
}

type parentInfo struct {
	parentKey string
	record    Record
}

// Verify exhaustively explores every legal operator interleaving for cfg.
//
// Because every instruction sequence is straight-line, each executed action
// strictly advances one program counter and every complete run executes
// exactly sum(len(program)) actions. Verify therefore checks the required
// property: every maximal scheduling reaches completion ("所有能推进的调度
// 都能跑完"). If any reachable state has no enabled action while work
// remains, the verdict is non-robust and Deadlock carries a globally
// shortest trace to such a state plus each blocker's awaited resource.
func Verify(g *Graph, programs map[OpID][]Instr, cfg Config) (*Verdict, error) {
	ops, edgeOrder, err := validateRequest(g, programs, &cfg, false)
	if err != nil {
		return nil, err
	}
	totalSteps := 0
	for _, op := range ops {
		totalSteps += len(programs[op])
	}

	root, err := newDriver(g, programs, cfg, false)
	if err != nil {
		return nil, err
	}
	rootSnap := root.Export()
	rootKey := rootSnap.CanonicalKey(edgeOrder)

	parents := map[string]parentInfo{rootKey: {}}
	visited := map[string]bool{rootKey: true}

	type queueNode struct {
		key  string
		snap Snapshot
	}
	queue := []queueNode{{key: rootKey, snap: rootSnap}}
	states := 0

	for len(queue) > 0 {
		node := queue[0]
		queue = queue[1:]
		states++

		d, err := newFromSnapshot(g, programs, node.snap, false)
		if err != nil {
			return nil, fmt.Errorf("dataflow: verifier internal snapshot error: %w", err)
		}
		enabled := d.Enabled()
		if len(enabled) == 0 {
			if d.AllDone() {
				continue // terminal success
			}
			// First deadlock popped by BFS is globally shortest.
			trace := reconstructTrace(parents, node.key)
			rep := &DeadlockReport{
				Config:   cfg.clone(),
				Length:   len(trace),
				Trace:    trace,
				Blockers: d.Blockers(),
				Terminal: d.Export(),
			}
			return &Verdict{Config: cfg.clone(), Robust: false, StatesVisited: states, TotalSteps: totalSteps, Deadlock: rep}, nil
		}

		for _, a := range enabled {
			child, err := newFromSnapshot(g, programs, node.snap, false)
			if err != nil {
				return nil, err
			}
			rec, err := child.Step(a.Op)
			if err != nil {
				// Enabled() and Step() evaluate the same predicate under the
				// same locked state, so this cannot happen.
				return nil, fmt.Errorf("dataflow: verifier enabled step failed: %w", err)
			}
			cs := child.Export()
			ck := cs.CanonicalKey(edgeOrder)
			if !visited[ck] {
				visited[ck] = true
				parents[ck] = parentInfo{parentKey: node.key, record: *rec}
				queue = append(queue, queueNode{key: ck, snap: cs})
			}
		}
	}

	return &Verdict{Config: cfg.clone(), Robust: true, StatesVisited: states, TotalSteps: totalSteps}, nil
}

// reconstructTrace walks the BFS parent chain and reverses it.
func reconstructTrace(parents map[string]parentInfo, key string) []Record {
	var rev []Record
	for {
		p, ok := parents[key]
		if !ok || p.parentKey == "" {
			break
		}
		rev = append(rev, p.record)
		key = p.parentKey
	}
	trace := make([]Record, 0, len(rev))
	for i := len(rev) - 1; i >= 0; i-- {
		trace = append(trace, rev[i])
	}
	// Re-number sequentially for a clean standalone trace.
	for i := range trace {
		trace[i].Seq = i + 1
	}
	return trace
}
