package exceptall

import (
	"context"
	"errors"
	"fmt"
	"os"
)

// ErrInvalidOptions indicates that Options failed validation.
var ErrInvalidOptions = errors.New("exceptall: invalid options")

// DefaultMemoryRows is used when Options.SegmentCapacity is zero.
const DefaultMemoryRows = 1024

// Options controls execution.
type Options struct {
	// SegmentCapacity is both the maximum bounded sort batch size and the
	// number of rows placed in each initial sorted segment. The pairwise
	// merge implementation keeps only two head records per merge. The minimum
	// supported value is three; tests deliberately lower it to three.
	SegmentCapacity int

	// Segments allocates sequential temporary segments. When nil, Run creates
	// and cleans up a private FileSegmentManager under TempDir. For tests or
	// environments with another spill medium, pass a custom implementation.
	Segments SegmentManager

	// TempDir is the base directory used only when Segments is nil. It
	// defaults to ".exceptall-tmp" in the current workspace.
	TempDir string

	// TextSortKey optionally maps non-NULL text to a comparable sort key.
	// For example, strings.ToLower makes mixed-case text equivalent.
	TextSortKey func(text string) string

	// CompareText optionally provides a full text comparator. It takes
	// precedence over TextSortKey.
	CompareText TextCompareFunc

	// CompareBusinessKey optionally overrides comparison of the equivalence
	// columns. It must compare Text with SQL NULL semantics (NULL equals
	// NULL) and must not compare Source. When nil, Text and Num are compared.
	CompareBusinessKey func(a, b Row) int
}

// Report contains execution and cleanup statistics.
type Report struct {
	LeftRows   int64
	RightRows  int64
	OutputRows int64

	SegmentCapacity  int
	SegmentsCreated  int
	SegmentsReleased int

	// MergeOperations is the number of binary segment merges performed across
	// all external sorts.
	MergeOperations int
	// MaxMergeRounds is the largest number of pairwise merge levels used by
	// one external sort.
	MaxMergeRounds int
}

// Run executes EXCEPT ALL and streams surviving rows to yield.
//
// All temporary segments created during the query are released before Run
// returns, including cancellation and consumer-error paths. If yield is nil,
// rows are counted but discarded.
func Run(
	ctx context.Context,
	left RowReader,
	right RowReader,
	options Options,
	yield func(OutputRow) error,
) (report Report, err error) {
	if ctx == nil {
		ctx = context.Background()
	}
	if left == nil || right == nil {
		return Report{}, fmt.Errorf("%w: left and right readers must not be nil", ErrInvalidOptions)
	}
	if yield == nil {
		yield = func(OutputRow) error { return nil }
	}

	capacity, keyCmp, err := options.normalized()
	if err != nil {
		return Report{}, err
	}
	report.SegmentCapacity = capacity

	manager := options.Segments
	defaultRunDir := ""
	if manager == nil {
		baseDir := options.TempDir
		if baseDir == "" {
			baseDir = ".exceptall-tmp"
		}
		if err := os.MkdirAll(baseDir, 0o755); err != nil {
			return Report{}, fmt.Errorf("exceptall: create temp base directory %q: %w", baseDir, err)
		}
		runDir, err := os.MkdirTemp(baseDir, "run-*")
		if err != nil {
			return Report{}, fmt.Errorf("exceptall: create temp run directory: %w", err)
		}
		fileManager, err := NewFileSegmentManager(runDir)
		if err != nil {
			_ = os.RemoveAll(runDir)
			return Report{}, err
		}
		manager = fileManager
		defaultRunDir = runDir
	}
	jan := newJanitor(manager)

	// Cleanup is registered before any segment can be created. It therefore
	// runs on cancellation and every error path.
	defer func() {
		releaseErr := jan.releaseAll()
		report.SegmentsCreated = jan.created
		report.SegmentsReleased = jan.released
		if releaseErr != nil {
			err = errors.Join(err, fmt.Errorf("release temporary segments: %w", releaseErr))
		}
		if defaultRunDir != "" {
			if removeErr := os.RemoveAll(defaultRunDir); removeErr != nil {
				err = errors.Join(err, fmt.Errorf("remove temporary run directory: %w", removeErr))
			}
		}
	}()

	keyRecordCmp := recordKeyComparator(keyCmp)

	leftEnum := &enumeratedReader{reader: left}
	leftSorted, leftSort, err := externalSort(ctx, leftEnum, keyRecordCmp, capacity, jan)
	if err != nil {
		return report, err
	}
	report.LeftRows = leftSort.rows
	report.MergeOperations += leftSort.mergeOperations
	report.MaxMergeRounds = maxInt(report.MaxMergeRounds, leftSort.rounds)

	rightEnum := &enumeratedReader{reader: right}
	rightSorted, rightSort, err := externalSort(ctx, rightEnum, keyRecordCmp, capacity, jan)
	if err != nil {
		return report, err
	}
	report.RightRows = rightSort.rows
	report.MergeOperations += rightSort.mergeOperations
	report.MaxMergeRounds = maxInt(report.MaxMergeRounds, rightSort.rounds)

	decisions, err := buildDecisions(ctx, leftSorted, rightSorted, keyCmp, jan)
	if err != nil {
		return report, err
	}

	survivorsRaw, err := buildSurvivorSegment(ctx, leftSorted, decisions, keyCmp, jan)
	if err != nil {
		return report, err
	}

	survivorReader, err := survivorsRaw.OpenReader(ctx)
	if err != nil {
		return report, err
	}

	finalSorted, finalSort, sortErr := externalSort(
		ctx,
		survivorReader,
		recordLineComparator,
		capacity,
		jan,
	)
	closeErr := survivorReader.Close()
	if sortErr != nil {
		return report, sortErr
	}
	if closeErr != nil {
		return report, closeErr
	}
	report.MergeOperations += finalSort.mergeOperations
	report.MaxMergeRounds = maxInt(report.MaxMergeRounds, finalSort.rounds)

	if finalSorted != nil {
		finalReader, err := finalSorted.OpenReader(ctx)
		if err != nil {
			return report, err
		}
		defer func() { _ = finalReader.Close() }()

		for {
			record, ok, err := finalReader.Next(ctx)
			if err != nil {
				return report, err
			}
			if !ok {
				break
			}

			report.OutputRows++
			if err := yield(record.Output()); err != nil {
				return report, err
			}
		}
	}

	return report, nil
}

