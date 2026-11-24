package dataflow

import (
	"fmt"
	"sort"
)

// IntRange is an inclusive integer capacity range [Min, Max].
type IntRange struct {
	Min int `json:"min"`
	Max int `json:"max"`
}

func (r IntRange) values() ([]int, error) {
	if r.Min < 0 {
		return nil, fmt.Errorf("dataflow: capacity range min must be >= 0, got %d", r.Min)
	}
	if r.Max < r.Min {
		return nil, fmt.Errorf("dataflow: invalid capacity range [%d,%d]", r.Min, r.Max)
	}
	out := make([]int, 0, r.Max-r.Min+1)
	for v := r.Min; v <= r.Max; v++ {
		out = append(out, v)
	}
	return out, nil
}

// Weights are the given unit costs in the objective
//
//	WPool*Pool + sum_e WEdge[e]*EdgeCaps[e].
//
// Nil pointer fields default to 1; explicit zero weights are honored (and
// naturally create cost ties).
type Weights struct {
	Pool        *int64           `json:"pool,omitempty"`
	EdgeDefault *int64           `json:"edge_default,omitempty"`
	Edge        map[EdgeID]int64 `json:"edge,omitempty"`
}

func (w Weights) poolWeight() int64 {
	if w.Pool == nil {
		return 1
	}
	return *w.Pool
}

func (w Weights) edgeWeight(e EdgeID) int64 {
	if v, ok := w.Edge[e]; ok {
		return v
	}
	if w.EdgeDefault == nil {
		return 1
	}
	return *w.EdgeDefault
}

// ConfigRequest is the input of Configure.
type ConfigRequest struct {
	Graph    *Graph
	Programs map[OpID][]Instr

	// Pool is the allowed shared-pool capacity range.
	Pool IntRange
	// Edges gives an inclusive range per edge. Every graph edge must have an
	// entry unless DefaultEdge is non-nil.
	Edges map[EdgeID]IntRange
	// DefaultEdge is the range used for edges absent from Edges.
	DefaultEdge *IntRange

	// Budget optionally caps the total slot count Pool + sum edge caps. Nil
	// means unlimited.
	Budget *int

	// CostBudget optionally caps the weighted objective
	// WPool*Pool + sum WEdge[e]*cap_e. It covers interpretations where the
	// "total budget" is itself a weighted cost ceiling. Nil means unlimited.
	CostBudget *int64

	// Weights for the minimization objective.
	Weights Weights
}

// ScoredConfig is a configuration together with its weighted cost.
type ScoredConfig struct {
	Config Config `json:"config"`
	Cost   int64  `json:"cost"`
}

// AttemptSummary records what the search knew about one candidate.
type AttemptSummary struct {
	Config           Config `json:"config"`
	Cost             int64  `json:"cost"`
	BudgetTotal      int    `json:"budget_total"`
	Robust           bool   `json:"robust"`
	StatesVisited    int    `json:"states_visited"`
	ShortestDeadlock int    `json:"shortest_deadlock"` // -1 when robust
}

// ConfigResult is the outcome of Configure.
//
// If Feasible is true, Best is the cost-minimal robust configuration, with
// ties broken by ascending edge number (lexicographic edge-capacity vector,
// smallest first difference wins), then pool capacity.
//
// If Feasible is false, Diagnostic is the globally shortest deadlock trace
// among all budget-feasible candidates, together with its configuration and
// per-blocker resource waits.
type ConfigResult struct {
	Feasible   bool             `json:"feasible"`
	Candidates int              `json:"candidates"`
	Best       *ScoredConfig    `json:"best,omitempty"`
	Diagnostic *DeadlockReport  `json:"diagnostic,omitempty"`
	Attempts   []AttemptSummary `json:"attempts,omitempty"`
}

// CostOf computes WPool*Pool + sum_e WEdge[e]*cap_e in edge-number order.
func CostOf(cfg Config, edgeOrder []EdgeID, w Weights) int64 {
	cost := w.poolWeight() * int64(cfg.Pool)
	for _, e := range edgeOrder {
		cost += w.edgeWeight(e) * int64(cfg.EdgeCaps[e])
	}
	return cost
}

// CompareConfigs implements the full deterministic preference order:
// weighted cost ascending, then lexicographic edge capacities in ascending
// edge-number order (smaller capacity at the first differing edge wins),
// then pool capacity. Returns -1 if a is preferred, 0 if equal.
func CompareConfigs(a, b Config, edgeOrder []EdgeID, w Weights) int {
	ca, cb := CostOf(a, edgeOrder, w), CostOf(b, edgeOrder, w)
	if ca < cb {
		return -1
	}
	if ca > cb {
		return 1
	}
	for _, e := range edgeOrder {
		if a.EdgeCaps[e] != b.EdgeCaps[e] {
			if a.EdgeCaps[e] < b.EdgeCaps[e] {
				return -1
			}
			return 1
		}
	}
	if a.Pool != b.Pool {
		if a.Pool < b.Pool {
			return -1
		}
		return 1
	}
	return 0
}

