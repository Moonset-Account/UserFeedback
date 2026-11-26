package relaygate

import (
	"fmt"
	"sort"
)

type candidate struct {
	proof     KeepaliveProof
	grant     DirectionGrant
	ackExists bool
}

func (g *Adjudicator) evaluateLocked(record Record) Verdict {
	verdict := Verdict{RecordID: record.ID}

	if issues := validateRecord(record); len(issues) > 0 {
		verdict.Status = StatusDenied
		verdict.Attribution = &Attribution{Basis: BasisNone}
		verdict.Issues = issues
		for _, issue := range issues {
			if issue.Code == IssueMissingTrustedTime {
				verdict.MissingEvidence = append(verdict.MissingEvidence, MissingEvidence{
					Code:        NeedTrustedTime,
					Description: "record needs a trusted ingress timestamp to evaluate the lease window",
					Session:     record.Session,
				})
			}
		}
		return verdict
	}

	verdict.Attribution = &Attribution{
		Session:   record.Session,
		OwnerKey:  record.Session.String(),
		Direction: record.Direction,
		Basis:     BasisSessionClaim,
	}

	proofsBySequence := g.proofs[record.Session]
	sequences := candidateSequences(record.KeepaliveSequence, proofsBySequence)

	var admitted []candidate
	var awaitingConfirmation []candidate
	var outsideWindow []candidate
	var missing []MissingEvidence

	for _, sequence := range sequences {
		proof, proofExists := proofsBySequence[sequence]
		if !proofExists {
			seq := sequence
			missing = append(missing, MissingEvidence{
				Code:              NeedKeepaliveProof,
				Description:       "no verified keepalive proof with the required sequence",
				Session:           record.Session,
				KeepaliveSequence: &seq,
				Direction:         record.Direction,
			})
			continue
		}

		directionGrants := grantsForDirection(proof, record.Direction)
		if len(directionGrants) == 0 {
			seq := proof.Sequence
			missing = append(missing, MissingEvidence{
				Code:              NeedDirectionGrant,
				Description:       "keepalive proof does not explicitly list this direction",
				Session:           record.Session,
				KeepaliveSequence: &seq,
				Direction:         record.Direction,
			})
			continue
		}

		ack, ackExists := g.acks[ackRef{session: record.Session, sequence: proof.Sequence}]
		for _, grant := range directionGrants {
			if !grant.Window.Contains(record.TrustedAt) {
				outsideWindow = append(outsideWindow, candidate{proof: proof, grant: grant, ackExists: ackExists})
				continue
			}

			confirmed := !g.requireWake || (ackExists && confirmationCoversGrant(ack, grant))
			cand := candidate{proof: proof, grant: grant, ackExists: ackExists}
			if confirmed {
				admitted = append(admitted, cand)
			} else {
				awaitingConfirmation = append(awaitingConfirmation, cand)
			}
		}
	}

	if len(admitted) > 0 {
		best := bestCandidate(admitted)
		sequence := best.proof.Sequence
		window := best.grant.Window
		verdict.Status = StatusAdmitted
		verdict.Deliverable = true
		verdict.Attribution = &Attribution{
			Session:           record.Session,
			OwnerKey:          record.Session.String(),
			Direction:         record.Direction,
			KeepaliveSequence: &sequence,
			Window:            &window,
			Basis:             BasisConfirmedLease,
		}
		return verdict
	}

	if len(awaitingConfirmation) > 0 {
		best := bestCandidate(awaitingConfirmation)
		sequence := best.proof.Sequence
		window := best.grant.Window
		basis := BasisUnconfirmedLease

		code := NeedWakeConfirmation
		description := "lease covers the record time, but no wake confirmation references this proof sequence"
		if best.ackExists {
			code = NeedConfirmationScope
			description = "wake confirmation does not explicitly include this exact direction and proof window"
		}

		verdict.Status = StatusPending
		verdict.Attribution = &Attribution{
			Session:           record.Session,
			OwnerKey:          record.Session.String(),
			Direction:         record.Direction,
			KeepaliveSequence: &sequence,
			Window:            &window,
			Basis:             basis,
		}
		verdict.MissingEvidence = append(verdict.MissingEvidence, MissingEvidence{
			Code:              code,
			Description:       description,
			Session:           record.Session,
			KeepaliveSequence: &sequence,
			Direction:         record.Direction,
			Window:            &window,
		})
		return verdict
	}

	if len(outsideWindow) > 0 {
		best := bestCandidate(outsideWindow)
		sequence := best.proof.Sequence
		window := best.grant.Window
		issueCode := IssueLeaseExpired
		description := "record time is at or after the exclusive lease expiry"
		if record.TrustedAt.Before(window.NotBefore) {
			issueCode = IssueLeaseNotStarted
			description = "record time is before the lease NotBefore"
		}

		verdict.Status = StatusDenied
		verdict.Attribution = &Attribution{
			Session:           record.Session,
			OwnerKey:          record.Session.String(),
			Direction:         record.Direction,
			KeepaliveSequence: &sequence,
			Window:            &window,
			Basis:             BasisCandidateLease,
		}
		verdict.Issues = append(verdict.Issues, Issue{
			Code:        issueCode,
			Description: description,
			Session:     record.Session,
			Sequence:    &sequence,
			Direction:   record.Direction,
			Window:      &window,
		})
		verdict.MissingEvidence = append(verdict.MissingEvidence, MissingEvidence{
			Code:              NeedValidWindowAtTime,
			Description:       "an explicit direction lease containing the trusted record time is required",
			Session:           record.Session,
			KeepaliveSequence: &sequence,
			Direction:         record.Direction,
			Window:            &window,
		})
		return verdict
	}

	if len(missing) == 0 {
		// Defensive fallback: a valid record normally produces a missing
		// proof/direction item or enters one of the candidate buckets above.
		missing = append(missing, MissingEvidence{
			Code:        NeedKeepaliveProof,
			Description: "no usable keepalive evidence",
			Session:     record.Session,
			Direction:   record.Direction,
		})
	}

	if len(g.proofs) > 0 {
		verdict.Issues = append(verdict.Issues, g.coordinateMismatchIssuesLocked(record.Session)...)
	}

	verdict.Status = StatusPending
	verdict.MissingEvidence = append(verdict.MissingEvidence, missing...)
	return verdict
}

