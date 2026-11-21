package relationindex

import (
	"errors"
	"math"
	"math/rand"
	"sort"
	"testing"
)

// orderRows is an independent reference implementation of the documented
// display order. Tests compare production output against it rather than
// against itself.
func orderRows(rows []Row) []Row {
	out := append([]Row(nil), rows...)
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if a.Region != b.Region {
			return a.Region < b.Region
		}
		aScored := a.Score != nil
		bScored := b.Score != nil
		if aScored != bScored {
			return aScored // unscored rows go last within a region
		}
		if aScored && *a.Score != *b.Score {
			return *a.Score > *b.Score
		}
		return a.ID < b.ID
	})
	return out
}

func mustSnapshot(t *testing.T, rows []Row) *Snapshot {
	t.Helper()
	s, err := NewSnapshot(rows)
	if err != nil {
		t.Fatalf("NewSnapshot: %v", err)
	}
	return s
}

// mustPage returns a checker that fails the test when err is non-nil. It is
// curried so call sites can pass a (Page, error) pair directly:
//
//	mustPage(t)(snap.FirstPage(10))
func mustPage(t *testing.T) func(Page, error) Page {
	t.Helper()
	return func(p Page, err error) Page {
		t.Helper()
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		return p
	}
}

func ids(rows []Row) []string {
	out := make([]string, len(rows))
	for i, r := range rows {
		out[i] = r.ID
	}
	return out
}

func equalIDs(a, b []Row) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i].ID != b[i].ID {
			return false
		}
	}
	return true
}

func equalRows(a, b []Row) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i].ID != b[i].ID || a[i].Region != b[i].Region {
			return false
		}
		if (a[i].Score == nil) != (b[i].Score == nil) {
			return false
		}
		if a[i].Score != nil && *a[i].Score != *b[i].Score {
			return false
		}
	}
	return true
}

func TestOrderRegionScoreAndKey(t *testing.T) {
	rows := []Row{
		{ID: "e", Region: "eu", Score: NewScore(10)},
		{ID: "d", Region: "ap", Score: NewScore(10)},
		{ID: "a", Region: "eu", Score: NewScore(99)},
		{ID: "c", Region: "ap", Score: NewScore(10)},
		{ID: "b", Region: "eu", Score: NewScore(10)},
		{ID: "f", Region: "eu"},
		{ID: "g", Region: "ap", Score: NewScore(-3)},
	}
	snap := mustSnapshot(t, rows)

	want := []string{"c", "d", "g", "a", "b", "e", "f"}
	if got := ids(mustPage(t)(snap.FirstPage(len(rows))).Rows); !equalStrings(got, want) {
		t.Fatalf("order = %v, want %v", got, want)
	}
}

func equalStrings(a, b []string) bool {
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

func TestUnscoredRowsSortAfterScoredWithinRegion(t *testing.T) {
	rows := []Row{
		{ID: "u1", Region: "r", Score: nil},
		{ID: "u0", Region: "r", Score: nil},
		{ID: "s0", Region: "r", Score: NewScore(-1e300)},
	}
	snap := mustSnapshot(t, rows)
	got := ids(mustPage(t)(snap.FirstPage(10)).Rows)
	want := []string{"s0", "u0", "u1"}
	if !equalStrings(got, want) {
		t.Fatalf("order = %v, want %v", got, want)
	}
}

func TestNonFiniteAndEmptyRowsRejected(t *testing.T) {
	cases := []Row{
		{ID: "", Region: "r", Score: NewScore(1)},
		{ID: "nan", Region: "r", Score: NewScore(math.NaN())},
		{ID: "inf", Region: "r", Score: NewScore(math.Inf(-1))},
	}
	for _, r := range cases {
		if _, err := NewSnapshot([]Row{r}); !errors.Is(err, ErrInvalidRow) {
			t.Fatalf("row %+v: err = %v, want ErrInvalidRow", r, err)
		}
	}
}

func TestDuplicatePrimaryKeyRejected(t *testing.T) {
	_, err := NewSnapshot([]Row{
		{ID: "x", Region: "r", Score: NewScore(1)},
		{ID: "x", Region: "r", Score: NewScore(2)},
	})
	if !errors.Is(err, ErrDuplicateID) {
		t.Fatalf("err = %v, want ErrDuplicateID", err)
	}
}

func TestPageSizeMustBePositive(t *testing.T) {
	snap := mustSnapshot(t, []Row{{ID: "a", Region: "r", Score: NewScore(1)}})
	for _, size := range []int{0, -1} {
		if _, err := snap.FirstPage(size); !errors.Is(err, ErrInvalidPageSize) {
			t.Fatalf("FirstPage(%d) err = %v", size, err)
		}
		if _, err := snap.FetchAfter("", size); !errors.Is(err, ErrInvalidPageSize) {
			t.Fatalf("FetchAfter(%d) err = %v", size, err)
		}
	}
}

func TestEmptySnapshot(t *testing.T) {
	snap := mustSnapshot(t, nil)
	if snap.Len() != 0 {
		t.Fatalf("Len = %d", snap.Len())
	}
	for _, p := range []Page{
		mustPage(t)(snap.FirstPage(5)),
		mustPage(t)(snap.LastPage(5)),
		mustPage(t)(snap.FetchBefore("", 5)),
	} {
		if len(p.Rows) != 0 || p.PrevCursor != "" || p.NextCursor != "" || p.HasPrev || p.HasNext {
			t.Fatalf("unexpected non-empty page: %+v", p)
		}
	}
}

// TestReferenceOrderMatchesOverRandomData cross-checks the production order
// against orderRows on randomised data with regions, ties and missing scores.
func TestReferenceOrderMatchesOverRandomData(t *testing.T) {
	rng := rand.New(rand.NewSource(7))
	regions := []string{"ap", "eu", "us", "cn"}
	rows := make([]Row, 0, 300)
	for i := 0; i < 300; i++ {
		r := Row{ID: "id-" + zeroPad(i), Region: regions[rng.Intn(len(regions))]}
		if rng.Intn(5) != 0 {
			r.Score = NewScore(float64(rng.Intn(6)))
		}
		rows = append(rows, r)
	}
	snap := mustSnapshot(t, rows)
	got := mustPage(t)(snap.FirstPage(len(rows))).Rows
	if !equalRows(got, orderRows(rows)) {
		t.Fatal("snapshot order does not match reference order")
	}
}

func zeroPad(i int) string {
	const digits = "0123456789"
	if i == 0 {
		return "0"
	}
	var b []byte
	for i > 0 {
		b = append([]byte{digits[i%10]}, b...)
		i /= 10
	}
	return string(b)
}
