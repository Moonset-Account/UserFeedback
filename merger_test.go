package segmentmerge_test

import (
	"errors"
	"fmt"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	sm "segmentmerge"
)

var testTime = time.Date(2026, time.November, 27, 8, 0, 0, 0, time.UTC)

func testSegment(id uint64, seq uint64, payload string) sm.Segment {
	return sm.Segment{
		ConnID:    "conn-1",
		RecvID:    sm.ReceiveID(id),
		SeqStart:  seq,
		SeqEnd:    seq + uint64(len(payload)),
		Data:      []byte(payload),
		Timestamp: testTime.Add(time.Duration(id) * time.Millisecond),
		Protocol:  sm.Protocol("IPv4"),
		Options:   sm.Options{"dscp": "cs1"},
	}
}

func mustSubmit(t *testing.T, m *sm.Merger, seg sm.Segment) {
	t.Helper()
	if err := m.Submit(seg); err != nil {
		t.Fatalf("Submit(recv=%d) returned error: %v", seg.RecvID, err)
	}
}

func drainOne(t *testing.T, m *sm.Merger) *sm.Aggregate {
	t.Helper()
	aggs := m.Drain()
	if len(aggs) != 1 {
		t.Fatalf("Drain() returned %d aggregates, want 1", len(aggs))
	}
	if err := aggs[0].Validate(); err != nil {
		t.Fatalf("aggregate boundary validation failed: %v", err)
	}
	return aggs[0]
}

func assertNoAggregate(t *testing.T, m *sm.Merger, context string) {
	t.Helper()
	if aggs := m.Drain(); len(aggs) != 0 {
		t.Fatalf("%s: Drain() returned %d aggregates, want none: %+v", context, len(aggs), aggs)
	}
}

func TestInOrderMergePreservesDataTimestampsBoundariesAndSplit(t *testing.T) {
	segs := []sm.Segment{
		testSegment(1, 0, "ab"),
		testSegment(2, 2, "cd"),
		testSegment(3, 4, "ef"),
	}
	m := sm.New(sm.Config{FirstReceiveID: 1})

	for _, seg := range segs {
		mustSubmit(t, m, seg)
	}
	assertNoAggregate(t, m, "partial group without cap or end marker")

	if err := m.Flush("conn-1"); err != nil {
		t.Fatalf("Flush: %v", err)
	}
	agg := drainOne(t, m)

	if got := string(agg.Data); got != "abcdef" {
		t.Fatalf("Data = %q, want abcdef", got)
	}
	if agg.ConnID != "conn-1" || agg.RecvStart != 1 || agg.RecvEnd != 3 {
		t.Fatalf("unexpected IDs: %+v", agg)
	}
	if agg.SeqStart != 0 || agg.SeqEnd != 6 {
		t.Fatalf("Seq range = [%d,%d), want [0,6)", agg.SeqStart, agg.SeqEnd)
	}
	if agg.Reason != sm.CloseFlush {
		t.Fatalf("Reason = %q, want %q", agg.Reason, sm.CloseFlush)
	}
	if len(agg.Boundaries) != 3 {
		t.Fatalf("len(Boundaries) = %d, want 3", len(agg.Boundaries))
	}

	wantOffsets := []int{0, 2, 4}
	for i, b := range agg.Boundaries {
		if b.Offset != wantOffsets[i] || b.Length != len(segs[i].Data) {
			t.Fatalf("boundary %d = offset %d length %d", i, b.Offset, b.Length)
		}
		if !b.Timestamp.Equal(segs[i].Timestamp) {
			t.Fatalf("boundary %d timestamp = %v, want %v", i, b.Timestamp, segs[i].Timestamp)
		}
	}

	split := agg.Split()
	if !reflect.DeepEqual(split, segs) {
		t.Fatalf("Split() did not restore original segments\ngot:  %#v\nwant: %#v", split, segs)
	}

	// Mutating split output must not corrupt the aggregate's retained originals.
	split[0].Data[0] = 'X'
	split[0].Options["dscp"] = "mutated"
	again := agg.Split()
	if !reflect.DeepEqual(again, segs) {
		t.Fatalf("second Split() changed after mutating first Split result")
	}
}

