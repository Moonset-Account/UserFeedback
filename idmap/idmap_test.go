package idmap_test

import (
	"errors"
	"reflect"
	"testing"
	"time"

	"idmapaudit/idmap"
)

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

func rng(start, end uint64) idmap.Range {
	return idmap.Range{Start: start, End: end}
}

func seg(src, dst, length uint64) idmap.Segment {
	return idmap.Segment{SrcStart: src, DstStart: dst, Length: length}
}

func mustVerifier(t *testing.T, cfg idmap.Config) *idmap.Verifier {
	t.Helper()
	v, err := idmap.NewVerifier(cfg)
	if err != nil {
		t.Fatalf("NewVerifier returned unexpected error: %v", err)
	}
	return v
}

// checkPartition asserts that the three buckets partition the request:
// pairwise disjoint, individually sorted and coalesced, union == request.
func checkPartition(t *testing.T, res idmap.Result, request idmap.Range) {
	t.Helper()
	for _, bucket := range [][]idmap.Range{res.Confirmed, res.OutOfBounds, res.MissingMapping} {
		for i, r := range bucket {
			if !r.Valid() {
				t.Fatalf("bucket contains invalid range %v", r)
			}
			if i > 0 && bucket[i-1].End >= r.Start {
				t.Fatalf("bucket not strictly disjoint/sorted: %v then %v", bucket[i-1], r)
			}
		}
	}
	all := append(append(append([]idmap.Range{}, res.Confirmed...), res.OutOfBounds...), res.MissingMapping...)
	if len(all) == 0 {
		t.Fatalf("partition of %v is empty", request)
	}
	got := mergeAll(all)
	want := []idmap.Range{request}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("partition union = %v, want exactly %v", got, want)
	}
}

func mergeAll(rs []idmap.Range) []idmap.Range {
	// Reuse the package's observable coalescing behavior indirectly: sort
	// here and merge adjacency manually.
	for i := 1; i < len(rs); i++ {
		for j := i; j > 0 && rs[j-1].Start > rs[j].Start; j-- {
			rs[j-1], rs[j] = rs[j], rs[j-1]
		}
	}
	merged := []idmap.Range{rs[0]}
	for _, r := range rs[1:] {
		last := &merged[len(merged)-1]
		if r.Start <= last.End {
			if r.End > last.End {
				last.End = r.End
			}
		} else {
			merged = append(merged, r)
		}
	}
	return merged
}

func contains(rs []idmap.Range, id uint64) bool {
	for _, r := range rs {
		if r.Start <= id && id < r.End {
			return true
		}
	}
	return false
}

// cloneConfig builds a structurally independent copy so later mutations of
// the original configuration can never affect the snapshot.
func cloneConfig(cfg idmap.Config) idmap.Config {
	out := idmap.Config{ApprovedRange: cfg.ApprovedRange, Layers: make([]idmap.Layer, len(cfg.Layers))}
	for i, layer := range cfg.Layers {
		out.Layers[i] = idmap.Layer{
			Name:     layer.Name,
			Segments: append([]idmap.Segment(nil), layer.Segments...),
		}
	}
	return out
}

// ---------------------------------------------------------------------------
// Configuration validation: the two cross-cutting configuration constraints
// ---------------------------------------------------------------------------

func TestNewRejectsOverlappingSource(t *testing.T) {
	cfg := idmap.Config{
		ApprovedRange: rng(0, 100),
		Layers: []idmap.Layer{{
			Name:     "ns0",
			Segments: []idmap.Segment{seg(0, 1000, 100), seg(50, 2000, 100)},
		}},
	}
	if _, err := idmap.NewVerifier(cfg); !errors.Is(err, idmap.ErrOverlappingSegments) {
		t.Fatalf("overlapping source segments: got err=%v, want ErrOverlappingSegments", err)
	}

	// Merely touching source ranges [0,100) and [100,200) are half-open and
	// therefore allowed.
	ok := idmap.Config{
		ApprovedRange: rng(0, 100),
		Layers: []idmap.Layer{{
			Name:     "ns0",
			Segments: []idmap.Segment{seg(0, 1000, 100), seg(100, 2000, 100)},
		}},
	}
	if _, err := idmap.NewVerifier(ok); err != nil {
		t.Fatalf("touching source segments should be allowed, got %v", err)
	}
}

