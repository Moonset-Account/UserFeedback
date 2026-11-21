package alignedjoin

import (
	"errors"
	"math/rand"
	"reflect"
	"testing"
)

// mustPull fails the test unless the Pull returns err == nil.
func mustPull(t *testing.T, op *Operator, sn Side, pos int64) Event {
	t.Helper()
	ev, err := op.Pull(sn, pos)
	if err != nil {
		t.Fatalf("Pull(%v, %d) unexpected error: %v", sn, pos, err)
	}
	return ev
}

func wantErr(t *testing.T, err, target error) {
	t.Helper()
	if !errors.Is(err, target) {
		t.Fatalf("got error %v, want %v", err, target)
	}
}

// TestBasicAccumulation covers the normal path: signed deltas from both inputs
// accumulate per key and every processed record emits the key's new value.
func TestBasicAccumulation(t *testing.T) {
	op := New()

	if err := op.Push(Left, Record("a", 1)); err != nil {
		t.Fatal(err)
	}
	if err := op.Push(Left, Record("a", 2)); err != nil {
		t.Fatal(err)
	}
	if err := op.Push(Right, Record("a", 10)); err != nil {
		t.Fatal(err)
	}
	if err := op.Push(Right, Record("b", -5)); err != nil {
		t.Fatal(err)
	}

	type out struct {
		sn    Side
		pos   int64
		key   string
		value int64
	}
	got := make([]out, 0, 4)
	pull := func(sn Side, pos int64) {
		ev := mustPull(t, op, sn, pos)
		if ev.Kind != EvOutput {
			t.Fatalf("Pull(%v,%d): got event kind %d, want EvOutput", sn, pos, ev.Kind)
		}
		got = append(got, out{sn, pos, ev.Key, ev.Value})
	}
	pull(Left, 0)
	pull(Right, 0)
	pull(Left, 1)
	pull(Right, 1)

	want := []out{
		{Left, 0, "a", 1},
		{Right, 0, "a", 11},
		{Left, 1, "a", 13},
		{Right, 1, "b", -5},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("outputs = %+v, want %+v", got, want)
	}
	if op.Value("a") != 13 || op.Value("b") != -5 {
		t.Fatalf("final state a=%d b=%d, want 13,-5", op.Value("a"), op.Value("b"))
	}
}

