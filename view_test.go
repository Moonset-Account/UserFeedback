package incview

import (
	"fmt"
	"reflect"
	"sort"
	"sync"
	"testing"
)

// mustTxn runs fn inside a transaction and commits it.
func mustCommit(t *testing.T, v *View, fn func(tx *Txn)) CommitResult {
	t.Helper()
	tx := v.Begin()
	fn(tx)
	return tx.Commit()
}

// seed installs the device mappings and facts used by most tests:
//
//	d1 -> east: rows 10 (5), 11 (9)
//	d2 -> east: rows 12 (5), 13 (30)
//	d3 -> west: row 20 (7)
func seed(t *testing.T, v *View) {
	t.Helper()
	mustCommit(t, v, func(tx *Txn) {
		for id, room := range map[string]string{"d1": "east", "d2": "east", "d3": "west"} {
			if err := tx.SetDevice(id, room); err != nil {
				t.Fatalf("SetDevice(%s,%s): %v", id, room, err)
			}
		}
		for _, f := range []Fact{
			{Row: 10, Device: "d1", Duration: 5},
			{Row: 11, Device: "d1", Duration: 9},
			{Row: 12, Device: "d2", Duration: 5},
			{Row: 13, Device: "d2", Duration: 30},
			{Row: 20, Device: "d3", Duration: 7},
		} {
			if err := tx.PutFact(f.Row, f.Device, f.Duration); err != nil {
				t.Fatalf("PutFact(%d): %v", f.Row, err)
			}
		}
	})
}

func mustGroup(t *testing.T, v *View, room string, want Group) {
	t.Helper()
	got, ok := v.Group(room)
	if !ok {
		t.Fatalf("group %q missing, want %+v", room, want)
	}
	if got != want {
		t.Fatalf("group %q = %+v, want %+v", room, got, want)
	}
}

func TestSeedAggregates(t *testing.T) {
	v := NewView()
	seed(t, v)
	mustGroup(t, v, "east", Group{Room: "east", Count: 4, Total: 49, Min: 5})
	mustGroup(t, v, "west", Group{Room: "west", Count: 1, Total: 7, Min: 7})
	if got := v.Groups(); len(got) != 2 {
		t.Fatalf("Groups() = %+v, want 2 rooms", got)
	}
}

func TestCommitReturnsOnlyNetChangedGroups(t *testing.T) {
	v := NewView()
	seed(t, v)

	res := mustCommit(t, v, func(tx *Txn) {
		if err := tx.PutFact(12, "d2", 6); err != nil {
			t.Fatal(err)
		}
	})
	want := []Delta{{
		Room:         "east",
		Before:       Group{Room: "east", Count: 4, Total: 49, Min: 5},
		BeforeExists: true,
		After:        Group{Room: "east", Count: 4, Total: 50, Min: 5},
		AfterExists:  true,
	}}
	if !reflect.DeepEqual(res.Deltas, want) {
		t.Fatalf("deltas = %+v, want %+v", res.Deltas, want)
	}
	if wantRows := []int{12}; !reflect.DeepEqual(res.VisitedRows, wantRows) {
		t.Fatalf("visited = %v, want %v", res.VisitedRows, wantRows)
	}
}

func TestNoOpChangeProducesNoDelta(t *testing.T) {
	v := NewView()
	seed(t, v)

	// Rewriting a fact with identical values is a net no-op.
	res := mustCommit(t, v, func(tx *Txn) {
		if err := tx.PutFact(10, "d1", 5); err != nil {
			t.Fatal(err)
		}
		if err := tx.SetDevice("d1", "east"); err != nil {
			t.Fatal(err)
		}
	})
	if len(res.Deltas) != 0 {
		t.Fatalf("deltas = %+v, want none", res.Deltas)
	}
	mustGroup(t, v, "east", Group{Room: "east", Count: 4, Total: 49, Min: 5})
}

