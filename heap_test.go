package heap_test

import (
	"bytes"
	"errors"
	"fmt"
	"sync"
	"testing"

	"heap"
)

// Page geometry used by most tests: 64-byte pages, 8-byte directory tiers.
// Empty page free space = 64 - 8 (header) = 56 bytes.
// An 8-byte record with a fresh slot costs 8 + 4 (line pointer) = 12 bytes,
// so a page holds exactly four such records (48 bytes), leaving an 8-byte gap.
const (
	pageSize = 64
	tierSize = 8
)

func rec(size int, fill byte) []byte {
	b := make([]byte, size)
	for i := range b {
		b[i] = fill
	}
	return b
}

func mustInsert(t *testing.T, tb *heap.Table, rec []byte) heap.Location {
	t.Helper()
	loc, err := tb.Insert(rec)
	if err != nil {
		t.Fatalf("Insert(%d bytes): %v", len(rec), err)
	}
	return loc
}

func mustGet(t *testing.T, tb *heap.Table, loc heap.Location) []byte {
	t.Helper()
	got, err := tb.Get(loc)
	if err != nil {
		t.Fatalf("Get(%v): %v", loc, err)
	}
	return got
}

func mustPageInfo(t *testing.T, tb *heap.Table, id heap.PageID) heap.PageInfo {
	t.Helper()
	pi, err := tb.PageInfo(id)
	if err != nil {
		t.Fatalf("PageInfo(%d): %v", id, err)
	}
	return pi
}

// Normal path: records come back byte-identical, slots are assigned in
// order, and the directory tier tracks the remaining free space.
func TestInsertAndGetRoundTrip(t *testing.T) {
	tb := heap.NewTable(pageSize, tierSize)

	var locs []heap.Location
	for i := 0; i < 4; i++ {
		loc := mustInsert(t, tb, rec(8, byte(0x10+i)))
		if loc.Page != 0 || loc.Slot != i {
			t.Fatalf("insert %d: got location %v, want page 0 slot %d", i, loc, i)
		}
		locs = append(locs, loc)
	}
	for i, loc := range locs {
		if got := mustGet(t, tb, loc); !bytes.Equal(got, rec(8, byte(0x10+i))) {
			t.Fatalf("Get(%v) = %x, want %x", loc, got, rec(8, byte(0x10+i)))
		}
	}
	// 56 - 4*12 = 8 bytes left -> tier 1.
	if tier, ok := tb.Directory().TierOf(0); !ok || tier != 1 {
		t.Fatalf("directory tier of page 0 = %d, %v; want 1, true", tier, ok)
	}
}

// A deleted record's slot is reused by a later insert; the dead tuple's
// bytes stay as a fragment (no compaction needed) and every other record
// keeps its slot number.
func TestSlotReuseAfterDelete(t *testing.T) {
	tb := heap.NewTable(pageSize, tierSize)
	loc0 := mustInsert(t, tb, rec(8, 0xA0))
	loc1 := mustInsert(t, tb, rec(8, 0xA1))
	loc2 := mustInsert(t, tb, rec(8, 0xA2))

	if err := tb.Delete(loc1); err != nil {
		t.Fatalf("Delete(%v): %v", loc1, err)
	}

	// Gap (20 bytes) fits the 8-byte record directly; the dead slot's line
	// pointer is reused, so no new line pointer is needed.
	locNew := mustInsert(t, tb, rec(8, 0xB1))
	if locNew != loc1 {
		t.Fatalf("reused location = %v, want %v (deleted slot)", locNew, loc1)
	}
	if got := mustGet(t, tb, locNew); !bytes.Equal(got, rec(8, 0xB1)) {
		t.Fatalf("Get(%v) = %x, want %x", locNew, got, rec(8, 0xB1))
	}
	// Old records are untouched and still reachable at their old slots.
	if got := mustGet(t, tb, loc0); !bytes.Equal(got, rec(8, 0xA0)) {
		t.Fatalf("Get(%v) = %x, want %x", loc0, got, rec(8, 0xA0))
	}
	if got := mustGet(t, tb, loc2); !bytes.Equal(got, rec(8, 0xA2)) {
		t.Fatalf("Get(%v) = %x, want %x", loc2, got, rec(8, 0xA2))
	}
	// The insert placed directly: the fragment from the delete is still there.
	if pi := mustPageInfo(t, tb, loc0.Page); pi.FragFree != 8 {
		t.Fatalf("FragFree = %d, want 8 (direct place must not compact)", pi.FragFree)
	}
}