// Configure enumerates every allowed capacity combination, keeps those
// satisfying Budget, verifies robustness against all interleavings, and
// returns the optimal robust setting or a shortest deadlock diagnostic.
func Configure(req ConfigRequest) (*ConfigResult, error) {
	g := req.Graph
	// Structural validation with a placeholder config happens per candidate;
	// first validate graph/programs once (no config, no operator cap).
	if _, _, err := validateRequest(g, req.Programs, nil, false); err != nil {
		return nil, err
	}
	edgeOrder := sortedEdgeOrder(g)

	poolVals, err := req.Pool.values()
	if err != nil {
		return nil, err
	}
	edgeRanges := make([]IntRange, len(edgeOrder))
	minSum := req.Pool.Min
	for idx, e := range edgeOrder {
		r, ok := req.Edges[e]
		if !ok {
			if req.DefaultEdge == nil {
				return nil, &ProgramError{Msg: fmt.Sprintf("no capacity range for edge %d and no DefaultEdge", e)}
			}
			r = *req.DefaultEdge
		}
		if _, err := r.values(); err != nil {
			return nil, err
		}
		edgeRanges[idx] = r
		minSum += r.Min
	}

	if req.Budget != nil && minSum > *req.Budget {
		return nil, &NoCandidateError{MinRequired: minSum, Budget: *req.Budget}
	}

	// Validate weights and pre-check the weighted cost budget against the
	// coordinatewise-minimum configuration.
	if req.Weights.poolWeight() < 0 {
		return nil, &ProgramError{Msg: "pool weight must be >= 0"}
	}
	minCfg := Config{Pool: req.Pool.Min, EdgeCaps: map[EdgeID]int{}}
	for idx, e := range edgeOrder {
		if req.Weights.edgeWeight(e) < 0 {
			return nil, &ProgramError{Msg: fmt.Sprintf("weight for edge %d must be >= 0", e)}
		}
		minCfg.EdgeCaps[e] = edgeRanges[idx].Min
	}
	minCost := CostOf(minCfg, edgeOrder, req.Weights)
	if req.CostBudget != nil && minCost > *req.CostBudget {
		return nil, &CostBudgetError{MinCost: minCost, CostBudget: *req.CostBudget}
	}

	res := &ConfigResult{}
	var bestRobust *ScoredConfig
	var bestDiag *DeadlockReport

	// Enumerate edge capacity Cartesian product, then pool values. Edge
	// recursion follows ascending edge numbers.
	edgeVals := make([][]int, len(edgeOrder))
	for i, r := range edgeRanges {
		vs, err := r.values()
		if err != nil {
			return nil, err
		}
		edgeVals[i] = vs
	}

	caps := make([]int, len(edgeOrder))
	var enumErr error

	evaluate := func(pool int) {
		cfg := Config{Pool: pool, EdgeCaps: map[EdgeID]int{}}
		total := pool
		for i, e := range edgeOrder {
			cfg.EdgeCaps[e] = caps[i]
			total += caps[i]
		}
		if req.Budget != nil && total > *req.Budget {
			return
		}
		cost := CostOf(cfg, edgeOrder, req.Weights)
		if req.CostBudget != nil && cost > *req.CostBudget {
			return
		}
		res.Candidates++
		v, err := Verify(g, req.Programs, cfg)
		if err != nil {
			enumErr = err
			return
		}
		sum := AttemptSummary{
			Config:           cfg.clone(),
			Cost:             cost,
			BudgetTotal:      total,
			Robust:           v.Robust,
			StatesVisited:    v.StatesVisited,
			ShortestDeadlock: -1,
		}
		if v.Robust {
			if bestRobust == nil || CompareConfigs(cfg, bestRobust.Config, edgeOrder, req.Weights) < 0 {
				bestRobust = &ScoredConfig{Config: cfg.clone(), Cost: cost}
			}
		} else {
			dl := v.Deadlock
			sum.ShortestDeadlock = dl.Length
			if bestDiag == nil ||
				dl.Length < bestDiag.Length ||
				(dl.Length == bestDiag.Length &&
					CompareConfigs(cfg, bestDiag.Config, edgeOrder, req.Weights) < 0) {
				cp := *dl
				cp.Trace = append([]Record(nil), dl.Trace...)
				cp.Blockers = append([]Blocker(nil), dl.Blockers...)
				bestDiag = &cp
			}
		}
		res.Attempts = append(res.Attempts, sum)
	}

	var recurseEdges func(i int)
	recurseEdges = func(i int) {
		if enumErr != nil {
			return
		}
		if i == len(edgeOrder) {
			for _, p := range poolVals {
				evaluate(p)
				if enumErr != nil {
					return
				}
			}
			return
		}
		for _, v := range edgeVals[i] {
			caps[i] = v
			recurseEdges(i + 1)
		}
	}
	recurseEdges(0)
	if enumErr != nil {
		return nil, enumErr
	}

	if bestRobust != nil {
		res.Feasible = true
		res.Best = bestRobust
	} else {
		res.Feasible = false
		res.Diagnostic = bestDiag
	}
	return res, nil
}

func sortedEdgeOrder(g *Graph) []EdgeID {
	set := map[EdgeID]bool{}
	for _, e := range g.Edges {
		set[e.ID] = true
	}
	out := make([]EdgeID, 0, len(set))
	for id := range set {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}
