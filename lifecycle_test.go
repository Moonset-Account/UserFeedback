package sparseblockvol

import (
	"testing"
)

// TestQueryAndLifecycleAPI covers the remaining query and lifecycle surface:
// volume listing, tag-based snapshots, mapped-range filtering and volume
// deletion with deferred physical reclamation.
func TestQueryAndLifecycleAPI(t *testing.T) {
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
	base, err := tx.Commit()
	if err != nil {
		t.Fatal(err)
	}
	if err := v.TagSnapshot("tagged", base.VersionID()); err != nil {
		t.Fatal(err)
	}
	names, err := v.SnapshotNames()
	if err != nil || len(names) != 1 || names[0] != "tagged" {
		t.Fatalf("snapshot names = %v, %v", names, err)
	}
	tagged, err := v.Snapshot("tagged")
	if err != nil {
		t.Fatal(err)
	}
	mapped, err := tagged.MappedRanges()
	if err != nil {
		t.Fatal(err)
	}
	if len(mapped) != 1 || mapped[0] != (Extent{Kind: KindMapped, Offset: 0, Length: 4}) {
		t.Fatalf("mapped ranges = %v", mapped)
	}

	ids, err := s.VolumeIDs()
	if err != nil || len(ids) != 1 || ids[0] != "v" {
		t.Fatalf("volume ids = %v, %v", ids, err)
	}
	// Deleting the volume drops every reference; its blocks become orphans
	// rather than being removed immediately.
	if err := s.DeleteVolume("v"); err != nil {
		t.Fatal(err)
	}
	orphans, _, err := s.Audit()
	if err != nil {
		t.Fatal(err)
	}
	if len(orphans) == 0 {
		t.Fatal("expected orphaned blocks after volume deletion")
	}
	reclaimed, err := s.Reclaim()
	if err != nil {
		t.Fatal(err)
	}
	if len(reclaimed) != len(orphans) {
		t.Fatalf("reclaimed %d, orphans %d", len(reclaimed), len(orphans))
	}
	if _, err := s.Volume("v"); err == nil {
		t.Fatal("deleted volume still resolvable")
	}
}