// When the gap alone is too small but gap+fragments suffice, the page is
// compacted exactly once; live records keep their slot numbers and contents.
func TestCompactionPreservesSlotNumbers(t *testing.T) {
	tb := heap.NewTable(pageSize, tierSize)
	locs := make([]heap.Location, 4)
	for i := range locs {
		locs[i] = mustInsert(t, tb, rec(8, byte(0xC0+i)))
	}
	// Delete the two middle records: gap 8 + fragments 16 = 24 free.
	for _, i := range []int{1, 2} {
		if err := tb.Delete(locs[i]); err != nil {
			t.Fatalf("Delete(%v): %v", locs[i], err)
		}
	}

	// A 12-byte record does not fit the 8-byte gap but fits after compaction.
	locBig := mustInsert(t, tb, rec(12, 0xD0))
	if locBig != locs[1] {
		t.Fatalf("compacted insert location = %v, want %v (first reusable slot)", locBig, locs[1])
	}

	// Slot numbers of the surviving old records are still usable.
	for _, i := range []int{0, 3} {
		if got := mustGet(t, tb, locs[i]); !bytes.Equal(got, rec(8, byte(0xC0+i))) {
			t.Fatalf("Get(%v) after compaction = %x, want %x", locs[i], got, rec(8, byte(0xC0+i)))
		}
	}
	if got := mustGet(t, tb, locBig); !bytes.Equal(got, rec(12, 0xD0)) {
		t.Fatalf("Get(%v) = %x, want %x", locBig, got, rec(12, 0xD0))
	}

	pi := mustPageInfo(t, tb, 0)
	if pi.FragFree != 0 || pi.Dead != 0 {
		t.Fatalf("after compaction FragFree=%d Dead=%d, want 0/0", pi.FragFree, pi.Dead)
	}
	if pi.Live != 3 {
		t.Fatalf("Live = %d, want 3", pi.Live)
	}

	// The second freed slot is still reusable afterwards.
	locSmall := mustInsert(t, tb, rec(4, 0xE0))
	if locSmall != locs[2] {
		t.Fatalf("follow-up insert location = %v, want %v", locSmall, locs[2])
	}
}

// A record that cannot fit anywhere existing goes to a freshly allocated page.
func TestInsertSwitchesToFreshPageWhenFull(t *testing.T) {
	tb := heap.NewTable(pageSize, tierSize)
	for i := 0; i < 4; i++ {
		mustInsert(t, tb, rec(8, byte(0x01+i)))
	}
	// Page 0 has 8 bytes free (tier 1); a 12-byte record needs 16 -> new page.
	loc := mustInsert(t, tb, rec(12, 0xF0))
	if loc.Page != 1 || loc.Slot != 0 {
		t.Fatalf("location = %v, want page 1 slot 0", loc)
	}
	if tb.PageCount() != 2 {
		t.Fatalf("PageCount = %d, want 2", tb.PageCount())
	}
	if got := mustGet(t, tb, loc); !bytes.Equal(got, rec(12, 0xF0)) {
		t.Fatalf("Get(%v) = %x, want %x", loc, got, rec(12, 0xF0))
	}
}

