package barrier

func cloneReceiptRecord(r *ReceiptRecord) *ReceiptRecord {
	if r == nil {
		return nil
	}
	c := *r
	return &c
}

func cloneReceiptRecords(rs []ReceiptRecord) []ReceiptRecord {
	if rs == nil {
		return nil
	}
	out := make([]ReceiptRecord, len(rs))
	copy(out, rs)
	return out
}

func cloneProofRequests(reqs []ProofRequest) []ProofRequest {
	if reqs == nil {
		return nil
	}
	out := make([]ProofRequest, len(reqs))
	for i, req := range reqs {
		out[i] = req
		out[i].Principals = append([]ParticipantID(nil), req.Principals...)
		out[i].DirectSubmitters = append([]ParticipantID(nil), req.DirectSubmitters...)
		out[i].ProxySubmitters = append([]ParticipantID(nil), req.ProxySubmitters...)
		out[i].AcceptedSubmitters = append([]ParticipantID(nil), req.AcceptedSubmitters...)
	}
	return out
}

func cloneMissingProof(mp MissingProof) MissingProof {
	mp.EligibleLogicalIDs = append([]ParticipantID(nil), mp.EligibleLogicalIDs...)
	mp.AwaitingLogicalIDs = append([]ParticipantID(nil), mp.AwaitingLogicalIDs...)
	mp.Requests = cloneProofRequests(mp.Requests)
	return mp
}

func cloneContributions(cs []Contribution) []Contribution {
	if cs == nil {
		return nil
	}
	out := make([]Contribution, len(cs))
	for i, c := range cs {
		out[i] = c
		out[i].Receipts = cloneReceiptRecords(c.Receipts)
	}
	return out
}

func cloneGroupStatus(st *GroupStatus) *GroupStatus {
	if st == nil {
		return nil
	}
	c := *st
	c.Proofs = cloneContributions(st.Proofs)
	if st.InvalidReceipts != nil {
		c.InvalidReceipts = append([]InvalidReceipt(nil), st.InvalidReceipts...)
	}
	c.PublishedRelease = cloneGroupReleaseRecord(st.PublishedRelease)
	if st.Missing != nil {
		mp := cloneMissingProof(*st.Missing)
		c.Missing = &mp
	}
	return &c
}

func cloneEvaluation(e *Evaluation) *Evaluation {
	if e == nil {
		return nil
	}
	c := *e
	c.ReleasableGroups = append([]GroupID(nil), e.ReleasableGroups...)
	if e.Groups != nil {
		c.Groups = make([]GroupStatus, len(e.Groups))
		for i := range e.Groups {
			c.Groups[i] = *cloneGroupStatus(&e.Groups[i])
		}
	}
	if e.Missing != nil {
		c.Missing = make([]MissingProof, len(e.Missing))
		for i, mp := range e.Missing {
			c.Missing[i] = cloneMissingProof(mp)
		}
	}
	return &c
}

func cloneGroupReleaseRecord(r *GroupReleaseRecord) *GroupReleaseRecord {
	if r == nil {
		return nil
	}
	c := *r
	c.Proofs = cloneContributions(r.Proofs)
	c.SourceReceipts = cloneReceiptRecords(r.SourceReceipts)
	return &c
}

func cloneReleaseRecord(r *ReleaseRecord) *ReleaseRecord {
	if r == nil {
		return nil
	}
	c := *r
	if r.Groups != nil {
		c.Groups = make([]GroupReleaseRecord, len(r.Groups))
		for i := range r.Groups {
			c.Groups[i] = *cloneGroupReleaseRecord(&r.Groups[i])
		}
	}
	c.SourceReceipts = cloneReceiptRecords(r.SourceReceipts)
	return &c
}
