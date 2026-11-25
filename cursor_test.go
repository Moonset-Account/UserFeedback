package logcursor

import (
	"encoding/json"
	"errors"
	"reflect"
	"sort"
	"sync"
	"testing"
)

// ---------------------------------------------------------------- helpers

func mustAppend(t *testing.T, l *Log, step string, attempt int, off int64, s string) {
	t.Helper()
	if err := l.AppendFragment(step, attempt, off, []byte(s)); err != nil {
		t.Fatalf("AppendFragment(%s,%d,%d,%q): %v", step, attempt, off, s, err)
	}
}

func mustGap(t *testing.T, l *Log, step string, attempt int, start, end int64) {
	t.Helper()
	if err := l.DeclareGap(step, attempt, start, end); err != nil {
		t.Fatalf("DeclareGap(%s,%d,%d,%d): %v", step, attempt, start, end, err)
	}
}

func mustEnd(t *testing.T, l *Log, step string, attempt int, off int64) {
	t.Helper()
	if err := l.DeclareEnd(step, attempt, off); err != nil {
		t.Fatalf("DeclareEnd(%s,%d,%d): %v", step, attempt, off, err)
	}
}

func read(t *testing.T, l *Log, c Cursor, limit int) Page {
	t.Helper()
	p, err := l.Read(c, limit)
	if err != nil {
		t.Fatalf("Read(limit=%d): %v", limit, err)
	}
	return p
}

func stepOf(t *testing.T, p Page, name string) StepData {
	t.Helper()
	sd, ok := p.Step(name)
	if !ok {
		t.Fatalf("page has no step %q", name)
	}
	return sd
}

func mustState(t *testing.T, c Cursor, name string) StepState {
	t.Helper()
	st, ok := c.StepState(name)
	if !ok {
		t.Fatalf("cursor has no step %q", name)
	}
	return st
}

func seqs(refs []FragmentRef) []int {
	out := make([]int, len(refs))
	for i, r := range refs {
		out[i] = r.Seq
	}
	return out
}

// ---------------------------------------------------------------- normal

func TestSingleStepContiguousRead(t *testing.T) {
	l := NewLog()
	mustAppend(t, l, "build", 0, 0, "hello ")
	mustAppend(t, l, "build", 0, 6, "world")
	mustEnd(t, l, "build", 0, 11)

	p := read(t, l, Cursor{}, 100)
	sd := stepOf(t, p, "build")
	if got := string(sd.Data); got != "hello world" {
		t.Fatalf("data = %q, want %q", got, "hello world")
	}
	if !sd.Ended {
		t.Fatal("step should be ended")
	}
	if sd.Waiting {
		t.Fatal("ended step must not be waiting")
	}
	if !p.Done {
		t.Fatal("page should be done")
	}
	if p.Bytes != 11 {
		t.Fatalf("bytes = %d, want 11", p.Bytes)
	}
	if want := []int{0, 1}; !reflect.DeepEqual(seqs(sd.Fragments), want) {
		t.Fatalf("fragment seqs = %v, want %v", seqs(sd.Fragments), want)
	}
	if !p.Cursor.Pinned || p.Cursor.Snapshot != l.Version() {
		t.Fatalf("cursor not pinned at current version: %+v", p.Cursor)
	}
}

func TestPerStepFragmentNumberingRestartsPerAttempt(t *testing.T) {
	l := NewLog()
	// Attempt 0.
	mustAppend(t, l, "s", 0, 0, "aa")
	// Retry: attempt 1 numbers its fragments from zero again.
	mustAppend(t, l, "s", 1, 0, "bb")
	mustEnd(t, l, "s", 1, 2)

	p := read(t, l, Cursor{}, 100)
	sd := stepOf(t, p, "s")
	if sd.Attempt != 1 {
		t.Fatalf("attempt = %d, want 1 (latest visible)", sd.Attempt)
	}
	if got := string(sd.Data); got != "bb" {
		t.Fatalf("data = %q, want %q", got, "bb")
	}
	if want := []int{0}; !reflect.DeepEqual(seqs(sd.Fragments), want) {
		t.Fatalf("fragment seqs = %v, want %v", seqs(sd.Fragments), want)
	}
}