func TestDeleteLastRowRemovesGroup(t *testing.T) {
	v := NewView()
	seed(t, v)

	res := mustCommit(t, v, func(tx *Txn) {
		if err := tx.DeleteFact(20); err != nil {
			t.Fatal(err)
		}
	})
	want := []Delta{{
		Room:         "west",
		Before:       Group{Room: "west", Count: 1, Total: 7, Min: 7},
		BeforeExists: true,
		AfterExists:  false,
	}}
	if !reflect.DeepEqual(res.Deltas, want) {
		t.Fatalf("deltas = %+v, want %+v", res.Deltas, want)
	}
	if _, ok := v.Group("west"); ok {
		t.Fatal("west should have left the view")
	}
	if _, ok := v.Fact(20); ok {
		t.Fatal("deleted fact 20 should be gone from the base table")
	}
	// Its device mapping is untouched.
	if _, ok := v.Device("d3"); !ok {
		t.Fatal("device d3 mapping should survive a fact delete")
	}
}

// The scenario from the task: one device moves from east to west and its
// lowest-duration fact is deleted in the same transaction. East keeps an
// identical minimum held by another device, so east's Min must not change.
func TestMoveDeviceAndDeleteItsMinimumTogether(t *testing.T) {
	v := NewView()
	seed(t, v)

	res := mustCommit(t, v, func(tx *Txn) {
		if err := tx.SetDevice("d1", "west"); err != nil {
			t.Fatal(err)
		}
		if err := tx.DeleteFact(10); err != nil { // d1's 5, tied with d2's 5 in east
			t.Fatal(err)
		}
	})

	wantDeltas := []Delta{
		{
			Room:         "east",
			Before:       Group{Room: "east", Count: 4, Total: 49, Min: 5},
			BeforeExists: true,
			After:        Group{Room: "east", Count: 2, Total: 35, Min: 5},
			AfterExists:  true,
		},
		{
			Room:         "west",
			Before:       Group{Room: "west", Count: 1, Total: 7, Min: 7},
			BeforeExists: true,
			After:        Group{Room: "west", Count: 2, Total: 16, Min: 7},
			AfterExists:  true,
		},
	}
	if !reflect.DeepEqual(res.Deltas, wantDeltas) {
		t.Fatalf("deltas = %+v, want %+v", res.Deltas, wantDeltas)
	}
	mustGroup(t, v, "east", Group{Room: "east", Count: 2, Total: 35, Min: 5})
	mustGroup(t, v, "west", Group{Room: "west", Count: 2, Total: 16, Min: 7})
	if rows := v.FactRowsForDevice("d1"); !reflect.DeepEqual(rows, []int{11}) {
		t.Fatalf("d1 rows = %v, want [11]", rows)
	}
	// Only d1's own rows are walked; d2 and d3 are untouched.
	if want := []int{10, 11}; !reflect.DeepEqual(res.VisitedRows, want) {
		t.Fatalf("visited = %v, want %v", res.VisitedRows, want)
	}
}

// A moved device whose minimum leaves with it must lose the old minimum in
// the target room if the target had only that value.
func TestMoveDeviceLowestIntoEmptyMinRoom(t *testing.T) {
	v := NewView()
	mustCommit(t, v, func(tx *Txn) {
		_ = tx.SetDevice("a", "north")
		_ = tx.SetDevice("b", "south")
		_ = tx.PutFact(1, "a", 4)
		_ = tx.PutFact(2, "a", 8)
		_ = tx.PutFact(3, "b", 20)
	})
	res := mustCommit(t, v, func(tx *Txn) {
		if err := tx.SetDevice("a", "south"); err != nil {
			t.Fatal(err)
		}
	})
	if len(res.Deltas) != 2 {
		t.Fatalf("deltas = %+v, want 2", res.Deltas)
	}
	// north had exactly one row and left the view entirely.
	if res.Deltas[0].Room != "north" || res.Deltas[0].AfterExists {
		t.Fatalf("north delta = %+v, want exit", res.Deltas[0])
	}
	mustGroup(t, v, "south", Group{Room: "south", Count: 3, Total: 32, Min: 4})
	if _, ok := v.Group("north"); ok {
		t.Fatal("north should have exited")
	}
	// Rebuilding the same aggregate from a different state still counts as a
	// change relative to the previous commit, and it is reported once.
	if res.Deltas[1].Room != "south" {
		t.Fatalf("second delta = %+v, want south", res.Deltas[1])
	}
}