func TestOutOfOrderReportsAreReorderedByReceiveID(t *testing.T) {
	segs := map[uint64]sm.Segment{
		1: testSegment(1, 0, "ab"),
		2: testSegment(2, 2, "cd"),
		3: testSegment(3, 4, "ef"),
	}
	m := sm.New(sm.Config{FirstReceiveID: 1})

	for _, id := range []uint64{3, 2, 1} {
		mustSubmit(t, m, segs[id])
	}
	if err := m.Flush("conn-1"); err != nil {
		t.Fatal(err)
	}
	agg := drainOne(t, m)

	if string(agg.Data) != "abcdef" {
		t.Fatalf("Data = %q, want abcdef", agg.Data)
	}
	wantIDs := []sm.ReceiveID{1, 2, 3}
	for i, b := range agg.Boundaries {
		if b.RecvID != wantIDs[i] {
			t.Fatalf("boundary %d RecvID = %d, want %d", i, b.RecvID, wantIDs[i])
		}
	}
}

func TestExplicitConfirmationGate(t *testing.T) {
	m := sm.New(sm.Config{FirstReceiveID: 1, RequireConfirmation: true})

	s1 := testSegment(1, 0, "a")
	s2 := testSegment(2, 1, "b")
	s3 := testSegment(3, 2, "c")
	s3.End = true

	mustSubmit(t, m, s1)
	mustSubmit(t, m, s2)
	mustSubmit(t, m, s3)

	// Confirming a later ID early must not overtake the unconfirmed IDs.
	if err := m.Confirm("conn-1", 3); err != nil {
		t.Fatal(err)
	}
	assertNoAggregate(t, m, "later receive ID confirmed before predecessors")

	if err := m.Confirm("conn-1", 1); err != nil {
		t.Fatal(err)
	}
	assertNoAggregate(t, m, "receive ID 2 is still unconfirmed")

	if err := m.Confirm("conn-1", 2); err != nil {
		t.Fatal(err)
	}
	agg := drainOne(t, m)
	if string(agg.Data) != "abc" || agg.Reason != sm.CloseEndFlag {
		t.Fatalf("unexpected aggregate: data=%q reason=%q", agg.Data, agg.Reason)
	}
}

