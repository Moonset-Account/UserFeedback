package barrier

import (
	"context"
	"fmt"
	"sort"
)

// DeclareBarrier declares a checkpoint and the groups whose thresholds must
// jointly release it. The returned evaluation describes current missing
// proofs. Declaring the same immutable barrier twice is an idempotent replay.
func (m *Manager) DeclareBarrier(runID RunID, cfg BarrierConfig) (*Evaluation, error) {
	if runID == "" || cfg.ID == "" {
		return nil, fmt.Errorf("%w: run id and barrier id are required", ErrInvalidRequest)
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	run, err := m.runLocked(runID)
	if err != nil {
		return nil, err
	}

	checkpoint := cfg.CheckpointID
	if checkpoint == "" {
		checkpoint = string(cfg.ID)
	}

	requirements := make(map[GroupID]GroupRequirement)
	order := make([]GroupID, 0)
	if len(cfg.Groups) == 0 {
		for _, gid := range run.groupOrder {
			g := run.groups[gid]
			requirements[gid] = GroupRequirement{GroupID: gid, Threshold: g.cfg.Threshold}
			order = append(order, gid)
		}
	} else {
		seen := make(map[GroupID]struct{}, len(cfg.Groups))
		for _, req := range cfg.Groups {
			if req.GroupID == "" {
				return nil, fmt.Errorf("%w: group id is required", ErrInvalidRequest)
			}
			g, err := run.groupLocked(req.GroupID)
			if err != nil {
				return nil, err
			}
			if _, dup := seen[req.GroupID]; dup {
				return nil, fmt.Errorf("%w: duplicate requirement for group %q", ErrInvalidRequest, req.GroupID)
			}
			seen[req.GroupID] = struct{}{}
			threshold := req.Threshold
			if threshold == 0 {
				threshold = g.cfg.Threshold
			}
			if threshold < 1 {
				return nil, fmt.Errorf("%w: group %q threshold must be >= 1", ErrInvalidConfig, req.GroupID)
			}
			req.Threshold = threshold
			requirements[req.GroupID] = req
			order = append(order, req.GroupID)
		}
	}

	if existing, ok := run.barriers[cfg.ID]; ok {
		if existing.checkpoint != checkpoint || !sameRequirements(existing.requirements, requirements) {
			return nil, fmt.Errorf("%w: barrier %q already exists with different checkpoint/groups", ErrBarrierExists, cfg.ID)
		}
		return m.evaluateLocked(run, existing), nil
	}

	b := &barrierState{
		cfg:           cfg,
		checkpoint:    checkpoint,
		requirements:  requirements,
		groupOrder:    order,
		receipts:      make([]*ReceiptRecord, 0),
		groupReleases: make(map[GroupID]*GroupReleaseRecord),
		createdAt:     m.now(),
	}
	run.barriers[cfg.ID] = b
	run.barrierOrder = append(run.barrierOrder, cfg.ID)
	return m.evaluateLocked(run, b), nil
}

// Evaluate recomputes every group against the current effective participant,
// mirror, proxy, revocation, and handoff state. It has no side effects.
func (m *Manager) Evaluate(runID RunID, barrierID BarrierID) (*Evaluation, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	run, b, err := m.barrierPairLocked(runID, barrierID)
	if err != nil {
		return nil, err
	}
	return cloneEvaluation(m.evaluateLocked(run, b)), nil
}

// SubmitPrepare is the domain-named alias for SubmitReceipt.
func (m *Manager) SubmitPrepare(receipt Receipt) (*SubmitReceiptResult, error) {
	return m.SubmitReceipt(receipt)
}

// SubmitReceipt accepts one prepare proof and returns its idempotent record
// plus the recomputed barrier evaluation.
func (m *Manager) SubmitReceipt(receipt Receipt) (*SubmitReceiptResult, error) {
	if receipt.RunID == "" || receipt.BarrierID == "" || receipt.GroupID == "" || receipt.SubmitterID == "" {
		return nil, fmt.Errorf("%w: run, barrier, group, and submitter ids are required", ErrInvalidRequest)
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	run, err := m.runLocked(receipt.RunID)
	if err != nil {
		return nil, err
	}

	normalized := receipt
	if normalized.SubjectID == "" {
		normalized.SubjectID = normalized.SubmitterID
	}

	// Idempotent retries are answered from the original accepted receipt.
	if receipt.ReceiptID != "" {
		if existing, ok := run.receipts[receipt.ReceiptID]; ok {
			if !sameReceiptRequest(existing.Receipt, normalized) {
				return nil, fmt.Errorf("%w: receipt id %q reused with different payload", ErrConflict, receipt.ReceiptID)
			}
			b, bErr := run.barrierLocked(receipt.BarrierID)
			var eval *Evaluation
			if bErr == nil {
				eval = cloneEvaluation(m.evaluateLocked(run, b))
			}
			return &SubmitReceiptResult{
				Receipt:    cloneReceiptRecord(existing),
				Duplicate:  true,
				Evaluation: eval,
			}, nil
		}
	}

	b, err := run.barrierLocked(receipt.BarrierID)
	if err != nil {
		return nil, err
	}
	if b.release != nil {
		return nil, fmt.Errorf("%w: %q", ErrBarrierReleased, receipt.BarrierID)
	}
	if _, ok := b.requirements[receipt.GroupID]; !ok {
		return nil, fmt.Errorf("%w: group %q is not required by barrier %q", ErrGroupNotRequired, receipt.GroupID, receipt.BarrierID)
	}
	if receipt.CheckpointID != b.checkpoint {
		return nil, fmt.Errorf("%w: receipt checkpoint %q does not match %q", ErrCheckpointMismatch, receipt.CheckpointID, b.checkpoint)
	}

	submitter, err := run.participantLocked(receipt.SubmitterID)
	if err != nil {
		return nil, err
	}
	if submitter.status != ParticipantActive {
		return nil, inactiveError(receipt.SubmitterID, submitter.status)
	}

	subjectID := normalized.SubjectID
	if subjectID == receipt.SubmitterID {
		if receipt.GrantID != "" {
			return nil, fmt.Errorf("%w: direct receipt must not carry proxy grant %q", ErrInvalidRequest, receipt.GrantID)
		}
		if !run.isCurrentGroupMember(receipt.SubmitterID, receipt.GroupID) {
			return nil, fmt.Errorf("submitter %q: %w", receipt.SubmitterID, ErrNotMember)
		}
	} else {
		subject, err := run.participantLocked(subjectID)
		if err != nil {
			return nil, err
		}
		if subject.status != ParticipantActive {
			return nil, inactiveError(subjectID, subject.status)
		}

		grantID, err := m.resolveGrantLocked(run, receipt, subjectID)
		if err != nil {
			return nil, err
		}
		normalized.GrantID = grantID

		roots := run.mirrorRootsLocked()
		logicalID := roots[subjectID]
		if !run.logicalIdentityInGroupLocked(logicalID, receipt.GroupID, roots) {
			return nil, fmt.Errorf("%w: subject %q (logical %q) is not represented in group %q", ErrNotMember, subjectID, logicalID, receipt.GroupID)
		}
	}

	id := receipt.ReceiptID
	if id == "" {
		for {
			run.nextOrder++
			candidate := ReceiptID(fmt.Sprintf("receipt-%d", run.nextOrder))
			if _, exists := run.receipts[candidate]; !exists {
				id = candidate
				break
			}
		}
	} else {
		run.nextOrder++
	}
	normalized.ReceiptID = id
	rec := &ReceiptRecord{
		Receipt:    normalized,
		Order:      run.nextOrder,
		ReceivedAt: m.now(),
	}
	run.receipts[id] = rec
	b.receipts = append(b.receipts, rec)
	run.notifyAllLocked()

	return &SubmitReceiptResult{
		Receipt:    cloneReceiptRecord(rec),
		Duplicate:  false,
		Evaluation: cloneEvaluation(m.evaluateLocked(run, b)),
	}, nil
}

func (m *Manager) resolveGrantLocked(run *runState, receipt Receipt, subjectID ParticipantID) (GrantID, error) {
	if receipt.GrantID != "" {
		grant, ok := run.grants[receipt.GrantID]
		if !ok {
			return "", fmt.Errorf("%w: %q", ErrGrantNotFound, receipt.GrantID)
		}
		if err := validateGrantForReceipt(&grant.rec, receipt.RunID, receipt.SubmitterID, subjectID, receipt.GroupID); err != nil {
			return "", err
		}
		return grant.rec.ID, nil
	}

	var candidates []GrantID
	for id, state := range run.grants {
		g := state.rec
		if g.Active && g.RunID == receipt.RunID && g.ProxyID == receipt.SubmitterID &&
			g.PrincipalID == subjectID && grantAllowsGroup(&g, receipt.GroupID) {
			candidates = append(candidates, id)
		}
	}
	if len(candidates) == 0 {
		return "", fmt.Errorf(
			"%w: %q is not authorized to prepare for %q in group %q",
			ErrUnauthorized, receipt.SubmitterID, subjectID, receipt.GroupID,
		)
	}
	sort.Slice(candidates, func(i, j int) bool { return candidates[i] < candidates[j] })
	chosen := candidates[0]
	grant := run.grants[chosen]
	if err := validateGrantForReceipt(&grant.rec, receipt.RunID, receipt.SubmitterID, subjectID, receipt.GroupID); err != nil {
		return "", err
	}
	return chosen, nil
}

func validateGrantForReceipt(g *Grant, runID RunID, proxy, principal ParticipantID, gid GroupID) error {
	if !g.Active {
		return fmt.Errorf("%w: grant %q", ErrRevoked, g.ID)
	}
	if g.RunID != runID || g.ProxyID != proxy || g.PrincipalID != principal {
		return fmt.Errorf("%w: grant %q does not match receipt", ErrUnauthorized, g.ID)
	}
	if !grantAllowsGroup(g, gid) {
		return fmt.Errorf("%w: grant %q does not cover group %q", ErrUnauthorized, g.ID, gid)
	}
	return nil
}

func (r *runState) isCurrentGroupMember(pid ParticipantID, gid GroupID) bool {
	g, ok := r.groups[gid]
	if !ok {
		return false
	}
	_, ok = g.members[pid]
	return ok
}

func (r *runState) logicalIdentityInGroupLocked(logical ParticipantID, gid GroupID, roots map[ParticipantID]ParticipantID) bool {
	g, ok := r.groups[gid]
	if !ok {
		return false
	}
	for pid := range g.members {
		if roots[pid] == logical {
			return true
		}
	}
	return false
}

func (m *Manager) barrierPairLocked(runID RunID, barrierID BarrierID) (*runState, *barrierState, error) {
	run, err := m.runLocked(runID)
	if err != nil {
		return nil, nil, err
	}
	b, err := run.barrierLocked(barrierID)
	if err != nil {
		return nil, nil, err
	}
	return run, b, nil
}

func (m *Manager) evaluateLocked(run *runState, b *barrierState) *Evaluation {
	now := m.now()
	roots := run.mirrorRootsLocked()
	eval := &Evaluation{
		RunID:           run.cfg.ID,
		BarrierID:       b.cfg.ID,
		CheckpointID:    b.checkpoint,
		TopologyVersion: run.topologyVersion,
		EvaluatedAt:     now,
	}

	for _, gid := range b.groupOrder {
		req := b.requirements[gid]
		group := run.groups[gid]
		status := GroupStatus{
			GroupID:   gid,
			Threshold: req.Threshold,
		}

		// Effective logical identities represented in this group.
		representatives := make(map[ParticipantID][]ParticipantID)
		for pid := range group.members {
			p := run.participants[pid]
			if p == nil || p.status != ParticipantActive {
				continue
			}
			lid := roots[pid]
			representatives[lid] = append(representatives[lid], pid)
		}
		logicalIDs := make([]ParticipantID, 0, len(representatives))
		for lid := range representatives {
			logicalIDs = append(logicalIDs, lid)
		}
		sortParticipantIDs(logicalIDs)
		for lid := range representatives {
			sortParticipantIDs(representatives[lid])
		}

		valid := make(map[ParticipantID][]ReceiptRecord)
		for _, recPtr := range b.receipts {
			rec := *recPtr
			if rec.GroupID != gid {
				continue
			}
			lid, invalid := m.validateReceiptEffectiveLocked(run, rec, roots, representatives)
			if invalid != nil {
				status.InvalidReceipts = append(status.InvalidReceipts, *invalid)
				continue
			}
			valid[lid] = append(valid[lid], rec)
		}

		for _, lid := range logicalIDs {
			recs := valid[lid]
			if len(recs) == 0 {
				continue
			}
			sort.SliceStable(recs, func(i, j int) bool { return recs[i].Order < recs[j].Order })
			status.Proofs = append(status.Proofs, Contribution{
				SubjectID: lid,
				LogicalID: lid,
				GroupID:   gid,
				Receipts:  append([]ReceiptRecord(nil), recs...),
			})
		}
		status.ProofCount = len(valid)
		status.CurrentlySatisfied = status.ProofCount >= status.Threshold

		if published := b.groupReleases[gid]; published != nil {
			status.Published = true
			status.Releasable = true
			status.PublishedRelease = cloneGroupReleaseRecord(published)
		} else {
			status.Releasable = status.CurrentlySatisfied
		}

		if status.Releasable {
			eval.ReleasableGroups = append(eval.ReleasableGroups, gid)
		}
		if !status.CurrentlySatisfied {
			mp := buildMissingProofLocked(run, b, gid, req.Threshold, status.ProofCount, logicalIDs, valid, roots)
			status.Missing = &mp
			// A published group is still releasable even if the current
			// effective set no longer reproduces its historical quorum.
			if !status.Published {
				eval.Missing = append(eval.Missing, mp)
			}
		}

		eval.Groups = append(eval.Groups, status)
	}

	eval.Ready = len(eval.Missing) == 0 && len(eval.Groups) > 0
	return eval
}

func (m *Manager) validateReceiptEffectiveLocked(
	run *runState,
	rec ReceiptRecord,
	roots map[ParticipantID]ParticipantID,
	representatives map[ParticipantID][]ParticipantID,
) (ParticipantID, *InvalidReceipt) {
	invalid := func(code, format string, args ...any) (ParticipantID, *InvalidReceipt) {
		return "", &InvalidReceipt{
			Receipt: rec,
			Code:    code,
			Reason:  fmt.Sprintf(format, args...),
		}
	}

	submitter, ok := run.participants[rec.SubmitterID]
	if !ok {
		return invalid("submitter_not_found", "submitter %q no longer exists", rec.SubmitterID)
	}
	if submitter.status != ParticipantActive {
		return invalid("submitter_inactive", "submitter %q is %s", rec.SubmitterID, submitter.status)
	}

	if rec.SubjectID == rec.SubmitterID {
		lid := roots[rec.SubmitterID]
		if _, ok := representatives[lid]; !ok {
			return invalid("identity_not_in_effective_group", "direct identity %q is not active in effective group", lid)
		}
		return lid, nil
	}

	subject, ok := run.participants[rec.SubjectID]
	if !ok {
		return invalid("subject_not_found", "subject %q no longer exists", rec.SubjectID)
	}
	if subject.status != ParticipantActive {
		return invalid("subject_inactive", "subject %q is %s", rec.SubjectID, subject.status)
	}
	grant, ok := run.grants[rec.GrantID]
	if !ok {
		return invalid("grant_not_found", "proxy grant %q no longer exists", rec.GrantID)
	}
	if !grant.rec.Active {
		return invalid("grant_revoked", "proxy grant %q was revoked", rec.GrantID)
	}
	if err := validateGrantForReceipt(&grant.rec, rec.RunID, rec.SubmitterID, rec.SubjectID, rec.GroupID); err != nil {
		return invalid("grant_invalid", "%v", err)
	}
	lid := roots[rec.SubjectID]
	if _, ok := representatives[lid]; !ok {
		return invalid("identity_not_in_effective_group", "proxied identity %q is not active in effective group", lid)
	}
	return lid, nil
}

func buildMissingProofLocked(
	run *runState,
	b *barrierState,
	gid GroupID,
	threshold, proofCount int,
	logicalIDs []ParticipantID,
	valid map[ParticipantID][]ReceiptRecord,
	roots map[ParticipantID]ParticipantID,
) MissingProof {
	mp := MissingProof{
		RunID:              run.cfg.ID,
		BarrierID:          b.cfg.ID,
		CheckpointID:       b.checkpoint,
		GroupID:            gid,
		Threshold:          threshold,
		ProofCount:         proofCount,
		EligibleLogicalIDs: append([]ParticipantID(nil), logicalIDs...),
	}
	mp.MissingCount = threshold - proofCount
	if mp.MissingCount < 0 {
		mp.MissingCount = 0
	}
	mp.Impossible = threshold > len(logicalIDs)

	awaiting := make([]ParticipantID, 0)
	for _, lid := range logicalIDs {
		if _, ok := valid[lid]; !ok {
			awaiting = append(awaiting, lid)
		}
	}
	mp.AwaitingLogicalIDs = awaiting

	switch {
	case mp.Impossible:
		mp.Reason = fmt.Sprintf(
			"threshold %d exceeds %d effective logical participant identity/identities",
			threshold, len(logicalIDs),
		)
	case proofCount == 0:
		mp.Reason = fmt.Sprintf("group %q needs %d distinct proof(s); none received", gid, threshold)
	default:
		mp.Reason = fmt.Sprintf("group %q needs %d more distinct proof(s)", gid, mp.MissingCount)
	}

	component := make(map[ParticipantID][]ParticipantID)
	for pid, lid := range roots {
		component[lid] = append(component[lid], pid)
	}
	for _, lid := range awaiting {
		req := ProofRequest{
			RunID:        run.cfg.ID,
			BarrierID:    b.cfg.ID,
			CheckpointID: b.checkpoint,
			GroupID:      gid,
			LogicalID:    lid,
			Reason:       "direct receipt from an active mirror representative or an active authorized proxy receipt",
		}
		for _, pid := range component[lid] {
			p := run.participants[pid]
			if p != nil && p.status == ParticipantActive {
				req.Principals = append(req.Principals, pid)
				req.DirectSubmitters = append(req.DirectSubmitters, pid)
			}
		}
		sortParticipantIDs(req.Principals)
		sortParticipantIDs(req.DirectSubmitters)

		proxySet := make(map[ParticipantID]struct{})
		for _, gs := range run.grants {
			g := gs.rec
			if !g.Active || g.RunID != run.cfg.ID || !grantAllowsGroup(&g, gid) {
				continue
			}
			principal := run.participants[g.PrincipalID]
			proxy := run.participants[g.ProxyID]
			if principal == nil || proxy == nil || principal.status != ParticipantActive || proxy.status != ParticipantActive {
				continue
			}
			if roots[g.PrincipalID] == lid {
				proxySet[g.ProxyID] = struct{}{}
			}
		}
		for pid := range proxySet {
			req.ProxySubmitters = append(req.ProxySubmitters, pid)
		}
		sortParticipantIDs(req.ProxySubmitters)
		accepted := make(map[ParticipantID]struct{})
		for _, pid := range req.DirectSubmitters {
			accepted[pid] = struct{}{}
		}
		for _, pid := range req.ProxySubmitters {
			accepted[pid] = struct{}{}
		}
		for pid := range accepted {
			req.AcceptedSubmitters = append(req.AcceptedSubmitters, pid)
		}
		sortParticipantIDs(req.AcceptedSubmitters)
		mp.Requests = append(mp.Requests, req)
	}
	return mp
}

// PublishGroup atomically publishes a group quorum if it is currently met.
// A published group record survives later handoff/revocation changes.
func (m *Manager) PublishGroup(runID RunID, barrierID BarrierID, groupID GroupID) (*PublishGroupOutcome, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	run, b, err := m.barrierPairLocked(runID, barrierID)
	if err != nil {
		return nil, err
	}
	if _, ok := b.requirements[groupID]; !ok {
		return nil, fmt.Errorf("%w: group %q", ErrGroupNotRequired, groupID)
	}
	if rec := b.groupReleases[groupID]; rec != nil {
		eval := m.evaluateLocked(run, b)
		st := findGroupStatus(eval, groupID)
		return &PublishGroupOutcome{
			Released:         true,
			AlreadyPublished: true,
			Release:          cloneGroupReleaseRecord(rec),
			Status:           cloneGroupStatus(st),
			Evaluation:       cloneEvaluation(eval),
		}, nil
	}

	eval := m.evaluateLocked(run, b)
	st := findGroupStatus(eval, groupID)
	if st == nil {
		return nil, fmt.Errorf("%w: group status %q", ErrGroupNotFound, groupID)
	}
	if !st.CurrentlySatisfied {
		return &PublishGroupOutcome{
			Released:   false,
			Status:     cloneGroupStatus(st),
			Evaluation: cloneEvaluation(eval),
		}, nil
	}

	rec := m.buildGroupReleaseLocked(run, b, *st)
	b.groupReleases[groupID] = rec
	run.notifyAllLocked()
	return &PublishGroupOutcome{
		Released:   true,
		Release:    cloneGroupReleaseRecord(rec),
		Status:     cloneGroupStatus(findGroupStatus(m.evaluateLocked(run, b), groupID)),
		Evaluation: cloneEvaluation(m.evaluateLocked(run, b)),
	}, nil
}

// TryReleaseBarrier performs an atomic compare-evaluate-and-publish operation.
// If the current effective set no longer meets an unpublished threshold, the
// returned evaluation contains the releasable groups and missing proofs.
func (m *Manager) TryReleaseBarrier(runID RunID, barrierID BarrierID) (*ReleaseOutcome, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	run, b, err := m.barrierPairLocked(runID, barrierID)
	if err != nil {
		return nil, err
	}
	if b.release != nil {
		return &ReleaseOutcome{
			Released:        true,
			AlreadyReleased: true,
			Release:         cloneReleaseRecord(b.release),
		}, nil
	}

	eval := m.evaluateLocked(run, b)
	if !eval.Ready {
		return &ReleaseOutcome{Released: false, Evaluation: cloneEvaluation(eval)}, nil
	}
	rec, err := m.publishBarrierLocked(run, b)
	if err != nil {
		return nil, err
	}
	return &ReleaseOutcome{Released: true, Release: cloneReleaseRecord(rec)}, nil
}

// ReleaseBarrier publishes or returns the immutable barrier success. When the
// barrier is not currently releasable it returns a *NotReadyError containing
// the evaluation.
func (m *Manager) ReleaseBarrier(runID RunID, barrierID BarrierID) (*ReleaseRecord, error) {
	out, err := m.TryReleaseBarrier(runID, barrierID)
	if err != nil {
		return nil, err
	}
	if !out.Released {
		return nil, &NotReadyError{Evaluation: out.Evaluation}
	}
	return out.Release, nil
}

// WaitForRelease waits until the barrier is currently satisfiable and then
// atomically publishes one immutable release. Concurrent waiters receive the
// same published record. Context cancellation does not remove an already
// published result.
func (m *Manager) WaitForRelease(ctx context.Context, runID RunID, barrierID BarrierID) (*ReleaseRecord, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	ch := make(chan struct{}, 1)

	m.mu.Lock()
	run, err := m.runLocked(runID)
	if err != nil {
		m.mu.Unlock()
		return nil, err
	}
	b, err := run.barrierLocked(barrierID)
	if err != nil {
		m.mu.Unlock()
		return nil, err
	}
	run.waiters[ch] = struct{}{}

	for {
		if b.release != nil {
			delete(run.waiters, ch)
			rec := cloneReleaseRecord(b.release)
			m.mu.Unlock()
			return rec, nil
		}
		if err := ctx.Err(); err != nil {
			delete(run.waiters, ch)
			m.mu.Unlock()
			return nil, err
		}

		eval := m.evaluateLocked(run, b)
		if eval.Ready {
			rec, pubErr := m.publishBarrierLocked(run, b)
			delete(run.waiters, ch)
			m.mu.Unlock()
			if pubErr != nil {
				return nil, pubErr
			}
			return cloneReleaseRecord(rec), nil
		}

		m.mu.Unlock()
		select {
		case <-ch:
		case <-ctx.Done():
			m.mu.Lock()
			delete(run.waiters, ch)
			var published *ReleaseRecord
			if b.release != nil {
				published = cloneReleaseRecord(b.release)
			}
			m.mu.Unlock()
			if published != nil {
				return published, nil
			}
			return nil, ctx.Err()
		}
		m.mu.Lock()
	}
}

// GetRelease returns the published release or (nil, nil) when it has not been
// published yet.
func (m *Manager) GetRelease(runID RunID, barrierID BarrierID) (*ReleaseRecord, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	_, b, err := m.barrierPairLocked(runID, barrierID)
	if err != nil {
		return nil, err
	}
	if b.release == nil {
		return nil, nil
	}
	return cloneReleaseRecord(b.release), nil
}

// PollRun returns every barrier with either its immutable release or current
// recomputed evaluation.
func (m *Manager) PollRun(runID RunID) (*PollResult, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	run, err := m.runLocked(runID)
	if err != nil {
		return nil, err
	}
	out := &PollResult{RunID: runID, TopologyVersion: run.topologyVersion}
	for _, bid := range run.barrierOrder {
		b := run.barriers[bid]
		view := BarrierView{BarrierID: bid, CheckpointID: b.checkpoint}
		if b.release != nil {
			view.Released = true
			view.Release = cloneReleaseRecord(b.release)
		} else {
			view.Evaluation = cloneEvaluation(m.evaluateLocked(run, b))
		}
		out.Barriers = append(out.Barriers, view)
	}
	return out, nil
}

func (m *Manager) buildGroupReleaseLocked(run *runState, b *barrierState, st GroupStatus) *GroupReleaseRecord {
	id := ReleaseID(run.nextIDLocked("group-release"))
	rec := &GroupReleaseRecord{
		ReleaseID:       id,
		RunID:           run.cfg.ID,
		BarrierID:       b.cfg.ID,
		GroupID:         st.GroupID,
		CheckpointID:    b.checkpoint,
		Threshold:       st.Threshold,
		Proofs:          cloneContributions(st.Proofs),
		TopologyVersion: run.topologyVersion,
		PublishedAt:     m.now(),
	}
	rec.SourceReceipts = sourceReceiptsFromContributions(rec.Proofs)
	return rec
}

func (m *Manager) publishBarrierLocked(run *runState, b *barrierState) (*ReleaseRecord, error) {
	if b.release != nil {
		return b.release, nil
	}
	eval := m.evaluateLocked(run, b)
	if !eval.Ready {
		return nil, &NotReadyError{Evaluation: cloneEvaluation(eval)}
	}

	groups := make([]GroupReleaseRecord, 0, len(eval.Groups))
	var sources []ReceiptRecord
	seenSource := make(map[ReceiptID]struct{})
	for _, st := range eval.Groups {
		var greq *GroupReleaseRecord
		if existing := b.groupReleases[st.GroupID]; existing != nil {
			greq = existing
		} else {
			greq = m.buildGroupReleaseLocked(run, b, st)
			b.groupReleases[st.GroupID] = greq
		}
		groups = append(groups, *cloneGroupReleaseRecord(greq))
		for _, rcpt := range greq.SourceReceipts {
			if _, ok := seenSource[rcpt.ReceiptID]; ok {
				continue
			}
			seenSource[rcpt.ReceiptID] = struct{}{}
			sources = append(sources, rcpt)
		}
	}
	sort.SliceStable(sources, func(i, j int) bool { return sources[i].Order < sources[j].Order })

	rec := &ReleaseRecord{
		ReleaseID:       ReleaseID(run.nextIDLocked("release")),
		RunID:           run.cfg.ID,
		BarrierID:       b.cfg.ID,
		CheckpointID:    b.checkpoint,
		Groups:          groups,
		SourceReceipts:  sources,
		TopologyVersion: run.topologyVersion,
		PublishedAt:     m.now(),
	}
	b.release = rec
	run.notifyAllLocked()
	return rec, nil
}

func findGroupStatus(eval *Evaluation, gid GroupID) *GroupStatus {
	if eval == nil {
		return nil
	}
	for i := range eval.Groups {
		if eval.Groups[i].GroupID == gid {
			st := eval.Groups[i]
			return &st
		}
	}
	return nil
}

func sourceReceiptsFromContributions(contribs []Contribution) []ReceiptRecord {
	var out []ReceiptRecord
	seen := make(map[ReceiptID]struct{})
	for _, c := range contribs {
		for _, r := range c.Receipts {
			if _, ok := seen[r.ReceiptID]; ok {
				continue
			}
			seen[r.ReceiptID] = struct{}{}
			out = append(out, r)
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].Order < out[j].Order })
	return out
}

func sameRequirements(a, b map[GroupID]GroupRequirement) bool {
	if len(a) != len(b) {
		return false
	}
	for id, ra := range a {
		rb, ok := b[id]
		if !ok || ra != rb {
			return false
		}
	}
	return true
}

func sameReceiptRequest(existing, incoming Receipt) bool {
	if existing.RunID != incoming.RunID ||
		existing.BarrierID != incoming.BarrierID ||
		existing.ReceiptID != incoming.ReceiptID ||
		existing.CheckpointID != incoming.CheckpointID ||
		existing.GroupID != incoming.GroupID ||
		existing.SubmitterID != incoming.SubmitterID ||
		existing.SubjectID != incoming.SubjectID ||
		existing.Proof != incoming.Proof {
		return false
	}
	// A caller may omit the resolved grant ID on an idempotent retry; when
	// supplied, it must agree.
	return incoming.GrantID == "" || existing.GrantID == incoming.GrantID
}