func TestMissingMappingKeepsFactOutOfView(t *testing.T) {
	v := NewView()
	mustCommit(t, v, func(tx *Txn) {
		_ = tx.SetDevice("ghosted", "east")
		_ = tx.PutFact(1, "ghosted", 3)
		_ = tx.SetDevice("known", "east")
		_ = tx.PutFact(2, "known", 10)
	})

	// Deleting the mapping removes the row's contribution but the fact stays.
	res := mustCommit(t, v, func(tx *Txn) {
		if err := tx.DeleteDevice("ghosted"); err != nil {
			t.Fatal(err)
		}
	})
	want := []Delta{{
		Room:         "east",
		Before:       Group{Room: "east", Count: 2, Total: 13, Min: 3},
		BeforeExists: true,
		After:        Group{Room: "east", Count: 1, Total: 10, Min: 10},
		AfterExists:  true,
	}}
	if !reflect.DeepEqual(res.Deltas, want) {
		t.Fatalf("deltas = %+v, want %+v", res.Deltas, want)
	}
	if _, ok := v.Fact(1); !ok {
		t.Fatal("unmapped fact must stay in the base table")
	}
	if _, ok := v.Device("ghosted"); ok {
		t.Fatal("mapping should be gone")
	}

	// An unmapped fact becomes visible again when the mapping returns.
	res = mustCommit(t, v, func(tx *Txn) {
		if err := tx.SetDevice("ghosted", "east"); err != nil {
			t.Fatal(err)
		}
	})
	if len(res.Deltas) != 1 || res.Deltas[0].After.Count != 2 || res.Deltas[0].After.Min != 3 {
		t.Fatalf("deltas = %+v, want east back with 2 rows and min 3", res.Deltas)
	}

	// A brand-new fact for a device that has never had a mapping is stored
	// but contributes nothing and reports no delta.
	res = mustCommit(t, v, func(tx *Txn) {
		if err := tx.PutFact(99, "unmapped", 1); err != nil {
			t.Fatal(err)
		}
	})
	if len(res.Deltas) != 0 {
		t.Fatalf("deltas = %+v, want none", res.Deltas)
	}
	if _, ok := v.Fact(99); !ok {
		t.Fatal("fact 99 should be in the base table")
	}
	if got := res.VisitedRows; !reflect.DeepEqual(got, []int{99}) {
		t.Fatalf("visited = %v, want [99]", got)
	}
}

func TestSingleDeviceChangeDoesNotVisitUnrelatedFacts(t *testing.T) {
	v := NewView()
	mustCommit(t, v, func(tx *Txn) {
		_ = tx.SetDevice("hot", "east")
		_ = tx.SetDevice("cold", "east")
		for row := 1; row <= 5; row++ {
			_ = tx.PutFact(row, "hot", row)
		}
		for row := 100; row <= 150; row++ {
			_ = tx.PutFact(row, "cold", row)
		}
	})

	res := mustCommit(t, v, func(tx *Txn) {
		if err := tx.SetDevice("hot", "west"); err != nil {
			t.Fatal(err)
		}
	})
	if len(res.VisitedRows) != 5 {
		t.Fatalf("visited %v, want the 5 hot rows only", res.VisitedRows)
	}
	for _, row := range res.VisitedRows {
		if row < 1 || row > 5 {
			t.Fatalf("visited unrelated row %d", row)
		}
	}
	// No unrelated row is touched, but the east aggregate still changes.
	mustGroup(t, v, "east", Group{Room: "east", Count: 51, Total: sumRange(100, 150), Min: 100})
	mustGroup(t, v, "west", Group{Room: "west", Count: 5, Total: sumRange(1, 5), Min: 1})
}