// ---------------------------------------------------------------- holes

func TestSlowStepHoleDoesNotBlockOtherSteps(t *testing.T) {
	l := NewLog()
	mustAppend(t, l, "slow", 0, 0, "AAA")
	mustAppend(t, l, "slow", 0, 6, "CCC") // hole [3,6)
	mustAppend(t, l, "fast", 0, 0, "BB")
	mustEnd(t, l, "fast", 0, 2)

	p := read(t, l, Cursor{}, 100)
	slow := stepOf(t, p, "slow")
	fast := stepOf(t, p, "fast")

	if got := string(slow.Data); got != "AAA" {
		t.Fatalf("slow data = %q, want %q", got, "AAA")
	}
	if !slow.Waiting || slow.WaitingAt != 3 {
		t.Fatalf("slow should wait at 3, got waiting=%v at=%d", slow.Waiting, slow.WaitingAt)
	}
	if slow.Ended {
		t.Fatal("slow must not be ended while the hole is open")
	}
	if got := string(fast.Data); got != "BB" {
		t.Fatalf("fast data = %q, want %q", got, "BB")
	}
	if !fast.Ended || fast.Waiting {
		t.Fatalf("fast should be ended and not waiting")
	}
	if p.Done {
		t.Fatal("page must not be done while slow has a hole")
	}

	// Fill the hole, then advance the cursor to the new snapshot.
	mustAppend(t, l, "slow", 0, 3, "BBB")
	mustEnd(t, l, "slow", 0, 9)
	c, err := l.Advance(p.Cursor)
	if err != nil {
		t.Fatalf("Advance: %v", err)
	}
	p2 := read(t, l, c, 100)
	slow2 := stepOf(t, p2, "slow")
	if got := string(slow2.Data); got != "BBBCCC" {
		t.Fatalf("slow data after hole = %q, want %q", got, "BBBCCC")
	}
	if !slow2.Ended {
		t.Fatal("slow should be ended after hole filled")
	}
	if !p2.Done {
		t.Fatal("page should be done once every step ended")
	}
}

func TestEndMarkerBeforeDataWaitsForHole(t *testing.T) {
	l := NewLog()
	// End is published before all bytes have arrived.
	mustEnd(t, l, "s", 0, 6)
	mustAppend(t, l, "s", 0, 0, "ab")
	mustAppend(t, l, "s", 0, 4, "ef") // hole [2,4)

	p := read(t, l, Cursor{}, 100)
	sd := stepOf(t, p, "s")
	if got := string(sd.Data); got != "ab" {
		t.Fatalf("data = %q, want %q", got, "ab")
	}
	if sd.Ended {
		t.Fatal("end marker must not finish the step while a hole is open")
	}
	if !sd.Waiting || sd.WaitingAt != 2 {
		t.Fatalf("should wait at 2, got waiting=%v at=%d", sd.Waiting, sd.WaitingAt)
	}
	if p.Done {
		t.Fatal("page must not be done")
	}

	// The hole is declared permanently lost instead of being filled.
	mustGap(t, l, "s", 0, 2, 4)
	c, err := l.Advance(p.Cursor)
	if err != nil {
		t.Fatalf("Advance: %v", err)
	}
	p2 := read(t, l, c, 100)
	sd2 := stepOf(t, p2, "s")
	if got := string(sd2.Data); got != "ef" {
		t.Fatalf("data after gap = %q, want %q", got, "ef")
	}
	if len(sd2.Gaps) != 1 || sd2.Gaps[0].Start != 2 || sd2.Gaps[0].End != 4 {
		t.Fatalf("gap notice = %+v, want [2,4)", sd2.Gaps)
	}
	if !sd2.Ended || !p2.Done {
		t.Fatalf("step and page should be done after gap closes the stream")
	}
}

// ---------------------------------------------------------------- gaps