// Pausable insert, interleaved fill: a session picks a page, another
// transaction fills it completely, and the paused session must switch pages
// instead of overwriting. The directory must end up pointing new inserts at
// the page that actually has room.
func TestPausableInsertInterleavedFill(t *testing.T) {
	tb := heap.NewTable(pageSize, tierSize)

	s1, err := tb.BeginInsert(rec(8, 0xAA))
	if err != nil {
		t.Fatalf("BeginInsert: %v", err)
	}
	picked := s1.PickPage()
	if picked != 0 {
		t.Fatalf("PickPage = %d, want 0", picked)
	}

	// Interleave: another transaction fills page 0 completely.
	fills := make([]heap.Location, 4)
	for i := range fills {
		fills[i] = mustInsert(t, tb, rec(8, byte(0x50+i)))
	}

	// The paused session must not overwrite: page 0 has 8 bytes left but the
	// record needs 12 (no reusable slot).
	if _, err := s1.Place(); !errors.Is(err, heap.ErrPageFull) {
		t.Fatalf("Place on filled page: err = %v, want ErrPageFull", err)
	}
	// The failed attempt corrected the hint, so the next pick moves on.
	if p := s1.PickPage(); p != 1 {
		t.Fatalf("PickPage after failure = %d, want 1", p)
	}
	loc, err := s1.Place()
	if err != nil {
		t.Fatalf("Place on fresh page: %v", err)
	}
	if loc.Page != 1 || loc.Slot != 0 {
		t.Fatalf("final location = %v, want page 1 slot 0", loc)
	}

	// Records: the interleaved ones are intact, and so is the paused one.
	for i, fl := range fills {
		if got := mustGet(t, tb, fl); !bytes.Equal(got, rec(8, byte(0x50+i))) {
			t.Fatalf("Get(%v) = %x, want %x", fl, got, rec(8, byte(0x50+i)))
		}
	}
	if got := mustGet(t, tb, loc); !bytes.Equal(got, rec(8, 0xAA)) {
		t.Fatalf("Get(%v) = %x, want %x", loc, got, rec(8, 0xAA))
	}

	// Directory's final capacity tiers: page 0 has 8 bytes -> tier 1;
	// page 1 has 56-12=44 bytes -> tier 5.
	if tier, _ := tb.Directory().TierOf(0); tier != 1 {
		t.Fatalf("final tier of page 0 = %d, want 1", tier)
	}
	if tier, _ := tb.Directory().TierOf(1); tier != 5 {
		t.Fatalf("final tier of page 1 = %d, want 5", tier)
	}
	// A lookup for another 8-byte record (12 with a slot) must select page 1.
	id, ok := tb.Directory().Search(12)
	if !ok || id != 1 {
		t.Fatalf("Search(12) = %d, %v; want 1, true", id, ok)
	}
}

// Pausable insert, interleaved partial fill: the interleaved writer eats the
// contiguous gap, so the paused session must compact once and still land on
// the page it picked — without disturbing the interleaved record.
func TestPausableInsertInterleavedCompaction(t *testing.T) {
	tb := heap.NewTable(pageSize, tierSize)
	locs := make([]heap.Location, 4)
	for i := range locs {
		locs[i] = mustInsert(t, tb, rec(8, byte(0x60+i)))
	}
	// Free the two middle slots: gap 8, fragments 16, total 24 (tier 3).
	for _, i := range []int{1, 2} {
		if err := tb.Delete(locs[i]); err != nil {
			t.Fatalf("Delete(%v): %v", locs[i], err)
		}
	}

	s1, err := tb.BeginInsert(rec(12, 0xCC))
	if err != nil {
		t.Fatalf("BeginInsert: %v", err)
	}
	if p := s1.PickPage(); p != 0 {
		t.Fatalf("PickPage = %d, want 0 (tier 3 covers 16 bytes)", p)
	}

	// Interleave: a 4-byte insert reuses slot 1 and shrinks the gap to 4.
	locB := mustInsert(t, tb, rec(4, 0xBB))
	if locB != locs[1] {
		t.Fatalf("interleaved location = %v, want %v", locB, locs[1])
	}

	// Now gap 4 < 12, but gap+fragments = 20 >= 12: compact once, then place.
	locA, err := s1.Place()
	if err != nil {
		t.Fatalf("Place: %v", err)
	}
	if locA != locs[2] {
		t.Fatalf("paused insert location = %v, want %v (second freed slot)", locA, locs[2])
	}

	// Every record is intact at its slot: two originals, the interleaved one,
	// and the paused one.
	want := map[heap.Location][]byte{
		locs[0]: rec(8, 0x60),
		locB:    rec(4, 0xBB),
		locA:    rec(12, 0xCC),
		locs[3]: rec(8, 0x63),
	}
	for loc, w := range want {
		if got := mustGet(t, tb, loc); !bytes.Equal(got, w) {
			t.Fatalf("Get(%v) = %x, want %x", loc, got, w)
		}
	}
	pi := mustPageInfo(t, tb, 0)
	if pi.FragFree != 0 || pi.Live != 4 {
		t.Fatalf("after compaction FragFree=%d Live=%d, want 0/4", pi.FragFree, pi.Live)
	}
}

