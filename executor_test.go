package exceptall_test

import (
	"context"
	"errors"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"exceptall"
)

func textPtr(s string) *string { return &s }

func gotLeftLines(rows []exceptall.OutputRow) []int64 {
	if len(rows) == 0 {
		return nil
	}
	lines := make([]int64, 0, len(rows))
	for _, row := range rows {
		lines = append(lines, row.LeftLine)
	}
	return lines
}

type referenceKey struct {
	isNull bool
	text   string
	num    int64
}

func referenceKeyOf(row exceptall.Row, foldCase bool) referenceKey {
	if row.Text == nil {
		return referenceKey{isNull: true, num: row.Num}
	}
	text := *row.Text
	if foldCase {
		text = strings.ToLower(text)
	}
	return referenceKey{text: text, num: row.Num}
}

// referenceExceptAll implements the documented multiplicity and earliest-left
// convention independently of the spill/merge implementation.
func referenceExceptAll(left, right []exceptall.Row, foldCase bool) []int64 {
	rightCounts := make(map[referenceKey]int)
	for _, row := range right {
		rightCounts[referenceKeyOf(row, foldCase)]++
	}

	leftLinesByKey := make(map[referenceKey][]int64)
	for i, row := range left {
		key := referenceKeyOf(row, foldCase)
		leftLinesByKey[key] = append(leftLinesByKey[key], int64(i+1))
	}

	survives := make(map[int64]bool)
	for key, lines := range leftLinesByKey {
		keep := len(lines) - rightCounts[key]
		if keep < 0 {
			keep = 0
		}
		// The first keep lines are the earliest left copies.
		for i := 0; i < keep; i++ {
			survives[lines[i]] = true
		}
	}

	var result []int64
	for i := range left {
		line := int64(i + 1)
		if survives[line] {
			result = append(result, line)
		}
	}
	return result
}

func runCollect(
	t *testing.T,
	ctx context.Context,
	left, right []exceptall.Row,
	opts exceptall.Options,
) ([]exceptall.OutputRow, exceptall.Report) {
	t.Helper()
	var got []exceptall.OutputRow
	report, err := exceptall.Run(
		ctx,
		exceptall.NewSliceReader(left),
		exceptall.NewSliceReader(right),
		opts,
		func(row exceptall.OutputRow) error {
			got = append(got, row)
			return nil
		},
	)
	if err != nil {
		t.Fatalf("Run returned error: %v", err)
	}
	return got, report
}

func assertOutputMatchesInput(t *testing.T, got []exceptall.OutputRow, left []exceptall.Row) {
	t.Helper()
	for _, out := range got {
		if out.LeftLine < 1 || int(out.LeftLine) > len(left) {
			t.Fatalf("output line %d outside left input [1,%d]", out.LeftLine, len(left))
		}
		want := left[out.LeftLine-1]
		if !reflect.DeepEqual(out.Row, want) {
			t.Fatalf("line %d row mismatch:\n got  %#v\n want %#v", out.LeftLine, out.Row, want)
		}
	}
}

func TestExceptAllSemanticsAndBoundaries(t *testing.T) {
	alpha := textPtr("alpha")
	beta := textPtr("beta")

	left := []exceptall.Row{
		{Text: alpha, Num: 1, Source: 101}, // 1
		{Text: alpha, Num: 1, Source: 102}, // 2
		{Text: alpha, Num: 1, Source: 103}, // 3 earliest survivor when R=2
		{Text: alpha, Num: 1, Source: 104}, // 4 canceled
		{Text: alpha, Num: 1, Source: 105}, // 5 canceled
		{Text: beta, Num: 7, Source: 106},  // 6
		{Text: nil, Num: 1, Source: 107},   // 7 NULL equals NULL
		{Text: nil, Num: 1, Source: 108},   // 8 canceled by two NULL rights
		{Text: nil, Num: 2, Source: 109},   // 9 different integer
	}
	right := []exceptall.Row{
		{Text: alpha, Num: 1, Source: 9001},
		{Text: alpha, Num: 1, Source: 9002},
		{Text: nil, Num: 1, Source: 9003},
		{Text: nil, Num: 1, Source: 9004},
		{Text: beta, Num: 8, Source: 9005}, // different Num, no cancellation
	}

	mgr := exceptall.NewMemorySegmentManager()
	got, report := runCollect(t, context.Background(), left, right, exceptall.Options{
		SegmentCapacity: 3,
		Segments:        mgr,
	})

	wantLines := []int64{1, 2, 3, 6, 9}
	if lines := gotLeftLines(got); !reflect.DeepEqual(lines, wantLines) {
		t.Fatalf("output lines = %v, want %v", lines, wantLines)
	}
	assertOutputMatchesInput(t, got, left)

	if report.LeftRows != int64(len(left)) || report.RightRows != int64(len(right)) {
		t.Fatalf("bad row counts: %+v", report)
	}
	if report.OutputRows != int64(len(wantLines)) {
		t.Fatalf("OutputRows=%d, want %d", report.OutputRows, len(wantLines))
	}
	if mgr.ActiveSegments() != 0 || mgr.CreatedSegments() != mgr.ReleasedSegments() {
		t.Fatalf("segments not cleaned: created=%d released=%d active=%d",
			mgr.CreatedSegments(), mgr.ReleasedSegments(), mgr.ActiveSegments())
	}
}

