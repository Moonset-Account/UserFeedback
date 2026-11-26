package causalcodec_test

import (
	"bytes"
	"errors"
	"strings"
	"testing"

	causalcodec "causalcodec"
)

func sampleReport() causalcodec.Report {
	source := causalcodec.Source{
		Bus: "eventbus", Topic: "orders.events", Partition: 7, Offset: 98213,
		Consumer: "order-worker", Attempt: 2,
	}
	return causalcodec.Report{
		Delivery: causalcodec.Delivery{
			ID: "dlv-20261127-001", EventID: "evt-order-42", Source: source,
		},
		Causes: []causalcodec.Cause{
			{
				ID: "cause-dispatch", Stage: "dispatch.accept", Kind: "DispatchRejected",
				Message: "worker queue saturated", Source: source,
				Priority: causalcodec.PriorityCritical, Retryable: true,
			},
			{
				ID: "cause-publish-timeout", Stage: "publish", Kind: "PublishTimeout",
				Message: "publish timed out after 2s", Source: source,
				Priority: causalcodec.PriorityHigh, Retryable: true,
			},
			{
				ID: "cause-broker-alt", Stage: "publish", Kind: "BrokerLeaderChanged",
				Message: "leader changed during publish", Source: source,
				Priority: causalcodec.PriorityNormal,
			},
			{
				ID: "cause-quorum", Stage: "commit", Kind: "QuorumWriteFailed",
				Message: "only 1 of 3 replicas acknowledged", Source: source,
				Priority: causalcodec.PriorityHigh, Retryable: true,
			},
			{
				ID: "cause-db-timeout", Stage: "storage.deep", Kind: "DatabaseTimeout",
				Message: "fsync waited five seconds for slow disk", Source: source,
				Priority: causalcodec.PriorityLow,
			},
			{
				ID: "cause-db-lock", Stage: "storage.deep", Kind: "LockWaitTimeout",
				Message: "row lock held by a long-running reconciliation transaction", Source: source,
				Priority: causalcodec.PriorityDebug,
			},
		},
		Paths: []causalcodec.Path{
			{
				ID: "path-east", Label: "us-east primary",
				Segments: []causalcodec.Segment{
					{Stage: "dispatch.accept", CauseIDs: []string{"cause-dispatch"}},
					{Stage: "publish", CauseIDs: []string{"cause-publish-timeout", "cause-broker-alt"}},
					{Stage: "commit", CauseIDs: []string{"cause-quorum"}},
					{Stage: "storage.deep", CauseIDs: []string{"cause-db-timeout", "cause-db-lock"}},
				},
			},
			{
				ID: "path-west", Label: "us-west retry",
				Segments: []causalcodec.Segment{
					{Stage: "dispatch.accept", CauseIDs: []string{"cause-dispatch"}},
					{Stage: "publish", CauseIDs: []string{"cause-publish-timeout", "cause-broker-alt"}},
					{Stage: "commit.retry", CauseIDs: []string{"cause-quorum"}},
				},
			},
		},
	}
}

func TestRoundTripPreservesDeliveryPathsAndAlternatives(t *testing.T) {
	t.Parallel()

	original := sampleReport()
	raw, err := causalcodec.Encode(original)
	if err != nil {
		t.Fatalf("Encode() error = %v", err)
	}
	if len(raw) == 0 {
		t.Fatal("Encode() returned empty packet")
	}

	decoded, err := causalcodec.Decode(raw, nil)
	if err != nil {
		t.Fatalf("Decode() error = %v", err)
	}
	if decoded.Delivery != original.Delivery {
		t.Fatalf("delivery mismatch:\n got=%+v\nwant=%+v", decoded.Delivery, original.Delivery)
	}
	if len(decoded.Paths) != len(original.Paths) {
		t.Fatalf("paths = %d, want %d", len(decoded.Paths), len(original.Paths))
	}
	if decoded.Paths[0].ID != "path-east" || decoded.Paths[0].Label != "us-east primary" {
		t.Fatalf("unexpected first path: %+v", decoded.Paths[0])
	}
	publish := decoded.Paths[0].Segments[1]
	wantIDs := []string{"cause-publish-timeout", "cause-broker-alt"}
	if strings.Join(publish.CauseIDs, ",") != strings.Join(wantIDs, ",") {
		t.Fatalf("publish alternatives = %v, want %v", publish.CauseIDs, wantIDs)
	}
	timeout, ok := decoded.Cause("cause-publish-timeout")
	if !ok {
		t.Fatal("cause-publish-timeout missing from decoded catalog")
	}
	if timeout.Message != "publish timed out after 2s" || timeout.Priority != causalcodec.PriorityHigh {
		t.Fatalf("cause body not preserved: %+v", timeout)
	}
	if got := decoded.OpenGaps(); len(got) != 0 {
		t.Fatalf("unexpected gaps in unlimited round trip: %+v", got)
	}
}

