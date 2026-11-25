package blockfilter

import (
	"errors"
	"math"
	"math/rand"
	"sync"
	"testing"
)

func mustNew(t *testing.T, slots uint32) *Filter {
	t.Helper()
	f, err := New(slots)
	if err != nil {
		t.Fatalf("New(%d): %v", slots, err)
	}
	return f
}

func mustApply(t *testing.T, f *Filter, batch ...Change) *Snapshot {
	t.Helper()
	snap, err := f.Apply(batch)
	if err != nil {
		t.Fatalf("Apply(%v): %v", batch, err)
	}
	return snap
}

func TestNewValidation(t *testing.T) {
	if _, err := New(0); !errors.Is(err, ErrNoSlots) {
		t.Fatalf("New(0) err = %v, want ErrNoSlots", err)
	}
	if _, err := New(MaxSlots + 1); !errors.Is(err, ErrTooManySlots) {
		t.Fatalf("New(MaxSlots+1) err = %v, want ErrTooManySlots", err)
	}
	if _, err := New(MaxSlots + 100); !errors.Is(err, ErrTooManySlots) {
		t.Fatalf("New(huge) err = %v, want ErrTooManySlots", err)
	}

	f := mustNew(t, 8)
	if f.Generation() != 0 {
		t.Fatalf("initial generation = %d, want 0", f.Generation())
	}
	if f.Slots() != 8 || f.Snapshot().Slots() != 8 {
		t.Fatalf("slots = %d/%d, want 8", f.Slots(), f.Snapshot().Slots())
	}
	if got := f.Snapshot().OverflowEntries(); got != 0 {
		t.Fatalf("initial overflow entries = %d, want 0", got)
	}
}

func TestAddAndRemoveBasic(t *testing.T) {
	f := mustNew(t, 16)
	positions := []uint32{2, 9}

	if !f.Snapshot().BlockDefinitelyAbsent(positions) {
		t.Fatal("empty filter must report the block definitely absent")
	}

	snap := mustApply(t, f, Change{Kind: KindAdd, Positions: positions})
	if snap.Generation() != 1 {
		t.Fatalf("generation after first apply = %d, want 1", snap.Generation())
	}
	if snap.BlockDefinitelyAbsent(positions) {
		t.Fatal("added block reported definitely absent")
	}
	if !snap.BlockMaybePresent(positions) {
		t.Fatal("added block not reported maybe present")
	}
	if c, ok := snap.Counter(2); !ok || c != 1 {
		t.Fatalf("counter[2] = %d,%v want 1,true", c, ok)
	}
	if snap.OverflowEntries() != 0 {
		t.Fatalf("small counts must not spill into overflow, got %d entries", snap.OverflowEntries())
	}

	snap = mustApply(t, f, Change{Kind: KindRemove, Positions: positions})
	if snap.Generation() != 2 {
		t.Fatalf("generation after second apply = %d, want 2", snap.Generation())
	}
	if !snap.BlockDefinitelyAbsent(positions) {
		t.Fatal("released block not reported definitely absent")
	}
	if snap.OverflowEntries() != 0 {
		t.Fatalf("overflow entries after release = %d, want 0", snap.OverflowEntries())
	}
}

func TestDuplicatePositionsAndRepeatedAdds(t *testing.T) {
	f := mustNew(t, 8)
	// The same block hashes twice onto the same slot and is added twice.
	positions := []uint32{5, 5}
	mustApply(t, f, Change{Kind: KindAdd, Positions: positions})
	mustApply(t, f, Change{Kind: KindAdd, Positions: positions})

	if c, ok := f.Snapshot().Counter(5); !ok || c != 4 {
		t.Fatalf("counter[5] = %d,%v want 4,true", c, ok)
	}
	if f.Snapshot().BlockDefinitelyAbsent(positions) {
		t.Fatal("held block reported definitely absent after two matched adds")
	}

	mustApply(t, f, Change{Kind: KindRemove, Positions: positions})
	if c, _ := f.Snapshot().Counter(5); c != 2 {
		t.Fatalf("counter[5] = %d after one release, want 2", c)
	}
	if f.Snapshot().BlockDefinitelyAbsent(positions) {
		t.Fatal("still-held block reported definitely absent after first release")
	}

	mustApply(t, f, Change{Kind: KindRemove, Positions: positions})
	if c, _ := f.Snapshot().Counter(5); c != 0 {
		t.Fatalf("counter[5] = %d after final release, want 0", c)
	}
	if !f.Snapshot().BlockDefinitelyAbsent(positions) {
		t.Fatal("fully released block not reported definitely absent")
	}
}

