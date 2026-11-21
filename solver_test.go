package capacityreview

import (
	"math/rand"
	"reflect"
	"testing"
)

// validAllocation checks every structural and semantic invariant of an
// allocation for the given scenario at multiplier k.
func validAllocation(t *testing.T, p Problem, failedZone int, k int64, alloc Allocation) {
	t.Helper()
	n := len(p.BaseDemand)
	m := len(p.ZoneCapacity)
	if len(alloc.Flow) != n {
		t.Fatalf("Flow has %d rows, want %d", len(alloc.Flow), n)
	}
	for i := 0; i < n; i++ {
		if len(alloc.Flow[i]) != m {
			t.Fatalf("Flow[%d] has %d columns, want %d", i, len(alloc.Flow[i]), m)
		}
		var row int64
		for j := 0; j < m; j++ {
			f := alloc.Flow[i][j]
			if f < 0 {
				t.Fatalf("negative flow %d on entry %d -> zone %d", f, i, j)
			}
			if j == failedZone && f != 0 {
				t.Fatalf("flow %d sent to failed zone %d", f, j)
			}
			if f > p.TransferLimit[i][j] {
				t.Fatalf("flow %d exceeds TransferLimit[%d][%d]=%d", f, i, j, p.TransferLimit[i][j])
			}
			row += f
		}
		want := k * p.BaseDemand[i]
		if row != want {
			t.Fatalf("entry %d total %d, want %d (k=%d, base=%d)", i, row, want, k, p.BaseDemand[i])
		}
		if alloc.EntryTotal[i] != row {
			t.Fatalf("EntryTotal[%d]=%d, want %d", i, alloc.EntryTotal[i], row)
		}
	}
	for j := 0; j < m; j++ {
		var col int64
		for i := 0; i < n; i++ {
			col += alloc.Flow[i][j]
		}
		if col > p.ZoneCapacity[j] {
			t.Fatalf("zone %d load %d exceeds capacity %d", j, col, p.ZoneCapacity[j])
		}
		if alloc.ZoneLoad[j] != col {
			t.Fatalf("ZoneLoad[%d]=%d, want %d", j, alloc.ZoneLoad[j], col)
		}
	}
}

func nodeKey(n Node) string { return n.String() }

// validCertificate checks a failure certificate end to end: the claimed
// scenario is genuinely infeasible at the claimed multiplier, the max flow is
// below the required traffic, and the cut is a real s-t cut whose capacity
// equals the max flow and is itself below the required traffic.
func validCertificate(t *testing.T, p Problem, f *FailureCertificate) {
	t.Helper()
	if f == nil {
		t.Fatal("expected a failure certificate")
	}
	if f.Multiplier <= 0 {
		t.Fatalf("certificate multiplier = %d, want positive", f.Multiplier)
	}
	if f.FailedZone < 0 || f.FailedZone >= len(p.ZoneCapacity) {
		t.Fatalf("certificate failed zone %d out of range", f.FailedZone)
	}
	var wantRequired int64
	for _, b := range p.BaseDemand {
		wantRequired += b * f.Multiplier
	}
	if f.Required != wantRequired {
		t.Fatalf("certificate required = %d, want %d", f.Required, wantRequired)
	}
	if f.MaxFlow >= f.Required {
		t.Fatalf("certificate max flow %d is not below required %d", f.MaxFlow, f.Required)
	}
	if refFeasible(p, f.FailedZone, f.Multiplier) {
		t.Fatalf("certificate claims zone %d infeasible at k=%d, reference says feasible", f.FailedZone, f.Multiplier)
	}
	validCut(t, p, f.FailedZone, f.Multiplier, f.Cut)
	if f.Cut.Capacity != f.MaxFlow {
		t.Fatalf("cut capacity %d != max flow %d", f.Cut.Capacity, f.MaxFlow)
	}
	if f.Cut.Capacity >= f.Required {
		t.Fatalf("cut capacity %d is not below required %d", f.Cut.Capacity, f.Required)
	}
}

