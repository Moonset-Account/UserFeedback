package capacityreview

import (
	"errors"
	"fmt"
)

// Problem describes a capacity-review instance.
//
// Entry i has a positive base demand BaseDemand[i]. If the uniform integer
// multiplier is k, entry i must send exactly k*BaseDemand[i] units. Zone j can
// process at most ZoneCapacity[j] units. TransferLimit[i][j] is the maximum
// number of units entry i may send to zone j; a zero limit means that direct
// transfer is not possible.
type Problem struct {
	BaseDemand    []int64
	ZoneCapacity  []int64
	TransferLimit [][]int64
}

// NodeKind identifies the kind of a node in the flow network built for one
// single-zone-failure scenario.
type NodeKind int

const (
	NodeSource NodeKind = iota
	NodeEntry
	NodeZone
	NodeSink
)

// Node identifies a node in a scenario flow network.
type Node struct {
	Kind  NodeKind
	Index int
}

func (n Node) String() string {
	switch n.Kind {
	case NodeSource:
		return "source"
	case NodeSink:
		return "sink"
	case NodeEntry:
		return fmt.Sprintf("entry[%d]", n.Index)
	case NodeZone:
		return fmt.Sprintf("zone[%d]", n.Index)
	default:
		return fmt.Sprintf("node(kind=%d,index=%d)", int(n.Kind), n.Index)
	}
}

// Allocation is one integral feasible routing for a scenario.
//
// Flow[i][j] is the amount sent from entry i to zone j. The column of the
// failed zone is all zeros. ZoneLoad[j] and EntryTotal[i] are the column and
// row sums of Flow.
type Allocation struct {
	Flow       [][]int64
	ZoneLoad   []int64
	EntryTotal []int64
}

// ScenarioResult is the allocation produced at the maximum multiplier for one
// single-zone-failure scenario.
type ScenarioResult struct {
	FailedZone int
	Feasible   bool
	Allocation Allocation
}

// CutEdge is a directed edge crossing an s-t cut, with its capacity.
type CutEdge struct {
	From     Node
	To       Node
	Capacity int64
}

// Cut is an s-t cut in the flow network of a failing scenario. The union of
// SourceSide and SinkSide partitions the active nodes (the failed zone is not
// part of the scenario network).
type Cut struct {
	SourceSide []Node
	SinkSide   []Node
	Edges      []CutEdge
	Capacity   int64
}

// FailureCertificate witnesses that NextMultiplier cannot be served in one
// single-zone-failure scenario. MaxFlow < Required, and Cut.Capacity equals
// MaxFlow, so the cut has strictly less capacity than the required traffic.
type FailureCertificate struct {
	FailedZone int
	Multiplier int64
	Required   int64
	MaxFlow    int64
	Cut        Cut
}

// Result is the answer of a capacity review.
//
// MaxMultiplier is the largest non-negative integer k for which every
// single-zone-failure scenario is feasible. Scenarios contains one feasible
// allocation for each zone failure at that multiplier. Failure is a
// certificate for NextMultiplier = MaxMultiplier+1.
type Result struct {
	MaxMultiplier  int64
	Scenarios      []ScenarioResult
	NextMultiplier int64
	Failure        *FailureCertificate
}

func (p Problem) validate() error {
	n := len(p.BaseDemand)
	m := len(p.ZoneCapacity)
	if n == 0 {
		return errors.New("capacityreview: at least one entry is required")
	}
	if m == 0 {
		return errors.New("capacityreview: at least one zone is required")
	}
	if len(p.TransferLimit) != n {
		return fmt.Errorf("capacityreview: TransferLimit has %d rows, want %d", len(p.TransferLimit), n)
	}
	for i, b := range p.BaseDemand {
		if b <= 0 {
			return fmt.Errorf("capacityreview: BaseDemand[%d] must be positive, got %d", i, b)
		}
		if len(p.TransferLimit[i]) != m {
			return fmt.Errorf("capacityreview: TransferLimit[%d] has %d columns, want %d", i, len(p.TransferLimit[i]), m)
		}
		for j, l := range p.TransferLimit[i] {
			if l < 0 {
				return fmt.Errorf("capacityreview: TransferLimit[%d][%d] must be non-negative, got %d", i, j, l)
			}
		}
	}
	for j, c := range p.ZoneCapacity {
		if c < 0 {
			return fmt.Errorf("capacityreview: ZoneCapacity[%d] must be non-negative, got %d", j, c)
		}
	}
	return nil
}
