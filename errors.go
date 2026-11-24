package dataflow

import (
	"errors"
	"fmt"
	"strings"
)

// Sentinel errors.
var (
	// ErrOperatorDone is returned by Step when the operator's finite program
	// has already been fully executed.
	ErrOperatorDone = errors.New("dataflow: operator program already complete")
	// ErrNoEnabledAction is returned by StepAny when the execution has
	// reached a state with no executable action before all operators finish.
	ErrNoEnabledAction = errors.New("dataflow: no executable action (deadlock)")
)

// ProgramError means a graph/program/config specification is invalid. It is
// a permanent error independent of scheduling.
type ProgramError struct {
	Op  OpID
	Msg string
	Err error
}

func (e *ProgramError) Error() string {
	var b strings.Builder
	b.WriteString("dataflow: invalid program")
	if e.Op != "" {
		fmt.Fprintf(&b, " for operator %q", e.Op)
	}
	if e.Msg != "" {
		b.WriteString(": ")
		b.WriteString(e.Msg)
	}
	if e.Err != nil {
		b.WriteString(": ")
		b.WriteString(e.Err.Error())
	}
	return b.String()
}

func (e *ProgramError) Unwrap() error { return e.Err }

// BlockedError is returned by Driver.Step when the chosen operator's next
// instruction currently cannot execute. The driver state is unchanged.
type BlockedError struct {
	Op         OpID
	InstrIndex int
	Instr      Instr
	Waits      []Wait
}

func (e *BlockedError) Error() string {
	parts := make([]string, 0, len(e.Waits))
	for _, w := range e.Waits {
		switch w.Resource {
		case ResourcePoolSlot:
			parts = append(parts, fmt.Sprintf("pool slot (%d/%d used)", w.Have, w.Cap))
		case ResourceEdgeSlot:
			parts = append(parts, fmt.Sprintf("free slot on edge %d (%d/%d used)", w.Edge, w.Have, w.Cap))
		case ResourceEdgeItem:
			parts = append(parts, fmt.Sprintf("a block on empty edge %d", w.Edge))
		}
	}
	return fmt.Sprintf("dataflow: operator %q blocked at instruction %d (%s): waiting for %s",
		e.Op, e.InstrIndex, e.Instr.Kind, strings.Join(parts, "; "))
}

// AsBlocked is a convenience errors.As wrapper.
func AsBlocked(err error) (*BlockedError, bool) {
	var be *BlockedError
	if errors.As(err, &be) {
		return be, true
	}
	return nil, false
}

// NoCandidateError means no capacity combination inside the allowed ranges
// satisfies the total budget at all.
type NoCandidateError struct {
	// MinRequired is the smallest possible P + sum(edge caps).
	MinRequired int
	Budget      int
}

func (e *NoCandidateError) Error() string {
	return fmt.Sprintf("dataflow: total budget %d is below minimum required slots %d", e.Budget, e.MinRequired)
}

// CostBudgetError means even the cheapest allowed configuration exceeds the
// optional weighted cost budget.
type CostBudgetError struct {
	MinCost    int64
	CostBudget int64
}

func (e *CostBudgetError) Error() string {
	return fmt.Sprintf("dataflow: minimum weighted cost %d exceeds cost budget %d", e.MinCost, e.CostBudget)
}