// Two writers pick the same page from the same hint. The later one must not
// overwrite the earlier one, and must not spin on the stale hint: after one
// failed Place it moves to a different page.
func TestConcurrentWritersSamePageHint(t *testing.T) {
	tb := heap.NewTable(pageSize, tierSize)
	for i := 0; i < 3; i++ {
		mustInsert(t, tb, rec(8, byte(0x30+i)))
	}
	// Page 0: gap 20 (tier 2), room for exactly one more 8-byte record.

	s1, err := tb.BeginInsert(rec(8, 0x01))
	if err != nil {
		t.Fatalf("BeginInsert s1: %v", err)
	}
	s2, err := tb.BeginInsert(rec(8, 0x02))
	if err != nil {
		t.Fatalf("BeginInsert s2: %v", err)
	}
	if p := s1.PickPage(); p != 0 {
		t.Fatalf("s1 PickPage = %d, want 0", p)
	}
	if p := s2.PickPage(); p != 0 {
		t.Fatalf("s2 PickPage = %d, want 0 (same hint as s1)", p)
	}

	loc1, err := s1.Place()
	if err != nil {
		t.Fatalf("s1 Place: %v", err)
	}
	if loc1.Page != 0 || loc1.Slot != 3 {
		t.Fatalf("s1 location = %v, want page 0 slot 3", loc1)
	}

	// Later writer: the page really is full now; it must fail cleanly instead
	// of overwriting s1's record.
	if _, err := s2.Place(); !errors.Is(err, heap.ErrPageFull) {
		t.Fatalf("s2 Place: err = %v, want ErrPageFull", err)
	}
	// And it must not retry page 0: the corrected hint plus the session's
	// tried-set send it to a new page.
	if p := s2.PickPage(); p == 0 {
		t.Fatalf("s2 PickPage returned page 0 again; stale hint was not corrected")
	}
	loc2, err := s2.Place()
	if err != nil {
		t.Fatalf("s2 Place after switch: %v", err)
	}
	if loc2 == loc1 {
		t.Fatalf("s2 overwrote s1: both at %v", loc2)
	}

	if got := mustGet(t, tb, loc1); !bytes.Equal(got, rec(8, 0x01)) {
		t.Fatalf("Get(%v) = %x, want %x (s1 record must survive)", loc1, got, rec(8, 0x01))
	}
	if got := mustGet(t, tb, loc2); !bytes.Equal(got, rec(8, 0x02)) {
		t.Fatalf("Get(%v) = %x, want %x", loc2, got, rec(8, 0x02))
	}
}

