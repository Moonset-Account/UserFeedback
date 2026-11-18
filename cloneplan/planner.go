package cloneplan

import (
	"fmt"
	"sort"
)

// BuildPlan resolves the clone range through the snapshot chain and applies
// the destination-tenant publication policy.
//
// A non-nil error indicates the request itself is invalid (unknown snapshot,
// malformed receipt, receipt against a non-effective version, ...). When the
// request is valid but the policy cannot allow some segment, the plan is
// returned together with ErrPublicationBlocked and its BlockedReasons are
// populated; the plan cannot be staged or committed.
func (s *Store) BuildPlan(req PlanRequest) (*ClonePlan, error) {
	if req.TargetTenant == "" {
		return nil, ErrEmptyTenant
	}
	if !req.CloneRange.Valid() {
		return nil, fmt.Errorf("%w: clone range %d..%d", ErrInvalidRange, req.CloneRange.Start, req.CloneRange.End)
	}

	s.mu.RLock()
	defer s.mu.RUnlock()

	if _, ok := s.snapshots[req.Leaf]; !ok {
		return nil, fmt.Errorf("%w: %s", ErrSnapshotNotFound, req.Leaf.String())
	}

	if err := validateReceipts(req); err != nil {
		return nil, err
	}

	chain, err := chainLocked(s, req.Leaf)
	if err != nil {
		return nil, err
	}

	// Resolve every block in the clone range through the parent chain.
	resolved := resolveLocked(chain, s.snapshots, req.CloneRange)

	// Every receipt must correspond to the block version actually taking
	// effect over its covered range, and must not target non-data ranges
	// (explicit zero layers, sparse holes).
	if err := validateReceiptCoverage(req.Receipts, resolved); err != nil {
		return nil, err
	}

	planned := planSegments(req, resolved)
	plan := &ClonePlan{
		PlanID:       s.nextPlanIDLocked(),
		TargetTenant: req.TargetTenant,
		Leaf:         req.Leaf,
		CloneRange:   req.CloneRange,
		PinnedChain:  append([]SnapshotRef(nil), chain...),
		Resolved:     resolved,
		Segments:     planned,
		Receipts:     append([]SanitizationReceipt(nil), req.Receipts...),
	}
	for _, seg := range planned {
		if seg.Decision == DecisionBlocked {
			plan.BlockedReasons = append(plan.BlockedReasons,
				fmt.Sprintf("%s: range %d..%d classification=%s owner=%s",
					seg.Reason, seg.Range.Start, seg.Range.End, seg.Classification, seg.OwnerTenant))
		}
	}

	if plan.Blocked() {
		return plan, ErrPublicationBlocked
	}
	return plan, nil
}

func validateReceipts(req PlanRequest) error {
	seen := make(map[string]struct{}, len(req.Receipts))
	for _, rc := range req.Receipts {
		if rc.ReceiptID == "" {
			return fmt.Errorf("%w: receipt id is empty", ErrInvalidRange)
		}
		if _, dup := seen[rc.ReceiptID]; dup {
			return fmt.Errorf("%w: %s", ErrDuplicateReceiptID, rc.ReceiptID)
		}
		seen[rc.ReceiptID] = struct{}{}
		if !rc.Range.Valid() {
			return fmt.Errorf("%w: receipt %s range %d..%d", ErrInvalidRange, rc.ReceiptID, rc.Range.Start, rc.Range.End)
		}
		if rc.TargetTenant != req.TargetTenant {
			return fmt.Errorf("%w: receipt %s tenant=%s want=%s",
				ErrReceiptTenantMismatch, rc.ReceiptID, rc.TargetTenant, req.TargetTenant)
		}
		if !rangeContained(rc.Range, req.CloneRange) {
			return fmt.Errorf("%w: receipt %s range %d..%d not inside %d..%d",
				ErrReceiptOutOfRange, rc.ReceiptID, rc.Range.Start, rc.Range.End,
				req.CloneRange.Start, req.CloneRange.End)
		}
	}
	return nil
}

func chainLocked(s *Store, leaf SnapshotRef) ([]SnapshotRef, error) {
	var chain []SnapshotRef
	visited := map[SnapshotRef]bool{}
	cur := &leaf
	for cur != nil {
		if visited[*cur] {
			return nil, fmt.Errorf("%w: %s", ErrSnapshotCycle, cur.String())
		}
		entry, ok := s.snapshots[*cur]
		if !ok {
			return nil, fmt.Errorf("%w: %s", ErrSnapshotNotFound, cur.String())
		}
		visited[*cur] = true
		chain = append(chain, entry.ref)
		cur = entry.parent
	}
	return chain, nil
}