func sumRange(lo, hi int) int64 {
	var s int64
	for i := lo; i <= hi; i++ {
		s += int64(i)
	}
	return s
}

func TestInterleavedMovesAndDeletesSettleOnce(t *testing.T) {
	v := NewView()
	mustCommit(t, v, func(tx *Txn) {
		_ = tx.SetDevice("x", "east")
		_ = tx.SetDevice("y", "west")
		_ = tx.PutFact(1, "x", 2)
		_ = tx.PutFact(2, "x", 6)
		_ = tx.PutFact(3, "y", 2)
		_ = tx.PutFact(4, "y", 11)
	})

	// Two devices swap rooms, one fact is deleted, one inserted, one duration
	// changed. Exactly two deltas, each reflecting the net effect.
	res := mustCommit(t, v, func(tx *Txn) {
		if err := tx.SetDevice("x", "west"); err != nil {
			t.Fatal(err)
		}
		if err := tx.SetDevice("y", "east"); err != nil {
			t.Fatal(err)
		}
		if err := tx.DeleteFact(2); err != nil {
			t.Fatal(err)
		}
		if err := tx.PutFact(5, "y", 4); err != nil {
			t.Fatal(err)
		}
		if err := tx.PutFact(3, "y", 8); err != nil {
			t.Fatal(err)
		}
	})
	want := []Delta{
		{
			Room:         "east",
			Before:       Group{Room: "east", Count: 2, Total: 8, Min: 2},
			BeforeExists: true,
			After:        Group{Room: "east", Count: 3, Total: 23, Min: 4},
			AfterExists:  true,
		},
		{
			Room:         "west",
			Before:       Group{Room: "west", Count: 2, Total: 13, Min: 2},
			BeforeExists: true,
			After:        Group{Room: "west", Count: 1, Total: 2, Min: 2},
			AfterExists:  true,
		},
	}
	if !reflect.DeepEqual(res.Deltas, want) {
		t.Fatalf("deltas = %+v, want %+v", res.Deltas, want)
	}
}

func TestCancelledInsertLeavesNoTrace(t *testing.T) {
	v := NewView()
	res := mustCommit(t, v, func(tx *Txn) {
		if err := tx.PutFact(1, "d", 3); err != nil {
			t.Fatal(err)
		}
		if err := tx.SetDevice("d", "east"); err != nil {
			t.Fatal(err)
		}
		if err := tx.DeleteFact(1); err != nil {
			t.Fatal(err)
		}
		if err := tx.DeleteDevice("d"); err != nil {
			t.Fatal(err)
		}
	})
	if len(res.Deltas) != 0 {
		t.Fatalf("deltas = %+v, want none", res.Deltas)
	}
	if len(v.Facts()) != 0 {
		t.Fatalf("facts = %+v, want empty", v.Facts())
	}
	if len(v.Groups()) != 0 {
		t.Fatalf("groups = %+v, want empty", v.Groups())
	}
}