func TestGapNoticeEmittedExactlyOnce(t *testing.T) {
	l := NewLog()
	mustAppend(t, l, "s", 0, 0, "ab")
	mustGap(t, l, "s", 0, 2, 6)
	mustAppend(t, l, "s", 0, 6, "gh")
	mustEnd(t, l, "s", 0, 8)

	// First page stops inside the gap: the notice must fire on entry only.
	p1 := read(t, l, Cursor{}, 4) // "ab" (2) + first 2 gap bytes
	sd1 := stepOf(t, p1, "s")
	if len(sd1.Gaps) != 1 || sd1.Gaps[0].Start != 2 || sd1.Gaps[0].End != 6 {
		t.Fatalf("first page gap notices = %+v, want one [2,6)", sd1.Gaps)
	}
	if got := string(sd1.Data); got != "ab" {
		t.Fatalf("first page data = %q, want %q", got, "ab")
	}
	if st := mustState(t, p1.Cursor, "s"); st.Offset != 4 {
		t.Fatalf("offset = %d, want 4 (inside gap)", st.Offset)
	}

	// Reconnect: serialise, restore, continue. No duplicate notice.
	blob, err := json.Marshal(p1.Cursor)
	if err != nil {
		t.Fatalf("marshal cursor: %v", err)
	}
	var restored Cursor
	if err := json.Unmarshal(blob, &restored); err != nil {
		t.Fatalf("unmarshal cursor: %v", err)
	}
	p2 := read(t, l, restored, 100)
	sd2 := stepOf(t, p2, "s")
	if len(sd2.Gaps) != 0 {
		t.Fatalf("gap re-announced after reconnect: %+v", sd2.Gaps)
	}
	if got := string(sd2.Data); got != "gh" {
		t.Fatalf("data after gap = %q, want %q", got, "gh")
	}
	if !sd2.Ended || !p2.Done {
		t.Fatal("stream should finish after the gap and trailing fragment")
	}
}

func TestGapFirstThenEndMarker(t *testing.T) {
	l := NewLog()
	mustEnd(t, l, "s", 0, 4)
	mustAppend(t, l, "s", 0, 0, "ab")
	mustGap(t, l, "s", 0, 2, 4)

	p := read(t, l, Cursor{}, 100)
	sd := stepOf(t, p, "s")
	if got := string(sd.Data); got != "ab" {
		t.Fatalf("data = %q, want %q", got, "ab")
	}
	if len(sd.Gaps) != 1 {
		t.Fatalf("want one gap notice, got %+v", sd.Gaps)
	}
	if !sd.Ended || !p.Done {
		t.Fatal("gap reaching the declared end should finish the step")
	}
}

// ---------------------------------------------------------------- paging

func TestPageLimitSplitsInsideFragmentAndResumesAtExactByte(t *testing.T) {
	l := NewLog()
	payload := "0123456789"
	mustAppend(t, l, "s", 0, 0, payload)
	mustEnd(t, l, "s", 0, int64(len(payload)))

	var got string
	c := Cursor{}
	n := 0
	for {
		p := read(t, l, c, 3)
		sd := stepOf(t, p, "s")
		got += string(sd.Data)
		c = p.Cursor
		if p.Done {
			break
		}
		if len(sd.Data) == 0 {
			t.Fatalf("page %d made no progress: %+v", n, sd)
		}
		n++
		if n > 20 {
			t.Fatal("too many pages")
		}
	}
	if got != payload {
		t.Fatalf("reassembled = %q, want %q", got, payload)
	}
	if st := mustState(t, c, "s"); st.Offset != int64(len(payload)) {
		t.Fatalf("final offset = %d, want %d", st.Offset, len(payload))
	}
}

