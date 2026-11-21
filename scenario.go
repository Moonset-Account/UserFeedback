package capacityreview

import (
	"errors"
	"math"
)

// flowArc records one original (forward) edge of a scenario network together
// with its position in the adjacency list, so residual capacity can be read
// back after the max-flow computation.
type flowArc struct {
	from   int
	to     int
	cap    int64
	adjIdx int
}

// scenarioNetwork is the flow network for one single-zone-failure scenario.
//
// Nodes: 0 = source, 1..n = entries, then one node per active zone, then sink.
// Edges: source->entry[i] with capacity k*BaseDemand[i],
// entry[i]->zone[j] with capacity TransferLimit[i][j] for active zones,
// zone[j]->sink with capacity ZoneCapacity[j].
//
// The failed zone is removed from the network entirely: no entry may send to
// it and it provides no capacity. A single entry's traffic may be split across
// any subset of the remaining zones, which is exactly a fractional/integral
// flow. The network is a simple bipartite transportation network with integral
// capacities, so a max flow is integral and yields an integral allocations.
type scenarioNetwork struct {
	failedZone int
	n, m       int
	entryNode  []int
	zoneNode   []int // node id per zone, -1 when failed
	sink       int
	nodeOf     []Node
	g          *dinic
	arcs       []flowArc
	zoneArc    map[[2]int]int // (entry,zone) -> arc index
}

func buildScenarioNetwork(p Problem, failedZone int, multiplier int64) (*scenarioNetwork, error) {
	n := len(p.BaseDemand)
	m := len(p.ZoneCapacity)
	if failedZone < 0 || failedZone >= m {
		return nil, errors.New("capacityreview: failed zone out of range")
	}
	sn := &scenarioNetwork{
		failedZone: failedZone,
		n:          n,
		m:          m,
		entryNode:  make([]int, n),
		zoneNode:   make([]int, m),
		zoneArc:    make(map[[2]int]int),
	}
	for j := 0; j < m; j++ {
		sn.zoneNode[j] = -1
	}
	sn.nodeOf = []Node{{Kind: NodeSource}}
	for i := 0; i < n; i++ {
		sn.entryNode[i] = len(sn.nodeOf)
		sn.nodeOf = append(sn.nodeOf, Node{Kind: NodeEntry, Index: i})
	}
	for j := 0; j < m; j++ {
		if j == failedZone {
			continue
		}
		sn.zoneNode[j] = len(sn.nodeOf)
		sn.nodeOf = append(sn.nodeOf, Node{Kind: NodeZone, Index: j})
	}
	sn.sink = len(sn.nodeOf)
	sn.nodeOf = append(sn.nodeOf, Node{Kind: NodeSink})
	sn.g = newDinic(len(sn.nodeOf))

	addArc := func(from, to int, capacity int64) int {
		sn.g.addEdge(from, to, capacity)
		sn.arcs = append(sn.arcs, flowArc{
			from:   from,
			to:     to,
			cap:    capacity,
			adjIdx: len(sn.g.adj[from]) - 1,
		})
		return len(sn.arcs) - 1
	}

	for i := 0; i < n; i++ {
		if multiplier != 0 && p.BaseDemand[i] > math.MaxInt64/multiplier {
			return nil, errors.New("capacityreview: requested demand overflows int64")
		}
		addArc(0, sn.entryNode[i], p.BaseDemand[i]*multiplier)
	}
	for i := 0; i < n; i++ {
		for j := 0; j < m; j++ {
			if j == failedZone {
				continue
			}
			if p.TransferLimit[i][j] == 0 {
				// A zero-capacity arc can never carry flow nor belong to a
				// minimum cut with positive contribution; omit it.
				continue
			}
			idx := addArc(sn.entryNode[i], sn.zoneNode[j], p.TransferLimit[i][j])
			sn.zoneArc[[2]int{i, j}] = idx
		}
	}
	for j := 0; j < m; j++ {
		if j == failedZone {
			continue
		}
		addArc(sn.zoneNode[j], sn.sink, p.ZoneCapacity[j])
	}
	return sn, nil
}

func (sn *scenarioNetwork) required(p Problem, multiplier int64) (int64, error) {
	var total int64
	for i := 0; i < sn.n; i++ {
		if multiplier != 0 && p.BaseDemand[i] > math.MaxInt64/multiplier {
			return 0, errors.New("capacityreview: requested demand overflows int64")
		}
		d := p.BaseDemand[i] * multiplier
		if total > math.MaxInt64-d {
			return 0, errors.New("capacityreview: requested demand overflows int64")
		}
		total += d
	}
	return total, nil
}

func (sn *scenarioNetwork) allocation() Allocation {
	alloc := Allocation{
		Flow:       make([][]int64, sn.n),
		ZoneLoad:   make([]int64, sn.m),
		EntryTotal: make([]int64, sn.n),
	}
	for i := 0; i < sn.n; i++ {
		alloc.Flow[i] = make([]int64, sn.m)
	}
	for pair, arcIdx := range sn.zoneArc {
		arc := sn.arcs[arcIdx]
		remaining := sn.g.adj[arc.from][arc.adjIdx].cap
		used := arc.cap - remaining
		i, j := pair[0], pair[1]
		alloc.Flow[i][j] = used
		alloc.ZoneLoad[j] += used
		alloc.EntryTotal[i] += used
	}
	return alloc
}

func (sn *scenarioNetwork) cut(maxFlow int64) (Cut, error) {
	side := sn.g.sourceSide(0)
	cut := Cut{}
	var sum int64
	for _, arc := range sn.arcs {
		if arc.cap == 0 {
			continue
		}
		if side[arc.from] && !side[arc.to] {
			cut.Edges = append(cut.Edges, CutEdge{
				From:     sn.nodeOf[arc.from],
				To:       sn.nodeOf[arc.to],
				Capacity: arc.cap,
			})
			if sum > math.MaxInt64-arc.cap {
				return Cut{}, errors.New("capacityreview: cut capacity overflows int64")
			}
			sum += arc.cap
		}
	}
	for id, node := range sn.nodeOf {
		if side[id] {
			cut.SourceSide = append(cut.SourceSide, node)
		} else {
			cut.SinkSide = append(cut.SinkSide, node)
		}
	}
	cut.Capacity = sum
	if sum != maxFlow {
		return Cut{}, errors.New("capacityreview: internal error: cut capacity does not equal max flow")
	}
	return cut, nil
}
