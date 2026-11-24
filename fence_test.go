package rebalancer

import (
	"errors"
	"testing"
)

// ---- 13. executable split-brain counter-example ---------------------------

func TestSplitBrainDoubleConsumptionCounterExample(t *testing.T) {
	// Two coordinator sites with the same durable log prefix. The last
	// durable round revoked partition 5 under generation 3; the next revoke
	// write may be lost, so the old holder c0 may still serve while both
	// sites issue the identical grant to c1 carrying generation 3.
	res, err := SimulateSplitBrain(5, 0, 1, 3, true)
	if err != nil {
		t.Fatalf("SimulateSplitBrain: %v", err)
	}

	// Unsafe world: the stale site's grant starts a second consumer while
	// the old holder is still serving -> double consumption at site-B.
	if !res.UnsafeDoubleConsumed {
		t.Fatalf("unfenced world must double-consume the partition")
	}
	if res.UnsafeSite != "site-B" {
		t.Fatalf("double consumption must be attributed to stale site-B, got %q",
			res.UnsafeSite)
	}

	// Safe world: the broker fence rejects the stale generation at one site
	// and the old holder remains the only consumer.
	if res.FencedRejection == "" {
		t.Fatalf("generation fence must reject at least one site")
	}
	if res.FencedDoubleConsumed {
		t.Fatalf("fenced world must never double-consume")
	}

	// Control case: when the old holder is known to be fenced off (not
	// serving anymore), granting is safe even without the broker fence.
	safe, err := SimulateSplitBrain(5, 0, 1, 3, false)
	if err != nil {
		t.Fatalf("SimulateSplitBrain(control): %v", err)
	}
	if safe.UnsafeDoubleConsumed || safe.FencedDoubleConsumed {
		t.Fatalf("no double consumption possible once old holder stopped serving")
	}
}

func TestSplitBrainValidation(t *testing.T) {
	if _, err := SimulateSplitBrain(0, 1, 1, 1, true); !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("same-holder split brain: want ErrInvalidInput, got %v", err)
	}
	if _, err := SimulateSplitBrain(0, 0, 1, 0, true); !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("non-positive generation: want ErrInvalidInput, got %v", err)
	}
}