func TestNewRejectsOverlappingDestination(t *testing.T) {
	// Sources are disjoint, but translated destinations overlap.
	cfg := idmap.Config{
		ApprovedRange: rng(0, 100),
		Layers: []idmap.Layer{{
			Name:     "ns0",
			Segments: []idmap.Segment{seg(0, 1000, 100), seg(200, 1050, 100)},
		}},
	}
	if _, err := idmap.NewVerifier(cfg); !errors.Is(err, idmap.ErrOverlappingSegments) {
		t.Fatalf("overlapping destination segments: got err=%v, want ErrOverlappingSegments", err)
	}

	// Touching destinations [1000,1100) and [1100,1200) stay allowed.
	ok := idmap.Config{
		ApprovedRange: rng(0, 100),
		Layers: []idmap.Layer{{
			Name:     "ns0",
			Segments: []idmap.Segment{seg(0, 1000, 100), seg(200, 1100, 100)},
		}},
	}
	if _, err := idmap.NewVerifier(ok); err != nil {
		t.Fatalf("touching destination segments should be allowed, got %v", err)
	}
}

func TestNewRejectsInvalidRanges(t *testing.T) {
	base := func(segs []idmap.Segment) idmap.Config {
		return idmap.Config{ApprovedRange: rng(0, 100), Layers: []idmap.Layer{{Name: "ns0", Segments: segs}}}
	}

	cases := map[string]idmap.Config{
		"zero length segment":              base([]idmap.Segment{seg(0, 1000, 0)}),
		"source exceeds uint32 domain":     base([]idmap.Segment{seg(idmap.MaxID, 0, 2)}),
		"destination exceeds uint32 domain": base([]idmap.Segment{seg(0, idmap.MaxID, 2)}),
		"empty approved range":             idmap.Config{ApprovedRange: rng(10, 10)},
		"approved range beyond domain":     idmap.Config{ApprovedRange: rng(0, idmap.MaxID+2)},
	}
	for name, cfg := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := idmap.NewVerifier(cfg); !errors.Is(err, idmap.ErrInvalidRange) {
				t.Fatalf("got err=%v, want ErrInvalidRange", err)
			}
		})
	}

	// A segment ending exactly at MaxID+1 (the exclusive domain bound) is
	// legal, including the identifier MaxID itself.
	edge := idmap.Config{
		ApprovedRange: rng(idmap.MaxID, idmap.MaxID+1),
		Layers: []idmap.Layer{{
			Name:     "ns0",
			Segments: []idmap.Segment{seg(idmap.MaxID, idmap.MaxID, 1)},
		}},
	}
	if _, err := idmap.NewVerifier(edge); err != nil {
		t.Fatalf("MaxID boundary segment should be legal, got %v", err)
	}
}

func TestVerifyInvalidRequest(t *testing.T) {
	v := mustVerifier(t, idmap.Config{
		ApprovedRange: rng(0, 100),
		Layers:        []idmap.Layer{{Name: "ns0", Segments: []idmap.Segment{seg(0, 0, 100)}}},
	})
	for _, bad := range []idmap.Range{rng(10, 10), rng(5, 4), rng(0, idmap.MaxID+2)} {
		if _, err := v.Verify(bad); !errors.Is(err, idmap.ErrInvalidRange) {
			t.Fatalf("Verify(%v): got err=%v, want ErrInvalidRange", bad, err)
		}
	}
}

// ---------------------------------------------------------------------------
// Acceptable result, rejected result and the interleaved boundary scenario
// ---------------------------------------------------------------------------

