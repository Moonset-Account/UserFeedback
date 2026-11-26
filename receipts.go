package slotcheck

import "sort"

func (c *checker) reconcileReissues() {
	// A chunk ID is normally delayed once. Record which later slots claim to
	// have reissued it so that a delay without a prefilled Reissue coordinate
	// can still be matched to its receipt.
	claims := map[string][]SlotRef{}
	for _, st := range c.sortedStates() {
		if st.record == nil {
			continue
		}
		for _, id := range st.record.ReissuedChunkIDs {
			delays := c.delaysByChunk[id]
			if len(delays) == 0 {
				st.addConfirmation(Finding{
					Code:    CodeUnknownReissuedChunk,
					Kind:    "confirmation",
					Message: "slot claims to reissue a chunk that is absent from the delayed-chunk ledger",
					ChunkID: id,
				})
				continue
			}
			claims[id] = append(claims[id], st.ref)
			for _, d := range delays {
				if d.Reissue != nil && *d.Reissue != st.ref {
					other := *d.Reissue
					st.addConfirmation(Finding{
						Code:      CodeReceiptReissueMismatch,
						Kind:      "confirmation",
						Message:   "recorded reissue slot does not match delayed-chunk ledger",
						ChunkID:   id,
						OtherSlot: &other,
					})
				}
			}
		}
	}
	c.reissueClaims = claims
}

func (c *checker) reconcileReceipts() {
	if c.usedReceiptIDs == nil {
		c.usedReceiptIDs = map[string]bool{}
	}

	for _, d := range c.in.DelayedChunks {
		origin := c.ensure(d.Origin)
		if !origin.policy.requireReceipts() {
			continue
		}

		effectiveReissue := d.Reissue
		if effectiveReissue == nil && len(c.reissueClaims[d.ChunkID]) == 1 {
			ref := c.reissueClaims[d.ChunkID][0]
			effectiveReissue = &ref
		}

		var receipt *Receipt
		if d.ClaimedReceiptID != "" {
			r, ok := c.receiptsByID[d.ClaimedReceiptID]
			if !ok {
				f := Finding{
					Code:      CodeClaimedReceiptMissing,
					Message:   "receipt ID claimed by delayed chunk does not exist",
					ChunkID:   d.ChunkID,
					ReceiptID: d.ClaimedReceiptID,
				}
				c.addDelayGap(d, effectiveReissue, f)
				continue
			}
			receipt = &r
			c.usedReceiptIDs[r.ID] = true
		} else {
			candidates := c.receiptsByChunk[d.ChunkID]
			switch len(candidates) {
			case 0:
				f := Finding{
					Code:    CodeReceiptMissing,
					Message: "cross-slot delayed chunk has no make-up receipt",
					ChunkID: d.ChunkID,
				}
				c.addDelayGap(d, effectiveReissue, f)
				continue
			case 1:
				r := candidates[0]
				receipt = &r
				c.usedReceiptIDs[r.ID] = true
			default:
				matches := c.filterReceiptCandidates(candidates, d, effectiveReissue)
				if len(matches) == 1 {
					r := matches[0]
					receipt = &r
					c.usedReceiptIDs[r.ID] = true
				} else {
					f := Finding{
						Code:    CodeAmbiguousReceipt,
						Message: "more than one receipt matches delayed chunk and no unique correspondence can be proved",
						ChunkID: d.ChunkID,
					}
					c.addDelayGap(d, effectiveReissue, f)
					continue
				}
			}
		}

		if receipt.ChunkID != d.ChunkID {
			c.addDelayGap(d, effectiveReissue, Finding{
				Code:      CodeReceiptChunkMismatch,
				Message:   "receipt is bound to a different chunk ID",
				ChunkID:   d.ChunkID,
				ReceiptID: receipt.ID,
				Expected:  0,
				Actual:    0,
			})
		}
		if receipt.Bytes != d.Bytes {
			c.addDelayGap(d, effectiveReissue, Finding{
				Code:      CodeReceiptBytesMismatch,
				Message:   "receipt byte count does not match delayed chunk",
				ChunkID:   d.ChunkID,
				ReceiptID: receipt.ID,
				Expected:  d.Bytes,
				Actual:    receipt.Bytes,
			})
		}
		if !refsEqualDirectionAware(receipt.Origin, d.Origin) {
			other := receipt.Origin
			c.addDelayGap(d, effectiveReissue, Finding{
				Code:      CodeReceiptOriginMismatch,
				Message:   "receipt origin slot does not match delayed chunk origin",
				ChunkID:   d.ChunkID,
				ReceiptID: receipt.ID,
				OtherSlot: &other,
			})
		}

		switch {
		case receipt.Reissue == nil:
			c.addDelayGap(d, effectiveReissue, Finding{
				Code:      CodeReceiptReissueMissing,
				Message:   "make-up receipt does not identify the slot in which delayed data was reissued",
				ChunkID:   d.ChunkID,
				ReceiptID: receipt.ID,
			})
		case d.Origin == *receipt.Reissue:
			other := *receipt.Reissue
			c.addDelayGap(d, effectiveReissue, Finding{
				Code:      CodeReceiptNotCrossSlot,
				Message:   "receipt claims reissue in the same slot as origin; this is not cross-slot delay evidence",
				ChunkID:   d.ChunkID,
				ReceiptID: receipt.ID,
				OtherSlot: &other,
			})
		case effectiveReissue != nil && *effectiveReissue != *receipt.Reissue:
			other := *receipt.Reissue
			c.addDelayGap(d, effectiveReissue, Finding{
				Code:      CodeReceiptReissueMismatch,
				Message:   "receipt reissue slot does not match delayed chunk or slot record",
				ChunkID:   d.ChunkID,
				ReceiptID: receipt.ID,
				OtherSlot: &other,
			})
		}

		if receipt.Reissue != nil {
			rs := c.ensure(*receipt.Reissue)
			if rs.record == nil {
				rs.report.EvidenceGaps = removeFindingCode(rs.report.EvidenceGaps, CodeMissingSlotRecord)
				rs.addEvidence(Finding{
					Code:      CodeReissueRecordMissing,
					Message:   "receipt references a reissue slot for which no transmission record was supplied",
					ChunkID:   d.ChunkID,
					ReceiptID: receipt.ID,
				})
			} else if len(rs.record.ReissuedChunkIDs) > 0 &&
				!containsString(rs.record.ReissuedChunkIDs, d.ChunkID) {
				rs.addConfirmation(Finding{
					Code:      CodeReissueNotRecorded,
					Message:   "receipt places chunk in this slot, but the slot's reissued-chunk list omits it",
					ChunkID:   d.ChunkID,
					ReceiptID: receipt.ID,
				})
			}
		}
	}

}