// validateReceiptCoverage enforces the cross-constraint that every receipt
// maps onto a real, effective block version. Receipts covering explicit zero
// layers, sparse holes, a different version, or a range boundary splitting a
// data segment from a non-data segment are rejected.
func validateReceiptCoverage(receipts []SanitizationReceipt, resolved []ResolvedSegment) error {
	for _, rc := range receipts {
		cursor := rc.Range.Start
		for _, seg := range resolved {
			if !seg.Range.overlaps(rc.Range) {
				continue
			}
			overlap, ok := seg.Range.intersect(rc.Range)
			if !ok {
				continue
			}
			if overlap.Start != cursor {
				return fmt.Errorf("%w: receipt %s covers non-contiguous range", ErrReceiptStaleVersion, rc.ReceiptID)
			}
			cursor = overlap.End
			if seg.Zero {
				return fmt.Errorf("%w: receipt %s over %d..%d (%s)",
					ErrReceiptCoversZero, rc.ReceiptID, seg.Range.Start, seg.Range.End, seg.Reason)
			}
			if seg.EffectiveVersion != rc.BlockVersion {
				return fmt.Errorf("%w: receipt %s expects %s but effective version over %d..%d is %s",
					ErrReceiptStaleVersion, rc.ReceiptID, rc.BlockVersion,
					seg.Range.Start, seg.Range.End, seg.EffectiveVersion)
			}
		}
		if cursor != rc.Range.End {
			return fmt.Errorf("%w: receipt %s covers range with no effective block", ErrReceiptStaleVersion, rc.ReceiptID)
		}
	}
	return nil
}

// resolveLocked walks the chain leaf-to-root. A range with no record in the
// current layer is a hole and consulted on the ancestor; explicit zero
// records terminate resolution immediately with zero semantics preserved.
func resolveLocked(chain []SnapshotRef, snaps map[SnapshotRef]*snapshotEntry, r Range) []ResolvedSegment {
	leaf := snaps[chain[0]]
	var out []ResolvedSegment
	cursor := r.Start
	for cursor < r.End {
		var rec *BlockRecord
		for i := range leaf.records {
			cand := &leaf.records[i]
			if cand.Range.Start <= cursor && cursor < cand.Range.End {
				rec = cand
				break
			}
		}
		if rec == nil {
			// Hole at this layer: find the next layer record boundary.
			next := r.End
			for i := range leaf.records {
				if leaf.records[i].Range.Start > cursor && leaf.records[i].Range.Start < next {
					next = leaf.records[i].Range.Start
				}
			}
			out = append(out, resolveParentLocked(chain[1:], snaps, Range{Start: cursor, End: minU64(next, r.End)})...)
			cursor = minU64(next, r.End)
			continue
		}

		end := minU64(rec.Range.End, r.End)
		if rec.Zero {
			ref := chain[0]
			out = append(out, ResolvedSegment{
				Range:        Range{Start: cursor, End: end},
				Origin:       &ref,
				Zero:         true,
				ExplicitZero: true,
				Reason:       ReasonExplicitZero,
			})
		} else {
			ref := chain[0]
			out = append(out, ResolvedSegment{
				Range:            Range{Start: cursor, End: end},
				Origin:           &ref,
				EffectiveVersion: rec.BlockVersion,
				Classification:   rec.Classification,
				OwnerTenant:      rec.OwnerTenant,
				Discardable:      rec.Discardable,
				Reason:           ReasonResolved,
			})
		}
		cursor = end
	}
	return out
}

// resolveParentLocked resolves a hole against ancestors; a hole that no
// ancestor ever fills terminates as sparse-hole zero.
func resolveParentLocked(ancestors []SnapshotRef, snaps map[SnapshotRef]*snapshotEntry, r Range) []ResolvedSegment {
	if len(ancestors) == 0 {
		return []ResolvedSegment{{
			Range:  r,
			Origin: nil,
			Zero:   true,
			Reason: ReasonSparseHole,
		}}
	}
	layer := snaps[ancestors[0]]
	cursor := r.Start
	var out []ResolvedSegment
	for cursor < r.End {
		var rec *BlockRecord
		for i := range layer.records {
			cand := &layer.records[i]
			if cand.Range.Start <= cursor && cursor < cand.Range.End {
				rec = cand
				break
			}
		}
		if rec == nil {
			next := r.End
			for i := range layer.records {
				if layer.records[i].Range.Start > cursor && layer.records[i].Range.Start < next {
					next = layer.records[i].Range.Start
				}
			}
			out = append(out, resolveParentLocked(ancestors[1:], snaps, Range{Start: cursor, End: minU64(next, r.End)})...)
			cursor = minU64(next, r.End)
			continue
		}
		end := minU64(rec.Range.End, r.End)
		if rec.Zero {
			ref := ancestors[0]
			out = append(out, ResolvedSegment{
				Range:        Range{Start: cursor, End: end},
				Origin:       &ref,
				Zero:         true,
				ExplicitZero: true,
				Reason:       ReasonExplicitZero,
			})
		} else {
			ref := ancestors[0]
			out = append(out, ResolvedSegment{
				Range:            Range{Start: cursor, End: end},
				Origin:           &ref,
				EffectiveVersion: rec.BlockVersion,
				Classification:   rec.Classification,
				OwnerTenant:      rec.OwnerTenant,
				Discardable:      rec.Discardable,
				Reason:           ReasonResolved,
			})
		}
		cursor = end
	}
	return out
}