func TestSingleLayerAllThreeOutcomes(t *testing.T) {
	// [100,200) -> [1000,1100) lands inside the approved host accounts;
	// [200,300) -> [5000,5100) lands outside; everything else has no mapping.
	v := mustVerifier(t, idmap.Config{
		ApprovedRange: rng(1000, 1100),
		Layers: []idmap.Layer{{
			Name: "ns0",
			Segments: []idmap.Segment{
				seg(100, 1000, 100),
				seg(200, 5000, 100),
			},
		}},
	})

	// Acceptable: a request fully covered inside the approved boundary.
	res, err := v.Verify(rng(100, 200))
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	want := idmap.Result{Confirmed: []idmap.Range{rng(100, 200)}}
	if !reflect.DeepEqual(res, want) {
		t.Fatalf("acceptable request: got %+v, want %+v", res, want)
	}

	// Rejected + interleaved: holes, confirmed and out-of-bounds alternate in
	// one request, and every bucket is reported in original coordinates.
	res, err = v.Verify(rng(50, 350))
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	want = idmap.Result{
		Confirmed:      []idmap.Range{rng(100, 200)},
		OutOfBounds:    []idmap.Range{rng(200, 300)},
		MissingMapping: []idmap.Range{rng(50, 100), rng(300, 350)},
	}
	if !reflect.DeepEqual(res, want) {
		t.Fatalf("interleaved request: got %+v, want %+v", res, want)
	}
	checkPartition(t, res, rng(50, 350))
}

func TestApprovedBoundaryHalfOpen(t *testing.T) {
	// Request [0,100) maps to host [1000,1100); only host [1010,1090) is
	// approved, i.e. request sub-range [10,90).
	v := mustVerifier(t, idmap.Config{
		ApprovedRange: rng(1010, 1090),
		Layers: []idmap.Layer{{
			Name:     "ns0",
			Segments: []idmap.Segment{seg(0, 1000, 100)},
		}},
	})

	// Boundary straddle: one fragment cut at both ends, OOB pieces coalesced
	// into two separate ranges because the confirmed piece sits between them.
	res, err := v.Verify(rng(0, 100))
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	want := idmap.Result{
		Confirmed:   []idmap.Range{rng(10, 90)},
		OutOfBounds: []idmap.Range{rng(0, 10), rng(90, 100)},
	}
	if !reflect.DeepEqual(res, want) {
		t.Fatalf("straddle: got %+v, want %+v", res, want)
	}
	checkPartition(t, res, rng(0, 100))

	// Touching-but-not-inside: half-open boundary means host 1010 is the
	// first approved identifier; [1000,1010) and [1090,1100) stay rejected.
	for _, req := range []idmap.Range{rng(0, 10), rng(90, 100)} {
		res, err = v.Verify(req)
		if err != nil {
			t.Fatalf("Verify(%v): %v", req, err)
		}
		if len(res.Confirmed) != 0 || !reflect.DeepEqual(res.OutOfBounds, []idmap.Range{req}) {
			t.Fatalf("request %v touching the boundary must be out of bounds, got %+v", req, res)
		}
	}

	// Exactly the approved slice, both edges inclusive of the approved
	// interval itself.
	res, err = v.Verify(rng(10, 90))
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if !reflect.DeepEqual(res.Confirmed, []idmap.Range{rng(10, 90)}) || len(res.OutOfBounds) != 0 {
		t.Fatalf("exact approved slice: got %+v", res)
	}
}

func TestHoleDoesNotInheritParentIdentity(t *testing.T) {
	// Layer 0 maps the whole request; layer 1 only maps the first half. The
	// second half reaches a hole at layer 1 and must be reported as missing,
	// even though layer 0 had given it a perfectly good identifier.
	v := mustVerifier(t, idmap.Config{
		ApprovedRange: rng(0, 50),
		Layers: []idmap.Layer{
			{Name: "ns1", Segments: []idmap.Segment{seg(0, 1000, 100)}},
			{Name: "ns0", Segments: []idmap.Segment{seg(1000, 0, 50)}},
		},
	})
	res, err := v.Verify(rng(0, 100))
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	want := idmap.Result{
		Confirmed:      []idmap.Range{rng(0, 50)},
		MissingMapping: []idmap.Range{rng(50, 100)},
	}
	if !reflect.DeepEqual(res, want) {
		t.Fatalf("hole inheritance: got %+v, want %+v", res, want)
	}
	checkPartition(t, res, rng(0, 100))
}