// TestHeadlineInterleaving reproduces the scenario from the specification:
// the left barrier arrives first and +7 is queued behind it; -7 is still in
// front of the right barrier. The snapshot must contain -7 but not +7.
func TestHeadlineInterleaving(t *testing.T) {
	op := New()

	// Left positions 0..1, right positions 0..1; +7 is pushed only after the
	// left side is blocked, exercising the staging path.
	if err := op.Push(Left, Record("k", 1)); err != nil {
		t.Fatal(err)
	}
	if err := op.Push(Left, Barrier(1)); err != nil {
		t.Fatal(err)
	}
	if err := op.Push(Right, Record("k", -7)); err != nil {
		t.Fatal(err)
	}
	if err := op.Push(Right, Barrier(1)); err != nil {
		t.Fatal(err)
	}

	ev := mustPull(t, op, Left, 0)
	if ev.Kind != EvOutput || ev.Key != "k" || ev.Value != 1 {
		t.Fatalf("L0 = %+v, want output k=1", ev)
	}

	ev = mustPull(t, op, Left, 1)
	if ev.Kind != EvBarrier || ev.BarrierID != 1 {
		t.Fatalf("L1 = %+v, want barrier 1", ev)
	}
	if !op.Blocked(Left) || op.Blocked(Right) || !op.PendingBarrier() {
		t.Fatalf("blocked state wrong: left=%v right=%v pending=%v",
			op.Blocked(Left), op.Blocked(Right), op.PendingBarrier())
	}

	// +7 pushed behind the already consumed left barrier: it stays queued and
	// cannot be applied while the side is blocked.
	if err := op.Push(Left, Record("k", 7)); err != nil {
		t.Fatal(err)
	}
	if op.Value("k") != 1 {
		t.Fatalf("staged +7 leaked into state: k=%d, want 1", op.Value("k"))
	}

	// Pulling the blocked side reports EvStaged and consumes no position.
	stagedEv := mustPull(t, op, Left, 2)
	if stagedEv.Kind != EvStaged || stagedEv.Pos != 2 || stagedEv.Key != "k" {
		t.Fatalf("blocked pull = %+v, want EvStaged at pos 2 for k", stagedEv)
	}
	if op.Value("k") != 1 {
		t.Fatalf("EvStaged pull changed the state: k=%d, want 1", op.Value("k"))
	}

	// The right side keeps updating while the left side waits.
	ev = mustPull(t, op, Right, 0)
	if ev.Kind != EvOutput || ev.Value != -6 {
		t.Fatalf("R0 = %+v, want output k=-6", ev)
	}

	ev = mustPull(t, op, Right, 1)
	if ev.Kind != EvCheckpoint || ev.BarrierID != 1 || ev.Snapshot == nil {
		t.Fatalf("R1 = %+v, want checkpoint for barrier 1", ev)
	}
	snap := ev.Snapshot
	if snap.BarrierID != 1 || snap.BarrierPos != [2]int64{1, 1} {
		t.Fatalf("snapshot header = %+v, want id1 positions {1,1}", snap)
	}
	if snap.Values["k"] != -6 {
		t.Fatalf("snapshot k=%d, want -6 (must contain -7, must not contain +7)", snap.Values["k"])
	}
	if snap.ResumePos(Left) != 2 || snap.ResumePos(Right) != 2 {
		t.Fatalf("resume positions = %d,%d, want 2,2", snap.ResumePos(Left), snap.ResumePos(Right))
	}
	if op.PendingBarrier() || op.Blocked(Left) || op.Blocked(Right) {
		t.Fatal("checkpoint did not release the sides")
	}

	// Staged +7 is consumed after the snapshot and now applies.
	ev = mustPull(t, op, Left, 2)
	if ev.Kind != EvOutput || ev.Value != 1 {
		t.Fatalf("L2 = %+v, want output k=1 (-6+7)", ev)
	}

	// Replay from the snapshot: only the first item after each barrier is
	// replayed, in the same pull order; the output suffix must match.
	re := Restore(snap)
	if err := re.Push(Left, Record("k", 7)); err != nil {
		t.Fatal(err)
	}
	if _, err := re.Pull(Left, 1); !errors.Is(err, ErrNonContiguousPos) {
		t.Fatalf("replay pull at barrier position: err=%v, want ErrNonContiguousPos", err)
	}
	ev = mustPull(t, re, Left, snap.ResumePos(Left))
	if ev.Kind != EvOutput || ev.Key != "k" || ev.Value != 1 {
		t.Fatalf("replay L2 = %+v, want output k=1", ev)
	}
	if re.Value("k") != op.Value("k") {
		t.Fatalf("replay state k=%d, original k=%d", re.Value("k"), op.Value("k"))
	}
}

// TestBoundaryAdjacentBarriers covers boundary conditions: a barrier at
// position 0, back-to-back barriers with no records between them, and a side
// whose barrier has no record in front of it.
func TestBoundaryAdjacentBarriers(t *testing.T) {
	op := New()
	// Left:  B1, x+5, B2
	// Right: x+1, B1, B2
	must(t, op.Push(Left, Barrier(1)))
	must(t, op.Push(Left, Record("x", 5)))
	must(t, op.Push(Left, Barrier(2)))
	must(t, op.Push(Right, Record("x", 1)))
	must(t, op.Push(Right, Barrier(1)))
	must(t, op.Push(Right, Barrier(2)))

	ev := mustPull(t, op, Left, 0)
	if ev.Kind != EvBarrier || ev.Pos != 0 {
		t.Fatalf("L0 = %+v, want barrier at pos 0", ev)
	}
	ev = mustPull(t, op, Right, 0)
	if ev.Kind != EvOutput || ev.Value != 1 {
		t.Fatalf("R0 = %+v, want output x=1 while left blocked", ev)
	}
	ev = mustPull(t, op, Right, 1)
	if ev.Kind != EvCheckpoint {
		t.Fatalf("R1 = %+v, want checkpoint 1", ev)
	}
	if ev.Snapshot.BarrierPos != [2]int64{0, 1} || ev.Snapshot.Values["x"] != 1 {
		t.Fatalf("snapshot = %+v, want positions {0,1} x=1", ev.Snapshot)
	}

	ev = mustPull(t, op, Left, 1)
	if ev.Kind != EvOutput || ev.Value != 6 {
		t.Fatalf("L1 = %+v, want output x=6", ev)
	}
	ev = mustPull(t, op, Left, 2)
	if ev.Kind != EvBarrier || ev.BarrierID != 2 {
		t.Fatalf("L2 = %+v, want barrier 2", ev)
	}
	ev = mustPull(t, op, Right, 2)
	if ev.Kind != EvCheckpoint || ev.BarrierID != 2 {
		t.Fatalf("R2 = %+v, want checkpoint 2", ev)
	}
	if ev.Snapshot.Values["x"] != 6 || ev.Snapshot.BarrierPos != [2]int64{2, 2} {
		t.Fatalf("snapshot 2 = %+v, want x=6 positions {2,2}", ev.Snapshot)
	}
	if op.LastBarrier() != 2 {
		t.Fatalf("last barrier = %d, want 2", op.LastBarrier())
	}
}