// validCut checks from first principles that cut is an s-t cut of the scenario
// network at multiplier k and that its listed edges are exactly the forward
// arcs crossing it.
func validCut(t *testing.T, p Problem, failedZone int, k int64, cut Cut) {
	t.Helper()
	side := make(map[string]bool)
	for _, node := range cut.SourceSide {
		if side[nodeKey(node)] {
			t.Fatalf("node %v listed twice on the source side", node)
		}
		side[nodeKey(node)] = true
	}
	sinkSide := make(map[string]bool)
	for _, node := range cut.SinkSide {
		key := nodeKey(node)
		if sinkSide[key] {
			t.Fatalf("node %v listed twice on the sink side", node)
		}
		if side[key] {
			t.Fatalf("node %v on both sides of the cut", node)
		}
		sinkSide[key] = true
	}
	if !side["source"] {
		t.Fatal("source is not on the source side")
	}
	if !sinkSide["sink"] {
		t.Fatal("sink is not on the sink side")
	}
	if side["sink"] {
		t.Fatal("sink is on the source side")
	}
	if side["zone["+itoa(failedZone)+"]"] || sinkSide["zone["+itoa(failedZone)+"]"] {
		t.Fatalf("failed zone %d appears in the scenario network", failedZone)
	}

	listed := map[string]int64{}
	for _, e := range cut.Edges {
		if !side[nodeKey(e.From)] || !sinkSide[nodeKey(e.To)] {
			t.Fatalf("edge %v -> %v does not cross from source side to sink side", e.From, e.To)
		}
		if e.Capacity <= 0 {
			t.Fatalf("cut edge %v -> %v has non-positive capacity %d", e.From, e.To, e.Capacity)
		}
		key := e.From.String() + "->" + e.To.String()
		if _, dup := listed[key]; dup {
			t.Fatalf("cut edge %v -> %v listed twice", e.From, e.To)
		}
		listed[key] = e.Capacity
	}

	// Independently recompute the crossing set over every arc of the network.
	recomputed := map[string]int64{}
	addCrossing := func(from Node, to Node, capacity int64) {
		if !side[from.String()] || !sinkSide[to.String()] || capacity == 0 {
			return
		}
		recomputed[from.String()+"->"+to.String()] = capacity
	}
	for i := range p.BaseDemand {
		addCrossing(
			Node{Kind: NodeSource},
			Node{Kind: NodeEntry, Index: i},
			k*p.BaseDemand[i],
		)
		for j := range p.ZoneCapacity {
			if j == failedZone {
				continue
			}
			addCrossing(
				Node{Kind: NodeEntry, Index: i},
				Node{Kind: NodeZone, Index: j},
				p.TransferLimit[i][j],
			)
		}
	}
	for j := range p.ZoneCapacity {
		if j == failedZone {
			continue
		}
		addCrossing(
			Node{Kind: NodeZone, Index: j},
			Node{Kind: NodeSink},
			p.ZoneCapacity[j],
		)
	}
	var recomputedSum int64
	for key, capacity := range recomputed {
		if got, ok := listed[key]; !ok {
			t.Fatalf("missing crossing edge %q with capacity %d (listed %v)", key, capacity, cut.Edges)
		} else if got != capacity {
			t.Fatalf("crossing edge %q listed with capacity %d, want %d", key, got, capacity)
		}
		recomputedSum += capacity
	}
	if len(listed) != len(recomputed) {
		t.Fatalf("cut lists %d edges, recomputed crossing set has %d: %v", len(listed), len(recomputed), cut.Edges)
	}
	if cut.Capacity != recomputedSum {
		t.Fatalf("cut capacity = %d, recomputed crossing capacity = %d", cut.Capacity, recomputedSum)
	}
}

func itoa(v int) string {
	if v == 0 {
		return "0"
	}
	neg := v < 0
	if neg {
		v = -v
	}
	var buf [20]byte
	pos := len(buf)
	for v > 0 {
		pos--
		buf[pos] = byte('0' + v%10)
		v /= 10
	}
	if neg {
		pos--
		buf[pos] = '-'
	}
	return string(buf[pos:])
}

