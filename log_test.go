package compactlog

import (
	"errors"
	"testing"
	"time"
)

var base = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

func at(sec int) time.Time { return base.Add(time.Duration(sec) * time.Second) }

// allMessages flattens the log's surviving messages in log order.
func allMessages(l *Log) []Message {
	var out []Message
	for _, s := range l.Segments() {
		out = append(out, s.Messages...)
	}
	return out
}

func offsets(ms []Message) []int64 {
	out := make([]int64, len(ms))
	for i, m := range ms {
		out[i] = m.Offset
	}
	return out
}

func equalOffsets(a, b []int64) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// TestCleanDropsStaleValues covers the normal path: within the selected
// segments an older record is dropped because a newer record for the same
// key exists, and the newer record may live outside the selection.
func TestCleanDropsStaleValues(t *testing.T) {
	l := NewLog()
	l.Append("a", []byte("v1"), at(0)) // offset 0, seg 0
	l.Append("b", []byte("v1"), at(1)) // offset 1, seg 0
	l.Seal()                           // seg 0 sealed
	l.Append("a", []byte("v2"), at(2)) // offset 2, seg 1
	l.Seal()                           // seg 1 sealed
	l.Append("c", []byte("v1"), at(3)) // offset 3, active seg 2

	// Clean only segment 0. "a"@0 is stale because of "a"@2 which lives
	// OUTSIDE the selection; "b"@1 is the latest for its key and stays.
	if err := l.Clean(at(10), time.Hour, 0, 0); err != nil {
		t.Fatalf("Clean: %v", err)
	}

	got := offsets(allMessages(l))
	want := []int64{1, 2, 3}
	if !equalOffsets(got, want) {
		t.Fatalf("surviving offsets = %v, want %v", got, want)
	}

	// Unselected sealed segment 1 and the active segment must be untouched.
	segs := l.Segments()
	if len(segs[1].Messages) != 1 || string(segs[1].Messages[0].Value) != "v2" {
		t.Fatalf("unselected segment modified: %+v", segs[1].Messages)
	}
	if segs[2].Sealed {
		t.Fatalf("active segment must stay unsealed")
	}
	if len(segs[2].Messages) != 1 || segs[2].Messages[0].Offset != 3 {
		t.Fatalf("active segment modified: %+v", segs[2].Messages)
	}
}

// TestCleanRejectsActiveAndBadRanges covers boundary validation.
func TestCleanRejectsActiveAndBadRanges(t *testing.T) {
	l := NewLog()
	l.Append("a", []byte("v1"), at(0))
	l.Seal()
	l.Append("b", []byte("v1"), at(1)) // active segment 1

	if err := l.Clean(at(10), time.Hour, 0, 1); !errors.Is(err, ErrUnsealedSegment) {
		t.Fatalf("cleaning active segment: err = %v, want ErrUnsealedSegment", err)
	}
	if err := l.Clean(at(10), time.Hour, 1, 0); !errors.Is(err, ErrEmptyRange) {
		t.Fatalf("inverted range: err = %v, want ErrEmptyRange", err)
	}
	if err := l.Clean(at(10), time.Hour, 0, 5); !errors.Is(err, ErrEmptyRange) {
		t.Fatalf("out-of-range: err = %v, want ErrEmptyRange", err)
	}
	// Log must be unchanged after failed cleans.
	if got := offsets(allMessages(l)); !equalOffsets(got, []int64{0, 1}) {
		t.Fatalf("log changed after rejected cleans: %v", got)
	}
}

// TestTombstoneRetentionNotElapsed: an expired-looking tombstone whose
// retention has NOT elapsed must be kept even when it is the only record
// for its key.
func TestTombstoneRetentionNotElapsed(t *testing.T) {
	l := NewLog()
	l.Append("a", []byte("v1"), at(0)) // offset 0
	l.Delete("a", at(1))               // offset 1
	l.Seal()

	// retention = 1h, only 2s elapsed -> tombstone must survive, and the
	// stale value beneath it is dropped.
	if err := l.Clean(at(2), time.Hour, 0, 0); err != nil {
		t.Fatalf("Clean: %v", err)
	}
	got := allMessages(l)
	if len(got) != 1 || !got[0].Tombstone || got[0].Offset != 1 {
		t.Fatalf("survivors = %+v, want only tombstone@1", got)
	}
}