func TestAnyZeroPositionProvesAbsence(t *testing.T) {
	f := mustNew(t, 32)
	// A block sits only on slot 1. Another block sits on 1 and 2. A probe with
	// positions 1 and 3 must stay definitely absent because slot 3 is zero.
	mustApply(t, f, Change{Kind: KindAdd, Positions: []uint32{1, 2}})
	snap := f.Snapshot()
	if !snap.BlockDefinitelyAbsent([]uint32{1, 3}) {
		t.Fatal("zero position 3 must prove absence even though position 1 is busy")
	}
	if snap.BlockDefinitelyAbsent([]uint32{1, 2}) {
		t.Fatal("held two-position block reported definitely absent")
	}
}

func TestEmptyPositions(t *testing.T) {
	f := mustNew(t, 4)
	// No positions gives no evidence, so the block is neither proven absent nor
	// claimed present.
	if f.Snapshot().BlockDefinitelyAbsent(nil) {
		t.Fatal("empty positions must not prove absence")
	}
	if f.Snapshot().BlockMaybePresent(nil) {
		t.Fatal("empty positions must not claim presence")
	}
}

func TestOutOfRangePosition(t *testing.T) {
	f := mustNew(t, 4)
	snap := f.Snapshot()
	if snap.MaybePresent(4) || snap.MaybePresent(1<<31) {
		t.Fatal("out-of-range position must not report presence")
	}
	if !snap.BlockDefinitelyAbsent([]uint32{0, 99}) {
		t.Fatal("out-of-range position must count as a zero counter")
	}
	if _, ok := snap.Counter(4); ok {
		t.Fatal("out-of-range Counter reported ok")
	}
}

func TestBatchValidationIsAtomic(t *testing.T) {
	cases := []struct {
		name  string
		batch []Change
		want  error
	}{
		{"empty", nil, ErrEmptyBatch},
		{"no positions", []Change{{Kind: KindAdd}}, ErrNoPositions},
		{"unknown kind", []Change{{Kind: Kind(99), Positions: []uint32{1}}}, ErrUnknownKind},
		{"position out of range", []Change{{Kind: KindAdd, Positions: []uint32{8}}}, ErrPositionOutOfRange},
		{
			"remove not held",
			[]Change{{Kind: KindAdd, Positions: []uint32{1}}, {Kind: KindRemove, Positions: []uint32{2}}},
			ErrRemoveNotHeld,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := mustNew(t, 8)
			before := f.Snapshot()
			snap, err := f.Apply(tc.batch)
			if !errors.Is(err, tc.want) {
				t.Fatalf("Apply err = %v, want %v", err, tc.want)
			}
			if snap != nil {
				t.Fatal("failed Apply returned a snapshot")
			}
			if f.Generation() != 0 {
				t.Fatalf("failed Apply bumped generation to %d", f.Generation())
			}
			if f.Snapshot() != before {
				t.Fatal("failed Apply replaced the published snapshot")
			}
			if !f.Snapshot().BlockDefinitelyAbsent([]uint32{1, 2}) {
				t.Fatal("failed batch leaked a partial add")
			}
		})
	}
}

func TestRemoveOverReleaseRejected(t *testing.T) {
	f := mustNew(t, 8)
	mustApply(t, f, Change{Kind: KindAdd, Positions: []uint32{3}})
	if _, err := f.Apply([]Change{{Kind: KindRemove, Positions: []uint32{3, 3, 3, 3}}}); !errors.Is(err, ErrRemoveNotHeld) {
		t.Fatalf("over-release err = %v, want ErrRemoveNotHeld", err)
	}
	if c, _ := f.Snapshot().Counter(3); c != 1 {
		t.Fatalf("rejected over-release changed counter to %d, want 1", c)
	}
	if f.Snapshot().BlockDefinitelyAbsent([]uint32{3}) {
		t.Fatal("rejected over-release made the held block definitely absent")
	}
}