func TestSimpleThreeZones(t *testing.T) {
	p := Problem{
		BaseDemand:   []int64{1, 1},
		ZoneCapacity: []int64{3, 3, 3},
		TransferLimit: [][]int64{
			{3, 3, 3},
			{3, 3, 3},
		},
	}
	res, err := Solve(p)
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	if res.MaxMultiplier != 3 {
		t.Fatalf("MaxMultiplier=%d, want 3", res.MaxMultiplier)
	}
	if res.NextMultiplier != 4 {
		t.Fatalf("NextMultiplier=%d, want 4", res.NextMultiplier)
	}
	if len(res.Scenarios) != 3 {
		t.Fatalf("got %d scenarios, want 3", len(res.Scenarios))
	}
	for j, sc := range res.Scenarios {
		if sc.FailedZone != j {
			t.Fatalf("scenario %d reports failed zone %d", j, sc.FailedZone)
		}
		if !sc.Feasible {
			t.Fatalf("scenario zone %d reported infeasible", sc.FailedZone)
		}
		validAllocation(t, p, sc.FailedZone, res.MaxMultiplier, sc.Allocation)
	}
	if res.Failure == nil {
		t.Fatal("expected a failure certificate")
	}
	f := res.Failure
	if f.Multiplier != 4 || f.Required != 8 {
		t.Fatalf("certificate multiplier/required = %d/%d, want 4/8", f.Multiplier, f.Required)
	}
	if f.MaxFlow != 6 || f.Cut.Capacity != 6 {
		t.Fatalf("maxflow/cut = %d/%d, want 6/6", f.MaxFlow, f.Cut.Capacity)
	}
	validCut(t, p, f.FailedZone, f.Multiplier, f.Cut)
}

func TestTransferLimitsAndCapacity(t *testing.T) {
	// E0 can only reach Z0 and Z1; E1 can only reach Z1 and Z2.
	// Z1 is a shared backup with capacity 4.
	p := Problem{
		BaseDemand:   []int64{1, 1},
		ZoneCapacity: []int64{10, 4, 10},
		TransferLimit: [][]int64{
			{10, 10, 0},
			{0, 10, 10},
		},
	}
	res, err := Solve(p)
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	// Fail Z0: E0 must use Z1, whose capacity caps k at 4.
	// Fail Z2: E1 must use Z1, same cap of 4.
	// Fail Z1: capacities 10/10 allow k up to the transfer limit 10.
	if res.MaxMultiplier != 4 {
		t.Fatalf("MaxMultiplier=%d, want 4", res.MaxMultiplier)
	}
	for _, sc := range res.Scenarios {
		validAllocation(t, p, sc.FailedZone, res.MaxMultiplier, sc.Allocation)
		if sc.FailedZone == 0 {
			if sc.Allocation.Flow[0][1] != 4 {
				t.Fatalf("scenario Z0: E0->Z1 = %d, want 4", sc.Allocation.Flow[0][1])
			}
			if sc.Allocation.Flow[1][2] != 4 {
				t.Fatalf("scenario Z0: E1->Z2 = %d, want 4", sc.Allocation.Flow[1][2])
			}
			if sc.Allocation.ZoneLoad[1] != 4 {
				t.Fatalf("scenario Z0: zone Z1 load %d, want 4", sc.Allocation.ZoneLoad[1])
			}
		}
	}
	if res.Failure == nil {
		t.Fatal("expected a failure certificate")
	}
	f := res.Failure
	if f.FailedZone != 0 {
		t.Fatalf("failed zone = %d, want 0 (both Z0 and Z2 cap at k=4)", f.FailedZone)
	}
	if f.Required != 10 || f.MaxFlow != 9 {
		t.Fatalf("required/maxflow = %d/%d, want 10/9", f.Required, f.MaxFlow)
	}
	validCut(t, p, f.FailedZone, f.Multiplier, f.Cut)
	// The bottleneck is the shared backup zone Z1's capacity (4) plus the
	// partially unserved demand of E1 (5).
	found := map[string]int64{}
	for _, e := range f.Cut.Edges {
		found[e.From.String()+"->"+e.To.String()] = e.Capacity
	}
	if got := found["zone[1]->sink"]; got != 4 {
		t.Fatalf("zone Z1 -> sink edge capacity = %d, want 4 (edges %v)", got, f.Cut.Edges)
	}
	if got := found["source->entry[1]"]; got != 5 {
		t.Fatalf("source -> entry[1] edge capacity = %d, want 5 (edges %v)", got, f.Cut.Edges)
	}
}

