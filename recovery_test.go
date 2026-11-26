package sparseblockvol

import (
	"errors"
	"os"
	"testing"
)

// TestTxnAfterMetaCrash covers the crash after the metadata switch: the new
// version is already durable, so recovery must keep it rather than roll back.
func TestTxnAfterMetaCrash(t *testing.T) {
	root := t.TempDir()
	s, _ := Create(root)
	v, _ := s.CreateVolume("v", 4)
	s.SetCrashPoint("txn.after_meta")
	tx, _ := v.Begin()
	if err := tx.WriteAt([]byte("abcd"), 0); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Commit(); !errors.Is(err, ErrInjectedCrash) {
		t.Fatalf("commit = %v want injected crash", err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	s2, report, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	if len(report.AbortedTxn) != 0 {
		t.Fatalf("committed txn was rolled back: %+v", report)
	}
	v2, _ := s2.Volume("v")
	head, _ := v2.Head()
	assertBytes(t, readAll(t, head), []byte("abcd"), "committed data survives")
	// Staging directory must have been cleaned during the crash path.
	dirEntries, err := os.ReadDir(pendingDir(root))
	if err != nil {
		t.Fatal(err)
	}
	if len(dirEntries) != 0 {
		var names []string
		for _, e := range dirEntries {
			names = append(names, e.Name())
		}
		t.Fatalf("pending dir not empty: %v", names)
	}
}

// TestReopenIdempotent opens a recovered store a second time and expects a
// clean, no-op recovery report.
func TestReopenIdempotent(t *testing.T) {
	root := t.TempDir()
	s, _ := Create(root)
	a, _ := s.CreateVolume("a", 4)
	b, _ := s.CreateVolume("b", 8)
	tx, _ := a.Begin()
	_ = tx.WriteAt([]byte("abcd"), 0)
	src, _ := tx.Commit()
	s.SetCrashPoint("merge.after_stage")
	if _, err := b.Merge(src, MergeOptions{CrashPoint: "merge.after_stage"}); !errors.Is(err, ErrInjectedCrash) {
		t.Fatal(err)
	}
	_ = s.Close()

	s1, r1, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(r1.ResumedMerge) != 1 {
		t.Fatalf("first reopen report = %+v", r1)
	}
	if err := s1.Close(); err != nil {
		t.Fatal(err)
	}
	s2, r2, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	if len(r2.ResumedMerge) != 0 || len(r2.AbortedTxn) != 0 || len(r2.OrphanedBlocks) != 0 {
		t.Fatalf("second reopen did work: %+v", r2)
	}
}

// TestAuditAndReclaimOrphans drops an unreferenced block into the block
// directory and verifies Audit flags it and Reclaim removes exactly it.
func TestAuditAndReclaimOrphans(t *testing.T) {
	s, _ := Create(t.TempDir())
	defer s.Close()
	v, _ := s.CreateVolume("v", 4)
	tx, _ := v.Begin()
	_ = tx.WriteAt([]byte("abcd"), 0)
	if _, err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	orphanData := []byte("orph")
	orphanID := hashBlock(orphanData)
	if err := s.putBlock(orphanID, orphanData); err != nil {
		t.Fatal(err)
	}
	orphans, missing, err := s.Audit()
	if err != nil {
		t.Fatal(err)
	}
	if len(missing) != 0 || len(orphans) != 1 || orphans[0] != orphanID {
		t.Fatalf("audit = orphans:%v missing:%v", orphans, missing)
	}
	reclaimed, err := s.Reclaim()
	if err != nil {
		t.Fatal(err)
	}
	if len(reclaimed) != 1 || reclaimed[0] != orphanID {
		t.Fatalf("reclaimed = %v", reclaimed)
	}
	if s.blockExists(orphanID) {
		t.Fatal("orphan block still on disk after Reclaim")
	}
	// Referenced blocks remain untouched and stay readable.
	head, _ := v.Head()
	assertBytes(t, readAll(t, head), []byte("abcd"), "referenced data intact")
}

// TestBlockSizeValidation exercises the alignment rules.
func TestBlockSizeValidation(t *testing.T) {
	s, _ := Create(t.TempDir())
	defer s.Close()
	for _, bs := range []int{0, -1, 3, 6, 12} {
		if _, err := s.CreateVolume("bad", bs); !errors.Is(err, ErrBlockSize) {
			t.Fatalf("block size %d: err = %v want ErrBlockSize", bs, err)
		}
	}
	v, err := s.CreateVolume("ok", 4096)
	if err != nil {
		t.Fatal(err)
	}
	if v.BlockSize() != 4096 {
		t.Fatalf("block size = %d", v.BlockSize())
	}
}