func TestSnapshotPinKeepsOldGeneration(t *testing.T) {
	f := mustNew(t, 16)
	positions := []uint32{4, 6}
	old := f.Snapshot()
	added := mustApply(t, f, Change{Kind: KindAdd, Positions: positions})

	if !old.BlockDefinitelyAbsent(positions) {
		t.Fatal("pinned pre-add snapshot changed after a newer generation was published")
	}
	if old.Generation() != 0 {
		t.Fatalf("pinned snapshot generation = %d, want 0", old.Generation())
	}
	if added.Generation() != 1 {
		t.Fatalf("published snapshot generation = %d, want 1", added.Generation())
	}

	mustApply(t, f, Change{Kind: KindRemove, Positions: positions})
	if added.BlockDefinitelyAbsent(positions) {
		t.Fatal("snapshot pinned after add changed after a later release")
	}
	if !f.Snapshot().BlockDefinitelyAbsent(positions) {
		t.Fatal("current snapshot did not observe the release")
	}
	if added.Generation() != 1 {
		t.Fatalf("pinned-after-add generation = %d, want 1", added.Generation())
	}
}

// TestCollisionChainOverflowsAndDrains drives one hot slot past 15 via many
// blocks that share that slot, then releases them all while one block stays
// held. Every held block must remain nonzero at every step, and the sparse
// overflow must vanish once the excess drains.
func TestCollisionChainOverflowsAndDrains(t *testing.T) {
	f := mustNew(t, 64)
	const colliders = 24

	held := Change{Kind: KindAdd, Positions: []uint32{3, 63}}
	batch := []Change{held}
	for i := uint32(0); i < colliders; i++ {
		batch = append(batch, Change{Kind: KindAdd, Positions: []uint32{3, 10 + i}})
	}
	snap := mustApply(t, f, batch...)

	if c, ok := snap.PrimaryCounter(3); !ok || c != nibbleMax {
		t.Fatalf("primary counter[3] = %d,%v want saturated %d", c, ok, nibbleMax)
	}
	excess, ok := snap.OverflowExcess(3)
	if !ok || excess != colliders+1-nibbleMax {
		t.Fatalf("overflow excess[3] = %d,%v want %d", excess, ok, colliders+1-nibbleMax)
	}
	if got := snap.OverflowEntries(); got != 1 {
		t.Fatalf("overflow entries = %d, want 1 (only slot 3 exceeds 15)", got)
	}
	if total, ok := snap.Counter(3); !ok || total != colliders+1 {
		t.Fatalf("total counter[3] = %d,%v want %d", total, ok, colliders+1)
	}
	if snap.BlockDefinitelyAbsent(held.Positions) {
		t.Fatal("held block with a saturated position reported definitely absent")
	}

	// Release the colliders one generation at a time. After each release, the
	// held block must still be maybe present and the total count must drop by 1.
	for i := uint32(0); i < colliders; i++ {
		snap = mustApply(t, f, Change{Kind: KindRemove, Positions: []uint32{3, 10 + i}})
		want := colliders + 1 - (i + 1)
		if total, _ := snap.Counter(3); total != want {
			t.Fatalf("step %d: total counter[3] = %d, want %d", i, total, want)
		}
		if snap.BlockDefinitelyAbsent(held.Positions) {
			t.Fatalf("step %d: still-held block became definitely absent", i)
		}
		// The sparse entry must disappear as soon as the excess reaches zero,
		// never lingering as a redundant zero-valued entry.
		if excess, _ := snap.OverflowExcess(3); excess == 0 && snap.OverflowEntries() != 0 {
			t.Fatalf("step %d: zero excess left a redundant overflow entry", i)
		}
		// Just after the excess drains, the primary counter is still saturated:
		// the held block keeps the slot nonzero through the handoff from the
		// overflow region back to the four-bit region.
		if i == colliders+1-nibbleMax-1 {
			if primary, _ := snap.PrimaryCounter(3); primary != nibbleMax {
				t.Fatalf("step %d: primary counter at excess handoff = %d, want %d", i, primary, nibbleMax)
			}
			if excess, _ := snap.OverflowExcess(3); excess != 0 {
				t.Fatalf("step %d: excess at handoff = %d, want 0", i, excess)
			}
			if got := snap.OverflowEntries(); got != 0 {
				t.Fatalf("step %d: overflow entries at handoff = %d, want 0", i, got)
			}
		}
	}

	if excess, _ := snap.OverflowExcess(3); excess != 0 {
		t.Fatalf("excess after draining = %d, want 0", excess)
	}
	if got := snap.OverflowEntries(); got != 0 {
		t.Fatalf("overflow entries after draining = %d, want 0 (no redundant entry)", got)
	}
	if primary, _ := snap.PrimaryCounter(3); primary != 1 {
		t.Fatalf("primary counter after excess drain = %d, want 1 (only the held block)", primary)
	}
	if !snap.BlockMaybePresent(held.Positions) {
		t.Fatal("held block not maybe present after colliders were released")
	}

	// Final release clears the hot slot and the block becomes definitely absent.
	snap = mustApply(t, f, Change{Kind: KindRemove, Positions: []uint32{3, 63}})
	if total, _ := snap.Counter(3); total != 0 {
		t.Fatalf("total counter[3] after final release = %d, want 0", total)
	}
	if excess, _ := snap.OverflowExcess(3); excess != 0 || snap.OverflowEntries() != 0 {
		t.Fatalf("overflow after final release = %d/%d entries, want 0/0", excess, snap.OverflowEntries())
	}
	if !snap.BlockDefinitelyAbsent(held.Positions) {
		t.Fatal("fully released block not reported definitely absent")
	}
}