// planSegments applies the publication policy. Segments are additionally split
// at receipt boundaries so that "multiple receipts cover one interval" and
// partially covered ranges get a uniform decision per sub-segment.
func planSegments(req PlanRequest, resolved []ResolvedSegment) []PlannedSegment {
	bounds := []uint64{req.CloneRange.Start}
	for _, rc := range req.Receipts {
		bounds = append(bounds, rc.Range.Start, rc.Range.End)
	}
	bounds = append(bounds, req.CloneRange.End)
	sort.Slice(bounds, func(i, j int) bool { return bounds[i] < bounds[j] })
	bounds = dedupU64(bounds)

	var out []PlannedSegment
	for _, seg := range resolved {
		var cuts []uint64
		for _, b := range bounds {
			if seg.Range.Start < b && b < seg.Range.End {
				cuts = append(cuts, b)
			}
		}
		part := seg.Range
		cursor := part.Start
		if len(cuts) == 0 {
			out = append(out, planOneSegment(req, seg, part))
			continue
		}
		for _, cut := range cuts {
			out = append(out, planOneSegment(req, seg, Range{Start: cursor, End: cut}))
			cursor = cut
		}
		out = append(out, planOneSegment(req, seg, Range{Start: cursor, End: part.End}))
	}
	return out
}

func planOneSegment(req PlanRequest, seg ResolvedSegment, r Range) PlannedSegment {
	base := PlannedSegment{
		Range:          r,
		Origin:         seg.Origin,
		BlockVersion:   seg.EffectiveVersion,
		Classification: seg.Classification,
		OwnerTenant:    seg.OwnerTenant,
		Discardable:    seg.Discardable,
		Zero:           seg.Zero,
		ExplicitZero:   seg.ExplicitZero,
	}

	// Explicit zero layers and sparse holes are zero content: always safe to
	// publish, zero semantics preserved.
	if seg.Zero {
		base.Decision = DecisionZero
		base.Reason = seg.Reason
		return base
	}

	// Blocks owned by the destination tenant may be published as-is.
	if seg.OwnerTenant == req.TargetTenant {
		base.Decision = DecisionPublish
		base.Reason = "owned-by-target"
		return base
	}

	hasReceipt := effectiveReceipt(req.Receipts, r, seg.EffectiveVersion)

	if seg.Classification == ClassSensitive {
		if hasReceipt {
			base.Decision = DecisionPublish
			base.Reason = "sanitization-receipt"
			return base
		}
		if seg.Discardable {
			// Sensitive data the source tenant permits to discard is zeroed
			// instead of being exposed.
			base.Decision = DecisionZero
			base.Reason = "discardable-sensitive-zeroed"
			return base
		}
		// Non-discardable sensitive data without a receipt blocks publication.
		base.Decision = DecisionBlocked
		base.Reason = BlockedMissingReceipt
		return base
	}

	// Public/internal data the target does not own requires a receipt; the
	// problem only grants publication rights via ownership or sanitization.
	if hasReceipt {
		base.Decision = DecisionPublish
		base.Reason = "sanitization-receipt"
		return base
	}
	base.Decision = DecisionBlocked
	base.Reason = BlockedMissingReceipt
	return base
}

// effectiveReceipt reports whether some fully-covering receipt exists whose
// block version matches the version effective over r. Structural validity
// (range, tenant, version against the whole resolved layer) was enforced
// earlier; this rechecks versions defensively.
func effectiveReceipt(receipts []SanitizationReceipt, r Range, version string) bool {
	covered := map[uint64]bool{}
	for _, rc := range receipts {
		if rc.BlockVersion != version {
			continue
		}
		ov, ok := rc.Range.intersect(r)
		if !ok {
			continue
		}
		for b := ov.Start; b < ov.End; b++ {
			covered[b] = true
		}
	}
	for b := r.Start; b < r.End; b++ {
		if !covered[b] {
			return false
		}
	}
	return true
}

func dedupU64(in []uint64) []uint64 {
	if len(in) == 0 {
		return in
	}
	out := in[:1]
	for _, v := range in[1:] {
		if v != out[len(out)-1] {
			out = append(out, v)
		}
	}
	return out
}
