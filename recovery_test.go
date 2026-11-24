package dataflow

import (
	"encoding/json"
	"reflect"
	"testing"
)

// TestRestoreHoldersFromTraceMiddle replays a prefix of a real interleaving,
// exports the holder map, round-trips the snapshot through JSON, restores it
// into a fresh driver, and proves both recovered drivers continue to the
// exact same final state as the original.
func TestRestoreHoldersFromTraceMiddle(t *testing.T) {
	cfg := Config{Pool: 1, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}
	d, err := NewDriver(fanoutGraph(), fanoutPrograms(), cfg)
	if err != nil {
		t.Fatal(err)
	}

	// Interleaving prefix: P allocates and fans out, then C1 takes and
	// releases. At this instant the only live block has exactly one holder?
	// After C1 release: no holders, one queued ref on edge2. Choose the
	// middle right after C1 TAKE instead for an interesting holder map.
	mustStep(t, d, "P")  // alloc
	mustStep(t, d, "P")  // publish
	mustStep(t, d, "P")  // release producer
	mustStep(t, d, "C1") // take edge1 -> C1 holds the block

	const middle = 4
	trace := d.Trace()
	if len(trace) != middle {
		t.Fatalf("trace length=%d, want %d", len(trace), middle)
	}
	bid := BlockID{Origin: "P", Index: 0}
	wantHolders := map[OpID]int{"C1": 1}
	if h := d.Holders(bid); !reflect.DeepEqual(h, wantHolders) {
		t.Fatalf("mid-trace holders=%v, want %v", h, wantHolders)
	}

	// Path 1: replay the recorded prefix into a fresh driver.
	replayed, err := Replay(fanoutGraph(), fanoutPrograms(), cfg, trace, middle)
	if err != nil {
		t.Fatalf("Replay failed: %v", err)
	}
	if h := replayed.Holders(bid); !reflect.DeepEqual(h, wantHolders) {
		t.Fatalf("replayed holders=%v, want %v", h, wantHolders)
	}
	if !reflect.DeepEqual(replayed.Export(), d.Export()) {
		t.Fatal("replayed state differs from original mid-trace state")
	}

	// Path 2: export -> JSON -> import ("restore holders from trace middle").
	snap := d.Export()
	raw, err := json.Marshal(snap)
	if err != nil {
		t.Fatalf("marshal snapshot: %v", err)
	}
	var wire Snapshot
	if err := json.Unmarshal(raw, &wire); err != nil {
		t.Fatalf("unmarshal snapshot: %v", err)
	}
	restored, err := NewFromSnapshot(fanoutGraph(), fanoutPrograms(), wire)
	if err != nil {
		t.Fatalf("NewFromSnapshot failed: %v", err)
	}
	if h := restored.Holders(bid); !reflect.DeepEqual(h, wantHolders) {
		t.Fatalf("restored holders=%v, want %v", h, wantHolders)
	}
	if !reflect.DeepEqual(restored.Export(), snap) {
		t.Fatal("JSON round-tripped snapshot did not restore an equal state")
	}

	// Path 3: in-place Restore on a driver that has progressed elsewhere.
	other, err := NewDriver(fanoutGraph(), fanoutPrograms(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := other.Step("C2"); err == nil {
		t.Fatal("C2 take on empty edge should block")
	}
	if len(other.Trace()) != 0 {
		t.Fatal("blocked step should not record anything")
	}
	if err := other.Restore(snap); err != nil {
		t.Fatalf("Restore failed: %v", err)
	}
	if !reflect.DeepEqual(other.Export(), snap) {
		t.Fatal("in-place restore mismatch")
	}

	// All three drivers now run the same suffix and must converge.
	suffix := []OpID{"C1", "C2", "C2"} // C1 release, C2 take, C2 release
	drivers := map[string]*Driver{"orig": d, "replay": replayed, "restored": restored, "inplace": other}
	for name, dd := range drivers {
		for _, op := range suffix {
			if _, err := dd.Step(op); err != nil {
				t.Fatalf("%s suffix step %q failed: %v", name, op, err)
			}
		}
		if !dd.AllDone() {
			t.Fatalf("%s not done after suffix", name)
		}
		if dd.PoolUsed() != 0 {
			t.Fatalf("%s pool used=%d, want 0", name, dd.PoolUsed())
		}
		if err := dd.CheckInvariants(); err != nil {
			t.Fatalf("%s invariant: %v", name, err)
		}
	}
	if !reflect.DeepEqual(d.Export(), replayed.Export()) || !reflect.DeepEqual(d.Export(), restored.Export()) {
		t.Fatal("recovered drivers diverged after running the suffix")
	}
}

// TestReplayRejectsForeignTrace ensures replay actually verifies records.
func TestReplayRejectsForeignTrace(t *testing.T) {
	cfg := Config{Pool: 1, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}
	d, err := NewDriver(fanoutGraph(), fanoutPrograms(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	mustStep(t, d, "P")
	tr := d.Trace()

	// Tamper with the expected instruction index.
	bad := []Record{tr[0]}
	bad[0].InstrIndex = 99
	if _, err := Replay(fanoutGraph(), fanoutPrograms(), cfg, bad, 1); err == nil {
		t.Fatal("expected replay mismatch error")
	}

	// Upto beyond length is clamped to full length; a one-record trace ends
	// with P only partially done but must reconstruct exactly.
	partial, err := Replay(fanoutGraph(), fanoutPrograms(), cfg, tr, -1)
	if err != nil {
		t.Fatal(err)
	}
	if partial.PoolUsed() != 1 {
		t.Fatalf("replayed pool=%d want 1", partial.PoolUsed())
	}
}

// TestSnapshotRejectsCorruption proves restored snapshots are validated.
func TestSnapshotRejectsCorruption(t *testing.T) {
	cfg := Config{Pool: 1, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}
	d, err := NewDriver(fanoutGraph(), fanoutPrograms(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	mustStep(t, d, "P")
	snap := d.Export()

	// Claim twice as many refs as queues+handles justify.
	if len(snap.Blocks) != 1 {
		t.Fatalf("blocks=%d want 1", len(snap.Blocks))
	}
	snap.Blocks[0].Refs = 99
	if _, err := NewFromSnapshot(fanoutGraph(), fanoutPrograms(), snap); err == nil {
		t.Fatal("expected invalid snapshot to be rejected")
	}
}