// Many goroutines insert concurrently; every record must land exactly once,
// at a unique location, and read back intact.
func TestConcurrentInsertsNoLostRecords(t *testing.T) {
	tb := heap.NewTable(256, 16)

	const writers = 8
	const perWriter = 25

	var mu sync.Mutex
	locs := make(map[heap.Location][]byte, writers*perWriter)

	var wg sync.WaitGroup
	for w := 0; w < writers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := 0; i < perWriter; i++ {
				payload := rec(12, byte(0x80+w))
				payload[0] = byte(w)
				payload[1] = byte(i)
				loc, err := tb.Insert(payload)
				if err != nil {
					t.Errorf("writer %d insert %d: %v", w, i, err)
					return
				}
				mu.Lock()
				if _, dup := locs[loc]; dup {
					t.Errorf("writer %d insert %d: duplicate location %v", w, i, loc)
				}
				locs[loc] = payload
				mu.Unlock()
			}
		}(w)
	}
	wg.Wait()

	if len(locs) != writers*perWriter {
		t.Fatalf("got %d unique locations, want %d", len(locs), writers*perWriter)
	}
	for loc, want := range locs {
		got, err := tb.Get(loc)
		if err != nil {
			t.Fatalf("Get(%v): %v", loc, err)
		}
		if !bytes.Equal(got, want) {
			t.Fatalf("Get(%v) = %x, want %x", loc, got, want)
		}
	}
}

// Boundary: a record that exactly fills an empty page fits; one byte more is
// rejected; the next insert after an exact fit goes to a new page.
func TestExactFitBoundary(t *testing.T) {
	tb := heap.NewTable(pageSize, tierSize)

	maxRec := pageSize - 8 - 4 // header + one line pointer = 52
	loc := mustInsert(t, tb, rec(maxRec, 0x77))
	if loc.Page != 0 || loc.Slot != 0 {
		t.Fatalf("location = %v, want page 0 slot 0", loc)
	}
	if pi := mustPageInfo(t, tb, 0); pi.ContigFree != 0 {
		t.Fatalf("ContigFree = %d, want 0 after exact fit", pi.ContigFree)
	}
	if got := mustGet(t, tb, loc); !bytes.Equal(got, rec(maxRec, 0x77)) {
		t.Fatalf("Get(%v) mismatch", loc)
	}

	if _, err := tb.Insert(rec(maxRec+1, 0x78)); !errors.Is(err, heap.ErrRecordTooLarge) {
		t.Fatalf("Insert(%d bytes): err = %v, want ErrRecordTooLarge", maxRec+1, err)
	}
	if _, err := tb.BeginInsert(rec(maxRec+1, 0x78)); !errors.Is(err, heap.ErrRecordTooLarge) {
		t.Fatalf("BeginInsert(%d bytes): err = %v, want ErrRecordTooLarge", maxRec+1, err)
	}

	// Page 0 is exactly full, so even a tiny record needs a new page.
	loc2 := mustInsert(t, tb, rec(1, 0x79))
	if loc2.Page != 1 {
		t.Fatalf("location = %v, want page 1", loc2)
	}
}

// Error paths for Get/Delete and for misusing a session.
func TestErrorPaths(t *testing.T) {
	tb := heap.NewTable(pageSize, tierSize)
	loc := mustInsert(t, tb, rec(8, 0x44))

	if _, err := tb.Get(heap.Location{Page: 99, Slot: 0}); !errors.Is(err, heap.ErrNotFound) {
		t.Fatalf("Get unknown page: %v, want ErrNotFound", err)
	}
	if _, err := tb.Get(heap.Location{Page: loc.Page, Slot: 7}); !errors.Is(err, heap.ErrNotLive) {
		t.Fatalf("Get out-of-range slot: %v, want ErrNotLive", err)
	}
	if err := tb.Delete(loc); err != nil {
		t.Fatalf("Delete(%v): %v", loc, err)
	}
	if _, err := tb.Get(loc); !errors.Is(err, heap.ErrNotLive) {
		t.Fatalf("Get deleted slot: %v, want ErrNotLive", err)
	}
	if err := tb.Delete(loc); !errors.Is(err, heap.ErrNotLive) {
		t.Fatalf("double Delete: %v, want ErrNotLive", err)
	}
	if err := tb.Delete(heap.Location{Page: 99, Slot: 0}); !errors.Is(err, heap.ErrNotFound) {
		t.Fatalf("Delete unknown page: %v, want ErrNotFound", err)
	}

	// Session misuse: Place before PickPage, and reuse after Finish.
	s, err := tb.BeginInsert(rec(4, 0x55))
	if err != nil {
		t.Fatalf("BeginInsert: %v", err)
	}
	if _, err := s.Place(); !errors.Is(err, heap.ErrNoPagePicked) {
		t.Fatalf("Place before PickPage: %v, want ErrNoPagePicked", err)
	}
	if _, err := s.Finish(); err != nil {
		t.Fatalf("Finish: %v", err)
	}
	if _, err := s.Place(); !errors.Is(err, heap.ErrSessionDone) {
		t.Fatalf("Place after Finish: %v, want ErrSessionDone", err)
	}
}