func (o Options) normalized() (int, businessKeyComparator, error) {
	capacity := o.SegmentCapacity
	if capacity == 0 {
		capacity = DefaultMemoryRows
	}
	if capacity < 3 {
		return 0, nil, fmt.Errorf(
			"%w: SegmentCapacity must be at least 3 for bounded pairwise external merge, got %d",
			ErrInvalidOptions,
			capacity,
		)
	}

	if o.CompareBusinessKey != nil {
		return capacity, o.CompareBusinessKey, nil
	}

	textCmp := o.CompareText
	if textCmp == nil && o.TextSortKey != nil {
		keyFunc := o.TextSortKey
		textCmp = func(a, b string) int {
			return BinaryTextCompare(keyFunc(a), keyFunc(b))
		}
	}
	if textCmp == nil {
		textCmp = BinaryTextCompare
	}
	return capacity, defaultBusinessKeyComparator(textCmp), nil
}

type enumeratedReader struct {
	reader RowReader
	line   int64
}

func (r *enumeratedReader) Next(ctx context.Context) (Record, bool, error) {
	row, ok, err := r.reader.Next(ctx)
	if err != nil {
		return Record{}, false, err
	}
	if !ok {
		return Record{}, false, nil
	}
	r.line++
	return Record{Row: row, Line: r.line}, true, nil
}

type janitor struct {
	manager  SegmentManager
	active   map[Segment]struct{}
	created  int
	released int
}

func newJanitor(manager SegmentManager) *janitor {
	return &janitor{manager: manager, active: make(map[Segment]struct{})}
}

func (j *janitor) create(ctx context.Context) (Segment, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	segment, err := j.manager.CreateSegment(ctx)
	if err != nil {
		return nil, err
	}
	if segment == nil {
		return nil, fmt.Errorf("%w: SegmentManager returned a nil segment", ErrInvalidOptions)
	}
	j.created++
	j.active[segment] = struct{}{}
	return segment, nil
}

func (j *janitor) release(segment Segment) error {
	if segment == nil {
		return nil
	}
	if _, ok := j.active[segment]; !ok {
		return nil
	}
	delete(j.active, segment)
	j.released++
	return segment.Release()
}