func TestCumulativeConfirmationDoesNotCoverUnknownGap(t *testing.T) {
	m := sm.New(sm.Config{FirstReceiveID: 1, RequireConfirmation: true})

	s1 := testSegment(1, 0, "a")
	s3 := testSegment(3, 1, "c")
	s3.End = true
	mustSubmit(t, m, s1)
	mustSubmit(t, m, s3)

	if err := m.ConfirmThrough("conn-1", 3); err != nil {
		t.Fatal(err)
	}
	assertNoAggregate(t, m, "unknown receive ID 2 remains unconfirmed")

	if err := m.MarkLost("conn-1", 2); err != nil {
		t.Fatal(err)
	}
	aggs := m.Drain()
	if len(aggs) != 2 {
		t.Fatalf("Drain() returned %d aggregates, want loss prefix and terminal suffix", len(aggs))
	}
	if string(aggs[0].Data) != "a" || aggs[0].Reason != sm.CloseReceiveLoss {
		t.Fatalf("prefix data=%q reason=%q", aggs[0].Data, aggs[0].Reason)
	}
	if err := aggs[0].Validate(); err != nil {
		t.Fatal(err)
	}
	if string(aggs[1].Data) != "c" || aggs[1].Reason != sm.CloseEndFlag {
		t.Fatalf("suffix data=%q reason=%q", aggs[1].Data, aggs[1].Reason)
	}
	if err := aggs[1].Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestMissingReceiveIDBlocksAndExplicitLossCutsAggregate(t *testing.T) {
	m := sm.New(sm.Config{FirstReceiveID: 1})

	s1 := testSegment(1, 0, "aa")
	s3 := testSegment(3, 2, "bb") // Sequence is adjacent to s1 on purpose.
	mustSubmit(t, m, s1)
	mustSubmit(t, m, s3)
	assertNoAggregate(t, m, "missing receive ID 2")

	if err := m.MarkLost("conn-1", 2); err != nil {
		t.Fatal(err)
	}
	first := drainOne(t, m)
	if string(first.Data) != "aa" || first.Reason != sm.CloseReceiveLoss {
		t.Fatalf("loss aggregate = %q reason %q", first.Data, first.Reason)
	}

	if err := m.Flush("conn-1"); err != nil {
		t.Fatal(err)
	}
	second := drainOne(t, m)
	if string(second.Data) != "bb" || second.RecvStart != 3 {
		t.Fatalf("post-loss aggregate = %q IDs [%d,%d]", second.Data, second.RecvStart, second.RecvEnd)
	}
}

func TestLossCanBeMarkedBeforeReportAndLateReportIsRejected(t *testing.T) {
	m := sm.New(sm.Config{FirstReceiveID: 1})
	mustSubmit(t, m, testSegment(1, 0, "aa"))

	if err := m.MarkLost("conn-1", 2); err != nil {
		t.Fatal(err)
	}
	first := drainOne(t, m)
	if first.Reason != sm.CloseReceiveLoss {
		t.Fatalf("Reason = %q, want loss", first.Reason)
	}

	late := testSegment(2, 2, "bb")
	err := m.Submit(late)
	if !errors.Is(err, sm.ErrReceiveLost) {
		t.Fatalf("Submit after MarkLost error = %v, want ErrReceiveLost", err)
	}

	mustSubmit(t, m, testSegment(3, 2, "cc"))
	if err := m.Flush("conn-1"); err != nil {
		t.Fatal(err)
	}
	second := drainOne(t, m)
	if string(second.Data) != "cc" {
		t.Fatalf("Data = %q, want cc", second.Data)
	}

	err = m.MarkLost("conn-1", 3)
	if !errors.Is(err, sm.ErrAlreadyReceived) {
		t.Fatalf("MarkLost after report error = %v, want ErrAlreadyReceived", err)
	}
}

func TestCapacityBoundary(t *testing.T) {
	t.Run("exact limit closes immediately", func(t *testing.T) {
		m := sm.New(sm.Config{FirstReceiveID: 1, MaxBytes: 5})
		mustSubmit(t, m, testSegment(1, 0, "ab"))
		assertNoAggregate(t, m, "below exact limit")
		mustSubmit(t, m, testSegment(2, 2, "cde"))

		agg := drainOne(t, m)
		if string(agg.Data) != "abcde" || agg.Reason != sm.CloseCapacity {
			t.Fatalf("data=%q reason=%q", agg.Data, agg.Reason)
		}
	})

	t.Run("next segment overflow closes old group and starts new", func(t *testing.T) {
		m := sm.New(sm.Config{FirstReceiveID: 1, MaxBytes: 5})
		mustSubmit(t, m, testSegment(1, 0, "ab"))
		mustSubmit(t, m, testSegment(2, 2, "cd"))
		assertNoAggregate(t, m, "four bytes are below limit")

		mustSubmit(t, m, testSegment(3, 4, "ef"))
		old := drainOne(t, m)
		if string(old.Data) != "abcd" || old.Reason != sm.CloseCapacity {
			t.Fatalf("old group data=%q reason=%q", old.Data, old.Reason)
		}

		if err := m.Flush("conn-1"); err != nil {
			t.Fatal(err)
		}
		next := drainOne(t, m)
		if string(next.Data) != "ef" || next.Reason != sm.CloseFlush {
			t.Fatalf("new group data=%q reason=%q", next.Data, next.Reason)
		}
	})

	t.Run("segment larger than limit is rejected", func(t *testing.T) {
		m := sm.New(sm.Config{FirstReceiveID: 1, MaxBytes: 1})
		err := m.Submit(testSegment(1, 0, "ab"))
		if !errors.Is(err, sm.ErrSegmentTooLarge) {
			t.Fatalf("Submit error = %v, want ErrSegmentTooLarge", err)
		}
	})
}

func TestEndMarkerClosesImmediatelyAndStartsNewGroup(t *testing.T) {
	m := sm.New(sm.Config{FirstReceiveID: 1, MaxBytes: 100})
	s1 := testSegment(1, 0, "ab")
	s2 := testSegment(2, 2, "cd")
	s2.End = true
	s3 := testSegment(3, 4, "ef")

	mustSubmit(t, m, s1)
	mustSubmit(t, m, s2)
	agg := drainOne(t, m)
	if string(agg.Data) != "abcd" || agg.Reason != sm.CloseEndFlag {
		t.Fatalf("data=%q reason=%q", agg.Data, agg.Reason)
	}

	mustSubmit(t, m, s3)
	assertNoAggregate(t, m, "post-end segment should start an open group")
	if err := m.Flush("conn-1"); err != nil {
		t.Fatal(err)
	}
	next := drainOne(t, m)
	if string(next.Data) != "ef" {
		t.Fatalf("post-end Data = %q, want ef", next.Data)
	}
}

func TestAttributeMismatchCutsGroup(t *testing.T) {
	m := sm.New(sm.Config{FirstReceiveID: 1})

	s1 := testSegment(1, 0, "a")
	s2 := testSegment(2, 1, "b")
	s2.Options = sm.Options{"dscp": "ef"}
	s3 := testSegment(3, 2, "c")
	s3.Protocol = "UDP"

	mustSubmit(t, m, s1)
	mustSubmit(t, m, s2)
	first := drainOne(t, m)
	if string(first.Data) != "a" || first.Reason != sm.CloseAttributes {
		t.Fatalf("first data=%q reason=%q", first.Data, first.Reason)
	}

	mustSubmit(t, m, s3)
	second := drainOne(t, m)
	if string(second.Data) != "b" || second.Reason != sm.CloseAttributes {
		t.Fatalf("second data=%q reason=%q", second.Data, second.Reason)
	}

	if err := m.Flush("conn-1"); err != nil {
		t.Fatal(err)
	}
	third := drainOne(t, m)
	if string(third.Data) != "c" {
		t.Fatalf("third Data = %q, want c", third.Data)
	}
}

func TestNilAndEmptyOptionsAreEquivalent(t *testing.T) {
	m := sm.New(sm.Config{FirstReceiveID: 1})
	s1 := testSegment(1, 0, "a")
	s1.Options = nil
	s2 := testSegment(2, 1, "b")
	s2.Options = sm.Options{}

	mustSubmit(t, m, s1)
	mustSubmit(t, m, s2)
	if err := m.Flush("conn-1"); err != nil {
		t.Fatal(err)
	}
	agg := drainOne(t, m)
	if agg.SegmentCount() != 2 || string(agg.Data) != "ab" {
		t.Fatalf("segments=%d data=%q, want one merged ab", agg.SegmentCount(), agg.Data)
	}
}

func TestSequenceDiscontinuityCutsGroup(t *testing.T) {
	m := sm.New(sm.Config{FirstReceiveID: 1})
	s1 := testSegment(1, 0, "ab")
	s2 := testSegment(2, 5, "cd") // Gap between sequence end 2 and start 5.

	mustSubmit(t, m, s1)
	mustSubmit(t, m, s2)
	first := drainOne(t, m)
	if string(first.Data) != "ab" || first.Reason != sm.CloseSequence {
		t.Fatalf("data=%q reason=%q", first.Data, first.Reason)
	}

	if err := m.Flush("conn-1"); err != nil {
		t.Fatal(err)
	}
	second := drainOne(t, m)
	if string(second.Data) != "cd" {
		t.Fatalf("Data = %q, want cd", second.Data)
	}
}

func TestConnectionsAreIsolatedUnderInterleavedReports(t *testing.T) {
	m := sm.New(sm.Config{FirstReceiveID: 1})

	a1 := testSegment(1, 0, "A1")
	a1.ConnID = "A"
	a2 := testSegment(2, 2, "A2")
	a2.ConnID = "A"
	a2.End = true

	b1 := testSegment(1, 0, "B1")
	b1.ConnID = "B"
	b2 := testSegment(2, 2, "B2")
	b2.ConnID = "B"
	b2.End = true

	mustSubmit(t, m, a1)
	mustSubmit(t, m, b2) // Out of order within B.
	mustSubmit(t, m, a2)

	agg := drainOne(t, m)
	if agg.ConnID != "A" || string(agg.Data) != "A1A2" {
		t.Fatalf("unexpected first aggregate conn=%q data=%q", agg.ConnID, agg.Data)
	}
	assertNoAggregate(t, m, "B1 has not been reported")

	mustSubmit(t, m, b1)
	agg = drainOne(t, m)
	if agg.ConnID != "B" || string(agg.Data) != "B1B2" {
		t.Fatalf("unexpected B aggregate conn=%q data=%q", agg.ConnID, agg.Data)
	}
}

func TestFlushDoesNotSkipReceiveGap(t *testing.T) {
	m := sm.New(sm.Config{FirstReceiveID: 1})
	mustSubmit(t, m, testSegment(1, 0, "a"))
	mustSubmit(t, m, testSegment(3, 1, "c"))

	if err := m.Flush("conn-1"); err != nil {
		t.Fatal(err)
	}
	agg := drainOne(t, m)
	if string(agg.Data) != "a" || agg.Reason != sm.CloseFlush {
		t.Fatalf("flushed data=%q reason=%q", agg.Data, agg.Reason)
	}
	if got := m.NextReceiveID("conn-1"); got != 2 {
		t.Fatalf("NextReceiveID = %d, want 2; flush must not skip gap", got)
	}

	if err := m.MarkLost("conn-1", 2); err != nil {
		t.Fatal(err)
	}
	if err := m.Flush("conn-1"); err != nil {
		t.Fatal(err)
	}
	agg = drainOne(t, m)
	if string(agg.Data) != "c" {
		t.Fatalf("buffered later segment data=%q, want c", agg.Data)
	}
}

func TestSubmitCopiesCallerBuffersAndOptions(t *testing.T) {
	m := sm.New(sm.Config{FirstReceiveID: 1})
	seg := testSegment(1, 0, "abc")
	mustSubmit(t, m, seg)

	seg.Data[0] = 'X'
	seg.Options["dscp"] = "mutated"

	if err := m.Flush("conn-1"); err != nil {
		t.Fatal(err)
	}
	agg := drainOne(t, m)
	if string(agg.Data) != "abc" {
		t.Fatalf("stored Data = %q, want caller mutation to be isolated", agg.Data)
	}
	if agg.Options["dscp"] != "cs1" {
		t.Fatalf("stored Options = %v, want cs1", agg.Options)
	}
}

func TestInvalidSegmentsAreRejected(t *testing.T) {
	tests := []struct {
		name string
		seg  sm.Segment
		want error
	}{
		{
			name: "empty connection",
			seg:  sm.Segment{RecvID: 1, SeqStart: 0, SeqEnd: 0},
			want: sm.ErrEmptyConnID,
		},
		{
			name: "reversed sequence",
			seg:  sm.Segment{ConnID: "c", RecvID: 1, SeqStart: 3, SeqEnd: 2},
			want: sm.ErrInvalidSegment,
		},
		{
			name: "payload length mismatch",
			seg:  sm.Segment{ConnID: "c", RecvID: 1, SeqStart: 0, SeqEnd: 3, Data: []byte("ab")},
			want: sm.ErrInvalidSegment,
		},
	}
	m := sm.New(sm.Config{FirstReceiveID: 1})
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := m.Submit(tc.seg)
			if !errors.Is(err, tc.want) {
				t.Fatalf("Submit error = %v, want %v", err, tc.want)
			}
		})
	}

	err := m.Submit(testSegment(0, 0, "x"))
	if !errors.Is(err, sm.ErrInvalidSegment) {
		t.Fatalf("receive ID before configured first error = %v, want ErrInvalidSegment", err)
	}
}

