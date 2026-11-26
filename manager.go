package barrier

import (
	"fmt"
	"sort"
	"sync"
	"time"
)

// Option configures a Manager.
type Option func(*Manager)

// WithClock replaces the wall clock, useful for deterministic tests.
func WithClock(now func() time.Time) Option {
	return func(m *Manager) {
		if now != nil {
			m.now = now
		}
	}
}

// Manager is a goroutine-safe, in-memory checkpoint barrier coordinator.
type Manager struct {
	mu   sync.Mutex
	now  func() time.Time
	runs map[RunID]*runState
}

// NewManager creates an empty barrier manager.
func NewManager(opts ...Option) *Manager {
	m := &Manager{
		now:  time.Now,
		runs: make(map[RunID]*runState),
	}
	for _, opt := range opts {
		opt(m)
	}
	return m
}

type participantState struct {
	cfg             ParticipantConfig
	status          ParticipantStatus
	successor       ParticipantID
	lastHandover    HandoverID
	revokeReason    string
	statusChangedAt time.Time
}

type groupState struct {
	cfg     GroupConfig
	members map[ParticipantID]struct{}
}

type mirrorState struct {
	rec MirrorRelation
}

type grantState struct {
	rec Grant
}

type barrierState struct {
	cfg           BarrierConfig
	checkpoint    string
	requirements  map[GroupID]GroupRequirement
	groupOrder    []GroupID
	receipts      []*ReceiptRecord
	groupReleases map[GroupID]*GroupReleaseRecord
	release       *ReleaseRecord
	createdAt     time.Time
}

type runState struct {
	cfg RunConfig

	participants map[ParticipantID]*participantState
	groups       map[GroupID]*groupState
	groupOrder   []GroupID

	mirrors     map[RelationID]*mirrorState
	mirrorOrder []RelationID
	grants      map[GrantID]*grantState

	barriers     map[BarrierID]*barrierState
	barrierOrder []BarrierID

	receipts  map[ReceiptID]*ReceiptRecord
	handovers map[HandoverID]*HandOverRecord

	waiters map[chan struct{}]struct{}

	nextOrder       int64
	topologyVersion int64
	createdAt       time.Time
}

// NewBarrierManager is a descriptive alias for callers that prefer the domain
// name over the generic Manager name.
func NewBarrierManager(opts ...Option) *Manager { return NewManager(opts...) }

func (m *Manager) newRunState(cfg RunConfig) *runState {
	now := m.now()
	return &runState{
		cfg:          cfg,
		participants: make(map[ParticipantID]*participantState),
		groups:       make(map[GroupID]*groupState),
		mirrors:      make(map[RelationID]*mirrorState),
		grants:       make(map[GrantID]*grantState),
		barriers:     make(map[BarrierID]*barrierState),
		receipts:     make(map[ReceiptID]*ReceiptRecord),
		handovers:    make(map[HandoverID]*HandOverRecord),
		waiters:      make(map[chan struct{}]struct{}),
		createdAt:    now,
	}
}

