package barrier

import (
	"fmt"
)

// DeclareMirror records that A and B are mirrors of the same responsible
// identity. Direct receipts from either participant collapse to one threshold
// contribution while the mirror relation remains active.
func (m *Manager) DeclareMirror(runID RunID, a, b ParticipantID) (*MirrorRelation, error) {
	if runID == "" || a == "" || b == "" {
		return nil, fmt.Errorf("%w: run id and both participant ids are required", ErrInvalidRequest)
	}
	if a == b {
		return nil, fmt.Errorf("%w: mirror endpoints must differ", ErrInvalidRequest)
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	run, err := m.runLocked(runID)
	if err != nil {
		return nil, err
	}
	pa, err := run.participantLocked(a)
	if err != nil {
		return nil, err
	}
	pb, err := run.participantLocked(b)
	if err != nil {
		return nil, err
	}
	if err := requireActivePair(pa, pb); err != nil {
		return nil, err
	}

	for _, edge := range run.mirrors {
		rec := edge.rec
		if rec.Active && ((rec.A == a && rec.B == b) || (rec.A == b && rec.B == a)) {
			out := rec
			return &out, nil
		}
	}

	id := RelationID(run.nextIDLocked("mirror"))
	now := m.now()
	rec := MirrorRelation{
		ID:        id,
		RunID:     runID,
		A:         a,
		B:         b,
		Active:    true,
		CreatedAt: now,
	}
	run.mirrors[id] = &mirrorState{rec: rec}
	run.mirrorOrder = append(run.mirrorOrder, id)
	run.topologyVersion++
	run.notifyAllLocked()
	out := rec
	return &out, nil
}

// RevokeMirror deactivates a mirror edge. Pending barriers are recomputed and
// the two endpoints may then count separately again.
func (m *Manager) RevokeMirror(runID RunID, relationID RelationID, reason string) (*MirrorRelation, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	run, err := m.runLocked(runID)
	if err != nil {
		return nil, err
	}
	edge, ok := run.mirrors[relationID]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrRelationNotFound, relationID)
	}
	if edge.rec.Active {
		now := m.now()
		edge.rec.Active = false
		edge.rec.RevokedAt = &now
		edge.rec.Reason = reason
		run.topologyVersion++
		run.notifyAllLocked()
	}
	out := edge.rec
	return &out, nil
}

// AuthorizeProxy allows ProxyID to submit receipts with SubjectID PrincipalID.
// An empty Groups list authorizes all groups of which the principal is
// currently a member.
func (m *Manager) AuthorizeProxy(auth ProxyAuthorization) (*Grant, error) {
	if auth.RunID == "" || auth.ProxyID == "" || auth.PrincipalID == "" {
		return nil, fmt.Errorf("%w: run, proxy, and principal ids are required", ErrInvalidRequest)
	}
	if auth.ProxyID == auth.PrincipalID {
		return nil, fmt.Errorf("%w: proxy and principal must differ", ErrInvalidRequest)
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	run, err := m.runLocked(auth.RunID)
	if err != nil {
		return nil, err
	}
	proxy, err := run.participantLocked(auth.ProxyID)
	if err != nil {
		return nil, err
	}
	principal, err := run.participantLocked(auth.PrincipalID)
	if err != nil {
		return nil, err
	}
	if proxy.status != ParticipantActive {
		return nil, inactiveError(auth.ProxyID, proxy.status)
	}
	if principal.status != ParticipantActive {
		return nil, inactiveError(auth.PrincipalID, principal.status)
	}

	groups, err := normalizeGrantGroupsLocked(run, principal.cfg.ID, auth.Groups)
	if err != nil {
		return nil, err
	}

	grantID := auth.GrantID
	if grantID != "" {
		if _, exists := run.grants[grantID]; exists {
			return nil, fmt.Errorf("%w: grant id %q already exists", ErrConflict, grantID)
		}
	} else {
		for {
			candidate := GrantID(run.nextIDLocked("grant"))
			if _, exists := run.grants[candidate]; !exists {
				grantID = candidate
				break
			}
		}
	}

	now := m.now()
	rec := Grant{
		ID:          grantID,
		RunID:       auth.RunID,
		ProxyID:     auth.ProxyID,
		PrincipalID: auth.PrincipalID,
		Groups:      groups,
		Active:      true,
		CreatedAt:   now,
	}
	run.grants[grantID] = &grantState{rec: rec}
	run.topologyVersion++
	run.notifyAllLocked()
	out := rec
	out.Groups = append([]GroupID(nil), rec.Groups...)
	return &out, nil
}

// RevokeProxy revokes one delegation. Existing receipts submitted under it
// stop counting toward pending, unpublished barriers. The call is idempotent.
func (m *Manager) RevokeProxy(runID RunID, grantID GrantID, reason string) (*Grant, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	run, err := m.runLocked(runID)
	if err != nil {
		return nil, err
	}
	grant, ok := run.grants[grantID]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrGrantNotFound, grantID)
	}
	if grant.rec.Active {
		now := m.now()
		grant.rec.Active = false
		grant.rec.RevokedAt = &now
		grant.rec.Reason = reason
		run.topologyVersion++
		run.notifyAllLocked()
	}
	out := grant.rec
	out.Groups = append([]GroupID(nil), grant.rec.Groups...)
	return &out, nil
}