func (j *janitor) releaseAll() error {
	var errs []error
	for segment := range j.active {
		delete(j.active, segment)
		j.released++
		if err := segment.Release(); err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

func buildDecisions(
	ctx context.Context,
	leftSorted Segment,
	rightSorted Segment,
	keyCmp businessKeyComparator,
	jan *janitor,
) (Segment, error) {
	// An empty left input has no left keys and therefore needs no decision
	// stream. Run handles a nil survivor segment.
	if leftSorted == nil {
		return nil, nil
	}

	out, err := jan.create(ctx)
	if err != nil {
		return nil, err
	}

	leftReader, err := leftSorted.OpenReader(ctx)
	if err != nil {
		return nil, err
	}
	defer func() { _ = leftReader.Close() }()

	var rightReader SegmentReader
	if rightSorted != nil {
		rightReader, err = rightSorted.OpenReader(ctx)
		if err != nil {
			return nil, err
		}
		defer func() { _ = rightReader.Close() }()
	}

	leftRec, leftOK, err := readNext(ctx, leftReader)
	if err != nil {
		return nil, err
	}
	rightRec, rightOK, err := readNext(ctx, rightReader)
	if err != nil {
		return nil, err
	}

	for leftOK || rightOK {
		if err := ctx.Err(); err != nil {
			return nil, err
		}

		var groupKey Row
		haveGroup := false
		if leftOK {
			groupKey = leftRec.Row
			haveGroup = true
		}
		if rightOK && (!haveGroup || keyCmp(rightRec.Row, groupKey) < 0) {
			groupKey = rightRec.Row
			haveGroup = true
		}

		var leftCount, rightCount int64

		for leftOK && keyCmp(leftRec.Row, groupKey) == 0 {
			leftCount++
			leftRec, leftOK, err = readNext(ctx, leftReader)
			if err != nil {
				return nil, err
			}
		}

		for rightOK && keyCmp(rightRec.Row, groupKey) == 0 {
			rightCount++
			rightRec, rightOK, err = readNext(ctx, rightReader)
			if err != nil {
				return nil, err
			}
		}

		if leftCount > 0 {
			keep := leftCount - rightCount
			if keep < 0 {
				keep = 0
			}
			decision := Record{
				// Key and Num are equivalent across the group. Copy only
				// equivalence columns into the decision.
				Row:  Row{Text: groupKey.Text, Num: groupKey.Num},
				Keep: keep,
			}
			// The decision is now the third live record alongside the two
			// look-ahead heads; drop the separate group-key root.
			groupKey = Row{}
			if err := out.Append(ctx, decision); err != nil {
				return nil, err
			}
		}
	}

	return out, nil
}

func buildSurvivorSegment(
	ctx context.Context,
	leftSorted Segment,
	decisions Segment,
	keyCmp businessKeyComparator,
	jan *janitor,
) (Segment, error) {
	out, err := jan.create(ctx)
	if err != nil {
		return nil, err
	}
	if leftSorted == nil {
		return out, nil
	}
	if decisions == nil {
		return nil, fmt.Errorf("exceptall: internal error: missing decision segment")
	}

	leftReader, err := leftSorted.OpenReader(ctx)
	if err != nil {
		return nil, err
	}
	defer func() { _ = leftReader.Close() }()

	decisionReader, err := decisions.OpenReader(ctx)
	if err != nil {
		return nil, err
	}
	defer func() { _ = decisionReader.Close() }()

	leftRec, leftOK, err := readNext(ctx, leftReader)
	if err != nil {
		return nil, err
	}
	decisionRec, decisionOK, err := readNext(ctx, decisionReader)
	if err != nil {
		return nil, err
	}

	for decisionOK {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if !leftOK {
			return nil, fmt.Errorf("exceptall: corrupt spill: decision has no matching left row")
		}
		if c := keyCmp(leftRec.Row, decisionRec.Row); c != 0 {
			return nil, fmt.Errorf("exceptall: corrupt spill: decision/left key order mismatch (%d)", c)
		}

		keep := decisionRec.Keep
		var rank int64
		for leftOK && keyCmp(leftRec.Row, decisionRec.Row) == 0 {
			rank++
			// leftSorted orders equal business keys by Line ascending.
			// Keeping ranks 1..keep therefore keeps the earliest survivors.
			if rank <= keep {
				if err := out.Append(ctx, leftRec); err != nil {
					return nil, err
				}
			}

			leftRec, leftOK, err = readNext(ctx, leftReader)
			if err != nil {
				return nil, err
			}
		}

		decisionRec, decisionOK, err = readNext(ctx, decisionReader)
		if err != nil {
			return nil, err
		}
	}

	if leftOK {
		return nil, fmt.Errorf("exceptall: corrupt spill: left rows remain without decisions")
	}
	return out, nil
}

func readNext(ctx context.Context, reader SegmentReader) (Record, bool, error) {
	if reader == nil {
		return Record{}, false, nil
	}
	return reader.Next(ctx)
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