func TestBoundaryCases(t *testing.T) {
	k := textPtr("k")
	rows := []exceptall.Row{{Text: k, Num: 1, Source: 1}}

	tests := []struct {
		name       string
		left       []exceptall.Row
		right      []exceptall.Row
		wantLines  []int64
		wantReport bool
	}{
		{name: "right empty", left: rows, right: nil, wantLines: []int64{1}},
		{name: "left empty", left: nil, right: rows, wantLines: nil},
		{
			name:      "right count greater cancels all",
			left:      []exceptall.Row{{Text: k, Num: 1}, {Text: k, Num: 1}},
			right:     []exceptall.Row{{Text: k, Num: 1}, {Text: k, Num: 1}, {Text: k, Num: 1}},
			wantLines: nil,
		},
		{
			name: "exact counts cancel all",
			left: []exceptall.Row{
				{Text: nil, Num: 0},
				{Text: nil, Num: 0},
			},
			right: []exceptall.Row{
				{Text: nil, Num: 0, Source: 77},
				{Text: nil, Num: 0, Source: 78},
			},
			wantLines: nil,
		},
		{
			name: "no matching integer",
			left: []exceptall.Row{
				{Text: k, Num: 1},
				{Text: k, Num: 1},
			},
			right:     []exceptall.Row{{Text: k, Num: 2}},
			wantLines: []int64{1, 2},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			mgr := exceptall.NewMemorySegmentManager()
			got, _ := runCollect(t, context.Background(), tc.left, tc.right, exceptall.Options{
				SegmentCapacity: 3,
				Segments:        mgr,
			})
			if lines := gotLeftLines(got); !reflect.DeepEqual(lines, tc.wantLines) {
				t.Fatalf("lines = %v, want %v", lines, tc.wantLines)
			}
			assertOutputMatchesInput(t, got, tc.left)
			if mgr.ActiveSegments() != 0 {
				t.Fatalf("active segments = %d, want 0", mgr.ActiveSegments())
			}
		})
	}
}

func syntheticRows(n int, seed int64, sourceBase int64) []exceptall.Row {
	rng := rand.New(rand.NewSource(seed))
	texts := []*string{
		nil,
		textPtr("apple"), textPtr("Apple"), textPtr("APPLE"),
		textPtr("banana"), textPtr("Banana"),
		textPtr("cherry"), textPtr("CHERRY"),
		textPtr("date"), textPtr("Date"),
	}
	rows := make([]exceptall.Row, n)
	for i := range rows {
		rows[i] = exceptall.Row{
			Text:   texts[rng.Intn(len(texts))],
			Num:    int64(rng.Intn(5)),
			Source: sourceBase + int64(i+1),
		}
	}
	return rows
}