// TestSharedBackupZoneCannotUseAggregateCapacity encodes the requirement that
// two entries which can only fail over to the same backup zone must not be
// approved on the strength of spare capacity elsewhere in the site.
func TestSharedBackupZoneCannotUseAggregateCapacity(t *testing.T) {
	// E0 homes to Z0, E1 homes to Z1, and both can only fail over to Z2.
	// Z2 has capacity 1; the other zones have plenty of spare capacity but are
	// unreachable for the displaced entry.
	p := Problem{
		BaseDemand:   []int64{1, 1},
		ZoneCapacity: []int64{100, 100, 1},
		TransferLimit: [][]int64{
			{10, 0, 10},
			{0, 10, 10},
		},
	}
	res, err := Solve(p)
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	if res.MaxMultiplier != 1 {
		t.Fatalf("MaxMultiplier=%d, want 1 (shared backup Z2 caps the site)", res.MaxMultiplier)
	}

	// Aggregate spare capacity at the next multiplier looks generous and would
	// wrongly approve k=2: total site capacity is 201 and demand is 4.
	if res.NextMultiplier != 2 {
		t.Fatalf("NextMultiplier=%d, want 2", res.NextMultiplier)
	}
	var aggregateCapacity, failedCapacity int64
	for _, c := range p.ZoneCapacity {
		aggregateCapacity += c
	}
	failedCapacity = aggregateCapacity - p.ZoneCapacity[res.Failure.FailedZone]
	if failedCapacity <= res.NextMultiplier*2 {
		t.Fatalf("test fixture broken: aggregate spare %d should dwarf demand %d", failedCapacity, res.NextMultiplier*2)
	}

	if res.Failure == nil {
		t.Fatal("expected a failure certificate for the shared-backup case")
	}
	cert := res.Failure
	if cert.Required != 4 || cert.MaxFlow != 3 {
		t.Fatalf("required/maxflow = %d/%d, want 4/3", cert.Required, cert.MaxFlow)
	}
	validCut(t, p, cert.FailedZone, cert.Multiplier, cert.Cut)
	// The shortage must be attributed to the shared backup zone Z2 (capacity
	// 1), never to the site-wide aggregate capacity.
	sawSharedZoneEdge := false
	for _, e := range cert.Cut.Edges {
		if e.From == (Node{Kind: NodeZone, Index: 2}) && e.To.Kind == NodeSink {
			if e.Capacity != 1 {
				t.Fatalf("shared backup edge capacity = %d, want 1", e.Capacity)
			}
			sawSharedZoneEdge = true
		}
	}
	if !sawSharedZoneEdge {
		t.Fatalf("cut %v does not expose the shared backup zone Z2", cert.Cut.Edges)
	}
	if cert.FailedZone == 2 {
		t.Fatalf("failed zone %d is not a displaced-entry scenario", cert.FailedZone)
	}
}

func TestEntrySplitsAcrossZones(t *testing.T) {
	// Three zones hold at most 6 each and every per-link limit is 6, so after
	// any single failure the entry must split its demand over the two survivors.
	p := Problem{
		BaseDemand:   []int64{10},
		ZoneCapacity: []int64{6, 6, 6},
		TransferLimit: [][]int64{
			{6, 6, 6},
		},
	}
	res, err := Solve(p)
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	if res.MaxMultiplier != 1 {
		t.Fatalf("MaxMultiplier=%d, want 1", res.MaxMultiplier)
	}
	sc := res.Scenarios[0]
	if sc.FailedZone != 0 {
		t.Fatalf("scenario failed zone = %d", sc.FailedZone)
	}
	validAllocation(t, p, 0, 1, sc.Allocation)
	if sc.Allocation.Flow[0][1] == 0 || sc.Allocation.Flow[0][2] == 0 {
		t.Fatalf("entry did not split: %v", sc.Allocation.Flow)
	}
	if sc.Allocation.Flow[0][1]+sc.Allocation.Flow[0][2] != 10 {
		t.Fatalf("row total = %d, want 10", sc.Allocation.Flow[0][1]+sc.Allocation.Flow[0][2])
	}
}