// RevokeParticipant removes a participant from the effective active set.
// Revoking an already inactive participant is an idempotent no-op.
func (m *Manager) RevokeParticipant(runID RunID, pid ParticipantID, reason string) (*ParticipantInfo, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	run, err := m.runLocked(runID)
	if err != nil {
		return nil, err
	}
	p, err := run.participantLocked(pid)
	if err != nil {
		return nil, err
	}
	if p.status == ParticipantActive {
		now := m.now()
		p.status = ParticipantRevoked
		p.revokeReason = reason
		p.statusChangedAt = now
		run.topologyVersion++
		run.notifyAllLocked()
	}
	info := run.infoLocked()
	for i := range info.Participants {
		if info.Participants[i].ID == pid {
			pi := info.Participants[i]
			return &pi, nil
		}
	}
	return nil, fmt.Errorf("%w: %q", ErrParticipantNotFound, pid)
}

// HandOver retires From and installs To as its replacement in the effective
// group set. Proxy grants are not transferred automatically; least privilege
// requires an explicit new authorization for To.
func (m *Manager) HandOver(req HandOverRequest) (*HandOverRecord, error) {
	if req.RunID == "" || req.From == "" || req.To == "" {
		return nil, fmt.Errorf("%w: run, from, and to ids are required", ErrInvalidRequest)
	}
	if req.From == req.To {
		return nil, fmt.Errorf("%w: handover endpoints must differ", ErrInvalidRequest)
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	run, err := m.runLocked(req.RunID)
	if err != nil {
		return nil, err
	}
	from, err := run.participantLocked(req.From)
	if err != nil {
		return nil, err
	}

	if from.status == ParticipantHandedOver {
		if from.successor == req.To && from.lastHandover != "" {
			if rec, ok := run.handovers[from.lastHandover]; ok {
				out := *rec
				out.Groups = append([]GroupID(nil), rec.Groups...)
				return &out, nil
			}
		}
		return nil, fmt.Errorf(
			"%w: participant %q already handed over to %q",
			ErrConflict, req.From, from.successor,
		)
	}
	if from.status == ParticipantRevoked {
		return nil, fmt.Errorf("%w: participant %q: %s", ErrRevoked, req.From, from.revokeReason)
	}

	to, exists := run.participants[req.To]
	if exists {
		if to.status != ParticipantActive {
			return nil, inactiveError(req.To, to.status)
		}
	} else {
		now := m.now()
		to = &participantState{
			cfg:             ParticipantConfig{ID: req.To},
			status:          ParticipantActive,
			statusChangedAt: now,
		}
		run.participants[req.To] = to
	}

	handedGroups := make([]GroupID, 0)
	for _, gid := range run.groupOrder {
		g := run.groups[gid]
		if _, ok := g.members[req.From]; !ok {
			continue
		}
		handedGroups = append(handedGroups, gid)
		delete(g.members, req.From)
		g.members[req.To] = struct{}{}
	}

	id := HandoverID(run.nextIDLocked("handover"))
	now := m.now()
	rec := &HandOverRecord{
		ID:              id,
		RunID:           req.RunID,
		From:            req.From,
		To:              req.To,
		Groups:          append([]GroupID(nil), handedGroups...),
		TopologyVersion: run.topologyVersion + 1,
		At:              now,
	}
	run.handovers[id] = rec

	from.status = ParticipantHandedOver
	from.successor = req.To
	from.lastHandover = id
	from.statusChangedAt = now

	run.topologyVersion++
	run.notifyAllLocked()

	out := *rec
	out.Groups = append([]GroupID(nil), rec.Groups...)
	return &out, nil
}

func requireActivePair(a, b *participantState) error {
	if a.status != ParticipantActive {
		return inactiveError(a.cfg.ID, a.status)
	}
	if b.status != ParticipantActive {
		return inactiveError(b.cfg.ID, b.status)
	}
	return nil
}

func inactiveError(id ParticipantID, status ParticipantStatus) error {
	switch status {
	case ParticipantRevoked:
		return fmt.Errorf("%w: participant %q", ErrRevoked, id)
	default:
		return fmt.Errorf("%w: participant %q is %s", ErrParticipantInactive, id, status)
	}
}

func normalizeGrantGroupsLocked(run *runState, principal ParticipantID, requested []GroupID) ([]GroupID, error) {
	var groups []GroupID
	if len(requested) == 0 {
		for _, gid := range run.groupOrder {
			if _, ok := run.groups[gid].members[principal]; ok {
				groups = append(groups, gid)
			}
		}
		if len(groups) == 0 {
			return nil, fmt.Errorf("%w: principal %q has no current groups", ErrInvalidRequest, principal)
		}
		return groups, nil
	}

	seen := make(map[GroupID]struct{}, len(requested))
	for _, gid := range requested {
		if gid == "" {
			return nil, fmt.Errorf("%w: group id is required", ErrInvalidRequest)
		}
		if _, dup := seen[gid]; dup {
			continue
		}
		seen[gid] = struct{}{}
		g, err := run.groupLocked(gid)
		if err != nil {
			return nil, err
		}
		if _, ok := g.members[principal]; !ok {
			return nil, fmt.Errorf("%w: principal %q is not in group %q", ErrNotMember, principal, gid)
		}
		groups = append(groups, gid)
	}
	return groups, nil
}

// mirrorRootsLocked builds active mirror equivalence classes and returns the
// minimum participant ID as a deterministic logical identity.
func (r *runState) mirrorRootsLocked() map[ParticipantID]ParticipantID {
	parent := make(map[ParticipantID]ParticipantID, len(r.participants))
	for pid := range r.participants {
		parent[pid] = pid
	}

	var find func(ParticipantID) ParticipantID
	find = func(x ParticipantID) ParticipantID {
		p := parent[x]
		if p != x {
			parent[x] = find(p)
		}
		return parent[x]
	}
	union := func(a, b ParticipantID) {
		ra, rb := find(a), find(b)
		if ra == rb {
			return
		}
		// Union by lexicographically smaller root for determinism.
		if ra < rb {
			parent[rb] = ra
		} else {
			parent[ra] = rb
		}
	}

	for _, edge := range r.mirrors {
		if edge.rec.Active {
			if _, ok := parent[edge.rec.A]; ok {
				if _, ok2 := parent[edge.rec.B]; ok2 {
					union(edge.rec.A, edge.rec.B)
				}
			}
		}
	}

	components := make(map[ParticipantID][]ParticipantID)
	for pid := range r.participants {
		root := find(pid)
		components[root] = append(components[root], pid)
	}
	roots := make(map[ParticipantID]ParticipantID, len(r.participants))
	for _, ids := range components {
		min := ids[0]
		for _, id := range ids[1:] {
			if id < min {
				min = id
			}
		}
		for _, id := range ids {
			roots[id] = min
		}
	}
	return roots
}

func grantAllowsGroup(g *Grant, gid GroupID) bool {
	if len(g.Groups) == 0 {
		return true
	}
	for _, allowed := range g.Groups {
		if allowed == gid {
			return true
		}
	}
	return false
}