func TestOverflowDrainStaysSparse(t *testing.T) {
	f := mustNew(t, 4096)
	// Saturate two slots far apart, then drain one back below 15 and confirm the
	// other stays saturated and the overflow map only tracks saturated slots.
	var batch []Change
	for i := 0; i < 20; i++ {
		batch = append(batch, Change{Kind: KindAdd, Positions: []uint32{7}})
		batch = append(batch, Change{Kind: KindAdd, Positions: []uint32{900}})
	}
	snap := mustApply(t, f, batch...)
	if got := snap.OverflowEntries(); got != 2 {
		t.Fatalf("overflow entries = %d, want 2", got)
	}

	var release []Change
	for i := 0; i < 10; i++ {
		release = append(release, Change{Kind: KindRemove, Positions: []uint32{7}})
	}
	snap = mustApply(t, f, release...)
	if primary, _ := snap.PrimaryCounter(7); primary != 10 {
		t.Fatalf("primary counter[7] = %d, want 10", primary)
	}
	if _, ok := snap.OverflowExcess(7); !ok {
		t.Fatal("OverflowExcess(7) not ok")
	}
	if excess, _ := snap.OverflowExcess(7); excess != 0 {
		t.Fatalf("excess[7] = %d, want 0", excess)
	}
	if got := snap.OverflowEntries(); got != 1 {
		t.Fatalf("overflow entries after partial drain = %d, want 1", got)
	}
	if primary, _ := snap.PrimaryCounter(900); primary != nibbleMax {
		t.Fatalf("primary counter[900] = %d, want saturated", primary)
	}
	if excess, _ := snap.OverflowExcess(900); excess != 5 {
		t.Fatalf("excess[900] = %d, want 5", excess)
	}
}

func TestCounterOverflowGuarded(t *testing.T) {
	f := mustNew(t, 4)
	snap := f.Snapshot().clone()
	snap.setNibble(1, nibbleMax)
	snap.overflow = map[uint32]uint32{1: maxExcess}
	if err := snap.increment(1); !errors.Is(err, ErrCounterOverflow) {
		t.Fatalf("increment at max excess err = %v, want ErrCounterOverflow", err)
	}
	if total, ok := snap.Counter(1); !ok || total != math.MaxUint32 {
		t.Fatalf("counter at max excess = %d,%v want %d,true", total, ok, uint32(math.MaxUint32))
	}
}

func TestManyDistinctBlocksStaySparse(t *testing.T) {
	f := mustNew(t, 2048)
	const blocks = 400
	var batch []Change
	for i := uint32(0); i < blocks; i++ {
		batch = append(batch, Change{Kind: KindAdd, Positions: []uint32{2 * i, 2*i + 1}})
	}
	snap := mustApply(t, f, batch...)
	if got := snap.OverflowEntries(); got != 0 {
		t.Fatalf("overflow entries = %d, want 0 when no slot exceeds 15", got)
	}
	if snap.BlockDefinitelyAbsent([]uint32{0, 1}) || snap.BlockDefinitelyAbsent([]uint32{798, 799}) {
		t.Fatal("added block reported definitely absent")
	}
}