// TestMultipleCheckpointsReplay runs two aligned checkpoints and verifies that
// replaying from the first one reproduces the exact output suffix of the
// original run under the same pull order.
func TestMultipleCheckpointsReplay(t *testing.T) {
	op := New()
	// Left:  a+2, B1, a+3, B2, a+4
	// Right: a+10, B1, a-1, B2
	must(t, op.Push(Left, Record("a", 2)))
	must(t, op.Push(Left, Barrier(1)))
	must(t, op.Push(Left, Record("a", 3)))
	must(t, op.Push(Left, Barrier(2)))
	must(t, op.Push(Left, Record("a", 4)))
	must(t, op.Push(Right, Record("a", 10)))
	must(t, op.Push(Right, Barrier(1)))
	must(t, op.Push(Right, Record("a", -1)))
	must(t, op.Push(Right, Barrier(2)))

	type out struct {
		sn    Side
		pos   int64
		value int64
	}
	outputs := make([]out, 0, 5)
	var snap1 *Snapshot
	pull := func(sn Side, pos int64) Event {
		ev := mustPull(t, op, sn, pos)
		switch ev.Kind {
		case EvOutput:
			outputs = append(outputs, out{sn, pos, ev.Value})
		case EvCheckpoint:
			if snap1 == nil {
				snap1 = ev.Snapshot
			}
		}
		return ev
	}

	if ev := pull(Left, 0); ev.Kind != EvOutput || ev.Value != 2 {
		t.Fatalf("L0 = %+v", ev)
	}
	if ev := pull(Right, 0); ev.Kind != EvOutput || ev.Value != 12 {
		t.Fatalf("R0 = %+v", ev)
	}
	if ev := pull(Left, 1); ev.Kind != EvBarrier {
		t.Fatalf("L1 = %+v", ev)
	}
	if ev := pull(Right, 1); ev.Kind != EvCheckpoint || ev.BarrierID != 1 {
		t.Fatalf("R1 = %+v", ev)
	}
	if ev := pull(Left, 2); ev.Kind != EvOutput || ev.Value != 15 {
		t.Fatalf("L2 = %+v", ev)
	}
	if ev := pull(Right, 2); ev.Kind != EvOutput || ev.Value != 14 {
		t.Fatalf("R2 = %+v", ev)
	}
	if ev := pull(Left, 3); ev.Kind != EvBarrier {
		t.Fatalf("L3 = %+v", ev)
	}
	if ev := pull(Right, 3); ev.Kind != EvCheckpoint || ev.Snapshot.Values["a"] != 14 {
		t.Fatalf("R3 = %+v", ev)
	}
	if ev := pull(Left, 4); ev.Kind != EvOutput || ev.Value != 18 {
		t.Fatalf("L4 = %+v", ev)
	}

	// Original suffix after checkpoint 1, in pull order.
	wantSuffix := []out{
		{Left, 2, 15},
		{Right, 2, 14},
		{Left, 4, 18},
	}
	if !reflect.DeepEqual(outputs[2:], wantSuffix) {
		t.Fatalf("original suffix = %+v, want %+v", outputs[2:], wantSuffix)
	}

	// Replay: items strictly after each side's barrier 1, same pull order.
	re := Restore(snap1)
	must(t, re.Push(Left, Record("a", 3)))   // pos 2
	must(t, re.Push(Left, Barrier(2)))       // pos 3
	must(t, re.Push(Left, Record("a", 4)))   // pos 4
	must(t, re.Push(Right, Record("a", -1))) // pos 2
	must(t, re.Push(Right, Barrier(2)))      // pos 3

	gotSuffix := make([]out, 0, 3)
	replayPulls := []struct {
		sn  Side
		pos int64
	}{
		{Left, 2}, {Right, 2}, {Left, 3}, {Right, 3}, {Left, 4},
	}
	for _, p := range replayPulls {
		ev := mustPull(t, re, p.sn, p.pos)
		if ev.Kind == EvOutput {
			gotSuffix = append(gotSuffix, out{p.sn, p.pos, ev.Value})
		}
	}
	if !reflect.DeepEqual(gotSuffix, wantSuffix) {
		t.Fatalf("replay suffix = %+v, want %+v", gotSuffix, wantSuffix)
	}
	if re.Value("a") != op.Value("a") || re.Value("a") != 18 {
		t.Fatalf("replay final a=%d, original a=%d, want 18", re.Value("a"), op.Value("a"))
	}
	if re.LastBarrier() != 2 || op.LastBarrier() != 2 {
		t.Fatalf("last barrier replay=%d original=%d, want 2", re.LastBarrier(), op.LastBarrier())
	}
}