func TestDuplicateReceiveIDIsRejected(t *testing.T) {
	m := sm.New(sm.Config{FirstReceiveID: 1})
	seg := testSegment(1, 0, "a")
	mustSubmit(t, m, seg)
	err := m.Submit(seg)
	if !errors.Is(err, sm.ErrDuplicateReceive) {
		t.Fatalf("duplicate Submit error = %v, want ErrDuplicateReceive", err)
	}
}

func TestConcurrentHardwareQueueReports(t *testing.T) {
	const n = 64
	m := sm.New(sm.Config{FirstReceiveID: 1, MaxBytes: 4096})

	var wg sync.WaitGroup
	errs := make(chan error, n)
	for id := uint64(1); id <= n; id++ {
		wg.Add(1)
		go func(id uint64) {
			defer wg.Done()
			payload := fmt.Sprintf("%02d", id) // Two bytes per segment.
			seq := (id - 1) * 2
			seg := testSegment(id, seq, payload)
			if err := m.Submit(seg); err != nil {
				errs <- err
			}
		}(id)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent Submit: %v", err)
	}

	if err := m.Flush("conn-1"); err != nil {
		t.Fatal(err)
	}
	aggs := m.Drain()
	if len(aggs) != 1 {
		t.Fatalf("len(Aggregates) = %d, want 1", len(aggs))
	}
	agg := aggs[0]
	if err := agg.Validate(); err != nil {
		t.Fatal(err)
	}
	if agg.SegmentCount() != n {
		t.Fatalf("SegmentCount = %d, want %d", agg.SegmentCount(), n)
	}
	if agg.Len() != 2*n {
		t.Fatalf("Len = %d, want %d", agg.Len(), 2*n)
	}

	var want strings.Builder
	for id := uint64(1); id <= n; id++ {
		want.WriteString(fmt.Sprintf("%02d", id))
	}
	if string(agg.Data) != want.String() {
		t.Fatalf("coalesced payload does not match receive-ID order")
	}
}