// Directory hints are approximate but conservative: the recorded tier never
// promises more than the page's real free space at update time.
func TestDirectoryTierIsConservative(t *testing.T) {
	tb := heap.NewTable(pageSize, tierSize)
	for i := 0; i < 3; i++ {
		mustInsert(t, tb, rec(8, byte(0x90+i)))
	}
	// Real free space is 56-3*12 = 20 bytes; tier must be floor(20/8) = 2,
	// guaranteeing 16 bytes, not 20.
	tier, ok := tb.Directory().TierOf(0)
	if !ok {
		t.Fatal("page 0 missing from directory")
	}
	if tier != 2 {
		t.Fatalf("tier = %d, want 2", tier)
	}
	if got := tb.Directory().TierBytes(tier); got != 16 {
		t.Fatalf("TierBytes(%d) = %d, want 16", tier, got)
	}
	pi := mustPageInfo(t, tb, 0)
	if tb.Directory().TierBytes(tier) > pi.ContigFree+pi.FragFree {
		t.Fatalf("tier promises %d bytes but page only has %d", tb.Directory().TierBytes(tier), pi.ContigFree+pi.FragFree)
	}
}

// Finish from a paused state: equivalent to Insert, but resumable after an
// interleaved transaction.
func TestFinishResumesFromPausedState(t *testing.T) {
	tb := heap.NewTable(pageSize, tierSize)
	s, err := tb.BeginInsert(rec(8, 0x0A))
	if err != nil {
		t.Fatalf("BeginInsert: %v", err)
	}
	s.PickPage()
	// Interleave a full page worth of inserts.
	for i := 0; i < 4; i++ {
		mustInsert(t, tb, rec(8, byte(0x20+i)))
	}
	loc, err := s.Finish()
	if err != nil {
		t.Fatalf("Finish: %v", err)
	}
	if got := mustGet(t, tb, loc); !bytes.Equal(got, rec(8, 0x0A)) {
		t.Fatalf("Get(%v) = %x, want %x", loc, got, rec(8, 0x0A))
	}
	if loc.Page != 1 {
		t.Fatalf("Finish location = %v, want page 1", loc)
	}
}

// Sanity: many mixed inserts and deletes keep every live record readable.
func TestMixedInsertDeleteWorkload(t *testing.T) {
	tb := heap.NewTable(128, 8)
	live := make(map[heap.Location][]byte)
	for round := 0; round < 200; round++ {
		payload := []byte(fmt.Sprintf("rec-%03d", round))
		loc, err := tb.Insert(payload)
		if err != nil {
			t.Fatalf("round %d insert: %v", round, err)
		}
		live[loc] = payload
		// Delete every third surviving record to create reusable slots and
		// fragments across pages.
		if round%3 == 0 {
			for l := range live {
				if err := tb.Delete(l); err != nil {
					t.Fatalf("round %d delete %v: %v", round, l, err)
				}
				delete(live, l)
				break
			}
		}
	}
	for loc, want := range live {
		got, err := tb.Get(loc)
		if err != nil {
			t.Fatalf("Get(%v): %v", loc, err)
		}
		if !bytes.Equal(got, want) {
			t.Fatalf("Get(%v) = %q, want %q", loc, got, want)
		}
	}
}