func TestSingleZoneAlwaysInfeasibleForPositiveDemand(t *testing.T) {
	p := Problem{
		BaseDemand:   []int64{5},
		ZoneCapacity: []int64{1000},
		TransferLimit: [][]int64{
			{1000},
		},
	}
	res, err := Solve(p)
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	if res.MaxMultiplier != 0 {
		t.Fatalf("MaxMultiplier=%d, want 0 for a single zone", res.MaxMultiplier)
	}
	if len(res.Scenarios) != 1 {
		t.Fatalf("got %d scenarios, want 1", len(res.Scenarios))
	}
	validAllocation(t, p, 0, 0, res.Scenarios[0].Allocation)
	if res.Failure == nil || res.Failure.Required != 5 || res.Failure.MaxFlow != 0 {
		t.Fatalf("certificate = %+v, want required 5 / maxflow 0", res.Failure)
	}
	// With no active zone the min cut is empty and sits between the entries
	// and the sink.
	if res.Failure.Cut.Capacity != 0 || len(res.Failure.Cut.Edges) != 0 {
		t.Fatalf("cut = %+v, want empty cut of capacity 0", res.Failure.Cut)
	}
}

func TestZeroMultiplierAlwaysFeasible(t *testing.T) {
	p := Problem{
		BaseDemand:   []int64{2, 3},
		ZoneCapacity: []int64{0, 0},
		TransferLimit: [][]int64{
			{0, 0},
			{0, 0},
		},
	}
	res, err := Solve(p)
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	if res.MaxMultiplier != 0 {
		t.Fatalf("MaxMultiplier=%d, want 0", res.MaxMultiplier)
	}
	for _, sc := range res.Scenarios {
		validAllocation(t, p, sc.FailedZone, 0, sc.Allocation)
	}
	if res.Failure == nil || res.Failure.MaxFlow != 0 || res.Failure.Cut.Capacity != 0 {
		t.Fatalf("certificate = %+v, want zero max flow and zero-capacity cut", res.Failure)
	}
}

func TestPerLinkLimitIsTheCut(t *testing.T) {
	// Zone Z0 has ample capacity, but the only entry may send at most 3 units
	// there. The bottleneck is the entry->zone link, not site capacity.
	p := Problem{
		BaseDemand:   []int64{10},
		ZoneCapacity: []int64{100, 100},
		TransferLimit: [][]int64{
			{3, 3},
		},
	}
	res, err := Solve(p)
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	if res.MaxMultiplier != 0 {
		t.Fatalf("MaxMultiplier=%d, want 0", res.MaxMultiplier)
	}
	cert := res.Failure
	if cert == nil {
		t.Fatal("expected a failure certificate")
	}
	if cert.MaxFlow != 3 || cert.Cut.Capacity != 3 {
		t.Fatalf("maxflow/cut = %d/%d, want 3/3", cert.MaxFlow, cert.Cut.Capacity)
	}
	validCut(t, p, cert.FailedZone, cert.Multiplier, cert.Cut)
	var found int64
	for _, e := range cert.Cut.Edges {
		if e.From.Kind == NodeEntry && e.To.Kind == NodeZone && e.From.Index == 0 {
			found += e.Capacity
		}
	}
	if found != 3 {
		t.Fatalf("entry->zone crossing capacity = %d, want 3 (edges %v)", found, cert.Cut.Edges)
	}
}

func TestInputNotMutated(t *testing.T) {
	p := Problem{
		BaseDemand:   []int64{2, 1},
		ZoneCapacity: []int64{5, 4, 3},
		TransferLimit: [][]int64{
			{5, 4, 3},
			{5, 0, 3},
		},
	}
	before := Problem{
		BaseDemand:    append([]int64(nil), p.BaseDemand...),
		ZoneCapacity:  append([]int64(nil), p.ZoneCapacity...),
		TransferLimit: make([][]int64, len(p.TransferLimit)),
	}
	for i := range p.TransferLimit {
		before.TransferLimit[i] = append([]int64(nil), p.TransferLimit[i]...)
	}
	first, err := Solve(p)
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	second, err := Solve(p)
	if err != nil {
		t.Fatalf("Solve again: %v", err)
	}
	if !reflect.DeepEqual(p.BaseDemand, before.BaseDemand) ||
		!reflect.DeepEqual(p.ZoneCapacity, before.ZoneCapacity) ||
		!reflect.DeepEqual(p.TransferLimit, before.TransferLimit) {
		t.Fatalf("Solve mutated its input: %+v, want %+v", p, before)
	}
	if !reflect.DeepEqual(first, second) {
		t.Fatalf("Solve is not deterministic:\nfirst=%+v\nsecond=%+v", first, second)
	}
}