func TestDuplicateCausesShareOneWireEntry(t *testing.T) {
	t.Parallel()

	message := "UNIQUE_DUPLICATE_CAUSE_BODY_7f3a"
	report := causalcodec.Report{
		Delivery: causalcodec.Delivery{ID: "d1", EventID: "e1"},
		Causes: []causalcodec.Cause{{
			ID: "shared-timeout", Stage: "publish", Kind: "Timeout",
			Message: message, Priority: causalcodec.PriorityHigh,
		}},
		Paths: []causalcodec.Path{
			{ID: "p1", Segments: []causalcodec.Segment{{Stage: "s1", CauseIDs: []string{"shared-timeout"}}}},
			{ID: "p2", Segments: []causalcodec.Segment{{Stage: "s2", CauseIDs: []string{"shared-timeout"}}}},
			{ID: "p3", Segments: []causalcodec.Segment{{Stage: "s3", CauseIDs: []string{"shared-timeout"}}}},
		},
	}

	pkt, err := causalcodec.NewCodec(-1).Encode(report)
	if err != nil {
		t.Fatalf("Encode() error = %v", err)
	}
	if pkt.Plan.UniqueCauses != 1 {
		t.Fatalf("UniqueCauses = %d, want 1", pkt.Plan.UniqueCauses)
	}
	if pkt.Plan.CauseReferences != 3 {
		t.Fatalf("CauseReferences = %d, want 3", pkt.Plan.CauseReferences)
	}
	if pkt.Plan.RepeatedReferences != 2 || pkt.Plan.DuplicateSavingsBytes <= 0 {
		t.Fatalf("dedup plan = %+v, want two repeated uses and positive savings", pkt.Plan)
	}
	if got := bytes.Count(pkt.Bytes, []byte(message)); got != 1 {
		t.Fatalf("duplicate message appears %d times in wire bytes, want once", got)
	}

	decoded, err := causalcodec.Decode(pkt.Bytes, nil)
	if err != nil {
		t.Fatalf("Decode() error = %v", err)
	}
	resolved := 0
	for _, p := range decoded.Paths {
		for _, s := range p.Segments {
			if c, ok := decoded.Cause(s.CauseIDs[0]); ok && c.Message == message {
				resolved++
			}
		}
	}
	if resolved != 3 {
		t.Fatalf("resolved shared cause on %d paths, want 3", resolved)
	}
}

func TestPriorityThresholdDisconnectsAndDisplayDoesNotInventCause(t *testing.T) {
	t.Parallel()

	codec := &causalcodec.Codec{MaxBytes: 2048, MinPriority: causalcodec.PriorityNormal}
	pkt, err := codec.Encode(sampleReport())
	if err != nil {
		t.Fatalf("Encode() error = %v", err)
	}
	if len(pkt.Bytes) > 2048 {
		t.Fatalf("encoded size %d exceeds fixed budget", len(pkt.Bytes))
	}

	dropped := strings.Join(droppedIDs(pkt.Plan), ",")
	if !strings.Contains(dropped, "cause-db-timeout") || !strings.Contains(dropped, "cause-db-lock") {
		t.Fatalf("low priority causes were not pruned: %+v", pkt.Plan.DroppedCauses)
	}
	if len(pkt.Plan.GapIDs) == 0 {
		t.Fatal("priority pruning did not create a chain gap")
	}

	decoded, err := causalcodec.Decode(pkt.Bytes, nil)
	if err != nil {
		t.Fatalf("Decode() error = %v", err)
	}
	gaps := decoded.OpenGaps()
	if len(gaps) != 1 || gaps[0].Kind != causalcodec.GapPriority {
		t.Fatalf("open gaps = %+v, want one priority gap", gaps)
	}

	display := causalcodec.Display(decoded)
	for _, want := range []string{
		"evt-order-42", "orders.events", "path-east",
		"[CHAIN BREAK/priority_disconnect]", "cause-db-timeout",
		"PublishTimeout", "QuorumWriteFailed",
	} {
		if !strings.Contains(display, want) {
			t.Fatalf("display missing %q\n%s", want, display)
		}
	}
	if strings.Contains(display, "fsync waited five seconds") {
		t.Fatalf("pruned verbose cause body leaked into display:\n%s", display)
	}
}

