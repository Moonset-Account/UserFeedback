package dataflow

import (
	"context"
	"errors"
	"runtime"
	"sync"
	"time"
)

// RunConcurrent drives every operator with its own goroutine until all
// programs finish or ctx is cancelled.
//
// Blocked steps are retried after a scheduler yield / tiny backoff: in a
// robust configuration every blocking condition is transient because some
// other goroutine holds the action that releases it. The mutex inside Driver
// linearizes goroutine calls, so every real interleaving is one of the
// interleavings Verify checked.
//
// It returns the first non-blocking step error, or ctx.Err() on timeout.
func RunConcurrent(ctx context.Context, d *Driver) error {
	ops := append([]OpID(nil), d.opOrder...)
	var wg sync.WaitGroup
	var once sync.Once
	var firstErr error
	setErr := func(err error) {
		if err == nil {
			return
		}
		once.Do(func() { firstErr = err })
	}

	for _, op := range ops {
		wg.Add(1)
		go func(op OpID) {
			defer wg.Done()
			spin := 0
			for {
				if ctx != nil {
					select {
					case <-ctx.Done():
						setErr(ctx.Err())
						return
					default:
					}
				}
				if d.OperatorDone(op) {
					return
				}
				_, err := d.Step(op)
				if err == nil {
					spin = 0
					continue
				}
				if errors.Is(err, ErrOperatorDone) {
					return
				}
				if be, ok := AsBlocked(err); ok {
					_ = be
					spin++
					if spin < 8 {
						runtime.Gosched()
						continue
					}
					// Gentle backoff for a genuinely contended wait.
					select {
					case <-time.After(time.Microsecond * time.Duration(spin)):
					case <-ctx.Done():
						setErr(ctx.Err())
						return
					}
					if spin > 200 {
						spin = 200
					}
					continue
				}
				setErr(err)
				return
			}
		}(op)
	}
	wg.Wait()
	return firstErr
}
