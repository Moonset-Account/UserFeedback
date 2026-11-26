package causalcodec_test

import (
	"errors"
	"strconv"
	"strings"
	"sync"
	"testing"

	causalcodec "causalcodec"
)

type anchorFixture struct {
	store      *causalcodec.AnchorStore
	report     causalcodec.Report
	missingV2  *causalcodec.Anchor
	sameNameV1 *causalcodec.Anchor
	cacheV3    *causalcodec.Anchor
}

func newAnchorFixture(t *testing.T) anchorFixture {
	t.Helper()

	sameNameV1, err := causalcodec.NewAnchor("db-context", "v1", causalcodec.Context{
		Causes: []causalcodec.Cause{{
			ID: "db-root-v1-local", Stage: "storage.undo", Kind: "OldLocalSnapshot",
			Message: "LOCAL_OLD_VERSION_MUST_NOT_BE_USED", Priority: causalcodec.PriorityNormal,
		}},
		Segments: []causalcodec.Segment{{
			Stage: "storage.undo", CauseIDs: []string{"db-root-v1-local"},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	missingV2, err := causalcodec.NewAnchor("db-context", "v2", causalcodec.Context{
		Causes: []causalcodec.Cause{{
			ID: "db-root-v2-remote", Stage: "storage.wal", Kind: "WALCorruption",
			Message: "REMOTE_V2_CONTEXT_VALUE", Priority: causalcodec.PriorityHigh,
		}},
		Segments: []causalcodec.Segment{{
			Stage: "storage.wal", CauseIDs: []string{"db-root-v2-remote"},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	cacheV3, err := causalcodec.NewAnchor("cache-context", "v3", causalcodec.Context{
		Causes: []causalcodec.Cause{{
			ID: "cache-root-v3", Stage: "cache.invalidate", Kind: "InvalidationBacklog",
			Message: "cache invalidation stream lagged", Priority: causalcodec.PriorityHigh,
		}},
		Segments: []causalcodec.Segment{{
			Stage: "cache.invalidate", CauseIDs: []string{"cache-root-v3"},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}

	store := causalcodec.NewAnchorStore().MustPut(sameNameV1).MustPut(cacheV3)
	report := causalcodec.Report{
		Delivery: causalcodec.Delivery{ID: "dlv-anchor", EventID: "evt-anchor-9"},
		Causes: []causalcodec.Cause{
			{ID: "inline-before", Stage: "db.pool", Kind: "PoolExhausted", Message: "pool waited 1s", Priority: causalcodec.PriorityHigh},
			{ID: "inline-after", Stage: "db.retry", Kind: "RetrySucceeded", Message: "read replica accepted retry", Priority: causalcodec.PriorityNormal},
		},
		Paths: []causalcodec.Path{
			{ID: "inline-independent", Segments: []causalcodec.Segment{
				{Stage: "api.handle", CauseIDs: []string{"inline-before"}},
			}},
			{ID: "cache-available", Segments: []causalcodec.Segment{
				{Stage: "cache.read", Anchor: &cacheV3.Ref},
			}},
			{ID: "db-interleaved", Segments: []causalcodec.Segment{
				{Stage: "db.pool", CauseIDs: []string{"inline-before"}},
				{Stage: "db.deep-context", Anchor: &missingV2.Ref},
				{Stage: "db.retry", CauseIDs: []string{"inline-after"}},
			}},
		},
	}
	return anchorFixture{
		store: store, report: report, missingV2: missingV2,
		sameNameV1: sameNameV1, cacheV3: cacheV3,
	}
}

func TestMissingOneAnchorVersionStillDecodesIndependentBranches(t *testing.T) {
	t.Parallel()

	fx := newAnchorFixture(t)
	pkt, err := causalcodec.NewCodec(-1).Encode(fx.report)
	if err != nil {
		t.Fatalf("Encode() error = %v", err)
	}

	decoded, err := causalcodec.Decode(pkt.Bytes, fx.store)
	if err == nil {
		t.Fatal("Decode() error = nil, want partial anchor error")
	}
	if !errors.Is(err, causalcodec.ErrAnchorNotFound) {
		t.Fatalf("Decode() error = %v, want ErrAnchorNotFound", err)
	}
	var partial *causalcodec.PartialDecodeError
	if !errors.As(err, &partial) {
		t.Fatalf("error %T is not PartialDecodeError", err)
	}
	var missing *causalcodec.MissingAnchorError
	if !errors.As(err, &missing) {
		t.Fatalf("error %v does not contain MissingAnchorError", err)
	}
	if missing.Ref.Key() != fx.missingV2.Ref.Key() {
		t.Fatalf("missing ref = %s, want %s", missing.Ref, fx.missingV2.Ref)
	}
	if len(missing.AvailableSameName) == 0 ||
		missing.AvailableSameName[0].Key() != fx.sameNameV1.Ref.Key() {
		t.Fatalf("same-name alternatives = %+v, want v1 only", missing.AvailableSameName)
	}

	if decoded == nil {
		t.Fatal("partial decode returned nil report")
	}
	if len(decoded.Paths) != 3 {
		t.Fatalf("decoded paths = %d, want 3", len(decoded.Paths))
	}

	// Fully independent inline branch.
	if _, ok := decoded.Cause("inline-before"); !ok {
		t.Fatal("inline independent cause was not decoded")
	}

	// Available exact anchor is resolved and marked with provenance.
	cachePath := decoded.Paths[1]
	if cachePath.ID != "cache-available" || len(cachePath.Segments) != 1 {
		t.Fatalf("cache path = %+v", cachePath)
	}
	if cachePath.Segments[0].ResolvedFrom == nil ||
		cachePath.Segments[0].ResolvedFrom.Key() != fx.cacheV3.Ref.Key() {
		t.Fatalf("cache anchor was not resolved: %+v", cachePath.Segments[0])
	}
	if _, ok := decoded.Cause("cache-root-v3"); !ok {
		t.Fatal("available anchor cause missing")
	}

	// Interleaved path retains both inline fragments around the exact gap.
	dbPath := decoded.Paths[2]
	if dbPath.ID != "db-interleaved" || len(dbPath.Segments) != 3 {
		t.Fatalf("interleaved segments = %+v, want before/gap/after", dbPath.Segments)
	}
	if got := dbPath.Segments[0].CauseIDs; len(got) != 1 || got[0] != "inline-before" {
		t.Fatalf("before fragment = %v", got)
	}
	gap := dbPath.Segments[1].Gap
	if gap == nil || gap.Kind != causalcodec.GapMissingAnchor {
		t.Fatalf("middle segment gap = %+v, want missing anchor", dbPath.Segments[1])
	}
	if gap.Anchor == nil || gap.Anchor.Key() != fx.missingV2.Ref.Key() {
		t.Fatalf("gap anchor = %+v, want v2 ref", gap.Anchor)
	}
	if got := dbPath.Segments[2].CauseIDs; len(got) != 1 || got[0] != "inline-after" {
		t.Fatalf("after fragment = %v", got)
	}

	if _, ok := decoded.Cause("db-root-v1-local"); ok {
		t.Fatal("decoder incorrectly imported local same-name v1 anchor")
	}
	display := causalcodec.Display(decoded)
	if strings.Contains(display, "LOCAL_OLD_VERSION_MUST_NOT_BE_USED") {
		t.Fatalf("same-name anchor content was used to fill missing chain:\n%s", display)
	}
	for _, want := range []string{"CHAIN BREAK/missing_anchor", "db-context@v2", "PoolExhausted", "RetrySucceeded"} {
		if !strings.Contains(display, want) {
			t.Fatalf("display missing %q\n%s", want, display)
		}
	}
}

func TestWrongDigestSameNameVersionIsNotSubstituted(t *testing.T) {
	t.Parallel()

	actual, err := causalcodec.NewAnchor("trace", "v7", causalcodec.Context{
		Causes: []causalcodec.Cause{{ID: "trace-1", Message: "correct context"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	store := causalcodec.NewAnchorStore().MustPut(actual)
	wrongRef := actual.Ref
	wrongRef.Digest = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
	report := causalcodec.Report{
		Delivery: causalcodec.Delivery{ID: "d", EventID: "e"},
		Paths: []causalcodec.Path{{
			ID: "p", Segments: []causalcodec.Segment{{Stage: "trace", Anchor: &wrongRef}},
		}},
	}
	pkt, err := causalcodec.Encode(report)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := causalcodec.Decode(pkt, store)
	if !errors.Is(err, causalcodec.ErrAnchorDigestMismatch) {
		t.Fatalf("Decode error = %v, want digest mismatch", err)
	}
	if decoded == nil || len(decoded.OpenGaps()) != 1 {
		t.Fatalf("decoded = %+v, want one gap", decoded)
	}
	if _, ok := decoded.Cause("trace-1"); ok {
		t.Fatal("wrong digest reference resolved the actual anchor")
	}
}

func TestMalformedPacketsAreRejected(t *testing.T) {
	t.Parallel()

	pkt, err := causalcodec.Encode(sampleReport())
	if err != nil {
		t.Fatal(err)
	}

	flipped := append([]byte(nil), pkt...)
	flipped[len(flipped)-1] ^= 0xff
	if _, err := causalcodec.Decode(flipped, nil); !errors.Is(err, causalcodec.ErrMalformedPacket) {
		t.Fatalf("corrupt packet error = %v, want ErrMalformedPacket", err)
	}
	if _, err := causalcodec.Decode([]byte("CCX1"), nil); !errors.Is(err, causalcodec.ErrMalformedPacket) {
		t.Fatalf("truncated packet error = %v, want ErrMalformedPacket", err)
	}
}

func TestAnchorContextRejectsNestedUnresolvedFragment(t *testing.T) {
	t.Parallel()

	nested := causalcodec.AnchorRef{Name: "nested", Version: "v1", Digest: "sha256:bad"}
	_, err := causalcodec.NewAnchor("outer", "v1", causalcodec.Context{
		Segments: []causalcodec.Segment{{Stage: "nested", Anchor: &nested}},
	})
	if !errors.Is(err, causalcodec.ErrInvalidAnchorRef) {
		t.Fatalf("NewAnchor() error = %v, want ErrInvalidAnchorRef", err)
	}
}

func TestAnchorStoreConcurrentExactAccess(t *testing.T) {
	t.Parallel()

	store := causalcodec.NewAnchorStore()
	refs := make([]causalcodec.AnchorRef, 0, 32)
	for i := 0; i < 32; i++ {
		refVersion := "v" + strconv.Itoa(i)
		a, err := causalcodec.NewAnchor("shared-name", refVersion, causalcodec.Context{
			Causes: []causalcodec.Cause{{ID: "c-" + strconv.Itoa(i), Message: "m-" + strconv.Itoa(i)}},
		})
		if err != nil {
			t.Fatal(err)
		}
		refs = append(refs, a.Ref)
		if err := store.Put(a); err != nil {
			t.Fatal(err)
		}
	}

	var wg sync.WaitGroup
	for _, ref := range refs {
		ref := ref
		wg.Add(1)
		go func() {
			defer wg.Done()
			a, err := store.ResolveAnchor(ref)
			if err != nil {
				t.Errorf("ResolveAnchor(%s) error = %v", ref, err)
				return
			}
			if a.Ref.Key() != ref.Key() {
				t.Errorf("resolved %s, want %s", a.Ref, ref)
			}
		}()
	}
	wg.Wait()
}