func TestByteBudgetExactBoundaryAndHardLimit(t *testing.T) {
	t.Parallel()

	full, err := causalcodec.Encode(sampleReport())
	if err != nil {
		t.Fatalf("full Encode() error = %v", err)
	}
	fullSize := len(full)

	exact, err := causalcodec.NewCodec(fullSize).Encode(sampleReport())
	if err != nil {
		t.Fatalf("exact-budget Encode(%d) error = %v", fullSize, err)
	}
	if len(exact.Bytes) != fullSize {
		t.Fatalf("exact budget size = %d, want %d", len(exact.Bytes), fullSize)
	}
	if len(exact.Plan.DroppedCauses) != 0 {
		t.Fatalf("exact-fit packet unexpectedly pruned: %+v", exact.Plan.DroppedCauses)
	}

	oneLess, err := causalcodec.NewCodec(fullSize - 1).Encode(sampleReport())
	if err != nil {
		t.Fatalf("one-byte-smaller Encode(%d) error = %v", fullSize-1, err)
	}
	if len(oneLess.Bytes) > fullSize-1 {
		t.Fatalf("pruned size %d exceeds budget %d", len(oneLess.Bytes), fullSize-1)
	}
	if len(oneLess.Plan.DroppedCauses) == 0 {
		t.Fatal("one-byte-smaller budget did not prune a cause")
	}

	// Every attempted budget must obey the hard limit when Encode succeeds;
	// impossibly small packets must return ErrBudgetExceeded.
	for budget := 120; budget <= fullSize; budget += 37 {
		pkt, err := causalcodec.NewCodec(budget).Encode(sampleReport())
		if err != nil {
			if !errors.Is(err, causalcodec.ErrBudgetExceeded) {
				t.Fatalf("budget %d returned unexpected error: %v", budget, err)
			}
			continue
		}
		if len(pkt.Bytes) > budget {
			t.Fatalf("budget %d produced %d-byte packet", budget, len(pkt.Bytes))
		}
	}

	_, err = causalcodec.NewCodec(16).Encode(sampleReport())
	if !errors.Is(err, causalcodec.ErrBudgetExceeded) {
		t.Fatalf("tiny budget error = %v, want ErrBudgetExceeded", err)
	}
}