// TestRandomInterleavingReplay drives many randomized pull interleavings. For
// every seed, replaying from the aligned snapshot with the recorded pull order
// must reproduce the original output suffix and final state exactly.
func TestRandomInterleavingReplay(t *testing.T) {
	const (
		seeds        = 30
		size         = 8
		barrierIndex = 4
	)
	keys := []string{"k0", "k1", "k2"}
	sawStaged := false

	for seed := int64(1); seed <= seeds; seed++ {
		rng := rand.New(rand.NewSource(seed))
		items := [2][]Item{}
		for sn := Left; sn <= Right; sn++ {
			for i := 0; i < size; i++ {
				if i == barrierIndex {
					items[sn] = append(items[sn], Barrier(1))
				} else {
					delta := int64(rng.Intn(7) - 3) // -3..3
					items[sn] = append(items[sn], Record(keys[rng.Intn(len(keys))], delta))
				}
			}
		}

		op := New()
		for sn := Left; sn <= Right; sn++ {
			for _, it := range items[sn] {
				if err := op.Push(sn, it); err != nil {
					t.Fatalf("seed %d push: %v", seed, err)
				}
			}
		}

		type pull struct {
			sn  Side
			pos int64
		}
		type out struct {
			key   string
			value int64
		}
		var pulls []pull // only consuming pulls; these define the replay order
		outputs := make([]out, 0)
		var snap *Snapshot
		cpOutIdx := -1
		next := [2]int64{}
		remaining := [2]int{size, size} // unconsumed queued items per side

		for remaining[Left] > 0 || remaining[Right] > 0 {
			sn := Side(rng.Intn(2))
			if remaining[sn] == 0 {
				continue
			}
			pos := next[sn]
			ev, err := op.Pull(sn, pos)
			if err != nil {
				t.Fatalf("seed %d Pull(%v,%d): %v", seed, sn, pos, err)
			}
			if ev.Kind == EvStaged {
				sawStaged = true
				// Nothing was consumed: the same position is retried later.
				continue
			}
			pulls = append(pulls, pull{sn, pos})
			next[sn] = pos + 1
			remaining[sn]--
			switch ev.Kind {
			case EvOutput:
				outputs = append(outputs, out{ev.Key, ev.Value})
			case EvCheckpoint:
				snap = ev.Snapshot
				cpOutIdx = len(outputs)
			}
		}
		if snap == nil || cpOutIdx < 0 {
			t.Fatalf("seed %d: checkpoint never completed", seed)
		}

		// Replay on a restored operator with only post-barrier items, driven
		// by the recorded consuming pull order.
		re := Restore(snap)
		for sn := Left; sn <= Right; sn++ {
			for i := barrierIndex + 1; i < size; i++ {
				if err := re.Push(sn, items[sn][i]); err != nil {
					t.Fatalf("seed %d replay push: %v", seed, err)
				}
			}
		}
		gotSuffix := make([]out, 0)
		for _, p := range pulls {
			if p.pos <= int64(barrierIndex) {
				continue // the barrier and everything before it are in the snapshot
			}
			ev, err := re.Pull(p.sn, p.pos)
			if err != nil {
				t.Fatalf("seed %d replay Pull(%v,%d): %v", seed, p.sn, p.pos, err)
			}
			if ev.Kind == EvStaged {
				t.Fatalf("seed %d: consuming pull (%v,%d) replayed as EvStaged", seed, p.sn, p.pos)
			}
			if ev.Kind == EvOutput {
				gotSuffix = append(gotSuffix, out{ev.Key, ev.Value})
			}
		}
		wantSuffix := outputs[cpOutIdx:]
		if !reflect.DeepEqual(gotSuffix, wantSuffix) {
			t.Fatalf("seed %d suffix mismatch:\n got %+v\nwant %+v", seed, gotSuffix, wantSuffix)
		}
		for _, k := range keys {
			if re.Value(k) != op.Value(k) {
				t.Fatalf("seed %d final state mismatch on %s: replay=%d original=%d",
					seed, k, re.Value(k), op.Value(k))
			}
		}
	}
	if !sawStaged {
		t.Fatal("no interleaving produced EvStaged; staging path not exercised")
	}
}