func TestPageLimitIsTotalAcrossSteps(t *testing.T) {
	l := NewLog()
	mustAppend(t, l, "a", 0, 0, "AAAAA")
	mustAppend(t, l, "b", 0, 0, "BBBBB")
	mustEnd(t, l, "a", 0, 5)
	mustEnd(t, l, "b", 0, 5)

	p := read(t, l, Cursor{}, 7)
	if p.Bytes != 7 {
		t.Fatalf("page bytes = %d, want 7 (total, not per step)", p.Bytes)
	}
	a := stepOf(t, p, "a")
	b := stepOf(t, p, "b")
	if len(a.Data)+len(b.Data) != 7 {
		t.Fatalf("delivered %d+%d bytes, want total 7", len(a.Data), len(b.Data))
	}
	// Round-robin fairness: the first step should have taken its full fragment.
	if got := string(a.Data); got != "AAAAA" {
		t.Fatalf("a data = %q, want full fragment", got)
	}
	if got := string(b.Data); got != "BB" {
		t.Fatalf("b data = %q, want the remaining 2 bytes", got)
	}
	if p.Done {
		t.Fatal("page cannot be done: budget ran out mid-stream")
	}
}

// ---------------------------------------------------------------- retries

func TestLateOldAttemptIsExcludedAfterAdvance(t *testing.T) {
	l := NewLog()
	mustAppend(t, l, "s", 0, 0, "old")
	mustEnd(t, l, "s", 0, 6)
	mustAppend(t, l, "s", 0, 4, "!!") // old attempt has a hole at [3,4)

	p := read(t, l, Cursor{}, 100)
	if got := string(stepOf(t, p, "s").Data); got != "old" {
		t.Fatalf("first attempt data = %q", got)
	}
	if p.Done {
		t.Fatal("attempt 0 still has an open hole")
	}

	// Retry begins before the old attempt's last byte shows up.
	mustAppend(t, l, "s", 1, 0, "new")
	mustEnd(t, l, "s", 1, 3)

	// Without an explicit advance the pinned cursor must not see the retry.
	stale := read(t, l, p.Cursor, 100)
	if got := string(stepOf(t, stale, "s").Data); got != "" {
		t.Fatalf("pinned cursor leaked post-pin content: %q", got)
	}
	if stale.Done {
		t.Fatal("pinned snapshot still has the old hole open")
	}

	// The old attempt's straggler finally arrives, after the retry started.
	mustAppend(t, l, "s", 0, 3, "?")

	c, err := l.Advance(p.Cursor)
	if err != nil {
		t.Fatalf("Advance: %v", err)
	}
	st := mustState(t, c, "s")
	if st.Attempt != 1 || st.Offset != 0 {
		t.Fatalf("advanced state = %+v, want attempt 1 at offset 0", st)
	}
	p2 := read(t, l, c, 100)
	sd2 := stepOf(t, p2, "s")
	if sd2.Attempt != 1 {
		t.Fatalf("attempt = %d, want 1", sd2.Attempt)
	}
	if got := string(sd2.Data); got != "new" {
		t.Fatalf("retry data = %q, want %q (old content must not mix in)", got, "new")
	}
	if !sd2.Ended || !p2.Done {
		t.Fatal("retry stream should be finished")
	}
}

func TestLateOldAttemptFragmentIgnoredByPinnedSnapshot(t *testing.T) {
	l := NewLog()
	mustAppend(t, l, "s", 0, 0, "ab")
	mustEnd(t, l, "s", 0, 4) // end declared, hole at [2,4)
	p := read(t, l, Cursor{}, 100)
	if p.Done {
		t.Fatal("attempt 0 still has an open hole")
	}
	if got := string(stepOf(t, p, "s").Data); got != "ab" {
		t.Fatalf("data = %q, want %q", got, "ab")
	}

	// After the page is pinned, an old-attempt straggler fills the hole; the
	// page pinned earlier must stay byte-for-byte identical.
	before := read(t, l, p.Cursor, 100) // pinned start of the next page
	mustAppend(t, l, "s", 0, 2, "LT")
	after := read(t, l, p.Cursor, 100)
	if !reflect.DeepEqual(before, after) {
		t.Fatalf("pinned re-read changed:\n before=%+v\n after=%+v", before, after)
	}
	if after.Done {
		t.Fatal("old attempt straggler advanced the pinned stream")
	}
	if got := string(stepOf(t, after, "s").Data); got != "" {
		t.Fatalf("late straggler surfaced: %q", got)
	}
}

// ---------------------------------------------------------------- snapshots / reproducibility

