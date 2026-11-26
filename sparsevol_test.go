package sparseblockvol

import (
	"errors"
	"testing"
)

// TestContentDedupAcrossSnapshots verifies that identical block content in
// different snapshots shares exactly one physical block, while changed
// content creates a new one and reference counts follow the live mappings.
func TestContentDedupAcrossSnapshots(t *testing.T) {
	s, err := Create(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	v, err := s.CreateVolume("v", 4)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := v.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := tx.WriteAt([]byte("abcdabcd"), 0); err != nil {
		t.Fatal(err)
	}
	w1, err := tx.Commit()
	if err != nil {
		t.Fatal(err)
	}
	id := hashBlock([]byte("abcd"))
	assertEntry(t, w1, 0, Entry{Kind: KindMapped, BlockID: id})
	assertEntry(t, w1, 1, Entry{Kind: KindMapped, BlockID: id})
	if s.recomputeRefs()[id] != 2 {
		t.Fatalf("refcount = %d, want 2", s.recomputeRefs()[id])
	}
	// Rewrite one block with different content.
	tx2, err := v.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := tx2.WriteAt([]byte("xyzw"), 0); err != nil {
		t.Fatal(err)
	}
	w2, err := tx2.Commit()
	if err != nil {
		t.Fatal(err)
	}
	id2 := hashBlock([]byte("xyzw"))
	assertEntry(t, w2, 0, Entry{Kind: KindMapped, BlockID: id2})
	assertEntry(t, w2, 1, Entry{Kind: KindMapped, BlockID: id})
	refs := s.recomputeRefs()
	if refs[id] != 3 || refs[id2] != 1 {
		t.Fatalf("refs = id:%d id2:%d, want 3 and 1", refs[id], refs[id2])
	}
}

// TestMiddleBlockOverwrite checks that writing only the middle of a block
// preserves the unmodified edges via read-modify-write.
func TestMiddleBlockOverwrite(t *testing.T) {
	s, _ := Create(t.TempDir())
	defer s.Close()
	v, _ := s.CreateVolume("v", 8)
	tx, _ := v.Begin()
	if err := tx.WriteAt([]byte("abcdefgh"), 0); err != nil {
		t.Fatal(err)
	}
	if err := tx.WriteAt([]byte("XY"), 3); err != nil {
		t.Fatal(err)
	}
	w, err := tx.Commit()
	if err != nil {
		t.Fatal(err)
	}
	assertBytes(t, readAll(t, w), []byte("abcXYfgh"), "middle overwrite")
	want := hashBlock([]byte("abcXYfgh"))
	assertEntry(t, w, 0, Entry{Kind: KindMapped, BlockID: want})
}

// TestZeroVersusHole proves the query interface distinguishes an explicit
// zero segment from an unallocated hole, although both read as zero bytes.
func TestZeroVersusHole(t *testing.T) {
	s, _ := Create(t.TempDir())
	defer s.Close()
	v, _ := s.CreateVolume("v", 4)
	tx, _ := v.Begin()
	if err := tx.WriteAt([]byte("abcd"), 0); err != nil {
		t.Fatal(err)
	}
	if err := tx.WriteZero(make([]byte, 4), 4); err != nil {
		t.Fatal(err)
	}
	if err := tx.PunchHole(8, 4); err != nil {
		t.Fatal(err)
	}
	w, err := tx.Commit()
	if err != nil {
		t.Fatal(err)
	}
	if w.Size() != 12 {
		t.Fatalf("size = %d want 12", w.Size())
	}
	assertEntry(t, w, 0, Entry{Kind: KindMapped, BlockID: hashBlock([]byte("abcd"))})
	assertEntry(t, w, 1, Entry{Kind: KindZero})
	assertEntry(t, w, 2, Entry{Kind: KindHole})
	assertEntry(t, w, 99, Entry{Kind: KindHole}) // beyond size
	exts, err := w.Extents()
	if err != nil {
		t.Fatal(err)
	}
	want := []Extent{
		{Kind: KindMapped, Offset: 0, Length: 4},
		{Kind: KindZero, Offset: 4, Length: 4},
		{Kind: KindHole, Offset: 8, Length: 4},
	}
	if len(exts) != len(want) {
		t.Fatalf("extents = %v want %v", exts, want)
	}
	for i := range want {
		if exts[i] != want[i] {
			t.Fatalf("extent %d = %v want %v", i, exts[i], want[i])
		}
	}
	buf := make([]byte, 12)
	if _, err := w.ReadAt(buf, 0); err != nil {
		t.Fatal(err)
	}
	assertBytes(t, buf[4:], make([]byte, 8), "zero and hole read alike")
}

// TestPunchHolePartialEdges verifies partial deallocation keeps edge bytes.
func TestPunchHolePartialEdges(t *testing.T) {
	s, _ := Create(t.TempDir())
	defer s.Close()
	v, _ := s.CreateVolume("v", 8)
	tx, _ := v.Begin()
	if err := tx.WriteAt([]byte("abcdefgh"), 0); err != nil {
		t.Fatal(err)
	}
	if err := tx.PunchHole(3, 2); err != nil { // zero bytes 3-4
		t.Fatal(err)
	}
	w, err := tx.Commit()
	if err != nil {
		t.Fatal(err)
	}
	assertBytes(t, readAll(t, w), []byte("abc\x00\x00fgh"), "partial punch")
}

// TestPersistenceReopen confirms all mappings survive a normal close/open.
func TestPersistenceReopen(t *testing.T) {
	root := t.TempDir()
	s, _ := Create(root)
	v, _ := s.CreateVolume("v", 4)
	tx, _ := v.Begin()
	if err := tx.WriteAt([]byte("hello!!"), 0); err != nil {
		t.Fatal(err)
	}
	if err := tx.SetSnapshot("snap"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	s2, report, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	if len(report.AbortedTxn) != 0 || len(report.ResumedMerge) != 0 {
		t.Fatalf("unexpected recovery work: %+v", report)
	}
	v2, err := s2.Volume("v")
	if err != nil {
		t.Fatal(err)
	}
	w, err := v2.Snapshot("snap")
	if err != nil {
		t.Fatal(err)
	}
	got := make([]byte, 7)
	if _, err := w.ReadAt(got, 0); err != nil {
		t.Fatal(err)
	}
	assertBytes(t, got, []byte("hello!!"), "persisted snapshot")
	if v2.BlockSize() != 4 {
		t.Fatalf("block size lost: %d", v2.BlockSize())
	}
}

// TestMergeAfterMetaCrash covers the second interruption point: metadata was
// switched but journal cleanup was interrupted; replay must not duplicate the
// version and the head must remain consistent.
func TestMergeAfterMetaCrash(t *testing.T) {
	root := t.TempDir()
	s, _ := Create(root)
	a, _ := s.CreateVolume("a", 4)
	b, _ := s.CreateVolume("b", 8)
	tx, _ := a.Begin()
	if err := tx.WriteAt([]byte("data"), 0); err != nil {
		t.Fatal(err)
	}
	src, err := tx.Commit()
	if err != nil {
		t.Fatal(err)
	}
	s.SetCrashPoint("merge.after_meta")
	if _, err := b.Merge(src, MergeOptions{CrashPoint: "merge.after_meta"}); !errors.Is(err, ErrInjectedCrash) {
		t.Fatalf("merge = %v want injected crash", err)
	}
	versionsBefore, _ := b.Versions()
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	s2, report, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	if !report.MergeCommitted || len(report.ResumedMerge) != 1 {
		t.Fatalf("report = %+v", report)
	}
	b2, _ := s2.Volume("b")
	versionsAfter, _ := b2.Versions()
	if len(versionsAfter) != len(versionsBefore) {
		t.Fatalf("version count changed on replay: before=%d after=%d",
			len(versionsBefore), len(versionsAfter))
	}
	head, _ := b2.Head()
	assertBytes(t, readAll(t, head), []byte("data\x00\x00\x00\x00"), "committed merge head")
	if fileExists(mergeJournalPath(root, "b")) {
		t.Fatal("merge journal left behind after replay")
	}
}

// TestMergeDifferentAlignments checks cross-alignment merge with holes and
// zero segments translated correctly into the target block size.
func TestMergeDifferentAlignments(t *testing.T) {
	s, _ := Create(t.TempDir())
	defer s.Close()
	a, _ := s.CreateVolume("a", 4)
	b, _ := s.CreateVolume("b", 8)
	tx, _ := a.Begin()
	if err := tx.WriteAt([]byte("abcd"), 0); err != nil {
		t.Fatal(err)
	}
	if err := tx.WriteZero(make([]byte, 4), 4); err != nil {
		t.Fatal(err)
	}
	if err := tx.PunchHole(8, 4); err != nil {
		t.Fatal(err)
	}
	src, err := tx.Commit()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := b.Merge(src, MergeOptions{DstOffset: 2}); err != nil {
		t.Fatal(err)
	}
	head, _ := b.Head()
	if head.Size() != 16 {
		t.Fatalf("size = %d want 16", head.Size())
	}
	buf := make([]byte, 16)
	if _, err := head.ReadAt(buf, 0); err != nil {
		t.Fatal(err)
	}
	want := make([]byte, 16)
	copy(want[2:6], "abcd")
	assertBytes(t, buf, want, "cross-alignment merge bytes")
	exts, _ := head.Extents()
	// [0,2) hole inside block 0 -> block 0 mapped (RMW with zero edges),
	// [6,8) zero; block 1 [8,12) hole region -> zero/hole mix resolved as
	// mapped-zero content; check no kind panics and offsets are bs-multiples.
	for _, ex := range exts {
		if ex.Offset%8 != 0 || ex.Length%8 != 0 {
			t.Fatalf("extent not 8-byte aligned: %+v", ex)
		}
	}
}

// TestSnapshotImmutabilityAndDeleteProtection covers version pinning.
func TestSnapshotImmutabilityAndDeleteProtection(t *testing.T) {
	s, _ := Create(t.TempDir())
	defer s.Close()
	v, _ := s.CreateVolume("v", 4)
	tx, _ := v.Begin()
	_ = tx.WriteAt([]byte("abcd"), 0)
	_ = tx.SetSnapshot("keep")
	w1, _ := tx.Commit()
	tx2, _ := v.Begin()
	_ = tx2.WriteAt([]byte("xyzw"), 0)
	if _, err := tx2.Commit(); err != nil {
		t.Fatal(err)
	}
	if err := v.DeleteVersion(w1.VersionID()); !errors.Is(err, ErrPinnedVersion) {
		t.Fatalf("delete pinned = %v want ErrPinnedVersion", err)
	}
	if err := v.DeleteSnapshot("keep"); err != nil {
		t.Fatal(err)
	}
	if err := v.DeleteVersion(w1.VersionID()); err != nil {
		t.Fatalf("delete unpinned: %v", err)
	}
}

// TestAbortDiscardsStaged ensures Abort removes staging and changes nothing.
func TestAbortDiscardsStaged(t *testing.T) {
	s, _ := Create(t.TempDir())
	defer s.Close()
	v, _ := s.CreateVolume("v", 4)
	tx, _ := v.Begin()
	_ = tx.WriteAt([]byte("abcd"), 0)
	if err := tx.Abort(); err != nil {
		t.Fatal(err)
	}
	if fileExists(txnDir(s.root, tx.ID())) {
		t.Fatal("staging directory remains after abort")
	}
	head, _ := v.Head()
	if head.Size() != 0 {
		t.Fatalf("size = %d want 0", head.Size())
	}
	if err := tx.Abort(); !errors.Is(err, ErrTxClosed) {
		t.Fatalf("double abort = %v want ErrTxClosed", err)
	}
}