func TestExactSaturationBoundary(t *testing.T) {
	f := mustNew(t, 16)
	var batch []Change
	for i := 0; i < int(nibbleMax); i++ {
		batch = append(batch, Change{Kind: KindAdd, Positions: []uint32{9}})
	}
	// Exactly 15 references fill the four bits and must not touch overflow.
	snap := mustApply(t, f, batch...)
	if primary, _ := snap.PrimaryCounter(9); primary != nibbleMax {
		t.Fatalf("primary counter[9] = %d, want %d", primary, nibbleMax)
	}
	if excess, _ := snap.OverflowExcess(9); excess != 0 {
		t.Fatalf("excess[9] = %d, want 0 at exactly 15 references", excess)
	}
	if got := snap.OverflowEntries(); got != 0 {
		t.Fatalf("overflow entries = %d, want 0 at exactly 15 references", got)
	}

	// The sixteenth reference is the first one that spills.
	snap = mustApply(t, f, Change{Kind: KindAdd, Positions: []uint32{9}})
	if primary, _ := snap.PrimaryCounter(9); primary != nibbleMax {
		t.Fatalf("primary counter[9] = %d, want %d", primary, nibbleMax)
	}
	if excess, _ := snap.OverflowExcess(9); excess != 1 {
		t.Fatalf("excess[9] = %d, want 1 at 16 references", excess)
	}
	if got := snap.OverflowEntries(); got != 1 {
		t.Fatalf("overflow entries = %d, want 1 at 16 references", got)
	}

	// Releasing back to 15 must remove the sole excess entry.
	snap = mustApply(t, f, Change{Kind: KindRemove, Positions: []uint32{9}})
	if excess, _ := snap.OverflowExcess(9); excess != 0 {
		t.Fatalf("excess[9] = %d, want 0 after dropping to 15", excess)
	}
	if got := snap.OverflowEntries(); got != 0 {
		t.Fatalf("overflow entries = %d, want 0 after dropping to 15", got)
	}
	if primary, _ := snap.PrimaryCounter(9); primary != nibbleMax {
		t.Fatalf("primary counter[9] = %d, want %d after dropping to 15", primary, nibbleMax)
	}
}

func TestApplyBatchAppliesInOrder(t *testing.T) {
	f := mustNew(t, 4)
	// A single batch that adds and then releases the same positions must be
	// accepted and leave the slot empty.
	snap := mustApply(t, f,
		Change{Kind: KindAdd, Positions: []uint32{1}},
		Change{Kind: KindRemove, Positions: []uint32{1}},
	)
	if c, _ := snap.Counter(1); c != 0 {
		t.Fatalf("counter[1] = %d, want 0", c)
	}
	if snap.Generation() != 1 {
		t.Fatalf("generation = %d, want 1 for one batch", snap.Generation())
	}
}

func TestConcurrentAppliesPublishDistinctGenerations(t *testing.T) {
	f := mustNew(t, 4096)
	const writers = 4
	const perWriter = 50

	var wg sync.WaitGroup
	done := make(chan struct{})
	// A concurrent reader pins the initial snapshot and keeps checking it
	// while writers publish newer generations.
	initial := f.Snapshot()
	go func() {
		for {
			select {
			case <-done:
				return
			default:
				if !initial.BlockDefinitelyAbsent([]uint32{0}) {
					t.Error("pinned empty snapshot changed during concurrent applies")
					return
				}
			}
		}
	}()

	for w := uint32(0); w < writers; w++ {
		wg.Add(1)
		go func(base uint32) {
			defer wg.Done()
			for i := uint32(0); i < perWriter; i++ {
				_, err := f.Apply([]Change{{Kind: KindAdd, Positions: []uint32{base*perWriter + i}}})
				if err != nil {
					t.Errorf("concurrent Apply: %v", err)
					return
				}
			}
		}(w)
	}
	wg.Wait()
	close(done)

	if got := f.Generation(); got != writers*perWriter {
		t.Fatalf("final generation = %d, want %d", got, writers*perWriter)
	}
	for w := uint32(0); w < writers; w++ {
		for i := uint32(0); i < perWriter; i++ {
			pos := w*perWriter + i
			if c, _ := f.Snapshot().Counter(pos); c != 1 {
				t.Fatalf("counter[%d] = %d, want 1", pos, c)
			}
		}
	}
}