func TestExtendHealsPriorityGapAndDoesNotMutateOriginal(t *testing.T) {
	t.Parallel()

	codec := &causalcodec.Codec{MaxBytes: 2048, MinPriority: causalcodec.PriorityNormal}
	pkt, err := codec.Encode(sampleReport())
	if err != nil {
		t.Fatalf("Encode() error = %v", err)
	}
	decoded, err := codec.Decode(pkt.Bytes)
	if err != nil {
		t.Fatalf("Decode() error = %v", err)
	}
	gap := decoded.OpenGaps()[0]

	root := causalcodec.Cause{
		ID: "cause-storage-controller", Stage: "storage.controller",
		Kind: "ControllerThrottled", Message: "disk controller queue throttled fsync completion",
		Priority: causalcodec.PriorityHigh,
	}
	extended, err := decoded.Extend(causalcodec.Extension{
		GapID:  gap.ID,
		Note:   "storage diagnostic returned after disconnect",
		Causes: []causalcodec.Cause{root},
		Segments: []causalcodec.Segment{
			{Stage: "storage.controller", CauseIDs: []string{root.ID}},
		},
	})
	if err != nil {
		t.Fatalf("Extend() error = %v", err)
	}
	if decoded.Gaps()[0].Extended {
		t.Fatal("Extend mutated the original report")
	}
	if len(extended.OpenGaps()) != 0 {
		t.Fatalf("extended report still has open gaps: %+v", extended.OpenGaps())
	}
	if _, ok := extended.Cause(root.ID); !ok {
		t.Fatal("extension cause missing")
	}
	display := causalcodec.Display(extended)
	for _, want := range []string{"[EXTENDED]", "ControllerThrottled", "disk controller queue throttled"} {
		if !strings.Contains(display, want) {
			t.Fatalf("extended display missing %q\n%s", want, display)
		}
	}

	// Healing metadata must survive another encode/decode cycle.
	repacked, err := causalcodec.Encode(*extended)
	if err != nil {
		t.Fatalf("re-Encode(extended) error = %v", err)
	}
	redecoded, err := causalcodec.Decode(repacked, nil)
	if err != nil {
		t.Fatalf("re-Decode(extended) error = %v", err)
	}
	reGaps := redecoded.Gaps()
	if len(reGaps) != 1 || !reGaps[0].Extended {
		t.Fatalf("persisted gaps = %+v, want one extended gap", reGaps)
	}
	if len(redecoded.OpenGaps()) != 0 {
		t.Fatalf("healed gap reopened after round trip: %+v", redecoded.OpenGaps())
	}
	if !strings.Contains(causalcodec.Display(redecoded), "[EXTENDED]") {
		t.Fatalf("redecoded display lost extension marker:\n%s", causalcodec.Display(redecoded))
	}

	if _, err := decoded.Extend(causalcodec.Extension{GapID: "gap:missing"}); !errors.Is(err, causalcodec.ErrGapNotFound) {
		t.Fatalf("unknown gap error = %v, want ErrGapNotFound", err)
	}
	_, err = extended.Extend(causalcodec.Extension{
		GapID:    gap.ID,
		Segments: []causalcodec.Segment{{Stage: "again", CauseIDs: []string{root.ID}}},
		Causes:   []causalcodec.Cause{root},
	})
	if !errors.Is(err, causalcodec.ErrGapNotFound) {
		t.Fatalf("double extension error = %v, want ErrGapNotFound", err)
	}
}

func TestEncodeValidationBoundaries(t *testing.T) {
	t.Parallel()

	unknown := causalcodec.Report{
		Delivery: causalcodec.Delivery{ID: "d", EventID: "e"},
		Paths: []causalcodec.Path{{
			ID:       "p",
			Segments: []causalcodec.Segment{{Stage: "s", CauseIDs: []string{"missing"}}},
		}},
	}
	if _, err := causalcodec.Encode(unknown); !errors.Is(err, causalcodec.ErrInvalidReport) {
		t.Fatalf("unknown cause error = %v, want ErrInvalidReport", err)
	}

	conflict := causalcodec.Report{
		Delivery: causalcodec.Delivery{ID: "d", EventID: "e"},
		Causes: []causalcodec.Cause{
			{ID: "c", Message: "first"},
			{ID: "c", Message: "conflicting"},
		},
		Paths: []causalcodec.Path{{
			ID:       "p",
			Segments: []causalcodec.Segment{{Stage: "s", CauseIDs: []string{"c"}}},
		}},
	}
	if _, err := causalcodec.Encode(conflict); !errors.Is(err, causalcodec.ErrInvalidReport) {
		t.Fatalf("conflicting cause error = %v, want ErrInvalidReport", err)
	}

	badAnchor := causalcodec.AnchorRef{Name: "ctx", Version: "v1"}
	anchorReport := causalcodec.Report{
		Delivery: causalcodec.Delivery{ID: "d", EventID: "e"},
		Paths: []causalcodec.Path{{
			ID:       "p",
			Segments: []causalcodec.Segment{{Stage: "s", Anchor: &badAnchor}},
		}},
	}
	if _, err := causalcodec.Encode(anchorReport); !errors.Is(err, causalcodec.ErrInvalidAnchorRef) {
		t.Fatalf("anchor without digest error = %v, want ErrInvalidAnchorRef", err)
	}
}

func droppedIDs(plan causalcodec.Plan) []string {
	out := make([]string, 0, len(plan.DroppedCauses))
	for _, d := range plan.DroppedCauses {
		out = append(out, d.ID)
	}
	return out
}