// CreateRun installs the participant and threshold configuration for one run.
func (m *Manager) CreateRun(cfg RunConfig) (*RunInfo, error) {
	if cfg.ID == "" {
		return nil, fmt.Errorf("%w: run id is required", ErrInvalidConfig)
	}
	if len(cfg.Groups) == 0 {
		return nil, fmt.Errorf("%w: at least one group is required", ErrInvalidConfig)
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	if _, exists := m.runs[cfg.ID]; exists {
		return nil, fmt.Errorf("%w: run %q", ErrRunExists, cfg.ID)
	}

	run := m.newRunState(cfg)

	seenParticipants := make(map[ParticipantID]struct{}, len(cfg.Participants))
	for _, p := range cfg.Participants {
		if p.ID == "" {
			return nil, fmt.Errorf("%w: participant id is required", ErrInvalidConfig)
		}
		if _, dup := seenParticipants[p.ID]; dup {
			return nil, fmt.Errorf("%w: duplicate participant %q", ErrInvalidConfig, p.ID)
		}
		seenParticipants[p.ID] = struct{}{}
		run.participants[p.ID] = &participantState{
			cfg:             p,
			status:          ParticipantActive,
			statusChangedAt: run.createdAt,
		}
	}

	seenGroups := make(map[GroupID]struct{}, len(cfg.Groups))
	for _, g := range cfg.Groups {
		if g.ID == "" {
			return nil, fmt.Errorf("%w: group id is required", ErrInvalidConfig)
		}
		if _, dup := seenGroups[g.ID]; dup {
			return nil, fmt.Errorf("%w: duplicate group %q", ErrInvalidConfig, g.ID)
		}
		seenGroups[g.ID] = struct{}{}
		if g.Threshold < 1 {
			return nil, fmt.Errorf("%w: group %q threshold must be >= 1", ErrInvalidConfig, g.ID)
		}
		run.groups[g.ID] = &groupState{
			cfg:     g,
			members: make(map[ParticipantID]struct{}),
		}
		run.groupOrder = append(run.groupOrder, g.ID)
	}

	// Membership can be declared on participants, on groups, or both.
	for _, p := range cfg.Participants {
		for _, gid := range p.Groups {
			g, ok := run.groups[gid]
			if !ok {
				return nil, fmt.Errorf("%w: participant %q references unknown group %q", ErrInvalidConfig, p.ID, gid)
			}
			g.members[p.ID] = struct{}{}
		}
	}
	for _, gcfg := range cfg.Groups {
		g := run.groups[gcfg.ID]
		for _, pid := range gcfg.Members {
			if _, ok := run.participants[pid]; !ok {
				return nil, fmt.Errorf("%w: group %q references unknown participant %q", ErrInvalidConfig, gcfg.ID, pid)
			}
			g.members[pid] = struct{}{}
		}
	}
	for _, gid := range run.groupOrder {
		g := run.groups[gid]
		if g.cfg.Threshold > len(g.members) {
			return nil, fmt.Errorf(
				"%w: group %q threshold %d exceeds member count %d",
				ErrInvalidConfig, gid, g.cfg.Threshold, len(g.members),
			)
		}
	}

	m.runs[cfg.ID] = run
	return run.infoLocked(), nil
}

// GetRun returns a deep copy of the current run configuration/state.
func (m *Manager) GetRun(id RunID) (*RunInfo, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	run, err := m.runLocked(id)
	if err != nil {
		return nil, err
	}
	return run.infoLocked(), nil
}

func (m *Manager) runLocked(id RunID) (*runState, error) {
	run, ok := m.runs[id]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrRunNotFound, id)
	}
	return run, nil
}

func (r *runState) participantLocked(id ParticipantID) (*participantState, error) {
	p, ok := r.participants[id]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrParticipantNotFound, id)
	}
	return p, nil
}

func (r *runState) groupLocked(id GroupID) (*groupState, error) {
	g, ok := r.groups[id]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrGroupNotFound, id)
	}
	return g, nil
}

func (r *runState) barrierLocked(id BarrierID) (*barrierState, error) {
	b, ok := r.barriers[id]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrBarrierNotFound, id)
	}
	return b, nil
}

func (r *runState) notifyAllLocked() {
	for ch := range r.waiters {
		select {
		case ch <- struct{}{}:
		default:
		}
	}
}

func (r *runState) nextIDLocked(prefix string) string {
	r.nextOrder++
	return fmt.Sprintf("%s-%d", prefix, r.nextOrder)
}

func (r *runState) infoLocked() *RunInfo {
	info := &RunInfo{
		ID:              r.cfg.ID,
		TopologyVersion: r.topologyVersion,
		CreatedAt:       r.createdAt,
	}

	// Include both configured participants and successors introduced by
	// handoff. Sorted IDs make output deterministic.
	participantIDs := make([]ParticipantID, 0, len(r.participants))
	for pid := range r.participants {
		participantIDs = append(participantIDs, pid)
	}
	sortParticipantIDs(participantIDs)
	for _, pid := range participantIDs {
		p := r.participants[pid]
		pi := ParticipantInfo{
			ID:              pid,
			Status:          p.status,
			Successor:       p.successor,
			LastHandoverID:  p.lastHandover,
			RevokeReason:    p.revokeReason,
			StatusChangedAt: p.statusChangedAt,
		}
		for _, gid := range r.groupOrder {
			if g := r.groups[gid]; g != nil {
				if _, ok := g.members[pid]; ok {
					pi.Groups = append(pi.Groups, gid)
				}
			}
		}
		info.Participants = append(info.Participants, pi)
	}

	for _, gid := range r.groupOrder {
		g := r.groups[gid]
		gi := GroupInfo{ID: gid, Threshold: g.cfg.Threshold}
		members := make([]ParticipantID, 0, len(g.members))
		for pid := range g.members {
			members = append(members, pid)
		}
		sortParticipantIDs(members)
		gi.Members = members
		info.Groups = append(info.Groups, gi)
	}
	return info
}

func sortParticipantIDs(ids []ParticipantID) {
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
}

func sortGroupIDs(ids []GroupID) {
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
}