func TestValidationAndRollback(t *testing.T) {
	v := NewView()
	tx := v.Begin()
	if err := tx.PutFact(0, "d", 1); err != ErrInvalidRow {
		t.Fatalf("PutFact row 0 = %v, want ErrInvalidRow", err)
	}
	if err := tx.PutFact(1, "", 1); err != ErrEmptyDevice {
		t.Fatalf("PutFact empty device = %v, want ErrEmptyDevice", err)
	}
	if err := tx.DeleteFact(7); err != ErrNoSuchFact {
		t.Fatalf("DeleteFact missing = %v, want ErrNoSuchFact", err)
	}
	if err := tx.DeleteDevice("nope"); err != ErrNoSuchDevice {
		t.Fatalf("DeleteDevice missing = %v, want ErrNoSuchDevice", err)
	}
	if err := tx.SetDevice("d", ""); err != ErrEmptyRoom {
		t.Fatalf("SetDevice empty room = %v, want ErrEmptyRoom", err)
	}
	_ = tx.PutFact(1, "d", 1)
	tx.Rollback()

	if len(v.Facts()) != 0 {
		t.Fatal("rollback must not publish facts")
	}
	if err := tx.PutFact(2, "d", 2); err != ErrTxnClosed {
		t.Fatalf("PutFact after rollback = %v, want ErrTxnClosed", err)
	}
	if res := tx.Commit(); len(res.Deltas) != 0 || len(res.VisitedRows) != 0 {
		t.Fatalf("commit after rollback = %+v, want empty", res)
	}

	// The write lock must have been released.
	tx2 := v.Begin()
	if err := tx2.PutFact(1, "d", 4); err != nil {
		t.Fatal(err)
	}
	tx2.Commit()
	if _, ok := v.Fact(1); !ok {
		t.Fatal("second transaction should have committed")
	}
}

func TestNegativeDurationsAndMinRestore(t *testing.T) {
	v := NewView()
	mustCommit(t, v, func(tx *Txn) {
		_ = tx.SetDevice("a", "east")
		_ = tx.PutFact(1, "a", -5)
		_ = tx.PutFact(2, "a", -5)
		_ = tx.PutFact(3, "a", 3)
	})
	mustGroup(t, v, "east", Group{Room: "east", Count: 3, Total: -7, Min: -5})

	// One occurrence of the minimum leaves; the duplicate holds the minimum.
	res := mustCommit(t, v, func(tx *Txn) {
		if err := tx.DeleteFact(1); err != nil {
			t.Fatal(err)
		}
	})
	mustGroup(t, v, "east", Group{Room: "east", Count: 2, Total: -2, Min: -5})
	if len(res.Deltas) != 1 || res.Deltas[0].After.Min != -5 {
		t.Fatalf("deltas = %+v, want min kept at -5", res.Deltas)
	}

	// The last occurrence leaves; the minimum must be recomputed.
	mustCommit(t, v, func(tx *Txn) {
		if err := tx.DeleteFact(2); err != nil {
			t.Fatal(err)
		}
	})
	mustGroup(t, v, "east", Group{Room: "east", Count: 1, Total: 3, Min: 3})
}

func TestReadersSeeCommittedStateOnly(t *testing.T) {
	v := NewView()
	seed(t, v)

	tx := v.Begin()
	if err := tx.PutFact(10, "d1", 1000); err != nil {
		t.Fatal(err)
	}
	// Uncommitted work is invisible to readers.
	mustGroup(t, v, "east", Group{Room: "east", Count: 4, Total: 49, Min: 5})
	tx.Commit()
	mustGroup(t, v, "east", Group{Room: "east", Count: 4, Total: 1044, Min: 5})
}

func TestConcurrentReadersDuringCommits(t *testing.T) {
	v := NewView()
	seed(t, v)

	var wg sync.WaitGroup
	stop := make(chan struct{})
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				got := v.Groups()
				rooms := make([]string, len(got))
				for j, g := range got {
					rooms[j] = g.Room
					if g.Count <= 0 {
						panic(fmt.Sprintf("non-positive count %+v", g))
					}
				}
				if !sort.StringsAreSorted(rooms) && len(rooms) > 1 {
					panic("Groups() not sorted")
				}
			}
		}()
	}
	for i := 0; i < 200; i++ {
		row := 1000 + i
		mustCommit(t, v, func(tx *Txn) {
			if err := tx.PutFact(row, "d3", i); err != nil {
				t.Fatal(err)
			}
			if err := tx.DeleteFact(row); err != nil {
				t.Fatal(err)
			}
			if err := tx.PutFact(10, "d1", i%13); err != nil {
				t.Fatal(err)
			}
		})
	}
	close(stop)
	wg.Wait()
}