func TestNewStepInvisibleUntilAdvance(t *testing.T) {
	l := NewLog()
	mustAppend(t, l, "a", 0, 0, "x")
	mustEnd(t, l, "a", 0, 1)

	p := read(t, l, Cursor{}, 100)
	if !p.Done {
		t.Fatal("page should be done with only step a")
	}

	mustAppend(t, l, "b", 0, 0, "y")
	mustEnd(t, l, "b", 0, 1)

	pinned := read(t, l, p.Cursor, 100)
	if _, ok := pinned.Step("b"); ok {
		t.Fatal("step b must stay invisible to the pinned cursor")
	}
	if !pinned.Done {
		t.Fatal("pinned snapshot should still be done")
	}

	c, err := l.Advance(p.Cursor)
	if err != nil {
		t.Fatalf("Advance: %v", err)
	}
	after := read(t, l, c, 100)
	b, ok := after.Step("b")
	if !ok {
		t.Fatal("step b should appear after advance")
	}
	if got := string(b.Data); got != "y" {
		t.Fatalf("b data = %q, want %q", got, "y")
	}
}

func TestSamePinnedCursorRereadsIdenticalPage(t *testing.T) {
	l := NewLog()
	mustAppend(t, l, "s", 0, 0, "abcdefgh")
	mustEnd(t, l, "s", 0, 8)

	first := read(t, l, Cursor{}, 3)
	c := first.Cursor // pinned start of page 2

	one := read(t, l, c, 3)

	// Mutate the backend between the two reads of the same pinned page.
	mustAppend(t, l, "s", 1, 0, "retry")
	mustAppend(t, l, "extra", 0, 0, "zzz")

	two := read(t, l, c, 3)
	if !reflect.DeepEqual(one, two) {
		t.Fatalf("re-reading the same pinned cursor changed the page:\n one=%+v\n two=%+v", one, two)
	}
	if got := string(stepOf(t, one, "s").Data); got != "def" {
		t.Fatalf("page2 data = %q, want %q", got, "def")
	}
}

func TestCursorJSONRoundTripResumesExactly(t *testing.T) {
	l := NewLog()
	mustAppend(t, l, "s", 0, 0, "abcdef")
	mustEnd(t, l, "s", 0, 6)

	p := read(t, l, Cursor{}, 2) // "ab"
	blob, err := json.Marshal(p.Cursor)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var restored Cursor
	if err := json.Unmarshal(blob, &restored); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	direct := read(t, l, p.Cursor, 100)
	fromJSON := read(t, l, restored, 100)
	if !reflect.DeepEqual(direct, fromJSON) {
		t.Fatalf("restored cursor diverged:\n direct=%+v\n json=%+v", direct, fromJSON)
	}
	if got := string(stepOf(t, fromJSON, "s").Data); got != "cdef" {
		t.Fatalf("resumed data = %q, want %q", got, "cdef")
	}
}

func TestZeroLimitReadsNoBytes(t *testing.T) {
	l := NewLog()
	mustAppend(t, l, "s", 0, 0, "abc")
	p := read(t, l, Cursor{}, 0)
	if p.Bytes != 0 {
		t.Fatalf("bytes = %d, want 0", p.Bytes)
	}
	if got := string(stepOf(t, p, "s").Data); got != "" {
		t.Fatalf("data = %q, want empty", got)
	}
	if p.Done {
		t.Fatal("stream is not ended")
	}
	if st := mustState(t, p.Cursor, "s"); st.Offset != 0 {
		t.Fatalf("offset = %d, want 0", st.Offset)
	}
}

// ---------------------------------------------------------------- validation