// TestSnapshotIsolation verifies that a frozen snapshot is an independent deep
// copy: mutating it after the checkpoint affects neither the live operator nor
// a later restore.
func TestSnapshotIsolation(t *testing.T) {
	op := New()
	must(t, op.Push(Left, Record("k", 5)))
	must(t, op.Push(Left, Barrier(1)))
	must(t, op.Push(Right, Barrier(1)))

	mustPull(t, op, Left, 0)
	mustPull(t, op, Left, 1)
	ev := mustPull(t, op, Right, 0)
	if ev.Kind != EvCheckpoint {
		t.Fatalf("R0 = %+v, want checkpoint", ev)
	}
	snap := ev.Snapshot

	// Mutate the caller-owned snapshot.
	snap.Values["k"] = 999
	snap.Values["ghost"] = 42
	snap.BarrierPos[Left] = 120

	if op.Value("k") != 5 {
		t.Fatalf("live state leaked from snapshot mutation: k=%d, want 5", op.Value("k"))
	}
	if op.Value("ghost") != 0 {
		t.Fatal("snapshot mutation inserted a key into the live state")
	}
	re := Restore(snap)
	if re.Value("k") != 999 || re.Value("ghost") != 42 {
		t.Fatalf("restore did not use the mutated snapshot values: k=%d ghost=%d",
			re.Value("k"), re.Value("ghost"))
	}
	// The live operator can still take the next checkpoint independently.
	must(t, op.Push(Left, Barrier(2)))
	must(t, op.Push(Right, Barrier(2)))
	ev2 := mustPull(t, op, Left, 2)
	if ev2.Kind != EvBarrier {
		t.Fatalf("L2 = %+v, want barrier 2", ev2)
	}
	ev2 = mustPull(t, op, Right, 1)
	if ev2.Kind != EvCheckpoint || ev2.Snapshot.Values["k"] != 5 {
		t.Fatalf("R1 = %+v, want clean checkpoint 2 with k=5", ev2)
	}
}