func TestCapacityThreeAcrossManyMergesMixedCaseAndNull(t *testing.T) {
	left := syntheticRows(65, 1729, 10000)
	right := syntheticRows(41, 8675, 20000)

	for _, fold := range []bool{false, true} {
		caseName := "binary-collation"
		var keyFunc func(string) string
		if fold {
			caseName = "folded-collation"
			keyFunc = strings.ToLower
		}

		t.Run(caseName, func(t *testing.T) {
			mgr := exceptall.NewMemorySegmentManager()
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()

			var got []exceptall.OutputRow
			report, err := exceptall.Run(ctx,
				exceptall.NewSliceReader(left),
				exceptall.NewSliceReader(right),
				exceptall.Options{
					SegmentCapacity: 3,
					Segments:        mgr,
					TextSortKey:     keyFunc,
				},
				func(row exceptall.OutputRow) error {
					got = append(got, row)
					return nil
				},
			)
			if err != nil {
				t.Fatalf("Run failed: %v", err)
			}

			wantLines := referenceExceptAll(left, right, fold)
			if lines := gotLeftLines(got); !reflect.DeepEqual(lines, wantLines) {
				t.Fatalf("output lines mismatch:\n got  %v\n want %v", lines, wantLines)
			}
			assertOutputMatchesInput(t, got, left)

			if report.SegmentCapacity != 3 {
				t.Fatalf("SegmentCapacity=%d, want 3", report.SegmentCapacity)
			}
			if report.MaxMergeRounds < 4 {
				t.Fatalf("MaxMergeRounds=%d, want at least 4 to exercise repeated merges", report.MaxMergeRounds)
			}
			if report.MergeOperations < 10 {
				t.Fatalf("MergeOperations=%d, want at least 10", report.MergeOperations)
			}
			if mgr.ActiveSegments() != 0 || mgr.CreatedSegments() != mgr.ReleasedSegments() {
				t.Fatalf("segment leak: created=%d released=%d active=%d",
					mgr.CreatedSegments(), mgr.ReleasedSegments(), mgr.ActiveSegments())
			}
		})
	}
}

func TestFileBackedExecutionMatchesReferenceAndCleansFiles(t *testing.T) {
	left := syntheticRows(38, 991, 30000)
	right := syntheticRows(29, 1773, 40000)
	mgr := newLocalFileManager(t)

	var got []exceptall.OutputRow
	report, err := exceptall.Run(context.Background(),
		exceptall.NewSliceReader(left),
		exceptall.NewSliceReader(right),
		exceptall.Options{
			SegmentCapacity: 3,
			Segments:        mgr,
			TextSortKey:     strings.ToLower,
		},
		func(row exceptall.OutputRow) error {
			got = append(got, row)
			return nil
		},
	)
	if err != nil {
		t.Fatalf("file-backed Run failed: %v", err)
	}

	want := referenceExceptAll(left, right, true)
	if gotLines := gotLeftLines(got); !reflect.DeepEqual(gotLines, want) {
		t.Fatalf("file-backed lines mismatch:\n got  %v\n want %v", gotLines, want)
	}
	assertOutputMatchesInput(t, got, left)
	if report.MergeOperations < 5 {
		t.Fatalf("MergeOperations=%d, want file-backed repeated merges", report.MergeOperations)
	}
	if active := mgr.ActiveSegments(); active != 0 {
		t.Fatalf("active file segments=%d, want 0", active)
	}
	entries, err := os.ReadDir(mgr.Dir())
	if err != nil {
		t.Fatalf("ReadDir: %v", err)
	}
	if len(entries) != 0 {
		t.Fatalf("leftover segment files: %v", entries)
	}
}

func TestDefaultFileSegmentsAreCreatedAndRemoved(t *testing.T) {
	base, err := os.MkdirTemp(".", "exceptall-default-*")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(base) })

	left := syntheticRows(12, 5, 1)
	got, _ := runCollect(t, context.Background(), left, nil, exceptall.Options{
		SegmentCapacity: 3,
		TempDir:         base,
	})
	if len(got) != len(left) {
		t.Fatalf("len=%d, want %d", len(got), len(left))
	}
	entries, err := os.ReadDir(base)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("default run directory leftovers: %v", entries)
	}
}

func TestRandomInterleavedInputsMatchReference(t *testing.T) {
	for seed := int64(1); seed <= 24; seed++ {
		t.Run(fmt.Sprintf("seed-%d", seed), func(t *testing.T) {
			rng := rand.New(rand.NewSource(seed))
			left := syntheticRows(rng.Intn(70), seed*11, 10000)
			right := syntheticRows(rng.Intn(60), seed*23, 20000)
			fold := seed%2 == 0

			mgr := exceptall.NewMemorySegmentManager()
			var got []exceptall.OutputRow
			_, err := exceptall.Run(context.Background(),
				exceptall.NewSliceReader(left),
				exceptall.NewSliceReader(right),
				exceptall.Options{
					SegmentCapacity: 3,
					Segments:        mgr,
					TextSortKey: func(s string) string {
						if fold {
							return strings.ToLower(s)
						}
						return s
					},
				},
				func(row exceptall.OutputRow) error {
					got = append(got, row)
					return nil
				},
			)
			if err != nil {
				t.Fatalf("Run failed: %v", err)
			}

			want := referenceExceptAll(left, right, fold)
			if gotLines := gotLeftLines(got); !reflect.DeepEqual(gotLines, want) {
				t.Fatalf("seed %d lines mismatch:\n got  %v\n want %v", seed, gotLines, want)
			}
			assertOutputMatchesInput(t, got, left)
			if mgr.ActiveSegments() != 0 {
				t.Fatalf("active segments=%d, want 0", mgr.ActiveSegments())
			}
		})
	}
}