func TestBackendValidation(t *testing.T) {
	l := NewLog()

	if err := l.AppendFragment("", 0, 0, []byte("x")); !errors.Is(err, ErrEmptyStep) {
		t.Fatalf("empty step: %v", err)
	}
	if err := l.AppendFragment("s", -1, 0, []byte("x")); !errors.Is(err, ErrInvalidAttempt) {
		t.Fatalf("negative attempt: %v", err)
	}
	if err := l.AppendFragment("s", 0, 0, nil); !errors.Is(err, ErrEmptyFragment) {
		t.Fatalf("empty fragment: %v", err)
	}
	if err := l.DeclareGap("s", 0, 3, 3); !errors.Is(err, ErrInvalidGap) {
		t.Fatalf("empty gap: %v", err)
	}

	mustAppend(t, l, "s", 0, 0, "abcd")
	if err := l.AppendFragment("s", 0, 2, []byte("ZZ")); !errors.Is(err, ErrFragmentOverlap) {
		t.Fatalf("overlap: %v", err)
	}
	// Identical re-append is idempotent.
	if err := l.AppendFragment("s", 0, 0, []byte("abcd")); err != nil {
		t.Fatalf("idempotent append: %v", err)
	}
	if err := l.DeclareGap("s", 0, 1, 3); !errors.Is(err, ErrGapOverlapsFragment) {
		t.Fatalf("gap over fragment: %v", err)
	}
	mustEnd(t, l, "s", 0, 4)
	if err := l.AppendFragment("s", 0, 4, []byte("x")); !errors.Is(err, ErrDataAfterEnd) {
		t.Fatalf("data after end: %v", err)
	}
	if err := l.DeclareEnd("s", 0, 5); !errors.Is(err, ErrEndConflict) {
		t.Fatalf("end conflict: %v", err)
	}
	if err := l.DeclareGap("s", 0, 4, 6); !errors.Is(err, ErrDataAfterEnd) {
		t.Fatalf("gap past end: %v", err)
	}
	if _, err := l.Read(Cursor{}, -1); !errors.Is(err, ErrInvalidLimit) {
		t.Fatalf("negative limit: %v", err)
	}
}

func TestInvalidCursorRejected(t *testing.T) {
	l := NewLog()
	bad := Cursor{Pinned: true, Snapshot: 0, Steps: []StepState{
		{Step: "b"}, {Step: "a"},
	}}
	if _, err := l.Read(bad, 10); !errors.Is(err, ErrInvalidCursor) {
		t.Fatalf("unsorted steps: %v", err)
	}
}

func TestSealBlocksMutation(t *testing.T) {
	l := NewLog()
	mustAppend(t, l, "s", 0, 0, "x")
	l.Seal()
	if !l.Sealed() {
		t.Fatal("Sealed() = false after Seal")
	}
	if err := l.AppendFragment("s", 0, 1, []byte("y")); !errors.Is(err, ErrSealed) {
		t.Fatalf("append after seal: %v", err)
	}
	p := read(t, l, Cursor{}, 10)
	if got := string(stepOf(t, p, "s").Data); got != "x" {
		t.Fatalf("sealed log data = %q", got)
	}
}

func TestHasGapRange(t *testing.T) {
	l := NewLog()
	mustGap(t, l, "s", 0, 2, 4)
	mustGap(t, l, "s", 0, 4, 6)
	if !l.HasGapRange("s", 0, 2, 6) {
		t.Fatal("expected [2,6) fully covered")
	}
	if l.HasGapRange("s", 0, 1, 3) {
		t.Fatal("[1,3) is not fully covered")
	}
}

// ---------------------------------------------------------------- fuzz-ish interleaving

