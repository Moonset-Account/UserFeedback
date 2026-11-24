package graphwalk

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
)

func TestCheckpointAtEveryPrefixInterleaving(t *testing.T) {
	_, cat, q := lazyTestFixture(t)
	want := [][]EdgeID{{1}, {2}, {3}, {9}}

	for cut := 0; cut <= 4; cut++ {
		t.Run(fmt.Sprintf("cut-%d", cut), func(t *testing.T) {
			source := NewMemoryReader()
			for _, p := range cat.Pages() {
				// Recreate the same synthetic details in the pre-crash reader.
				switch p.ID {
				case "cheap":
					source.Put(p, []Edge{
						{ID: 1, From: "s", To: "t", Label: "a", Cost: 1},
						{ID: 2, From: "s", To: "t", Label: "a", Cost: 1},
						{ID: 3, From: "s", To: "t", Label: "a", Cost: 2},
					})
				case "isolated":
					source.Put(p, []Edge{{ID: 9, From: "s", To: "t", Label: "a", Cost: 1000}})
				}
			}
			c, err := NewCursor(q, 4, cat, source.ReadPage)
			if err != nil {
				t.Fatal(err)
			}
			collect(t, c, cut)
			data, err := c.MarshalCheckpoint()
			if err != nil {
				t.Fatal(err)
			}

			recoveryStore := NewMemoryReader()
			for _, p := range cat.Pages() {
				switch p.ID {
				case "cheap":
					recoveryStore.Put(p, []Edge{
						{ID: 1, From: "s", To: "t", Label: "a", Cost: 1},
						{ID: 2, From: "s", To: "t", Label: "a", Cost: 1},
						{ID: 3, From: "s", To: "t", Label: "a", Cost: 2},
					})
				case "isolated":
					recoveryStore.Put(p, []Edge{{ID: 9, From: "s", To: "t", Label: "a", Cost: 1000}})
				}
			}
			restored, err := RestoreCursor(data, RestoreOptions{Reader: recoveryStore.ReadPage})
			if err != nil {
				t.Fatal(err)
			}
			collect(t, restored, 4-cut)
			got := restored.Results()
			if len(got) != 4 {
				t.Fatalf("cut=%d restored rank=%d, want4", cut, len(got))
			}
			for i := range want {
				if len(got[i].IDs) != 1 || got[i].IDs[0] != want[i][0] {
					t.Fatalf("cut=%d rank%d=%v, want edge %v", cut, i+1, got[i].IDs, want[i])
				}
			}
		})
	}
}

func TestConcurrentNextCallsYieldUniqueOrderedPrefix(t *testing.T) {
	r := NewMemoryReader()
	page := PageInfo{ID: "loop", Version: "v1", From: "s", MinLabel: "a", MaxLabel: "a", MinCost: 0}
	r.Put(page, []Edge{
		{ID: 1, From: "s", To: "s", Label: "a", Cost: 0},
		{ID: 2, From: "s", To: "s", Label: "a", Cost: 0},
	})
	cat := NewCatalog("concurrent", page)
	q := Query{Source: "s", Target: "s", Expr: Star(Lit("a")), MaxHops: 3}
	c, err := NewCursor(q, 8, cat, r.ReadPage)
	if err != nil {
		t.Fatal(err)
	}

	var wg sync.WaitGroup
	var mu sync.Mutex
	seen := map[string]bool{}
	errs := make(chan error, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			w, err := c.Next(context.Background())
			if err != nil {
				errs <- err
				return
			}
			mu.Lock()
			defer mu.Unlock()
			key := sequenceSignature(w.IDs)
			if seen[key] {
				errs <- errDuplicateWalk
				return
			}
			seen[key] = true
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if len(seen) != 8 {
		t.Fatalf("concurrent unique results = %d, want8", len(seen))
	}

	// With all eight calls serialized, the emitted prefix is exactly the
	// deterministic first eight from the sequential oracle test.
	got := c.Results()
	assertIDs(t, got,
		[]EdgeID{}, []EdgeID{1}, []EdgeID{2}, []EdgeID{1, 1},
		[]EdgeID{1, 2}, []EdgeID{2, 1}, []EdgeID{2, 2}, []EdgeID{1, 1, 1},
	)
}

var errDuplicateWalk = errors.New("duplicate walk returned concurrently")
