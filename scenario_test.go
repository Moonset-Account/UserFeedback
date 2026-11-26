package sparseblockvol

import (
	"errors"
	"fmt"
	"testing"
)

func reopen(t *testing.T, root string) *Store {
	t.Helper()
	s, report, err := Open(root)
	if err != nil {
		t.Fatalf("Open failed: %v", err)
	}
	if len(report.MissingBlocks) != 0 {
		t.Fatalf("missing blocks after recovery: %v", report.MissingBlocks)
	}
	return s
}

func mustView(t *testing.T, v *Volume, id int64) *View {
	t.Helper()
	w, err := v.Version(id)
	if err != nil {
		t.Fatalf("version %d: %v", id, err)
	}
	return w
}

func readAll(t *testing.T, w *View) []byte {
	t.Helper()
	buf := make([]byte, w.Size())
	if _, err := w.ReadAt(buf, 0); err != nil {
		t.Fatalf("ReadAt: %v", err)
	}
	return buf
}

func assertEntry(t *testing.T, w *View, lba int64, want Entry) {
	t.Helper()
	got, err := w.LookupEntry(lba)
	if err != nil {
		t.Fatalf("LookupEntry(%d): %v", lba, err)
	}
	if got != want {
		t.Fatalf("lba %d: got %+v want %+v", lba, got, want)
	}
}

func assertBytes(t *testing.T, got, want []byte, msg string) {
	t.Helper()
	if string(got) != string(want) {
		t.Fatalf("%s: got %q want %q", msg, got, want)
	}
}

func countPhysical(t *testing.T, s *Store) int {
	t.Helper()
	orphans, missing, err := s.Audit()
	if err != nil {
		t.Fatalf("Audit: %v", err)
	}
	if len(missing) != 0 {
		t.Fatalf("missing blocks: %v", missing)
	}
	refs := s.recomputeRefs()
	return len(refs) + len(orphans)
}

