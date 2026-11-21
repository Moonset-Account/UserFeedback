package coordination

import (
	"fmt"
	"sync"
	"testing"
	"time"
)

// TestConcurrentHandoffSingleHolder drives every contender from its own
// goroutine. The queue must hand the lock off one holder at a time, every
// contender must eventually write exactly once, and every accepted write must
// carry a strictly larger fencing token than the previous accepted one.
//
// It is a scheduling stress test rather than a source of truth for protocol
// semantics; the deterministic tests cover the individual protocol steps.
func TestConcurrentHandoffSingleHolder(t *testing.T) {
	svc := NewService()
	res := NewResource()

	const n = 5
	contenders := make([]*Contender, n)
	for i := 0; i < n; i++ {
		id := fmt.Sprintf("client-%d", i)
		mustSession(t, svc, id)
		contenders[i] = NewContender(svc, id, lockA)
		if err := contenders[i].Enqueue(); err != nil {
			t.Fatalf("enqueue %s: %v", id, err)
		}
	}

	deadline := time.Now().Add(20 * time.Second)
	errs := make(chan error, n)
	var wg sync.WaitGroup

	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(c *Contender) {
			defer wg.Done()
			for time.Now().Before(deadline) {
				switch c.Phase() {
				case PhaseHeld:
					if err := c.TakeOver(res, c.SessionID()); err != nil {
						errs <- fmt.Errorf("%s takeover: %w", c.SessionID(), err)
						return
					}
					if err := c.Release(); err != nil {
						errs <- fmt.Errorf("%s release: %w", c.SessionID(), err)
						return
					}
					return
				case PhaseLost:
					errs <- fmt.Errorf("%s lost the session unexpectedly", c.SessionID())
					return
				}
				if _, err := c.Step(); err != nil {
					errs <- fmt.Errorf("%s step: %w", c.SessionID(), err)
					return
				}
				time.Sleep(50 * time.Microsecond)
			}
			errs <- fmt.Errorf("%s timed out in phase %v", c.SessionID(), c.Phase())
		}(contenders[i])
	}

	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}

	if got := svc.Children(lockA); len(got) != 0 {
		t.Fatalf("queue not drained: %+v", got)
	}
	if res.Version() != n {
		t.Fatalf("accepted writes = %d, want %d", res.Version(), n)
	}

	history := res.History()
	if len(history) != n {
		t.Fatalf("history length = %d, want %d", len(history), n)
	}
	seen := make(map[string]bool, n)
	var last uint64
	for _, rec := range history {
		if !rec.Accepted {
			t.Fatalf("rejected write in serial hand-off: %+v", rec)
		}
		if rec.Token <= last {
			t.Fatalf("token %d not greater than previous %d", rec.Token, last)
		}
		last = rec.Token
		if seen[rec.SessionID] {
			t.Fatalf("session %s wrote twice", rec.SessionID)
		}
		seen[rec.SessionID] = true
	}
	if len(seen) != n {
		t.Fatalf("only %d distinct writers: %v", len(seen), seen)
	}
	if res.Fence() != last {
		t.Fatalf("resource fence = %d, last write token = %d", res.Fence(), last)
	}
}
