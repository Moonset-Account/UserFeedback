package sharedscan

import (
	"context"
	"errors"
	"io"
)

// Scanner is the pull-based inner scan. Implementations may return rows in
// arbitrary incremental chunks, including dependency-grouped chunks.
type Scanner interface {
	NextChunk(ctx context.Context) (Chunk, error)
}

// ScannerFunc adapts a function to Scanner.
type ScannerFunc func(ctx context.Context) (Chunk, error)

// NextChunk calls f.
func (f ScannerFunc) NextChunk(ctx context.Context) (Chunk, error) {
	return f(ctx)
}

// Evaluate runs one shared scan for a request batch. It pulls each inner
// chunk once and fans it out inside Operator. If every request finds decisive
// evidence, Evaluate may stop before io.EOF; otherwise it scans until EOF so
// pending requests receive all evidence required for FALSE/UNKNOWN results.
func Evaluate(
	ctx context.Context,
	requests []Request,
	scanner Scanner,
	options ...Option,
) ([]Result, Stats, error) {
	if scanner == nil {
		return nil, Stats{}, errors.New("sharedscan: scanner is nil")
	}
	if ctx == nil {
		ctx = context.Background()
	}

	op, err := NewOperator(requests, options...)
	if err != nil {
		return nil, Stats{}, err
	}
	if len(requests) == 0 {
		return []Result{}, Stats{}, nil
	}

	stoppedEarly := false
	for {
		if err := ctx.Err(); err != nil {
			return nil, op.Stats(), err
		}

		chunk, scanErr := scanner.NextChunk(ctx)
		eof := errors.Is(scanErr, io.EOF)
		if scanErr != nil && !eof {
			return nil, op.Stats(), scanErr
		}

		// A conventional scanner returns zero Chunk + io.EOF. Do not count
		// that terminal marker as a processed data chunk. An EOF may also
		// carry a final non-empty chunk, which must be consumed.
		if !eof || len(chunk.Rows) > 0 {
			if err := op.ConsumeChunk(chunk); err != nil {
				return nil, op.Stats(), err
			}
		}

		if eof {
			break
		}
		if op.AllDecisive() {
			stoppedEarly = true
			break
		}
	}

	results, err := op.Finish()
	stats := op.Stats()
	stats.ScanStoppedEarly = stoppedEarly
	if err != nil {
		return nil, stats, err
	}
	return results, stats, nil
}