func TestMultiLayerOffsetsInBothDirections(t *testing.T) {
	// Two request pieces survive two layers; one reaches approved host
	// accounts, the other is translated above the approved ceiling.
	v := mustVerifier(t, idmap.Config{
		ApprovedRange: rng(0, 100),
		Layers: []idmap.Layer{
			{Name: "ns1", Segments: []idmap.Segment{
				seg(0, 200, 100),
				seg(100, 500, 100),
			}},
			{Name: "ns0", Segments: []idmap.Segment{
				seg(200, 0, 100),
				seg(500, 900, 100),
			}},
		},
	})
	res, err := v.Verify(rng(0, 200))
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	want := idmap.Result{
		Confirmed:   []idmap.Range{rng(0, 100)},
		OutOfBounds: []idmap.Range{rng(100, 200)},
	}
	if !reflect.DeepEqual(res, want) {
		t.Fatalf("multi-layer: got %+v, want %+v", res, want)
	}
	checkPartition(t, res, rng(0, 200))
}

func TestNoLayersApprovedRangeStillApplies(t *testing.T) {
	// With no translation layers the request's own numbers are host numbers.
	v := mustVerifier(t, idmap.Config{ApprovedRange: rng(50, 250)})
	res, err := v.Verify(rng(0, 300))
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	want := idmap.Result{
		Confirmed:   []idmap.Range{rng(50, 250)},
		OutOfBounds: []idmap.Range{rng(0, 50), rng(250, 300)},
	}
	if !reflect.DeepEqual(res, want) {
		t.Fatalf("no-layer verify: got %+v, want %+v", res, want)
	}
	checkPartition(t, res, rng(0, 300))
}

func TestReordersInputAndCoalescesAdjacentPieces(t *testing.T) {
	// Segments are supplied out of source order; the constructor normalizes
	// them. The two confirmed pieces become adjacent after splitting and must
	// be coalesced into one original sub-range.
	v := mustVerifier(t, idmap.Config{
		ApprovedRange: rng(1100, 1300),
		Layers: []idmap.Layer{{
			Name: "ns0",
			Segments: []idmap.Segment{
				seg(200, 1200, 100),
				seg(100, 1100, 100),
			},
		}},
	})
	res, err := v.Verify(rng(0, 300))
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}
	want := idmap.Result{
		Confirmed:      []idmap.Range{rng(100, 300)},
		MissingMapping: []idmap.Range{rng(0, 100)},
	}
	if !reflect.DeepEqual(res, want) {
		t.Fatalf("reorder/coalesce: got %+v, want %+v", res, want)
	}
	checkPartition(t, res, rng(0, 300))
}

func TestVerifierDoesNotMutateInputs(t *testing.T) {
	cfg := idmap.Config{
		ApprovedRange: rng(1100, 1300),
		Layers: []idmap.Layer{{
			Name: "ns0",
			Segments: []idmap.Segment{
				seg(200, 1200, 100),
				seg(100, 1100, 100),
			},
		}},
	}
	snapshot := cloneConfig(cfg)

	v := mustVerifier(t, cfg)
	if _, err := v.Verify(rng(0, 300)); err != nil {
		t.Fatalf("Verify: %v", err)
	}
	if !reflect.DeepEqual(cfg, snapshot) {
		t.Fatalf("Verify mutated the caller's configuration:\nbefore=%+v\nafter =%+v", snapshot, cfg)
	}

	// Re-running with a freshly built, pre-sorted config must give identical
	// answers: normalization is observable only through correctness.
	sorted := idmap.Config{
		ApprovedRange: cfg.ApprovedRange,
		Layers: []idmap.Layer{{
			Name:     "ns0",
			Segments: []idmap.Segment{seg(100, 1100, 100), seg(200, 1200, 100)},
		}},
	}
	v2 := mustVerifier(t, sorted)
	got, _ := v.Verify(rng(0, 300))
	want, _ := v2.Verify(rng(0, 300))
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("unsorted vs sorted config differ: %+v vs %+v", got, want)
	}
}

// ---------------------------------------------------------------------------
// Billion-identifier synthetic configuration: cost must track segments and
// split points, never identifier count.
// ---------------------------------------------------------------------------

const (
	billion = uint64(1_000_000_000)
	requestStart = uint64(0)
	requestEnd   = uint64(4 * billion)
)

