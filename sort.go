package exceptall

import (
	"context"
	"sort"
)

// RecordReader is the internal streaming record interface. SegmentReader and
// enumerated input readers implement it.
type RecordReader interface {
	Next(ctx context.Context) (Record, bool, error)
}

type sortStats struct {
	rows            int64
	initialRuns     int
	mergeOperations int
	rounds          int
}

// externalSort builds sorted runs no larger than capacity and then performs
// binary pairwise external merges. Binary merging needs only two current head
// records, which keeps the operator's live row state small.
func externalSort(
	ctx context.Context,
	source RecordReader,
	compare func(a, b Record) int,
	capacity int,
	jan *janitor,
) (Segment, sortStats, error) {
	var stats sortStats

	runs, err := buildSortedRuns(ctx, source, compare, capacity, jan, &stats)
	if err != nil {
		return nil, stats, err
	}
	if len(runs) == 0 {
		return nil, stats, nil
	}

	merged, rounds, mergeOps, err := mergePairwise(ctx, runs, compare, jan)
	if err != nil {
		return nil, stats, err
	}
	stats.rounds = rounds
	stats.mergeOperations = mergeOps
	return merged, stats, nil
}

func buildSortedRuns(
	ctx context.Context,
	source RecordReader,
	compare func(a, b Record) int,
	capacity int,
	jan *janitor,
	stats *sortStats,
) ([]Segment, error) {
	runs := make([]Segment, 0)
	batch := make([]Record, 0, capacity)

	flush := func() error {
		if len(batch) == 0 {
			return nil
		}

		sort.Slice(batch, func(i, j int) bool {
			return compare(batch[i], batch[j]) < 0
		})

		segment, err := jan.create(ctx)
		if err != nil {
			return err
		}
		for i := range batch {
			if err := segment.Append(ctx, batch[i]); err != nil {
				return err
			}
		}

		// Release references before reusing the bounded batch.
		for i := range batch {
			batch[i] = Record{}
		}
		batch = batch[:0]
		runs = append(runs, segment)
		stats.initialRuns++
		return nil
	}

	for {
		record, ok, err := source.Next(ctx)
		if err != nil {
			return nil, err
		}
		if !ok {
			break
		}

		stats.rows++
		batch = append(batch, record)
		// Do not retain a second root for the last appended record.
		record = Record{}
		if len(batch) == capacity {
			if err := flush(); err != nil {
				return nil, err
			}
		}
	}

	if err := flush(); err != nil {
		return nil, err
	}
	return runs, nil
}

func mergePairwise(
	ctx context.Context,
	runs []Segment,
	compare func(a, b Record) int,
	jan *janitor,
) (Segment, int, int, error) {
	rounds := 0
	mergeOperations := 0
	current := runs

	for len(current) > 1 {
		if err := ctx.Err(); err != nil {
			return nil, rounds, mergeOperations, err
		}

		rounds++
		next := make([]Segment, 0, (len(current)+1)/2)
		for i := 0; i < len(current); i += 2 {
			if err := ctx.Err(); err != nil {
				return nil, rounds, mergeOperations, err
			}

			// An odd final run advances unchanged to the next round.
			if i+1 == len(current) {
				next = append(next, current[i])
				continue
			}

			merged, err := mergeTwo(ctx, current[i], current[i+1], compare, jan)
			if err != nil {
				return nil, rounds, mergeOperations, err
			}
			mergeOperations++

			if err := jan.release(current[i]); err != nil {
				return nil, rounds, mergeOperations, err
			}
			if err := jan.release(current[i+1]); err != nil {
				return nil, rounds, mergeOperations, err
			}
			next = append(next, merged)
		}
		current = next
	}

	return current[0], rounds, mergeOperations, nil
}

func mergeTwo(
	ctx context.Context,
	left Segment,
	right Segment,
	compare func(a, b Record) int,
	jan *janitor,
) (Segment, error) {
	out, err := jan.create(ctx)
	if err != nil {
		return nil, err
	}

	leftReader, err := left.OpenReader(ctx)
	if err != nil {
		return nil, err
	}
	defer func() { _ = leftReader.Close() }()

	rightReader, err := right.OpenReader(ctx)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rightReader.Close() }()

	leftRec, leftOK, err := leftReader.Next(ctx)
	if err != nil {
		return nil, err
	}
	rightRec, rightOK, err := rightReader.Next(ctx)
	if err != nil {
		return nil, err
	}

	for leftOK && rightOK {
		if err := ctx.Err(); err != nil {
			return nil, err
		}

		if compare(leftRec, rightRec) <= 0 {
			if err := out.Append(ctx, leftRec); err != nil {
				return nil, err
			}
			leftRec, leftOK, err = leftReader.Next(ctx)
		} else {
			if err := out.Append(ctx, rightRec); err != nil {
				return nil, err
			}
			rightRec, rightOK, err = rightReader.Next(ctx)
		}
		if err != nil {
			return nil, err
		}
	}

	for leftOK {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if err := out.Append(ctx, leftRec); err != nil {
			return nil, err
		}
		leftRec, leftOK, err = leftReader.Next(ctx)
		if err != nil {
			return nil, err
		}
	}

	for rightOK {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if err := out.Append(ctx, rightRec); err != nil {
			return nil, err
		}
		rightRec, rightOK, err = rightReader.Next(ctx)
		if err != nil {
			return nil, err
		}
	}

	return out, nil
}
