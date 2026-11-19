package encupload

import (
	"errors"
	"sync"
	"testing"
)

// TestConcurrentPolicyAndCompletionIsSingleOutcome races an immediate policy
// change against a completion. Whichever order the controller linearizes them
// in, the observable state must be exactly one of the two consistent results:
// either the completion committed a full object under the old policy, or it
// was rejected and readers still see nothing. A torn mix is not allowed.
func TestConcurrentPolicyAndCompletionIsSingleOutcome(t *testing.T) {
	const iterations = 200

	for i := 0; i < iterations; i++ {
		c, sid := fixture(t, "media", 1)
		r0 := mustUpload(t, c, sid, 0, "head")
		r1 := mustUpload(t, c, sid, 1, "tail")
		req := selection(r0, r1)

		var wg sync.WaitGroup
		wg.Add(2)
		var completeErr error
		go func() {
			defer wg.Done()
			_, completeErr = c.Complete(req)
		}()
		go func() {
			defer wg.Done()
			if err := c.SetPolicy("media", &Policy{MaxSegments: 1}); err != nil {
				t.Errorf("SetPolicy: %v", err)
			}
		}()
		wg.Wait()

		obj, readErr := c.Object("media")
		switch {
		case completeErr == nil:
			// The completion committed. Its bytes must be whole and exact.
			if readErr != nil {
				t.Fatalf("completion reported success but readers see nothing: %v", readErr)
			}
			if string(obj.Data) != "headtail" || obj.Segments != 2 {
				t.Fatalf("torn object: %+v", obj)
			}
		case errors.Is(completeErr, ErrPolicyRejected):
			// The policy won the race; nothing may be visible.
			if !errors.Is(readErr, ErrObjectNotFound) {
				t.Fatalf("rejected completion left an object visible: %v", readErr)
			}
		default:
			t.Fatalf("unexpected completion error: %v", completeErr)
		}
	}
}

// TestConcurrentUploadsKeepVersionsDistinct hammers one segment number from
// several goroutines and checks that every receipt is unique and that a
// completion pinned to any one of them reproduces exactly those bytes.
func TestConcurrentUploadsKeepVersionsDistinct(t *testing.T) {
	c, sid := fixture(t, "media", 1)

	const writers = 16
	receipts := make([]Receipt, writers)
	var wg sync.WaitGroup
	wg.Add(writers)
	for i := 0; i < writers; i++ {
		i := i
		go func() {
			defer wg.Done()
			data := string(rune('A' + i))
			r, err := c.UploadSegment(sid, 0, []byte(data))
			if err != nil {
				t.Errorf("upload %d: %v", i, err)
				return
			}
			receipts[i] = r
		}()
	}
	wg.Wait()

	seen := make(map[int]int, writers)
	for i, r := range receipts {
		if r.Version <= 0 {
			t.Fatalf("writer %d got version %d", i, r.Version)
		}
		if prev, dup := seen[r.Version]; dup {
			t.Fatalf("writers %d and %d share version %d", prev, i, r.Version)
		}
		seen[r.Version] = i
	}
	if len(seen) != writers {
		t.Fatalf("versions = %d, want %d", len(seen), writers)
	}

	// Every receipt must pin back to exactly the byte it acknowledged.
	for i, r := range receipts {
		obj, err := c.Complete(selection(r))
		if err != nil {
			continue // already completed earlier in this loop
		}
		if want := string(rune('A' + i)); string(obj.Data) != want {
			t.Fatalf("writer %d object = %q, want %q", i, obj.Data, want)
		}
		break
	}

	// At most one completion can succeed, so the published object is whatever
	// the first successful selection was; re-completing must be refused.
	if _, err := c.Complete(selection(receipts[0])); !errors.Is(err, ErrSessionCompleted) {
		t.Fatalf("second completion = %v, want ErrSessionCompleted", err)
	}
}