func (c *checker) reconcileOrphanReceipts() {
	// Receipts that correspond to neither a delayed chunk nor aggregate
	// backlog evidence are unlocated correspondence material. This runs
	// after aggregate backlog reconciliation so explicitly referenced
	// receipts are not falsely called orphans.
	for _, r := range c.in.Receipts {
		if len(c.delaysByChunk[r.ChunkID]) > 0 || c.usedReceiptIDs[r.ID] {
			continue
		}
		st := c.ensure(r.Origin)
		st.addConfirmation(Finding{
			Code:      CodeOrphanReceipt,
			Message:   "receipt has no matching delayed chunk or backlog receipt reference",
			ChunkID:   r.ChunkID,
			ReceiptID: r.ID,
		})
	}
}

func (c *checker) filterReceiptCandidates(in []Receipt, d DelayedChunk, reissue *SlotRef) []Receipt {
	var out []Receipt
	for _, r := range in {
		if r.Bytes != d.Bytes || !refsEqualDirectionAware(r.Origin, d.Origin) {
			continue
		}
		if reissue != nil && r.Reissue != nil && *r.Reissue != *reissue {
			continue
		}
		out = append(out, r)
	}
	return out
}

func (c *checker) addDelayGap(d DelayedChunk, reissue *SlotRef, f Finding) {
	origin := c.ensure(d.Origin)
	origin.addConfirmation(f)
	if reissue != nil {
		rs := c.ensure(*reissue)
		rs.addConfirmation(f)
	}
}

func (c *checker) reconcileBacklogReceipts() {
	if c.usedReceiptIDs == nil {
		c.usedReceiptIDs = map[string]bool{}
	}
	for _, st := range c.sortedStates() {
		r := st.record
		if r == nil || r.PriorBacklogBytes <= 0 || len(r.ReissuedChunkIDs) > 0 {
			continue
		}
		backlogSent := minInt(r.PriorBacklogBytes, r.RealBytes)
		if backlogSent <= 0 {
			continue
		}

		var receipts []Receipt
		if len(r.BacklogReceiptIDs) > 0 {
			for _, id := range r.BacklogReceiptIDs {
				rcpt, ok := c.receiptsByID[id]
				if !ok {
					st.addConfirmation(Finding{
						Code:      CodeClaimedReceiptMissing,
						Message:   "backlog receipt ID referenced by slot record does not exist",
						ReceiptID: id,
					})
					continue
				}
				receipts = append(receipts, rcpt)
				c.usedReceiptIDs[id] = true
			}
		} else {
			for _, rcpt := range c.in.Receipts {
				if rcpt.Reissue != nil && *rcpt.Reissue == st.ref {
					receipts = append(receipts, rcpt)
					c.usedReceiptIDs[rcpt.ID] = true
				}
			}
		}

		sum := 0
		for _, rcpt := range receipts {
			sum += rcpt.Bytes
		}
		if len(r.BacklogReceiptIDs) > 0 {
			if sum != backlogSent {
				st.addConfirmation(Finding{
					Code:     CodeBacklogReceiptMismatch,
					Message:  "referenced backlog receipt bytes do not equal prior backlog bytes sent",
					Expected: backlogSent,
					Actual:   sum,
				})
			}
		} else if sum < backlogSent {
			st.addConfirmation(Finding{
				Code:     CodeBacklogReceiptMissing,
				Message:  "prior backlog bytes sent in this slot are not fully covered by make-up receipts",
				Expected: backlogSent,
				Actual:   sum,
			})
		}
	}
}

func (c *checker) sortedStates() []*slotState {
	refs := make([]SlotRef, 0, len(c.states))
	for ref := range c.states {
		refs = append(refs, ref)
	}
	sort.Slice(refs, func(i, j int) bool {
		a, b := refs[i], refs[j]
		if a.Direction != b.Direction {
			return a.Direction < b.Direction
		}
		if a.Generation != b.Generation {
			return a.Generation < b.Generation
		}
		return a.Slot < b.Slot
	})
	out := make([]*slotState, 0, len(refs))
	for _, ref := range refs {
		out = append(out, c.states[ref])
	}
	return out
}

func refsEqualDirectionAware(a, b SlotRef) bool {
	if a.Generation != b.Generation || a.Slot != b.Slot {
		return false
	}
	if a.Direction == "" || b.Direction == "" {
		return true
	}
	return a.Direction == b.Direction
}

func containsString(xs []string, want string) bool {
	for _, x := range xs {
		if x == want {
			return true
		}
	}
	return false
}

func removeFindingCode(fs []Finding, code string) []Finding {
	out := fs[:0]
	for _, f := range fs {
		if f.Code != code {
			out = append(out, f)
		}
	}
	return out
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