func TestAllIdenticalKeysTerminatesAndKeepsEarliest(t *testing.T) {
	const leftN = 601
	const rightN = 239
	key := textPtr("same")
	left := make([]exceptall.Row, leftN)
	for i := range left {
		left[i] = exceptall.Row{Text: key, Num: 42, Source: int64(i + 1)}
	}
	right := make([]exceptall.Row, rightN)
	for i := range right {
		right[i] = exceptall.Row{Text: key, Num: 42, Source: int64(5000 + i)}
	}

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	mgr := exceptall.NewMemorySegmentManager()
	got, report := runCollect(t, ctx, left, right, exceptall.Options{
		SegmentCapacity: 3,
		Segments:        mgr,
	})

	wantN := leftN - rightN
	if len(got) != wantN {
		t.Fatalf("len(output)=%d, want %d", len(got), wantN)
	}
	for i, out := range got {
		wantLine := int64(i + 1)
		if out.LeftLine != wantLine {
			t.Fatalf("output[%d].LeftLine=%d, want %d", i, out.LeftLine, wantLine)
		}
		if out.Source != wantLine {
			t.Fatalf("output[%d].Source=%d, want %d", i, out.Source, wantLine)
		}
	}
	if report.MaxMergeRounds < 5 {
		t.Fatalf("MaxMergeRounds=%d, want repeated merges for all-identical input", report.MaxMergeRounds)
	}
	if mgr.ActiveSegments() != 0 {
		t.Fatalf("active segments=%d, want 0", mgr.ActiveSegments())
	}
}

type cancelAfterReader struct {
	inner  exceptall.RowReader
	cancel context.CancelFunc
	after  int
	calls  int
}

func (r *cancelAfterReader) Next(ctx context.Context) (exceptall.Row, bool, error) {
	row, ok, err := r.inner.Next(ctx)
	r.calls++
	if r.calls == r.after && r.cancel != nil {
		r.cancel()
	}
	return row, ok, err
}

func TestCancellationReturnsMemorySegments(t *testing.T) {
	key := textPtr("cancel")
	left := make([]exceptall.Row, 30)
	for i := range left {
		left[i] = exceptall.Row{Text: key, Num: 1, Source: int64(i)}
	}

	ctx, cancel := context.WithCancel(context.Background())
	reader := &cancelAfterReader{
		inner:  exceptall.NewSliceReader(left),
		cancel: cancel,
		after:  4,
	}
	mgr := exceptall.NewMemorySegmentManager()
	_, err := exceptall.Run(ctx, reader, exceptall.NewSliceReader(left), exceptall.Options{
		SegmentCapacity: 3,
		Segments:        mgr,
	}, nil)

	if !errors.Is(err, context.Canceled) {
		t.Fatalf("error=%v, want context.Canceled", err)
	}
	if mgr.CreatedSegments() == 0 {
		t.Fatal("expected at least one segment to be allocated before cancellation")
	}
	if active := mgr.ActiveSegments(); active != 0 {
		t.Fatalf("active segments after cancellation=%d, want 0", active)
	}
	if mgr.CreatedSegments() != mgr.ReleasedSegments() {
		t.Fatalf("created=%d released=%d, want equal", mgr.CreatedSegments(), mgr.ReleasedSegments())
	}
}