func billionConfig() idmap.Config {
	return idmap.Config{
		ApprovedRange: rng(billion, 3_500_000_000),
		Layers: []idmap.Layer{
			{
				Name: "ns2",
				// Two holes at [1e9,1.5e9) and [3e9,3.5e9) in request space.
				Segments: []idmap.Segment{
					seg(0, 500_000_000, billion),
					seg(3*billion/2, 3*billion/2, 3*billion/2),
					seg(7*billion/2, 7*billion/2, billion/2),
				},
			},
			{
				Name: "ns1",
				Segments: []idmap.Segment{
					seg(500_000_000, 100_000_000, billion),
					seg(3*billion/2, 1_100_000_000, 3*billion/2),
					seg(7*billion/2, 3*billion, billion/2),
				},
			},
			{
				Name: "ns0",
				// Last piece is pushed below the approved floor; the first
				// two reach the approved host range.
				Segments: []idmap.Segment{
					seg(100_000_000, billion, 5*billion/2),
					seg(3*billion, 200_000_000, billion/2),
				},
			},
		},
	}
}

// translateOneID is an independent per-identifier reference implementation
// used to cross-check the range-based verifier on sampled identifiers.
func translateOneID(cfg idmap.Config, id uint64) (host uint64, mapped bool) {
	cur := id
	for _, layer := range cfg.Layers {
		next, ok := func() (uint64, bool) {
			for _, s := range layer.Segments {
				if s.SrcStart <= cur && cur < s.SrcStart+s.Length {
					return cur - s.SrcStart + s.DstStart, true
				}
			}
			return 0, false
		}()
		if !ok {
			return 0, false
		}
		cur = next
	}
	return cur, true
}

func TestBillionSpanIsSegmentSizedWork(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping billion-span performance assertion in -short mode")
	}
	cfg := billionConfig()
	v := mustVerifier(t, cfg)
	request := rng(requestStart, requestEnd)

	start := time.Now()
	res, err := v.Verify(request)
	elapsed := time.Since(start)
	if err != nil {
		t.Fatalf("Verify: %v", err)
	}

	// Hand-computed partition (all numbers in original request coordinates).
	want := idmap.Result{
		Confirmed: []idmap.Range{
			rng(0, billion),
			rng(3*billion/2, 3*billion),
		},
		MissingMapping: []idmap.Range{
			rng(billion, 3*billion/2),
			rng(3*billion, 7*billion/2),
		},
		OutOfBounds: []idmap.Range{
			rng(7*billion/2, 4*billion),
		},
	}
	if !reflect.DeepEqual(res, want) {
		t.Fatalf("billion-span partition:\n got %+v\nwant %+v", res, want)
	}
	checkPartition(t, res, request)

	// Structural bound: 8 mapping segments across 3 layers can produce only a
	// handful of sub-ranges, never anything proportional to the 4e9 width.
	totalRanges := len(res.Confirmed) + len(res.OutOfBounds) + len(res.MissingMapping)
	if totalRanges > 12 {
		t.Fatalf("range count %d scales with identifiers; expected a segment-sized constant", totalRanges)
	}
	if elapsed > 200*time.Millisecond {
		t.Fatalf("Verify over %d identifiers took %v; work must depend on segments/split points only",
			request.End-request.Start, elapsed)
	}

	// Independent cross-check: 200 evenly sampled identifiers classified by
	// the straightforward per-ID reference must agree with every bucket.
	const samples = 200
	width := request.End - request.Start
	for i := 0; i < samples; i++ {
		id := request.Start + width*uint64(i)/samples
		host, mapped := translateOneID(cfg, id)
		switch {
		case !mapped:
			if !contains(res.MissingMapping, id) {
				t.Fatalf("id %d: reference says missing mapping, buckets say otherwise (%+v)", id, res)
			}
		case host >= cfg.ApprovedRange.Start && host < cfg.ApprovedRange.End:
			if !contains(res.Confirmed, id) {
				t.Fatalf("id %d -> host %d: reference says confirmed, buckets say otherwise", id, host)
			}
		default:
			if !contains(res.OutOfBounds, id) {
				t.Fatalf("id %d -> host %d: reference says out of bounds, buckets say otherwise", id, host)
			}
		}
	}
}

func BenchmarkVerifyBillionSpan(b *testing.B) {
	v, err := idmap.NewVerifier(billionConfig())
	if err != nil {
		b.Fatal(err)
	}
	request := rng(requestStart, requestEnd)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := v.Verify(request); err != nil {
			b.Fatal(err)
		}
	}
}