// Validate checks graph topology, operator programs (straight-line handle
// def/use) and a configuration. programs may be nil; a missing program means
// an immediately completed operator. When checkDriverLimit is true the
// five-operator step-driver cap is enforced.
func validateRequest(g *Graph, programs map[OpID][]Instr, cfg *Config, checkDriverLimit bool) ([]OpID, []EdgeID, error) {
	if g == nil {
		return nil, nil, &ProgramError{Msg: "nil graph"}
	}
	if len(g.Ops) == 0 {
		return nil, nil, &ProgramError{Msg: "graph has no operators"}
	}
	seenOp := map[OpID]bool{}
	for _, op := range g.Ops {
		if op == "" {
			return nil, nil, &ProgramError{Msg: "operator id must not be empty"}
		}
		if seenOp[op] {
			return nil, nil, &ProgramError{Op: op, Msg: "duplicate operator id"}
		}
		seenOp[op] = true
	}
	if checkDriverLimit && len(g.Ops) > MaxOperators {
		return nil, nil, &ProgramError{Msg: fmt.Sprintf("step driver supports at most %d operators, got %d", MaxOperators, len(g.Ops))}
	}

	edgeSeen := map[EdgeID]bool{}
	edgeOrder := make([]EdgeID, 0, len(g.Edges))
	for _, e := range g.Edges {
		if edgeSeen[e.ID] {
			return nil, nil, &ProgramError{Msg: fmt.Sprintf("duplicate edge id %d", e.ID)}
		}
		edgeSeen[e.ID] = true
		edgeOrder = append(edgeOrder, e.ID)
		if e.From == "" || e.To == "" {
			return nil, nil, &ProgramError{Msg: fmt.Sprintf("edge %d has empty endpoint", e.ID)}
		}
		if !seenOp[e.From] {
			return nil, nil, &ProgramError{Msg: fmt.Sprintf("edge %d producer %q is not in graph", e.ID, e.From)}
		}
		if !seenOp[e.To] {
			return nil, nil, &ProgramError{Msg: fmt.Sprintf("edge %d consumer %q is not in graph", e.ID, e.To)}
		}
	}

	for _, op := range g.Ops {
		prog := programs[op]
		bound := map[Handle]bool{}
		for i, in := range prog {
			switch in.Kind {
			case InstrTake:
				e, ok := g.edgeByID(in.Edge)
				if !ok {
					return nil, nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d: take from unknown edge %d", i, in.Edge)}
				}
				if e.To != op {
					return nil, nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d: edge %d consumer is %q, not %q", i, in.Edge, e.To, op)}
				}
				if in.Into == "" {
					return nil, nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d: take requires Into handle", i)}
				}
				if bound[in.Into] {
					return nil, nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d: handle %q already bound (release it first)", i, in.Into)}
				}
				bound[in.Into] = true
			case InstrAlloc:
				if in.As == "" {
					return nil, nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d: alloc requires As handle", i)}
				}
				if bound[in.As] {
					return nil, nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d: handle %q already bound (release it first)", i, in.As)}
				}
				bound[in.As] = true
			case InstrPublish:
				if in.Handle == "" {
					return nil, nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d: publish requires Handle", i)}
				}
				if !bound[in.Handle] {
					return nil, nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d: publish of unbound handle %q", i, in.Handle)}
				}
				if len(in.Targets) == 0 {
					return nil, nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d: publish requires at least one target edge", i)}
				}
				tseen := map[EdgeID]bool{}
				for _, tid := range in.Targets {
					if tseen[tid] {
						return nil, nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d: duplicate target edge %d in fanout", i, tid)}
					}
					tseen[tid] = true
					e, ok := g.edgeByID(tid)
					if !ok {
						return nil, nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d: publish to unknown edge %d", i, tid)}
					}
					if e.From != op {
						return nil, nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d: edge %d producer is %q, not %q", i, tid, e.From, op)}
					}
				}
			case InstrRelease:
				if in.Handle == "" {
					return nil, nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d: release requires Handle", i)}
				}
				if !bound[in.Handle] {
					return nil, nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d: release of unbound handle %q", i, in.Handle)}
				}
				delete(bound, in.Handle)
			default:
				return nil, nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d: unknown kind %q", i, in.Kind)}
			}
		}
	}

	if cfg != nil {
		if cfg.Pool < 0 {
			return nil, nil, &ProgramError{Msg: fmt.Sprintf("pool capacity must be >= 0, got %d", cfg.Pool)}
		}
		for _, id := range edgeOrder {
			c, ok := cfg.EdgeCaps[id]
			if !ok {
				return nil, nil, &ProgramError{Msg: fmt.Sprintf("missing capacity for edge %d", id)}
			}
			if c < 0 {
				return nil, nil, &ProgramError{Msg: fmt.Sprintf("capacity of edge %d must be >= 0, got %d", id, c)}
			}
		}
		for id := range cfg.EdgeCaps {
			if !edgeSeen[id] {
				return nil, nil, &ProgramError{Msg: fmt.Sprintf("capacity supplied for unknown edge %d", id)}
			}
		}
	}
	return g.Ops, edgeOrder, nil
}