// TestTwoRoundTombstoneCleanup is the interleaved scenario from the spec:
// cleaning the tombstone-bearing last segment alone keeps the tombstone
// (an earlier record survives outside the selection); cleaning it again
// together with the segment holding the old value drops both. The two
// rounds must yield different retention results.
func TestTwoRoundTombstoneCleanup(t *testing.T) {
	newLog := func() *Log {
		l := NewLog()
		l.Append("a", []byte("old"), at(0)) // offset 0, seg 0 (old value)
		l.Append("b", []byte("keep"), at(1))
		// offset 1, seg 0
		l.Seal()             // seg 0 sealed
		l.Delete("a", at(2)) // offset 2, seg 1 (tombstone, last segment)
		l.Append("z", nil, at(3))
		l.Seal() // seg 1 sealed
		return l
	}
	retention := time.Hour
	now := at(2).Add(2 * time.Hour) // retention for tombstone@2 has elapsed

	// Round 1: clean ONLY the last sealed segment (the one with the
	// tombstone). The earlier "a" record lives in unselected segment 0 and
	// survives, so the tombstone must be KEPT.
	l1 := newLog()
	if err := l1.Clean(now, retention, 1, 1); err != nil {
		t.Fatalf("round 1 Clean: %v", err)
	}
	got1 := offsets(allMessages(l1))
	want1 := []int64{0, 1, 2, 3}
	if !equalOffsets(got1, want1) {
		t.Fatalf("round 1 survivors = %v, want %v (tombstone kept)", got1, want1)
	}

	// Round 2: clean the tombstone segment TOGETHER with the segment
	// holding the old value. The old value is dropped (the tombstone is
	// later), and with no earlier "a" record surviving, the expired
	// tombstone is dropped too.
	l2 := newLog()
	if err := l2.Clean(now, retention, 0, 1); err != nil {
		t.Fatalf("round 2 Clean: %v", err)
	}
	got2 := offsets(allMessages(l2))
	want2 := []int64{1, 3}
	if !equalOffsets(got2, want2) {
		t.Fatalf("round 2 survivors = %v, want %v (old value and tombstone dropped)", got2, want2)
	}

	// The two rounds must differ.
	if equalOffsets(got1, got2) {
		t.Fatalf("rounds 1 and 2 produced identical results %v, want different retention", got1)
	}
}

// TestReadFromGapAndEnd covers offset-based reading: offsets keep their
// original numbering after compaction, and a reader resuming from a gap
// lands on the next existing message or the end of the log.
func TestReadFromGapAndEnd(t *testing.T) {
	l := NewLog()
	l.Append("a", []byte("v1"), at(0)) // offset 0
	l.Append("b", []byte("v1"), at(1)) // offset 1
	l.Append("c", []byte("v1"), at(2)) // offset 2
	l.Seal()
	l.Append("a", []byte("v2"), at(3)) // offset 3
	l.Append("b", []byte("v2"), at(4)) // offset 4
	l.Seal()

	if err := l.Clean(at(10), time.Hour, 0, 0); err != nil {
		t.Fatalf("Clean: %v", err)
	}
	// Offsets 0 and 1 are gone; 2, 3, 4 survive with original numbering.
	if got := offsets(allMessages(l)); !equalOffsets(got, []int64{2, 3, 4}) {
		t.Fatalf("survivors = %v, want [2 3 4]", got)
	}

	// Resuming from the dropped offset 0 must land on offset 2.
	m, ok := l.ReadFrom(0)
	if !ok || m.Offset != 2 || string(m.Value) != "v1" {
		t.Fatalf("ReadFrom(0) = %+v, %v; want offset 2", m, ok)
	}
	// Resuming exactly at a surviving offset returns that message.
	m, ok = l.ReadFrom(3)
	if !ok || m.Offset != 3 || string(m.Value) != "v2" {
		t.Fatalf("ReadFrom(3) = %+v, %v; want offset 3", m, ok)
	}
	// Past the last offset: end of log.
	if _, ok := l.ReadFrom(4 + 1); ok {
		t.Fatalf("ReadFrom(5) = true, want end-of-log")
	}
}

// TestReadFromEmptyLog is the boundary case of reading an empty log.
func TestReadFromEmptyLog(t *testing.T) {
	l := NewLog()
	if _, ok := l.ReadFrom(0); ok {
		t.Fatalf("ReadFrom on empty log = true, want false")
	}
	// Cleaning an empty sealed segment is a no-op, not an error.
	l.Seal()
	if err := l.Clean(at(10), time.Hour, 0, 0); err != nil {
		t.Fatalf("Clean on empty segment: %v", err)
	}
	if _, ok := l.ReadFrom(0); ok {
		t.Fatalf("ReadFrom after no-op clean = true, want false")
	}
}

// TestTombstoneLatestOutsideSelection: when the key's latest record is a
// tombstone outside the selected range, older records inside the selection
// are dropped while the tombstone itself is untouched.
func TestTombstoneLatestOutsideSelection(t *testing.T) {
	l := NewLog()
	l.Append("a", []byte("v1"), at(0)) // offset 0, seg 0
	l.Seal()
	l.Delete("a", at(1)) // offset 1, seg 1 (tombstone, not selected)
	l.Seal()

	if err := l.Clean(at(10), time.Hour, 0, 0); err != nil {
		t.Fatalf("Clean: %v", err)
	}
	got := allMessages(l)
	if len(got) != 1 || !got[0].Tombstone || got[0].Offset != 1 {
		t.Fatalf("survivors = %+v, want only tombstone@1", got)
	}
}