func newLocalFileManager(t *testing.T) *exceptall.FileSegmentManager {
	t.Helper()
	parent, err := os.MkdirTemp(".", "exceptall-test-*")
	if err != nil {
		t.Fatalf("MkdirTemp: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(parent) })

	dir := filepath.Join(parent, "segments")
	mgr, err := exceptall.NewFileSegmentManager(dir)
	if err != nil {
		t.Fatalf("NewFileSegmentManager: %v", err)
	}
	return mgr
}

func TestCancellationRemovesFileSegments(t *testing.T) {
	key := textPtr("cancel-file")
	left := make([]exceptall.Row, 24)
	for i := range left {
		left[i] = exceptall.Row{Text: key, Num: 1}
	}

	mgr := newLocalFileManager(t)
	ctx, cancel := context.WithCancel(context.Background())
	reader := &cancelAfterReader{
		inner:  exceptall.NewSliceReader(left),
		cancel: cancel,
		after:  4,
	}

	_, err := exceptall.Run(ctx, reader, exceptall.NewSliceReader(left), exceptall.Options{
		SegmentCapacity: 3,
		Segments:        mgr,
	}, nil)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("error=%v, want context.Canceled", err)
	}
	if mgr.CreatedSegments() == 0 {
		t.Fatal("expected file segments before cancellation")
	}
	if active := mgr.ActiveSegments(); active != 0 {
		t.Fatalf("active=%d, want 0", active)
	}
	entries, err := os.ReadDir(mgr.Dir())
	if err != nil {
		t.Fatalf("ReadDir segment dir: %v", err)
	}
	if len(entries) != 0 {
		t.Fatalf("segment directory has %d leftover entries: %v", len(entries), entries)
	}
}

func TestConsumerErrorReleasesSegments(t *testing.T) {
	left := syntheticRows(20, 42, 1)
	mgr := exceptall.NewMemorySegmentManager()
	boom := errors.New("consumer stopped")
	ctx := context.Background()

	_, err := exceptall.Run(ctx,
		exceptall.NewSliceReader(left),
		exceptall.NewSliceReader(nil),
		exceptall.Options{SegmentCapacity: 3, Segments: mgr},
		func(exceptall.OutputRow) error { return boom },
	)
	if !errors.Is(err, boom) {
		t.Fatalf("error=%v, want %v", err, boom)
	}
	if active := mgr.ActiveSegments(); active != 0 {
		t.Fatalf("active segments=%d, want 0", active)
	}
}

func TestInvalidCapacity(t *testing.T) {
	_, err := exceptall.Run(context.Background(),
		exceptall.NewSliceReader(nil),
		exceptall.NewSliceReader(nil),
		exceptall.Options{SegmentCapacity: 2},
		nil,
	)
	if !errors.Is(err, exceptall.ErrInvalidOptions) {
		t.Fatalf("error=%v, want ErrInvalidOptions", err)
	}
}

func TestSegmentImplementationsSequentialRoundTrip(t *testing.T) {
	records := []exceptall.Record{
		{Row: exceptall.Row{Text: nil, Num: 1, Source: 9}, Line: 1},
		{Row: exceptall.Row{Text: textPtr("Mixed"), Num: -2, Source: 8}, Line: 2, Keep: 3},
		{Row: exceptall.Row{Text: textPtr("case"), Num: 0, Source: 7}, Line: 3},
	}

	t.Run("memory", func(t *testing.T) {
		mgr := exceptall.NewMemorySegmentManager()
		seg, err := mgr.CreateSegment(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		assertSegmentRoundTrip(t, seg, records)
		if err := seg.Release(); err != nil {
			t.Fatal(err)
		}
		if mgr.ActiveSegments() != 0 {
			t.Fatalf("active=%d", mgr.ActiveSegments())
		}
	})

	t.Run("file", func(t *testing.T) {
		mgr := newLocalFileManager(t)
		seg, err := mgr.CreateSegment(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		assertSegmentRoundTrip(t, seg, records)
		path := seg.(*exceptall.FileSegment).Path()
		if err := seg.Release(); err != nil {
			t.Fatal(err)
		}
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Fatalf("segment file %s still exists, stat err=%v", path, err)
		}
	})
}

func assertSegmentRoundTrip(t *testing.T, seg exceptall.Segment, want []exceptall.Record) {
	t.Helper()
	ctx := context.Background()
	for _, rec := range want {
		if err := seg.Append(ctx, rec); err != nil {
			t.Fatalf("Append: %v", err)
		}
	}

	reader, err := seg.OpenReader(ctx)
	if err != nil {
		t.Fatalf("OpenReader: %v", err)
	}
	defer reader.Close()

	var got []exceptall.Record
	for {
		rec, ok, err := reader.Next(ctx)
		if err != nil {
			t.Fatalf("Next: %v", err)
		}
		if !ok {
			break
		}
		got = append(got, rec)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("records mismatch:\n got  %#v\n want %#v", got, want)
	}
}