func TestLargeButRepresentableDemand(t *testing.T) {
	p := Problem{
		BaseDemand:   []int64{1 << 62},
		ZoneCapacity: []int64{1 << 62},
		TransferLimit: [][]int64{
			{1 << 62},
		},
	}
	// A single zone is always infeasible for positive demand. The k=1
	// certificate needs 2^62 units, which still fits in int64.
	res, err := Solve(p)
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	if res.MaxMultiplier != 0 {
		t.Fatalf("MaxMultiplier=%d, want 0", res.MaxMultiplier)
	}
	if res.Failure == nil || res.Failure.Required != 1<<62 {
		t.Fatalf("certificate = %+v", res.Failure)
	}
}

func TestAggregateOverflowIsReported(t *testing.T) {
	p := Problem{
		BaseDemand:   []int64{1<<62 + 1, 1<<62 + 1},
		ZoneCapacity: []int64{1},
		TransferLimit: [][]int64{
			{1},
			{1},
		},
	}
	if _, err := Solve(p); err == nil {
		t.Fatal("expected an overflow error for oversized aggregate demand")
	}
}

func TestValidationErrors(t *testing.T) {
	cases := []struct {
		name string
		p    Problem
	}{
		{"no entries", Problem{ZoneCapacity: []int64{1}}},
		{"no zones", Problem{BaseDemand: []int64{1}}},
		{"zero base", Problem{BaseDemand: []int64{0}, ZoneCapacity: []int64{1}, TransferLimit: [][]int64{{1}}}},
		{"negative base", Problem{BaseDemand: []int64{-1}, ZoneCapacity: []int64{1}, TransferLimit: [][]int64{{1}}}},
		{"wrong row count", Problem{BaseDemand: []int64{1, 1}, ZoneCapacity: []int64{1}, TransferLimit: [][]int64{{1}}}},
		{"wrong column count", Problem{BaseDemand: []int64{1}, ZoneCapacity: []int64{1, 1}, TransferLimit: [][]int64{{1}}}},
		{"negative limit", Problem{BaseDemand: []int64{1}, ZoneCapacity: []int64{1}, TransferLimit: [][]int64{{-1}}}},
		{"negative capacity", Problem{BaseDemand: []int64{1}, ZoneCapacity: []int64{-1}, TransferLimit: [][]int64{{1}}}},
	}
	for _, tc := range cases {
		if _, err := Solve(tc.p); err == nil {
			t.Fatalf("%s: expected error, got nil", tc.name)
		}
	}
}

// ---------- reference implementation for randomized cross-checks ----------

// refFeasible enumerates integral allocations for tiny instances. It is an
// independent feasibility oracle, not a max-flow solver.
func refFeasible(p Problem, failedZone int, k int64) bool {
	n := len(p.BaseDemand)
	m := len(p.ZoneCapacity)
	remaining := make([]int64, m)
	copy(remaining, p.ZoneCapacity)
	remaining[failedZone] = 0

	var place func(entry int) bool
	place = func(entry int) bool {
		if entry == n {
			return true
		}
		demand := k * p.BaseDemand[entry]
		var rec func(zone int, left int64) bool
		rec = func(zone int, left int64) bool {
			if zone == m {
				return left == 0 && place(entry+1)
			}
			if zone == failedZone {
				return rec(zone+1, left)
			}
			maxTake := p.TransferLimit[entry][zone]
			if remaining[zone] < maxTake {
				maxTake = remaining[zone]
			}
			if left < maxTake {
				maxTake = left
			}
			for take := maxTake; take >= 0; take-- {
				remaining[zone] -= take
				if rec(zone+1, left-take) {
					remaining[zone] += take
					return true
				}
				remaining[zone] += take
			}
			return false
		}
		return rec(0, demand)
	}
	return place(0)
}

func refMaxMultiplier(p Problem) int64 {
	var result int64
	for k := int64(0); ; k++ {
		ok := true
		for j := range p.ZoneCapacity {
			if !refFeasible(p, j, k) {
				ok = false
				break
			}
		}
		if !ok {
			return result
		}
		result = k
		if k > 1000 {
			panic("reference search did not terminate")
		}
	}
}