func confirmationCoversGrant(ack ackState, grant DirectionGrant) bool {
	return directionListContains(ack.directions, grant.Direction) ||
		grantListContains(ack.grants, grant)
}

func validateRecord(record Record) []Issue {
	var issues []Issue
	if !record.Direction.Valid() {
		issues = append(issues, Issue{
			Code:        IssueUnknownDirection,
			Description: fmt.Sprintf("direction %q is not supported", record.Direction),
			Direction:   record.Direction,
			Session:     record.Session,
		})
	}
	if record.TrustedAt.IsZero() {
		issues = append(issues, Issue{
			Code:        IssueMissingTrustedTime,
			Description: "TrustedAt must be supplied by a trusted ingress clock",
			Session:     record.Session,
			Direction:   record.Direction,
		})
	}
	return issues
}

func candidateSequences(requested *uint64, proofs map[uint64]KeepaliveProof) []uint64 {
	if requested != nil {
		return []uint64{*requested}
	}
	return sortedSequences(proofs)
}

func bestCandidate(candidates []candidate) candidate {
	best := candidates[0]
	for _, current := range candidates[1:] {
		if candidateLess(best, current) {
			best = current
		}
	}
	return best
}

// candidateLess reports whether right should be preferred over left. Higher
// proof sequence wins; for equal sequences the later-expiring explicit window
// wins, with start time as a final deterministic tie-breaker.
func candidateLess(left, right candidate) bool {
	if left.proof.Sequence != right.proof.Sequence {
		return left.proof.Sequence < right.proof.Sequence
	}
	if !left.grant.Window.ExpiresAt.Equal(right.grant.Window.ExpiresAt) {
		return left.grant.Window.ExpiresAt.Before(right.grant.Window.ExpiresAt)
	}
	if !left.grant.Window.NotBefore.Equal(right.grant.Window.NotBefore) {
		return left.grant.Window.NotBefore.Before(right.grant.Window.NotBefore)
	}
	return false
}

func (g *Adjudicator) coordinateMismatchIssuesLocked(wanted SessionKey) []Issue {
	var sessions []SessionKey
	for session := range g.proofs {
		if session != wanted {
			sessions = append(sessions, session)
		}
	}
	sort.Slice(sessions, func(i, j int) bool {
		if sessions[i].ConnectionGeneration != sessions[j].ConnectionGeneration {
			return sessions[i].ConnectionGeneration < sessions[j].ConnectionGeneration
		}
		return sessions[i].PeerStartNumber < sessions[j].PeerStartNumber
	})

	var issues []Issue
	for _, existing := range sessions {
		switch {
		case existing.ConnectionGeneration == wanted.ConnectionGeneration &&
			existing.PeerStartNumber != wanted.PeerStartNumber:
			related := existing
			issues = append(issues, Issue{
				Code:           IssuePeerStartMismatch,
				Description:    "a proof exists for the same connection generation but a different peer start number",
				Session:        wanted,
				RelatedSession: &related,
			})
		case existing.PeerStartNumber == wanted.PeerStartNumber &&
			existing.ConnectionGeneration != wanted.ConnectionGeneration:
			related := existing
			issues = append(issues, Issue{
				Code:           IssueGenerationMismatch,
				Description:    "a proof exists for the same peer start number but a different connection generation",
				Session:        wanted,
				RelatedSession: &related,
			})
		}
	}
	return issues
}
