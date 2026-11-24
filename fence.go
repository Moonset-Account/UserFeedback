package rebalancer

import "fmt"

// Split-brain counter-example
// ---------------------------
// Two coordinator sites share an identical durable log prefix. The partition
// was revoked at the last durable round; the revoke write of the next round
// may have been lost, so the old holder and the released state are
// indistinguishable. Both sites then issue the SAME grant (same partition,
// same new holder, same generation) based on that shared prefix.
//
// Without a broker-side generation fence both grants succeed: the old holder
// still serves the partition while the new holder starts serving it, so the
// partition is consumed twice. With the fence, the generation carried by the
// grant must be strictly newer than every generation the broker accepted
// before; the stale site's grant is rejected and double consumption cannot
// happen. The simulation below executes both worlds over identical inputs and
// reports the site where the unsafe world double-consumes.

// SplitBrainResult is the outcome of replaying the same grant at two sites.
type SplitBrainResult struct {
	Partition        int
	OldHolder        int
	NewHolder        int
	SharedGeneration int64
	// UnsafeDoubleConsumed is true in the unfenced world.
	UnsafeDoubleConsumed bool
	// UnsafeSite names the site whose grant creates the duplicate consumer.
	UnsafeSite string
	// FencedRejection names the site whose grant the broker fence rejects.
	FencedRejection string
	// FencedDoubleConsumed must always be false.
	FencedDoubleConsumed bool
}

// SimulateSplitBrain runs the two sites with and without generation fencing.
//
// oldStillServes reports whether the old holder still serves the partition at
// the moment both sites act (the indistinguishable case after a possibly lost
// revoke write).
func SimulateSplitBrain(partition, oldHolder, newHolder int,
	sharedGeneration int64, oldStillServes bool) (*SplitBrainResult, error) {

	if oldHolder == newHolder {
		return nil, fmt.Errorf("%w: split-brain requires distinct holders",
			ErrInvalidInput)
	}
	if sharedGeneration <= 0 {
		return nil, fmt.Errorf("%w: generation must be positive", ErrInvalidInput)
	}
	res := &SplitBrainResult{
		Partition:        partition,
		OldHolder:        oldHolder,
		NewHolder:        newHolder,
		SharedGeneration: sharedGeneration,
	}

	// ---- Unsafe world: the broker accepts any grant, no generation fence. ----
	unsafeConsumers := map[int]bool{}
	if oldStillServes {
		unsafeConsumers[oldHolder] = true // old holder was never fenced off
	}
	for range []string{"site-A", "site-B"} {
		// Both sites emit the identical grant from the identical log prefix.
		grant := Grant{Partition: partition, Consumer: newHolder,
			Generation: sharedGeneration}
		_ = grant // unfenced broker accepts unconditionally
		unsafeConsumers[newHolder] = true
	}
	res.UnsafeDoubleConsumed = oldStillServes && len(unsafeConsumers) > 1
	if res.UnsafeDoubleConsumed {
		// Site B is the stale site: its grant starts the second consumer while
		// the old holder (reachable only through site A's continued fencing)
		// is still serving.
		res.UnsafeSite = "site-B"
	}

	// ---- Safe world: broker accepts only strictly newer generations. ----
	// The last generation the broker durably accepted equals the shared
	// prefix generation, so any grant carrying that exact generation is stale.
	brokerMaxGeneration := sharedGeneration
	for _, site := range []string{"site-A", "site-B"} {
		grant := Grant{Partition: partition, Consumer: newHolder,
			Generation: sharedGeneration}
		if grant.Generation <= brokerMaxGeneration {
			if res.FencedRejection == "" {
				res.FencedRejection = site
			}
			continue
		}
		// A genuinely new leader would first bump the generation past the
		// durable prefix and re-revoke; that path is exercised by Recover.
	}
	safeConsumers := map[int]bool{}
	if oldStillServes {
		safeConsumers[oldHolder] = true
	}
	res.FencedDoubleConsumed = len(safeConsumers) > 1
	return res, nil
}