// TestInterleavedMultiStepStreams drives several steps through random-looking
// hole/gap/end ordering and checks that the concatenated pages equal the
// expected stream for every step.
func TestInterleavedMultiStepStreams(t *testing.T) {
	l := NewLog()

	type spec struct {
		step     string
		chunks   map[int64]string
		gaps     [][2]int64
		end      int64
		expected string
	}
	specs := []spec{
		{
			step:     "alpha",
			chunks:   map[int64]string{0: "he", 2: "llo", 5: "!!"},
			end:      7,
			expected: "hello!!",
		},
		{
			step:     "beta",
			chunks:   map[int64]string{3: "world", 0: "hi-"},
			gaps:     [][2]int64{{8, 10}},
			end:      10,
			expected: "hi-world",
		},
		{
			step:     "gamma",
			chunks:   map[int64]string{0: "abc"},
			gaps:     [][2]int64{{3, 5}},
			end:      5,
			expected: "abc",
		},
	}

	// Append everything, publishing ends before some fragments arrive.
	for _, sp := range specs {
		for off, s := range sp.chunks {
			mustAppend(t, l, sp.step, 0, off, s)
		}
	}
	for _, sp := range specs {
		mustEnd(t, l, sp.step, 0, sp.end)
		for _, g := range sp.gaps {
			mustGap(t, l, sp.step, 0, g[0], g[1])
		}
	}

	got := map[string]string{}
	notices := map[string]int{}
	c := Cursor{}
	for iter := 0; ; iter++ {
		if iter > 100 {
			t.Fatal("did not converge")
		}
		p := read(t, l, c, 4) // small total budget to force interleaving
		for _, sd := range p.Steps {
			got[sd.Step] += string(sd.Data)
			notices[sd.Step] += len(sd.Gaps)
		}
		c = p.Cursor
		if p.Done {
			break
		}
	}

	for _, sp := range specs {
		if got[sp.step] != sp.expected {
			t.Errorf("%s: got %q, want %q", sp.step, got[sp.step], sp.expected)
		}
		if want := len(sp.gaps); notices[sp.step] != want {
			t.Errorf("%s: %d gap notices, want %d", sp.step, notices[sp.step], want)
		}
	}
}

// TestConcurrentAppendAndRead exercises the backend under -race.
func TestConcurrentAppendAndRead(t *testing.T) {
	l := NewLog()
	const workers = 6
	const perWorker = 30
	const width = 4

	var wg sync.WaitGroup
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			step := "s"
			for i := 0; i < perWorker; i++ {
				off := int64((w*perWorker + i) * width)
				payload := []byte{'a' + byte(w), '0' + byte(i/10), '0' + byte(i%10), '.'}
				if err := l.AppendFragment(step, 0, off, payload); err != nil {
					t.Errorf("append: %v", err)
					return
				}
			}
		}(w)
	}
	// Concurrent reader that pins and advances repeatedly.
	stop := make(chan struct{})
	var readerErr error
	var rmu sync.Mutex
	go func() {
		c := Cursor{}
		for {
			select {
			case <-stop:
				return
			default:
			}
			p, err := l.Read(c, 16)
			if err != nil {
				rmu.Lock()
				readerErr = err
				rmu.Unlock()
				return
			}
			c = p.Cursor
			if nc, err := l.Advance(c); err == nil {
				c = nc
			}
		}
	}()

	wg.Wait()
	close(stop)
	rmu.Lock()
	if readerErr != nil {
		t.Fatalf("concurrent read: %v", readerErr)
	}
	rmu.Unlock()

	total := int64(workers * perWorker * width)
	mustEnd(t, l, "s", 0, total)
	c := Cursor{}
	var collected []byte
	for iter := 0; ; iter++ {
		if iter > 10000 {
			t.Fatal("did not converge")
		}
		p := read(t, l, c, 64)
		collected = append(collected, stepOf(t, p, "s").Data...)
		c = p.Cursor
		if p.Done {
			break
		}
	}
	if int64(len(collected)) != total {
		t.Fatalf("collected %d bytes, want %d", len(collected), total)
	}
	// Every 4-byte slot must be intact.
	for i := 0; i < workers*perWorker; i++ {
		slot := collected[i*width : (i+1)*width]
		w := i / perWorker
		n := i % perWorker
		want := []byte{'a' + byte(w), '0' + byte(n/10), '0' + byte(n%10), '.'}
		if !reflect.DeepEqual(slot, want) {
			t.Fatalf("slot %d = %q, want %q", i, slot, want)
		}
	}
}

// TestPageStepOrderStable ensures page step order matches sorted step names.
func TestPageStepOrderStable(t *testing.T) {
	l := NewLog()
	for _, s := range []string{"z", "a", "m"} {
		mustAppend(t, l, s, 0, 0, s)
		mustEnd(t, l, s, 0, 1)
	}
	p := read(t, l, Cursor{}, 100)
	var names []string
	for _, sd := range p.Steps {
		names = append(names, sd.Step)
	}
	if !sort.StringsAreSorted(names) {
		t.Fatalf("page steps not sorted: %v", names)
	}
}

