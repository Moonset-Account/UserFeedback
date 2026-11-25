package zonetransfer

import (
	"container/heap"
	"fmt"
)

// edge is a validated, replayable delta between two known snapshots.
type edge struct {
	delta Delta
	from  string
	to    string
	cost  int
}

// validateEdge checks every condition that must hold for a delta to be
// chainable at all:
//
//  1. both endpoint versions are known snapshots;
//  2. the delta's declared serials agree with the snapshots' serials;
//  3. serial ordering is RFC 1982 comparable and forward (a half-circle
//     difference, or a backwards edge, is rejected); equal serials are only
//     allowed when both snapshots carry identical content;
//  4. applying the delta to the "from" snapshot reproduces the "to"
//     snapshot's record set exactly ("same serial, different content" edges
//     therefore cannot be chained).
func validateEdge(d Delta, snaps map[string]Snapshot) (edge, error) {
	from, okFrom := snaps[d.FromVersion]
	to, okTo := snaps[d.ToVersion]
	if !okFrom || !okTo {
		return edge{}, fmt.Errorf("delta %q->%q references unknown snapshot", d.FromVersion, d.ToVersion)
	}
	if d.FromSerial != from.Serial || d.ToSerial != to.Serial {
		return edge{}, fmt.Errorf("delta %q->%q serials do not match endpoint snapshots", d.FromVersion, d.ToVersion)
	}
	if d.Bytes < 0 {
		return edge{}, fmt.Errorf("delta %q->%q has negative byte cost", d.FromVersion, d.ToVersion)
	}

	switch CompareSerial(from.Serial, to.Serial) {
	case SerialOrderIncomparable:
		return edge{}, fmt.Errorf("delta %q->%q serials are half a circle apart and incomparable", d.FromVersion, d.ToVersion)
	case SerialOrderGreater:
		return edge{}, fmt.Errorf("delta %q->%q goes backwards in serial space", d.FromVersion, d.ToVersion)
	case SerialOrderEqual:
		// Same serial may only connect identical content; different content
		// with an equal serial can never be distinguished by a secondary.
		if !sameRecords(from.Records, to.Records) {
			return edge{}, fmt.Errorf("delta %q->%q keeps serial %d but changes content", d.FromVersion, d.ToVersion, from.Serial)
		}
	}

	got, err := ApplyDelta(from.Records, d)
	if err != nil {
		return edge{}, err
	}
	if !sameRecords(got, to.Records) {
		return edge{}, fmt.Errorf("%w: delta %q->%q does not reproduce the target snapshot", ErrInvalidDelta, d.FromVersion, d.ToVersion)
	}
	return edge{delta: d, from: d.FromVersion, to: d.ToVersion, cost: d.Bytes}, nil
}

// pqItem is a Dijkstra frontier entry.
type pqItem struct {
	version string
	cost    int
	index   int
}

type pq []*pqItem

func (p pq) Len() int           { return len(p) }
func (p pq) Less(i, j int) bool { return p[i].cost < p[j].cost }
func (p pq) Swap(i, j int) {
	p[i], p[j] = p[j], p[i]
	p[i].index = i
	p[j].index = j
}
func (p *pq) Push(x any) {
	it := x.(*pqItem)
	it.index = len(*p)
	*p = append(*p, it)
}
func (p *pq) Pop() any {
	old := *p
	n := len(old)
	it := old[n-1]
	old[n-1] = nil
	*p = old[:n-1]
	return it
}

// shortestChain finds the minimum total-byte edge path from startVersion to
// targetVersion over validated edges. Ties on cost resolve to the
// lexicographically smallest joined sequence of versions, so the chosen
// chain is deterministic.
func shortestChain(adj map[string][]edge, startVersion, targetVersion string) ([]edge, int, bool) {
	dist := map[string]int{startVersion: 0}
	prev := map[string]edge{}
	visited := map[string]bool{}
	// bestPath breaks cost ties with a stable, lexicographic path key.
	bestPath := map[string]string{startVersion: startVersion}

	queue := &pq{{version: startVersion, cost: 0}}
	heap.Init(queue)

	for queue.Len() > 0 {
		cur := heap.Pop(queue).(*pqItem)
		if visited[cur.version] {
			continue
		}
		visited[cur.version] = true
		if cur.version == targetVersion {
			break
		}
		for _, e := range adj[cur.version] {
			if visited[e.to] {
				continue
			}
			nd := cur.cost + e.cost
			pathKey := bestPath[cur.version] + "|" + e.to
			old, seen := dist[e.to]
			if !seen || nd < old || (nd == old && pathKey < bestPath[e.to]) {
				dist[e.to] = nd
				prev[e.to] = e
				bestPath[e.to] = pathKey
				heap.Push(queue, &pqItem{version: e.to, cost: nd})
			}
		}
	}

	total, ok := dist[targetVersion]
	if !ok {
		return nil, 0, false
	}
	// Reconstruct.
	var reversed []edge
	v := targetVersion
	for v != startVersion {
		e, ok := prev[v]
		if !ok {
			return nil, 0, false
		}
		reversed = append(reversed, e)
		v = e.from
	}
	chain := make([]edge, len(reversed))
	for i, e := range reversed {
		chain[len(reversed)-1-i] = e
	}
	return chain, total, true
}