// checkAgainstReference validates a Solve result for a tiny instance using the
// brute-force oracle as ground truth.
func checkAgainstReference(t *testing.T, iter int, p Problem, res Result) {
	t.Helper()
	want := refMaxMultiplier(p)
	if res.MaxMultiplier != want {
		t.Fatalf("iter %d: MaxMultiplier=%d, reference=%d, problem=%+v", iter, res.MaxMultiplier, want, p)
	}
	if res.NextMultiplier != want+1 {
		t.Fatalf("iter %d: NextMultiplier=%d, want %d", iter, res.NextMultiplier, want+1)
	}
	if len(res.Scenarios) != len(p.ZoneCapacity) {
		t.Fatalf("iter %d: got %d scenarios, want %d", iter, len(res.Scenarios), len(p.ZoneCapacity))
	}
	for j, sc := range res.Scenarios {
		if sc.FailedZone != j {
			t.Fatalf("iter %d: scenario %d reports failed zone %d", iter, j, sc.FailedZone)
		}
		if !sc.Feasible {
			t.Fatalf("iter %d: scenario %d marked infeasible at the claimed maximum", iter, j)
		}
		validAllocation(t, p, j, want, sc.Allocation)
	}
	validCertificate(t, p, res.Failure)
	if res.Failure.Multiplier != want+1 {
		t.Fatalf("iter %d: certificate multiplier=%d, want %d", iter, res.Failure.Multiplier, want+1)
	}
	// Every scenario must be feasible at the maximum and the certificate's
	// scenario must be exactly the one that breaks at the next step.
	for j := 0; j < len(p.ZoneCapacity); j++ {
		if !refFeasible(p, j, want) {
			t.Fatalf("iter %d: zone %d infeasible at the claimed maximum %d", iter, j, want)
		}
	}
	if refFeasible(p, res.Failure.FailedZone, want+1) {
		t.Fatalf("iter %d: zone %d actually feasible at %d", iter, res.Failure.FailedZone, want+1)
	}
}

func TestRandomizedAgainstReference(t *testing.T) {
	rng := rand.New(rand.NewSource(20261122))
	for iter := 0; iter < 300; iter++ {
		n := 1 + rng.Intn(2)
		m := 2 + rng.Intn(2)
		p := Problem{
			BaseDemand:    make([]int64, n),
			ZoneCapacity:  make([]int64, m),
			TransferLimit: make([][]int64, n),
		}
		for i := 0; i < n; i++ {
			p.BaseDemand[i] = int64(1 + rng.Intn(3))
			p.TransferLimit[i] = make([]int64, m)
			for j := 0; j < m; j++ {
				p.TransferLimit[i][j] = int64(rng.Intn(5))
			}
		}
		for j := 0; j < m; j++ {
			p.ZoneCapacity[j] = int64(rng.Intn(5))
		}

		res, err := Solve(p)
		if err != nil {
			t.Fatalf("iter %d: Solve: %v", iter, err)
		}
		checkAgainstReference(t, iter, p, res)
	}
}

func TestRandomizedDenseSmall(t *testing.T) {
	// More entries and zones, but tiny magnitudes so the brute-force oracle
	// stays cheap. This exercises shared-backup and partial-split cases.
	rng := rand.New(rand.NewSource(7))
	for iter := 0; iter < 150; iter++ {
		n := 2 + rng.Intn(2)
		m := 3 + rng.Intn(2)
		p := Problem{
			BaseDemand:    make([]int64, n),
			ZoneCapacity:  make([]int64, m),
			TransferLimit: make([][]int64, n),
		}
		for i := 0; i < n; i++ {
			p.BaseDemand[i] = int64(1 + rng.Intn(2))
			p.TransferLimit[i] = make([]int64, m)
			for j := 0; j < m; j++ {
				p.TransferLimit[i][j] = int64(rng.Intn(3))
			}
		}
		for j := 0; j < m; j++ {
			p.ZoneCapacity[j] = int64(rng.Intn(3))
		}

		res, err := Solve(p)
		if err != nil {
			t.Fatalf("iter %d: Solve: %v", iter, err)
		}
		checkAgainstReference(t, iter, p, res)
	}
}