func TestEmptyBackendIsDone(t *testing.T) {
	l := NewLog()
	p := read(t, l, Cursor{}, 10)
	if !p.Done {
		t.Fatal("an empty backend has nothing outstanding")
	}
	if p.Bytes != 0 || len(p.Steps) != 0 {
		t.Fatalf("empty page = %+v", p)
	}
	if !p.Cursor.Pinned || p.Cursor.Snapshot != 0 {
		t.Fatalf("cursor = %+v, want pinned at version 0", p.Cursor)
	}
}

func TestEmptyStreamWithZeroEndIsDone(t *testing.T) {
	l := NewLog()
	mustEnd(t, l, "s", 0, 0)
	p := read(t, l, Cursor{}, 10)
	sd := stepOf(t, p, "s")
	if !sd.Ended || sd.Waiting || !p.Done {
		t.Fatalf("zero-length stream should be done: %+v", sd)
	}
	if len(sd.Data) != 0 {
		t.Fatalf("data = %q, want empty", sd.Data)
	}
}

func TestGapSpansMultiplePagesAnnouncedOnce(t *testing.T) {
	l := NewLog()
	mustGap(t, l, "s", 0, 0, 7)
	mustEnd(t, l, "s", 0, 7)

	notices := 0
	var offsets []int64
	c := Cursor{}
	for i := 0; ; i++ {
		if i > 20 {
			t.Fatal("did not converge")
		}
		p := read(t, l, c, 3)
		sd := stepOf(t, p, "s")
		notices += len(sd.Gaps)
		offsets = append(offsets, sd.Offset)
		c = p.Cursor
		if p.Done {
			break
		}
	}
	if notices != 1 {
		t.Fatalf("gap notices = %d, want exactly 1 across pages", notices)
	}
	if want := []int64{3, 6, 7}; !reflect.DeepEqual(offsets, want) {
		t.Fatalf("offsets = %v, want %v (limit 3 per page)", offsets, want)
	}
}

func TestAdvancePreservesPositionOfPartiallyReadStep(t *testing.T) {
	l := NewLog()
	mustAppend(t, l, "s", 0, 0, "abcdef")
	mustEnd(t, l, "s", 0, 6)

	p := read(t, l, Cursor{}, 2)      // "ab"
	mustAppend(t, l, "t", 0, 0, "xy") // new step, no end yet
	c, err := l.Advance(p.Cursor)
	if err != nil {
		t.Fatalf("Advance: %v", err)
	}
	if st := mustState(t, c, "s"); st.Offset != 2 || st.Attempt != 0 {
		t.Fatalf("s state after advance = %+v, want offset 2 attempt 0", st)
	}
	p2 := read(t, l, c, 100)
	if got := string(stepOf(t, p2, "s").Data); got != "cdef" {
		t.Fatalf("s resumed = %q, want %q", got, "cdef")
	}
	if got := string(stepOf(t, p2, "t").Data); got != "xy" {
		t.Fatalf("t data = %q, want %q", got, "xy")
	}
}

func TestCursorFromFutureSnapshotRejected(t *testing.T) {
	l := NewLog()
	mustAppend(t, l, "s", 0, 0, "x")
	bad := Cursor{Pinned: true, Snapshot: l.Version() + 5, Steps: []StepState{{Step: "s", Attempt: 0}}}
	if _, err := l.Read(bad, 10); !errors.Is(err, ErrInvalidCursor) {
		t.Fatalf("future snapshot: %v", err)
	}
}

func TestCursorUnknownStepRejected(t *testing.T) {
	l := NewLog()
	mustAppend(t, l, "s", 0, 0, "x")
	v := l.Version()
	bad := Cursor{Pinned: true, Snapshot: v, Steps: []StepState{{Step: "ghost", Attempt: 0}}}
	if _, err := l.Read(bad, 10); !errors.Is(err, ErrInvalidCursor) {
		t.Fatalf("unknown step: %v", err)
	}
}