// PlanTransfer builds the update stream a primary should send to a backup
// whose current zone reports backupSerial.
//
// snapshots is every zone state known to the primary (each Version must be
// unique), deltas is the full set of explicitly bounded change records, and
// targetVersion names the state the backup must converge to.
//
// An IXFR plan is returned only when exactly one snapshot matches the
// backup's serial, a delta chain replays that snapshot into the target, and
// the chain costs strictly fewer bytes than sending the whole zone. Every
// other situation yields an AXFR plan with a populated Reason.
func PlanTransfer(snapshots []Snapshot, deltas []Delta, targetVersion string, backupSerial uint32) (Plan, error) {
	axfr := func(reason string, target Snapshot) Plan {
		return Plan{Kind: KindAXFR, Reason: reason, Target: target, Bytes: SnapshotBytes(target)}
	}

	snaps := make(map[string]Snapshot, len(snapshots))
	for _, s := range snapshots {
		if s.Version == "" {
			return Plan{}, fmt.Errorf("%w: snapshot with empty version id", ErrInvalidInput)
		}
		if _, dup := snaps[s.Version]; dup {
			return Plan{}, fmt.Errorf("%w: duplicate snapshot version %q", ErrInvalidInput, s.Version)
		}
		snaps[s.Version] = s
	}
	target, ok := snaps[targetVersion]
	if !ok {
		return Plan{}, fmt.Errorf("%w: %q", ErrBadTarget, targetVersion)
	}

	var matches []Snapshot
	for _, s := range snapshots {
		if s.Serial == backupSerial {
			matches = append(matches, s)
		}
	}
	if len(matches) == 0 {
		return axfr(ReasonNoSnapshot, target), nil
	}
	// Distinct version IDs with the same serial are distinct start points;
	// the backup cannot tell them apart, so the start is not unique.
	if len(matches) > 1 {
		return axfr(ReasonNonUniqueStart, target), nil
	}
	start := matches[0]

	if start.Version == target.Version {
		if !sameRecords(start.Records, target.Records) {
			// Same version id must mean the same snapshot; guard anyway.
			return axfr(ReasonNonUniqueStart, target), nil
		}
		return Plan{
			Kind:   KindIXFR,
			Target: target,
			Chain:  nil,
			Bytes:  0,
		}, nil
	}

	adj := make(map[string][]edge)
	for _, d := range deltas {
		e, err := validateEdge(d, snaps)
		if err != nil {
			// An unusable edge is ignored: planning only follows edges that
			// genuinely replay to the same target content.
			continue
		}
		adj[e.from] = append(adj[e.from], e)
	}

	edges, chainBytes, reachable := shortestChain(adj, start.Version, target.Version)
	if !reachable {
		return axfr(ReasonNoChain, target), nil
	}

	fullBytes := SnapshotBytes(target)
	// "If the full zone is no more expensive, send the full zone."
	if fullBytes <= chainBytes {
		return axfr(ReasonFullZoneCheaper, target), nil
	}

	chain := make([]Delta, len(edges))
	for i, e := range edges {
		chain[i] = e.delta
	}
	// Defensive full-chain re-verification before handing the stream out.
	replayed, err := Replay(start.Records, chain)
	if err != nil || !sameRecords(replayed, target.Records) {
		return axfr(ReasonNoChain, target), nil
	}
	return Plan{
		Kind:   KindIXFR,
		Target: target,
		Chain:  chain,
		Bytes:  chainBytes,
	}, nil
}