// TestProtocolErrors checks all documented misuse errors.
func TestProtocolErrors(t *testing.T) {
	op := New()

	// Invalid side.
	if _, err := op.Pull(Side(7), 0); !errors.Is(err, ErrInvalidSide) {
		t.Fatalf("invalid side pull: %v", err)
	}
	if err := op.Push(Side(7), Record("k", 1)); !errors.Is(err, ErrInvalidSide) {
		t.Fatalf("invalid side push: %v", err)
	}
	if op.Blocked(Side(7)) {
		t.Fatal("Blocked on invalid side must report false")
	}

	// Malformed items.
	if err := op.Push(Left, Item{Kind: KindRecord, BarrierID: 1}); !errors.Is(err, ErrInvalidItem) {
		t.Fatalf("record with barrier id: %v", err)
	}
	if err := op.Push(Left, Barrier(0)); !errors.Is(err, ErrInvalidItem) {
		t.Fatalf("barrier id 0: %v", err)
	}
	if err := op.Push(Left, Item{Kind: ItemKind(9)}); !errors.Is(err, ErrInvalidItem) {
		t.Fatalf("unknown item kind: %v", err)
	}

	// Empty queue is reported but leaves the operator usable.
	ev, err := op.Pull(Left, 0)
	if !errors.Is(err, ErrEmptyQueue) || ev.Kind != EvIdle {
		t.Fatalf("empty pull: ev=%+v err=%v", ev, err)
	}
	must(t, op.Push(Left, Record("k", 1)))
	if ev := mustPull(t, op, Left, 0); ev.Kind != EvOutput || ev.Value != 1 {
		t.Fatalf("pull after idle: %+v", ev)
	}

	// Non-contiguous positions; the failed pull does not advance the position.
	if _, err := op.Pull(Left, 0); !errors.Is(err, ErrNonContiguousPos) {
		t.Fatalf("repeated position: %v", err)
	}
	if _, err := op.Pull(Left, 2); !errors.Is(err, ErrNonContiguousPos) {
		t.Fatalf("skipped position: %v", err)
	}
	must(t, op.Push(Left, Record("k", 2)))
	if ev := mustPull(t, op, Left, 1); ev.Kind != EvOutput || ev.Value != 3 {
		t.Fatalf("pull after rejected positions: %+v", ev)
	}

	// Barrier identifier mismatch between the sides.
	must(t, op.Push(Left, Barrier(1)))
	must(t, op.Push(Right, Barrier(2)))
	if ev := mustPull(t, op, Left, 2); ev.Kind != EvBarrier {
		t.Fatalf("L2 = %+v, want barrier 1", ev)
	}
	if _, err := op.Pull(Right, 0); !errors.Is(err, ErrBarrierMismatch) {
		t.Fatalf("mismatched barriers: %v", err)
	}

	// A second barrier on the already waiting side is rejected: only one
	// checkpoint in flight at a time.
	must(t, op.Push(Left, Barrier(1)))
	if _, err := op.Pull(Left, 3); !errors.Is(err, ErrCheckpointInFlight) {
		t.Fatalf("second barrier while waiting: %v", err)
	}

	// Old barrier identifiers are rejected once a checkpoint completed, also
	// after a restore.
	snap := &Snapshot{BarrierID: 1, BarrierPos: [2]int64{2, 0}, Values: map[string]int64{}}
	re := Restore(snap)
	must(t, re.Push(Right, Barrier(1)))
	if _, err := re.Pull(Right, 1); !errors.Is(err, ErrBarrierOrder) {
		t.Fatalf("old barrier after restore: %v", err)
	}
}

// TestZeroAndCrossingValues covers boundary values: an absent key reads as
// zero, a key can accumulate back to exactly zero, and negative deltas from
// the right input cross the zero line.
func TestZeroAndCrossingValues(t *testing.T) {
	op := New()
	if got := op.Value("missing"); got != 0 {
		t.Fatalf("absent key = %d, want 0", got)
	}
	must(t, op.Push(Left, Record("z", 5)))
	ev := mustPull(t, op, Left, 0)
	if ev.Kind != EvOutput || ev.Value != 5 {
		t.Fatalf("L0 = %+v, want z=5", ev)
	}
	must(t, op.Push(Right, Record("z", -5)))
	ev = mustPull(t, op, Right, 0)
	if ev.Kind != EvOutput || ev.Value != 0 {
		t.Fatalf("R0 = %+v, want z back to 0", ev)
	}
	must(t, op.Push(Right, Record("z", -1)))
	ev = mustPull(t, op, Right, 1)
	if ev.Kind != EvOutput || ev.Value != -1 {
		t.Fatalf("R1 = %+v, want z=-1 crossing zero", ev)
	}
}

// TestResumePos checks the resume arithmetic on the snapshot API.
func TestResumePos(t *testing.T) {
	s := &Snapshot{BarrierID: 7, BarrierPos: [2]int64{3, 10}}
	if got := s.ResumePos(Left); got != 4 {
		t.Fatalf("ResumePos(Left) = %d, want 4", got)
	}
	if got := s.ResumePos(Right); got != 11 {
		t.Fatalf("ResumePos(Right) = %d, want 11", got)
	}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}
