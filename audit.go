package seatledger

import "sort"

// BranchExplanation accounts for every seat one branch is involved with.
type BranchExplanation struct {
	Branch string
	Issued int // seats HQ allocated to this branch
	// Seats currently owned and explained locally:
	Available int
	InUse     int
	// Seats frozen for outgoing transfers the peer has not accepted yet
	// (in flight, still owned by this branch):
	InFlightOut int
	// Seats frozen here but already accepted by the peer; they are
	// explained at the destination and only cross-referenced here:
	CreditedElsewhere int
}

// AuditReport is the result of expanding all branches.
type AuditReport struct {
	Issued    int // total seats ever allocated by HQ
	Explained int // seats accounted for across all branches
	// OK is true when every issued seat is explained exactly once, i.e.
	// Explained == Issued.
	OK       bool
	Branches []BranchExplanation
}

// Audit expands all given branches and explains every seat as in use,
// available, or in flight, verifying that the total equals the initial
// issuance. All branches that exchange transfers must be included.
func Audit(ledgers ...*Ledger) AuditReport {
	accepted := map[string]bool{}
	for _, l := range ledgers {
		for id := range l.acceptedIn {
			accepted[id] = true
		}
	}

	rep := AuditReport{}
	for _, l := range ledgers {
		be := BranchExplanation{
			Branch:    l.name,
			Issued:    l.issued,
			Available: l.available,
			InUse:     l.inUse,
		}
		rep.Issued += l.issued
		rep.Explained += l.available + l.inUse
		for id, o := range l.frozenOut {
			if accepted[id] {
				// Ownership already moved to the receiver; the seats are
				// counted in the receiver's available/in-use share.
				be.CreditedElsewhere += o.Count
			} else {
				be.InFlightOut += o.Count
				rep.Explained += o.Count
			}
		}
		rep.Branches = append(rep.Branches, be)
	}
	sort.Slice(rep.Branches, func(i, j int) bool { return rep.Branches[i].Branch < rep.Branches[j].Branch })
	rep.OK = rep.Issued == rep.Explained
	return rep
}