func TestCloneIsIndependent(t *testing.T) {
	f := mustNew(t, 8)
	mustApply(t, f, Change{Kind: KindAdd, Positions: []uint32{1}})
	a := f.Snapshot()
	b := a.clone()
	if err := b.increment(2); err != nil {
		t.Fatal(err)
	}
	if a.MaybePresent(2) {
		t.Fatal("clone mutation leaked into the source snapshot")
	}
	if !a.MaybePresent(1) || !b.MaybePresent(1) {
		t.Fatal("clone lost source state")
	}
}

// TestRandomizedModelMatch drives many interleaved adds and releases over a
// small slot space (so collisions are frequent) and compares the filter's
// counters against a plain reference model after every published generation.
// The reference model is only used to verify counts; it lives in the test and
// is never stored inside the filter.
func TestRandomizedModelMatch(t *testing.T) {
	const slots = 24
	f := mustNew(t, slots)
	rng := rand.New(rand.NewSource(12345))

	// model[slot] is the exact reference count for each primary slot.
	model := make([]uint32, slots)
	heldPositions := make([]uint32, 0, 8)

	check := func(step int, snap *Snapshot) {
		t.Helper()
		for pos := uint32(0); pos < slots; pos++ {
			c, ok := snap.Counter(pos)
			if !ok {
				t.Fatalf("step %d: Counter(%d) not ok", step, pos)
			}
			if c != model[pos] {
				t.Fatalf("step %d: Counter(%d) = %d, model %d", step, pos, c, model[pos])
			}
			primary, _ := snap.PrimaryCounter(pos)
			wantPrimary := model[pos]
			if wantPrimary > nibbleMax {
				wantPrimary = nibbleMax
			}
			if primary != wantPrimary {
				t.Fatalf("step %d: PrimaryCounter(%d) = %d, want %d", step, pos, primary, wantPrimary)
			}
			var wantExcess uint32
			if model[pos] > nibbleMax {
				wantExcess = model[pos] - nibbleMax
			}
			if excess, _ := snap.OverflowExcess(pos); excess != wantExcess {
				t.Fatalf("step %d: OverflowExcess(%d) = %d, want %d", step, pos, excess, wantExcess)
			}
			if (model[pos] > 0) != snap.MaybePresent(pos) {
				t.Fatalf("step %d: MaybePresent(%d) = %v with count %d", step, pos, !snap.MaybePresent(pos), model[pos])
			}
		}
	}

	for step := 0; step < 5000; step++ {
		// Interleave: add a fresh two-position block sometimes, otherwise mutate
		// a block we already hold.
		var batch []Change
		if len(heldPositions) == 0 || rng.Intn(100) < 55 {
			p1 := uint32(rng.Intn(slots))
			p2 := uint32(rng.Intn(slots))
			positions := []uint32{p1, p2}
			batch = []Change{{Kind: KindAdd, Positions: positions}}
			for _, p := range positions {
				model[p]++
			}
			heldPositions = append(heldPositions, p1, p2)
		} else {
			// Release a previously added block (the last two entries).
			p1 := heldPositions[len(heldPositions)-2]
			p2 := heldPositions[len(heldPositions)-1]
			batch = []Change{{Kind: KindRemove, Positions: []uint32{p1, p2}}}
			if model[p1] == 0 || model[p2] == 0 {
				t.Fatalf("step %d: model underflow for positions %d,%d", step, p1, p2)
			}
			model[p1]--
			model[p2]--
			heldPositions = heldPositions[:len(heldPositions)-2]
		}

		snap, err := f.Apply(batch)
		if err != nil {
			t.Fatalf("step %d: Apply: %v", step, err)
		}
		check(step, snap)

		// Sparse invariant: an entry exists exactly for slots whose count exceeds
		// 15, and never with a zero value.
		wantEntries := 0
		for _, c := range model {
			if c > nibbleMax {
				wantEntries++
			}
		}
		if got := snap.OverflowEntries(); got != wantEntries {
			t.Fatalf("step %d: OverflowEntries = %d, want %d", step, got, wantEntries)
		}
		if f.Generation() != uint64(step+1) {
			t.Fatalf("step %d: generation = %d, want %d", step, f.Generation(), step+1)
		}
	}

	// Drain everything; the filter must end completely empty.
	var drain []Change
	for i := 0; i+1 < len(heldPositions); i += 2 {
		drain = append(drain, Change{Kind: KindRemove, Positions: []uint32{heldPositions[i], heldPositions[i+1]}})
	}
	snap := mustApply(t, f, drain...)
	for pos := uint32(0); pos < slots; pos++ {
		if c, _ := snap.Counter(pos); c != 0 {
			t.Fatalf("after drain: Counter(%d) = %d, want 0", pos, c)
		}
	}
	if snap.OverflowEntries() != 0 {
		t.Fatalf("after drain: OverflowEntries = %d, want 0", snap.OverflowEntries())
	}
}