// TestEndToEndTwoWritesInterruptedMerge drives the whole narrative:
// volumes with different alignments, two snapshot-producing writes per
// volume, content identical across different block boundaries, an
// interrupted write, and an interrupted merge whose replay is verified after
// reopening, followed by physical block reclamation.
func TestEndToEndTwoWritesInterruptedMerge(t *testing.T) {
	root := t.TempDir()
	s, err := Create(root)
	if err != nil {
		t.Fatal(err)
	}

	// Alignment upgrade in the field: volume A keeps the old 4-byte blocks,
	// new volume B uses 8-byte blocks. Old block numbers stay valid.
	volA, err := s.CreateVolume("volA", 4)
	if err != nil {
		t.Fatal(err)
	}
	volB, err := s.CreateVolume("volB", 8)
	if err != nil {
		t.Fatal(err)
	}

	// ---- write 1 on A: one 12-byte write covering three 4-byte blocks ----
	t1, err := volA.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := t1.WriteAt([]byte("abcdefghijkl"), 0); err != nil {
		t.Fatal(err)
	}
	if err := t1.SetSnapshot("s1"); err != nil {
		t.Fatal(err)
	}
	v1, err := t1.Commit()
	if err != nil {
		t.Fatal(err)
	}
	if v1.VersionID() != 2 {
		t.Fatalf("first write version = %d, want 2", v1.VersionID())
	}
	idAbcd := hashBlock([]byte("abcd"))
	idEfgh := hashBlock([]byte("efgh"))
	idIjkl := hashBlock([]byte("ijkl"))
	assertEntry(t, v1, 0, Entry{Kind: KindMapped, BlockID: idAbcd})
	assertEntry(t, v1, 1, Entry{Kind: KindMapped, BlockID: idEfgh})
	assertEntry(t, v1, 2, Entry{Kind: KindMapped, BlockID: idIjkl})
	assertEntry(t, v1, 3, Entry{Kind: KindHole})
	assertBytes(t, readAll(t, v1), []byte("abcdefghijkl"), "A s1 contents")

	// ---- write 2 on A: overlapping, differently bounded ranges ----
	// [1,7) rewrites parts of blocks 0 and 1 and all of... block 1's middle;
	// [8,12) re-splits the same 8 bytes that previously formed "ijkl" as two
	// 4-byte blocks.
	t2, err := volA.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := t2.WriteAt([]byte("BCDEFG"), 1); err != nil {
		t.Fatal(err)
	}
	if err := t2.WriteAt([]byte("IJKL"), 8); err != nil {
		t.Fatal(err)
	}
	if err := t2.SetSnapshot("s2"); err != nil {
		t.Fatal(err)
	}
	v2, err := t2.Commit()
	if err != nil {
		t.Fatal(err)
	}
	idA0 := hashBlock([]byte("aBCDEFG"[:4])) // block 0: a B C D
	_ = idA0
	wantA0 := hashBlock([]byte{'a', 'B', 'C', 'D'})
	wantA1 := hashBlock([]byte{'E', 'F', 'G', 'h'})
	wantA2 := hashBlock([]byte("IJKL"))
	assertEntry(t, v2, 0, Entry{Kind: KindMapped, BlockID: wantA0})
	assertEntry(t, v2, 1, Entry{Kind: KindMapped, BlockID: wantA1})
	assertEntry(t, v2, 2, Entry{Kind: KindMapped, BlockID: wantA2})
	assertBytes(t, readAll(t, v2), []byte("aBCDEFGhIJKL"), "A s2 contents")

	// Old snapshot s1 must remain byte-for-byte readable.
	s1, err := volA.Snapshot("s1")
	if err != nil {
		t.Fatal(err)
	}
	assertBytes(t, readAll(t, s1), []byte("abcdefghijkl"), "A s1 still readable")

	// ---- volume B (8-byte blocks): same content, different boundaries ----
	bt1, err := volB.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := bt1.WriteAt([]byte("abcdefghijkl"), 0); err != nil {
		t.Fatal(err)
	}
	bv1, err := bt1.Commit()
	if err != nil {
		t.Fatal(err)
	}
	idB0 := hashBlock([]byte("abcdefgh"))
	idB1 := hashBlock([]byte("ijkl\x00\x00\x00\x00"))
	assertEntry(t, bv1, 0, Entry{Kind: KindMapped, BlockID: idB0})
	assertEntry(t, bv1, 1, Entry{Kind: KindMapped, BlockID: idB1})
	if bv1.Size() != 16 {
		t.Fatalf("B size = %d, want 16", bv1.Size())
	}

	// Second B write: a middle-only overwrite inside one 8-byte block,
	// an explicit zero segment and a punched hole.
	bt2, err := volB.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := bt2.WriteAt([]byte("XY"), 9); err != nil { // middle of block 1
		t.Fatal(err)
	}
	if err := bt2.WriteZero(make([]byte, 8), 16); err != nil {
		t.Fatal(err)
	}
	if err := bt2.PunchHole(0, 8); err != nil {
		t.Fatal(err)
	}
	bv2, err := bt2.Commit()
	if err != nil {
		t.Fatal(err)
	}
	wantB1 := hashBlock([]byte{'i', 'X', 'Y', 'l', 0, 0, 0, 0})
	assertEntry(t, bv2, 0, Entry{Kind: KindHole})
	assertEntry(t, bv2, 1, Entry{Kind: KindMapped, BlockID: wantB1})
	assertEntry(t, bv2, 2, Entry{Kind: KindZero})
	assertEntry(t, bv2, 3, Entry{Kind: KindHole})
	ext, err := bv2.Extents()
	if err != nil {
		t.Fatal(err)
	}
	wantExtents := []Extent{
		{Kind: KindHole, Offset: 0, Length: 8},
		{Kind: KindMapped, Offset: 8, Length: 8},
		{Kind: KindZero, Offset: 16, Length: 8},
	}
	if fmt.Sprint(ext) != fmt.Sprint(wantExtents) {
		t.Fatalf("B extents = %v want %v", ext, wantExtents)
	}
	// Zero and hole both read as zero bytes but remain distinguishable.
	buf := make([]byte, bv2.Size())
	if _, err := bv2.ReadAt(buf, 0); err != nil {
		t.Fatal(err)
	}
	assertBytes(t, buf[:8], make([]byte, 8), "hole reads zero")
	assertBytes(t, buf[16:24], make([]byte, 8), "zero segment reads zero")

	// Cross-boundary content identity: when a later write reproduces a block
	// that already exists (here B's original block 0, "abcdefgh", rewritten at
	// offset 24), no new physical block is created: both logical blocks point
	// at one content-addressed file.
	physicalBefore := countPhysical(t, s)
	bt3, err := volB.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := bt3.WriteAt([]byte("abcdefgh"), 24); err != nil {
		t.Fatal(err)
	}
	bv3, err := bt3.Commit()
	if err != nil {
		t.Fatal(err)
	}
	idShared := hashBlock([]byte("abcdefgh"))
	assertEntry(t, bv3, 3, Entry{Kind: KindMapped, BlockID: idShared})
	physicalAfter := countPhysical(t, s)
	if physicalAfter != physicalBefore {
		t.Fatalf("rewriting identical content created a physical block: before=%d after=%d",
			physicalBefore, physicalAfter)
	}

	refsBefore := s.recomputeRefs()
	if refsBefore[idShared] < 2 {
		t.Fatalf("shared block %s referenced %d times, want >=2", idShared, refsBefore[idShared])
	}

	// ---- interrupted write: staged but never committed ----
	s.SetCrashPoint("txn.after_stage")
	tx, err := volA.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := tx.WriteAt([]byte("zzzz"), 0); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Commit(); !errors.Is(err, ErrInjectedCrash) {
		t.Fatalf("Commit crash = %v, want ErrInjectedCrash", err)
	}
	head, err := volA.Head()
	if err != nil {
		t.Fatal(err)
	}
	assertBytes(t, readAll(t, head), []byte("aBCDEFGhIJKL"), "A unchanged after staged crash")
	// The payload lives only inside the private staging directory; nothing
	// has entered the shared block directory and no mapping changed.
	stagedRoot := txnDir(s.root, tx.ID())
	if !fileExists(stagedRoot) {
		t.Fatal("staging directory missing after injected crash")
	}
	orphansBefore, _, err := s.Audit()
	if err != nil {
		t.Fatal(err)
	}
	if len(orphansBefore) != 0 {
		t.Fatalf("staged payload leaked into block directory: %v", orphansBefore)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}

	// ---- reopen: staged txn rolled back ----
	s = reopen(t, root)
	report, err := s.LastRecoveryReport()
	if err != nil {
		t.Fatal(err)
	}
	if len(report.AbortedTxn) != 1 {
		t.Fatalf("AbortedTxn = %v, want one entry", report.AbortedTxn)
	}
	volA, err = s.Volume("volA")
	if err != nil {
		t.Fatal(err)
	}
	volB, err = s.Volume("volB")
	if err != nil {
		t.Fatal(err)
	}
	head, err = volA.Head()
	if err != nil {
		t.Fatal(err)
	}
	assertBytes(t, readAll(t, head), []byte("aBCDEFGhIJKL"), "A head after rollback")
	s1, err = volA.Snapshot("s1")
	if err != nil {
		t.Fatal(err)
	}
	assertBytes(t, readAll(t, s1), []byte("abcdefghijkl"), "s1 survives recovery")
	orphans, _, err := s.Audit()
	if err != nil {
		t.Fatal(err)
	}
	if len(orphans) != 0 {
		t.Fatalf("orphans after txn rollback: %v", orphans)
	}

	// ---- interrupted merge: B := merge A at unaligned offset 2 ----
	src, err := volA.Snapshot("s1")
	if err != nil {
		t.Fatal(err)
	}
	s.SetCrashPoint("merge.after_stage")
	if _, err := volB.Merge(src, MergeOptions{DstOffset: 2, CrashPoint: "merge.after_stage"}); !errors.Is(err, ErrInjectedCrash) {
		t.Fatalf("Merge crash = %v, want ErrInjectedCrash", err)
	}
	bHead, err := volB.Head()
	if err != nil {
		t.Fatal(err)
	}
	if bHead.VersionID() != bv3.VersionID() {
		t.Fatalf("B head changed during staged merge: %d", bHead.VersionID())
	}
	if !fileExists(mergeJournalPath(s.root, "volB")) {
		t.Fatal("merge journal missing after injected crash")
	}
	orphans, _, err = s.Audit()
	if err != nil {
		t.Fatal(err)
	}
	if len(orphans) != 0 {
		t.Fatalf("merge staging leaked into block directory: %v", orphans)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}

	// ---- reopen: merge journal replayed ----
	s = reopen(t, root)
	report, err = s.LastRecoveryReport()
	if err != nil {
		t.Fatal(err)
	}
	if len(report.ResumedMerge) != 1 || report.ResumedMerge[0] != "volB" {
		t.Fatalf("ResumedMerge = %v", report.ResumedMerge)
	}
	volB, err = s.Volume("volB")
	if err != nil {
		t.Fatal(err)
	}
	merged, err := volB.Head()
	if err != nil {
		t.Fatal(err)
	}
	// B was 32 bytes; merged region [2,14) keeps size at 32.
	if merged.Size() != 32 {
		t.Fatalf("merged size = %d want 32", merged.Size())
	}
	mergedBuf := make([]byte, 32)
	if _, err := merged.ReadAt(mergedBuf, 0); err != nil {
		t.Fatal(err)
	}
	wantMerged := make([]byte, 32)
	copy(wantMerged[2:14], []byte("abcdefghijkl"))
	// Pre-existing B data outside the merged window is preserved.
	copy(wantMerged[24:32], []byte("abcdefgh"))
	assertBytes(t, mergedBuf, wantMerged, "merged B contents")
	// Every earlier B version and both A snapshots remain readable.
	for _, id := range []int64{bv1.VersionID(), bv2.VersionID(), bv3.VersionID()} {
		w := mustView(t, volB, id)
		_ = readAll(t, w)
	}
	volA, err = s.Volume("volA")
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"s1", "s2"} {
		w, err := volA.Snapshot(name)
		if err != nil {
			t.Fatalf("snapshot %s: %v", name, err)
		}
		_ = readAll(t, w)
	}
	orphans, _, err = s.Audit()
	if err != nil {
		t.Fatal(err)
	}
	if len(orphans) != 0 {
		t.Fatalf("orphans after merge replay: %v", orphans)
	}

	// ---- reclamation: publish an unpinned version with exclusive blocks,
	// then drop it so its physical blocks become reclaimable ----
	excl, err := volA.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := excl.WriteAt([]byte("00001111"), 0); err != nil {
		t.Fatal(err)
	}
	exclView, err := excl.Commit()
	if err != nil {
		t.Fatal(err)
	}
	exclID := exclView.VersionID()
	// Publish another head so the exclusive version becomes deletable.
	next, err := volA.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := next.WriteAt([]byte("2222"), 0); err != nil {
		t.Fatal(err)
	}
	if _, err := next.Commit(); err != nil {
		t.Fatal(err)
	}
	versions, err := volA.Versions()
	if err != nil {
		t.Fatal(err)
	}
	for _, ver := range versions {
		if ver.ID == 1 && len(ver.Snapshots) == 0 {
			if err := volA.DeleteVersion(ver.ID); err != nil {
				t.Fatal(err)
			}
		}
	}
	before := countPhysical(t, s)
	if err := volA.DeleteVersion(exclID); err != nil {
		t.Fatal(err)
	}
	orphans, _, err = s.Audit()
	if err != nil {
		t.Fatal(err)
	}
	if len(orphans) == 0 {
		t.Fatal("deleting a version must make its exclusive blocks reclaimable")
	}
	reclaimed, err := s.Reclaim()
	if err != nil {
		t.Fatal(err)
	}
	if len(reclaimed) != len(orphans) {
		t.Fatalf("reclaimed %d blocks, orphans were %d", len(reclaimed), len(orphans))
	}
	after := countPhysical(t, s)
	if after >= before {
		t.Fatalf("physical blocks did not shrink: before=%d after=%d", before, after)
	}
	orphans, _, err = s.Audit()
	if err != nil {
		t.Fatal(err)
	}
	if len(orphans) != 0 {
		t.Fatalf("orphans remain after Reclaim: %v", orphans)
	}
	// Pinned snapshot data is still fully readable after reclamation.
	s1, err = volA.Snapshot("s1")
	if err != nil {
		t.Fatal(err)
	}
	assertBytes(t, readAll(t, s1), []byte("abcdefghijkl"), "s1 readable after Reclaim")
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
}