func TestKindString(t *testing.T) {
	cases := []struct {
		kind Kind
		want string
	}{
		{KindAdd, "add"},
		{KindRemove, "remove"},
		{Kind(7), "kind(7)"},
	}
	for _, tc := range cases {
		if got := tc.kind.String(); got != tc.want {
			t.Fatalf("Kind(%d).String() = %q, want %q", uint8(tc.kind), got, tc.want)
		}
	}
}

func TestSnapshotAccessorsReportRange(t *testing.T) {
	f := mustNew(t, 3)
	snap := f.Snapshot()
	if snap.Slots() != 3 {
		t.Fatalf("Snapshot.Slots() = %d, want 3", snap.Slots())
	}
	if _, ok := snap.PrimaryCounter(3); ok {
		t.Fatal("out-of-range PrimaryCounter reported ok")
	}
	if _, ok := snap.OverflowExcess(3); ok {
		t.Fatal("out-of-range OverflowExcess reported ok")
	}
	if _, ok := snap.PrimaryCounter(2); !ok {
		t.Fatal("in-range PrimaryCounter reported not ok")
	}
	if _, ok := snap.OverflowExcess(2); !ok {
		t.Fatal("in-range OverflowExcess reported not ok")
	}
	// A fresh filter with no overflow map must still answer safely.
	if excess, ok := snap.OverflowExcess(0); !ok || excess != 0 {
		t.Fatalf("empty overflow excess[0] = %d,%v want 0,true", excess, ok)
	}
}

func TestFilterGenerationAndSlots(t *testing.T) {
	f := mustNew(t, 64)
	if f.Slots() != 64 {
		t.Fatalf("Filter.Slots() = %d, want 64", f.Slots())
	}
	if f.Generation() != 0 {
		t.Fatalf("Filter.Generation() = %d, want 0", f.Generation())
	}
	mustApply(t, f, Change{Kind: KindAdd, Positions: []uint32{0}})
	if f.Generation() != 1 {
		t.Fatalf("Filter.Generation() = %d after one batch, want 1", f.Generation())
	}
}

func TestBlockMaybePresentRequiresAllPositions(t *testing.T) {
	f := mustNew(t, 8)
	mustApply(t, f, Change{Kind: KindAdd, Positions: []uint32{1}})
	snap := f.Snapshot()
	if snap.BlockMaybePresent([]uint32{1}) != true {
		t.Fatal("single present position must report maybe present")
	}
	if snap.BlockMaybePresent([]uint32{1, 5}) {
		t.Fatal("block with a zero position must not report maybe present")
	}
}

func TestBatchCounterOverflowDropsWholeBatch(t *testing.T) {
	// Reaching uint32 saturation through Apply would take billions of changes,
	// so drive the internal batch path from a state near the ceiling.
	f := mustNew(t, 4)
	seeded := f.Snapshot().clone()
	seeded.setNibble(1, nibbleMax)
	seeded.overflow = map[uint32]uint32{1: maxExcess}

	before := seeded.clone()
	if err := seeded.applyBatch([]Change{{Kind: KindAdd, Positions: []uint32{1, 2}}}); !errors.Is(err, ErrCounterOverflow) {
		t.Fatalf("applyBatch overflow err = %v, want ErrCounterOverflow", err)
	}
	// The batch was partially attempted before failing, but Apply never publishes
	// a failed copy, so the persisted state must not contain the earlier add.
	if before.MaybePresent(2) {
		t.Fatal("reference state unexpectedly present at slot 2")
	}
	if _, err := f.Apply([]Change{{Kind: KindAdd, Positions: []uint32{2}}}); err != nil {
		t.Fatalf("subsequent Apply: %v", err)
	}
	if c, _ := f.Snapshot().Counter(2); c != 1 {
		t.Fatalf("counter[2] = %d after fresh Apply, want 1; failed batch leaked", c)
	}
}
