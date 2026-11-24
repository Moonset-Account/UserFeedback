package migrationplanner

import (
	"fmt"
	"sort"
)

// ActionKind enumerates the single-item changes the planner may take.
type ActionKind int

const (
	// ActReplace swaps one replica to another version (same replica id).
	ActReplace ActionKind = iota
	// ActAdd deploys one additional replica.
	ActAdd
	// ActRemove removes one replica.
	ActRemove
	// ActProfile changes the simultaneously enabled codec pair of one replica.
	ActProfile
	// ActDrain processes an observed drain event and releases its sessions.
	ActDrain
)

func (k ActionKind) String() string {
	switch k {
	case ActReplace:
		return "replace"
	case ActAdd:
		return "add"
	case ActRemove:
		return "remove"
	case ActProfile:
		return "set-profile"
	case ActDrain:
		return "drain"
	default:
		return "unknown"
	}
}

// Action is one atomic plan step.
type Action struct {
	Kind       ActionKind
	Service    string
	Replica    string
	FromVer    string
	ToVer      string
	FromProf   Profile
	ToProf     Profile
	DrainEvent string
}

func (a Action) String() string {
	switch a.Kind {
	case ActReplace:
		return fmt.Sprintf("replace %s/%s: %s->%s", a.Service, a.Replica, a.FromVer, a.ToVer)
	case ActAdd:
		return fmt.Sprintf("add %s replica on %s (%s)", a.Service, a.ToVer, a.ToProf.key())
	case ActRemove:
		return fmt.Sprintf("remove %s/%s", a.Service, a.Replica)
	case ActProfile:
		return fmt.Sprintf("set-profile %s/%s: %s->%s", a.Service, a.Replica, a.FromProf.key(), a.ToProf.key())
	case ActDrain:
		return fmt.Sprintf("drain event %q", a.DrainEvent)
	default:
		return "unknown action"
	}
}

// Blocked reasons classify why an action or the whole search was rejected.
type BlockedReason string

const (
	ReasonCodec               BlockedReason = "codec-incompatible"
	ReasonPinBroken           BlockedReason = "pinned-session-codec-broken"
	ReasonPinRemoved          BlockedReason = "pinned-replica-removed"
	ReasonCapacity            BlockedReason = "replica-capacity-exceeded"
	ReasonCoexistLimit        BlockedReason = "coexistence-step-limit-exceeded"
	ReasonRollbackBudget      BlockedReason = "rollback-budget-exceeded"
	ReasonRollbackUnreachable BlockedReason = "rollback-unreachable"
	ReasonStreamDivergence    BlockedReason = "in-flight-stream-not-equivalent"
	ReasonDrainNotObserved    BlockedReason = "drain-event-not-observed"
	ReasonProfileNotAllowed   BlockedReason = "profile-not-supported-by-version"
	ReasonNoCandidate         BlockedReason = "no-candidate-replica"
)

// Blocked records one rejected action with the offending caller/callee pair.
type Blocked struct {
	Action  Action
	Reason  BlockedReason
	Detail  string
	Caller  string // replica id, empty when not pair-specific
	Callee  string
	Edge    string
	Diverge *Divergence
}

// RollbackReserve is the config/replica-only sequence reserved for one prefix.
type RollbackReserve struct {
	// PrefixLen is the number of forward steps already taken.
	PrefixLen int
	// Actions returns to the initial layout without any drain step.
	Actions []Action
}

// Step is one element of a successful plan.
type Step struct {
	Index    int
	Action   Action
	Rollback RollbackReserve
}

// Plan is a successful shortest forward plan.
type Plan struct {
	Steps           []Step
	GoalReached     bool
	VisitedStates   int
	FrontierReached int
}

// Frontier is the reachable diagnostic returned when no plan exists.
type Frontier struct {
	ReachedStates int
	VisitedStates int
	// Blocked lists rejected actions sampled from the reachable frontier.
	Blocked []Blocked
	// WaitingEvents are drain events that at least one pinned session needs.
	WaitingEvents []string
	// FrontierStates is a bounded sample of reachable state keys.
	FrontierStates []string
}

// PlannerConfig bounds the search and supplies initially observed events.
type PlannerConfig struct {
	// MaxVisited caps BFS expansion. Zero uses a built-in default.
	MaxVisited int
	// MaxBlocked caps the diagnostic sample. Zero uses a default.
	MaxBlocked int
	// ObservedEvents are drain events that have already happened.
	ObservedEvents []string
}

// Planner holds the immutable spec and the initial fleet layout.
type Planner struct {
	spec             *Spec
	initial          *State
	initialKey       string
	initialConfigKey string
	nextReplica      map[string]int
	cfg              PlannerConfig

	// rbCache memoizes resolved rollback sequences per config layout; rbFail
	// memoizes layouts proven to have no in-budget, mesh-safe rollback.
	rbCache map[string][]Action
	rbFail  map[string]bool
	// rbExplored bounds the total number of reverse-BFS node expansions across
	// all rollback checks in one Plan call.
	rbExplored int
}

// NewPlanner validates the spec and the initial layout and compiles streams.
func NewPlanner(spec *Spec, initial map[string]*Replica, sessions []*Session, cfg PlannerConfig) (*Planner, error) {
	if err := spec.validate(); err != nil {
		return nil, err
	}
	st := &State{
		spec:          spec,
		Replicas:      map[string]*Replica{},
		coexistAge:    map[string]int{},
		pins:          map[string]*Session{},
		drainedEvents: map[string]bool{},
	}
	seenID := map[string]bool{}
	counts := map[string]int{}
	for id, r := range initial {
		if seenID[id] {
			return nil, fmt.Errorf("duplicate replica id %q", id)
		}
		seenID[id] = true
		svc, ok := spec.Services[r.Service]
		if !ok {
			return nil, fmt.Errorf("replica %q references unknown service %q", id, r.Service)
		}
		v, ok := svc.Versions[r.Version]
		if !ok {
			return nil, fmt.Errorf("replica %q runs undeployable version %q", id, r.Version)
		}
		if !profileAllowed(v, r.Profile) {
			return nil, fmt.Errorf("replica %q enables profile unsupported by %s/%s", id, r.Service, r.Version)
		}
		cp := *r
		st.Replicas[id] = &cp
		counts[r.Service]++
	}
	for name, svc := range spec.Services {
		if counts[name] == 0 {
			return nil, fmt.Errorf("service %q has no initial replica", name)
		}
		if counts[name] > svc.MaxReplicas {
			return nil, fmt.Errorf("service %q starts above MaxReplicas", name)
		}
	}
	for _, e := range spec.Edges {
		for vp, proto := range e.Streams {
			if proto == nil {
				return nil, fmt.Errorf("edge %s has nil stream for %s/%s", e.Key(), vp.CallerVersion, vp.CalleeVersion)
			}
			if err := proto.Build(); err != nil {
				return nil, fmt.Errorf("edge %s: %w", e.Key(), err)
			}
		}
	}
	knownSessions := map[string]bool{}
	for _, sess := range sessions {
		if knownSessions[sess.ID] {
			return nil, fmt.Errorf("duplicate session %q", sess.ID)
		}
		knownSessions[sess.ID] = true
		e := spec.edgeByKey(sess.Edge)
		if e == nil {
			return nil, fmt.Errorf("session %q references unknown edge %q", sess.ID, sess.Edge)
		}
		cr, ok := st.Replicas[sess.CallerRep]
		if !ok || cr.Service != e.Caller {
			return nil, fmt.Errorf("session %q caller replica missing", sess.ID)
		}
		ce, ok := st.Replicas[sess.CalleeRep]
		if !ok || ce.Service != e.Callee {
			return nil, fmt.Errorf("session %q callee replica missing", sess.ID)
		}
		if cr.Version != sess.CallerVersion || ce.Version != sess.CalleeVersion {
			return nil, fmt.Errorf("session %q start versions do not match replicas", sess.ID)
		}
		cp := *sess
		st.pins[sess.ID] = &cp
	}
	// Seed the coexistence counters so the initial config key has the same
	// shape as every post-action key (zero means no mixed layout yet).
	for name := range spec.Services {
		st.coexistAge[name] = 0
	}
	for _, ev := range cfg.ObservedEvents {
		st.drainedEvents[ev] = true
	}
	// Observed events only unlock the corresponding drain action; the session
	// pin is released inside the plan so drain remains an explicit,
	// irreversible step.

	if cfg.MaxVisited <= 0 {
		cfg.MaxVisited = 20000
	}
	if cfg.MaxBlocked <= 0 {
		cfg.MaxBlocked = 40
	}
	return &Planner{
		spec:             spec,
		initial:          st,
		initialKey:       st.key(),
		initialConfigKey: st.configKey(),
		nextReplica:      map[string]int{},
		rbCache:          map[string][]Action{},
		rbFail:           map[string]bool{},
		cfg:              cfg,
	}, nil
}

type checkFailure struct {
	reason  BlockedReason
	detail  string
	caller  string
	callee  string
	edge    string
	diverge *Divergence
}

// checkConnectivity enforces the full-mesh rule across every edge plus the
// codec/stream obligations of every still-pinned session.
func (p *Planner) checkConnectivity(st *State, a Action) *checkFailure {
	if f := p.checkMesh(st); f != nil {
		return f
	}
	return p.checkPins(st)
}

// checkMesh verifies every traffic-receiving caller reaches every candidate
// receiver across every edge.
func (p *Planner) checkMesh(st *State) *checkFailure {
	for _, svc := range p.spec.Services {
		if len(st.serviceReplicas(svc.Name)) == 0 {
			return &checkFailure{reason: ReasonNoCandidate,
				detail: fmt.Sprintf("service %q has no live replica", svc.Name)}
		}
	}
	for _, e := range p.spec.Edges {
		callers := st.serviceReplicas(e.Caller)
		callees := st.serviceReplicas(e.Callee)
		if len(callees) == 0 {
			return &checkFailure{reason: ReasonNoCandidate, edge: e.Key(),
				detail: fmt.Sprintf("service %q has no candidate receiver", e.Callee)}
		}
		for _, cr := range callers {
			for _, ce := range callees {
				if !e.profileConnects(cr.Profile, ce.Profile) {
					return &checkFailure{
						reason: ReasonCodec, edge: e.Key(), caller: cr.ID, callee: ce.ID,
						detail: fmt.Sprintf("profiles %s and %s are not mutually compatible",
							cr.Profile.key(), ce.Profile.key()),
					}
				}
			}
		}
	}
	return nil
}

// checkPins verifies the codec and stream obligations of every session that is
// still pinned to its starting endpoint pair.
func (p *Planner) checkPins(st *State) *checkFailure {
	for _, e := range p.spec.Edges {
		for _, sess := range st.pins {
			if sess.Edge != e.Key() {
				continue
			}
			cr := st.Replicas[sess.CallerRep]
			ce := st.Replicas[sess.CalleeRep]
			if cr == nil || ce == nil {
				return &checkFailure{
					reason: ReasonPinRemoved, edge: e.Key(),
					detail: fmt.Sprintf("pinned session %q lost an endpoint replica", sess.ID),
				}
			}
			if !e.supportsRequest(sess.ReqCodec, ce.Profile.Request) ||
				!e.supportsResponse(sess.RespCodec, cr.Profile.Response) {
				return &checkFailure{
					reason: ReasonPinBroken, edge: e.Key(), caller: cr.ID, callee: ce.ID,
					detail: fmt.Sprintf("pinned session %q codec pair %s/%s no longer supported",
						sess.ID, sess.ReqCodec, sess.RespCodec),
				}
			}
			// In-flight stream equivalence for the version pair this session
			// actually started with.
			oldProto := e.Streams[VPair{sess.CallerVersion, sess.CalleeVersion}]
			newProto := e.Streams[VPair{cr.Version, ce.Version}]
			if oldProto != nil && newProto != nil && oldProto != newProto {
				if d := oldProto.Compare(newProto); d != nil {
					return &checkFailure{
						reason: ReasonStreamDivergence, edge: e.Key(),
						caller: cr.ID, callee: ce.ID, diverge: d,
						detail: fmt.Sprintf("pinned session %q crosses non-equivalent stream FSMs", sess.ID),
					}
				}
			}
		}
	}
	return nil
}

type planNode struct {
	st     *State
	parent *planNode
	action Action
	depth  int
}

// rbVisitNode is one node of an on-demand rollback BFS.
type rbVisitNode struct {
	st     *State
	parent *rbVisitNode
	action Action // concrete inverse action that led here
	spent  map[string]int
	depth  int
}

// Plan runs shortest-plan BFS and returns a Plan or a reachable Frontier.
func (p *Planner) Plan() (*Plan, *Frontier) {
	if p.isGoal(p.initial) {
		return &Plan{Steps: []Step{}, GoalReached: true, VisitedStates: 1, FrontierReached: 1}, nil
	}
	if fail := p.checkConnectivity(p.initial, Action{}); fail != nil {
		return nil, &Frontier{
			ReachedStates: 1, VisitedStates: 1,
			Blocked:        []Blocked{p.failureToBlocked(Action{}, fail)},
			WaitingEvents:  p.waitingEvents(p.initial),
			FrontierStates: []string{p.initialKey},
		}
	}

	visited := map[string]bool{p.initialKey: true}
	queue := []*planNode{{st: p.initial, depth: 0}}

	var frontier *Frontier
	blockedSeen := map[string]bool{}
	frontierSeen := map[string]bool{}
	addBlocked := func(b Blocked) {
		if len(frontier.Blocked) >= p.cfg.MaxBlocked {
			return
		}
		k := b.Action.String() + "|" + string(b.Reason) + "|" + b.Detail
		if blockedSeen[k] {
			return
		}
		blockedSeen[k] = true
		frontier.Blocked = append(frontier.Blocked, b)
	}

	for len(queue) > 0 {
		cur := queue[0]
		queue = queue[1:]
		if len(visited) > p.cfg.MaxVisited {
			break
		}

		actions, genBlocked := p.actions(cur.st)
		for _, b := range genBlocked {
			if frontier == nil {
				frontier = &Frontier{}
			}
			addBlocked(b)
		}

		expanded := false
		for _, a := range actions {
			next := p.apply(cur.st, a)
			if fail := p.checkConnectivity(next, a); fail != nil {
				if frontier == nil {
					frontier = &Frontier{}
				}
				addBlocked(p.failureToBlocked(a, fail))
				continue
			}
			if b := p.coexistExceeded(next); b != nil {
				if frontier == nil {
					frontier = &Frontier{}
				}
				b.Action = a
				addBlocked(*b)
				continue
			}
			// Drain is irreversible: rollback reservation is computed on the
			// config state and must never contain a drain step.
			if _, ok := p.reserveRollback(next); !ok {
				if frontier == nil {
					frontier = &Frontier{}
				}
				addBlocked(Blocked{
					Action: a, Reason: ReasonRollbackUnreachable,
					Detail: fmt.Sprintf("no config-only sequence within rollback budget returns to the start"),
				})
				continue
			}
			key := next.key()
			if visited[key] {
				continue
			}
			visited[key] = true
			child := &planNode{st: next, parent: cur, action: a, depth: cur.depth + 1}
			if p.isGoal(next) {
				return p.reconstruct(child, visited), nil
			}
			queue = append(queue, child)
			expanded = true
		}
		if !expanded {
			if frontier == nil {
				frontier = &Frontier{}
			}
			if len(frontier.FrontierStates) < p.cfg.MaxBlocked && !frontierSeen[cur.st.key()] {
				frontierSeen[cur.st.key()] = true
				frontier.FrontierStates = append(frontier.FrontierStates, cur.st.key())
			}
		}
	}

	if frontier == nil {
		frontier = &Frontier{}
	}
	frontier.ReachedStates = len(visited)
	frontier.VisitedStates = len(visited)
	frontier.WaitingEvents = p.waitingEvents(p.initial)
	// Also collect events needed anywhere reachable.
	return nil, frontier
}

func (p *Planner) failureToBlocked(a Action, f *checkFailure) Blocked {
	return Blocked{
		Action: a, Reason: f.reason, Detail: f.detail,
		Caller: f.caller, Callee: f.callee, Edge: f.edge, Diverge: f.diverge,
	}
}

func (p *Planner) waitingEvents(st *State) []string {
	seen := map[string]bool{}
	var out []string
	for _, sess := range st.pins {
		if !seen[sess.DrainEvent] {
			seen[sess.DrainEvent] = true
			out = append(out, sess.DrainEvent)
		}
	}
	sort.Strings(out)
	return out
}

func (p *Planner) isGoal(st *State) bool {
	if len(st.pins) != 0 {
		return false
	}
	for name, svc := range p.spec.Services {
		reps := st.serviceReplicas(name)
		if len(reps) != len(p.initial.serviceReplicas(name)) {
			return false
		}
		for _, r := range reps {
			if r.Version != svc.Target {
				return false
			}
		}
	}
	return true
}

// actions enumerates every legal single-item change from this state. Entries
// rejected by local (non-edge) constraints come back as Blocked diagnostics.
func (p *Planner) actions(st *State) ([]Action, []Blocked) {
	var actions []Action
	var blocked []Blocked

	svcNames := make([]string, 0, len(p.spec.Services))
	for name := range p.spec.Services {
		svcNames = append(svcNames, name)
	}
	sort.Strings(svcNames)

	for _, name := range svcNames {
		svc := p.spec.Services[name]
		reps := st.serviceReplicas(name)
		versions := make([]string, 0, len(svc.Versions))
		for vn := range svc.Versions {
			versions = append(versions, vn)
		}
		sort.Strings(versions)

		// Replace a replica: the new copy runs a target version and one of
		// that version's supported profiles. Version and profile are the
		// atomic content of the single replica replacement.
		for _, r := range reps {
			curV := svc.Versions[r.Version]
			for _, vn := range versions {
				if vn == r.Version {
					continue
				}
				nv := svc.Versions[vn]
				for _, np := range nv.Profiles {
					a := Action{
						Kind: ActReplace, Service: name, Replica: r.ID,
						FromVer:  r.Version,
						ToVer:    vn,
						FromProf: r.Profile,
						ToProf:   np,
					}
					if p.pinTouchesReplica(st, r.ID) {
						blocked = append(blocked, Blocked{Action: a, Reason: ReasonPinBroken,
							Detail: "replica hosts a pinned session; drain it or add a transitional replica"})
						continue
					}
					actions = append(actions, a)
				}
			}

			// Profile changes within the current version.
			for _, np := range curV.Profiles {
				if np == r.Profile {
					continue
				}
				actions = append(actions, Action{
					Kind: ActProfile, Service: name, Replica: r.ID,
					FromProf: r.Profile, ToProf: np,
				})
			}
		}

		// Add a replica, including a +1 read-compatible transitional copy.
		if len(reps) < svc.MaxReplicas {
			for _, vn := range versions {
				nv := svc.Versions[vn]
				for _, prof := range nv.Profiles {
					actions = append(actions, Action{
						Kind: ActAdd, Service: name, ToVer: vn, ToProf: prof,
					})
				}
			}
		} else {
			for _, vn := range versions {
				blocked = append(blocked, Blocked{
					Action: Action{Kind: ActAdd, Service: name, ToVer: vn},
					Reason: ReasonCapacity,
					Detail: fmt.Sprintf("service %q already at MaxReplicas=%d", name, svc.MaxReplicas),
				})
			}
		}

		// Remove a replica (never the last one, never a pinned endpoint).
		if len(reps) > 1 {
			for _, r := range reps {
				a := Action{Kind: ActRemove, Service: name, Replica: r.ID, FromVer: r.Version}
				if p.pinTouchesReplica(st, r.ID) {
					blocked = append(blocked, Blocked{Action: a, Reason: ReasonPinRemoved,
						Detail: "replica hosts a pinned session and cannot be removed before drain"})
					continue
				}
				actions = append(actions, a)
			}
		}
	}

	// Drain actions: only events actually observed can be processed.
	needed := map[string]bool{}
	for _, sess := range st.pins {
		needed[sess.DrainEvent] = true
	}
	events := make([]string, 0, len(needed))
	for ev := range needed {
		events = append(events, ev)
	}
	sort.Strings(events)
	for _, ev := range events {
		a := Action{Kind: ActDrain, DrainEvent: ev}
		if !st.drainedEvents[ev] {
			blocked = append(blocked, Blocked{Action: a, Reason: ReasonDrainNotObserved,
				Detail: fmt.Sprintf("pinned sessions require drain event %q before they can be released", ev)})
			continue
		}
		actions = append(actions, a)
	}

	sort.SliceStable(actions, func(i, j int) bool {
		if actions[i].Kind != actions[j].Kind {
			return actions[i].Kind < actions[j].Kind
		}
		return actions[i].String() < actions[j].String()
	})
	return actions, blocked
}

func (p *Planner) pinTouchesReplica(st *State, replicaID string) bool {
	for _, sess := range st.pins {
		if sess.CallerRep == replicaID || sess.CalleeRep == replicaID {
			return true
		}
	}
	return false
}

func (p *Planner) apply(st *State, a Action) *State {
	next := st.clone()
	switch a.Kind {
	case ActReplace:
		r := next.Replicas[a.Replica]
		r.Version = a.ToVer
		r.Profile = a.ToProf
	case ActProfile:
		r := next.Replicas[a.Replica]
		r.Profile = a.ToProf
	case ActAdd:
		id := p.freshReplicaID(next, a.Service)
		next.Replicas[id] = &Replica{ID: id, Service: a.Service, Version: a.ToVer, Profile: a.ToProf}
	case ActRemove:
		delete(next.Replicas, a.Replica)
	case ActDrain:
		for id, sess := range next.pins {
			if sess.DrainEvent == a.DrainEvent {
				delete(next.pins, id)
			}
		}
	}
	p.advanceAges(next, a)
	return next
}

func (p *Planner) freshReplicaID(st *State, service string) string {
	n := p.nextReplica[service] + 1
	for {
		id := fmt.Sprintf("%s-r%d", service, n)
		if _, exists := st.Replicas[id]; !exists {
			p.nextReplica[service] = n
			return id
		}
		n++
	}
}

// advanceAges maintains the per-service counter of steps spent in a
// mixed-version layout and enforces the coexistence bound.
func (p *Planner) advanceAges(st *State, a Action) {
	for name := range p.spec.Services {
		if st.mixedVersions(name) {
			st.coexistAge[name]++
		} else {
			st.coexistAge[name] = 0
		}
	}
}

func (p *Planner) coexistExceeded(st *State) *Blocked {
	for name, svc := range p.spec.Services {
		if st.coexistAge[name] > svc.CoexistSteps {
			return &Blocked{
				Reason: ReasonCoexistLimit,
				Detail: fmt.Sprintf("service %q exceeded %d coexistence steps (at %d)",
					name, svc.CoexistSteps, st.coexistAge[name]),
			}
		}
	}
	return nil
}

// reserveRollback searches, from the given state, for a config/replica-only
// sequence that returns the fleet to the initial layout within every
// service's rollback budget. Each inverse step is validated against the
// full mesh as it is taken, so the reserved sequence never breaks a live
// edge. Drain actions are never generated: drain is irreversible.
func (p *Planner) reserveRollback(st *State) ([]Action, bool) {
	startKey := st.configKey()
	if rb, ok := p.rbCache[startKey]; ok {
		return rb, true
	}
	if p.rbFail[startKey] {
		return nil, false
	}
	if startKey == p.initialConfigKey {
		p.rbCache[startKey] = []Action{}
		return []Action{}, true
	}
	// Cheap necessary-condition prune: the rollback must at minimum undo the
	// per-service layout differences, so any service already beyond its budget
	// makes the search hopeless before BFS even starts.
	if !p.withinBudgetLowerBound(st, map[string]int{}) {
		p.rbFail[startKey] = true
		return nil, false
	}

	maxDepth := 0
	for _, svc := range p.spec.Services {
		maxDepth += svc.RollbackBudget
	}
	// Rollback BFS is bounded independently of the forward search so an
	// unreachable layout fails fast instead of exploring the whole space.
	rbCap := p.cfg.MaxVisited
	if rbCap > 800 {
		rbCap = 800
	}
	visited := map[string]bool{startKey: true}
	queue := []*rbVisitNode{{st: st, spent: map[string]int{}}}

	for len(queue) > 0 {
		cur := queue[0]
		queue = queue[1:]
		if cur.depth >= maxDepth || len(visited) >= rbCap || p.rbExplored >= p.cfg.MaxVisited {
			continue
		}
		p.rbExplored++
		for _, inv := range p.rollbackMoves(cur.st) {
			svc := p.spec.Services[inv.Service]
			if cur.spent[inv.Service]+1 > svc.RollbackBudget {
				continue
			}
			next := p.apply(cur.st, inv)
			if p.coexistExceeded(next) != nil {
				continue
			}
			// Rollback must keep every live caller/callee pair connected.
			// Pinned sessions are not re-evaluated: a drained pin cannot be
			// resurrected, and a still-pinned endpoint is never removable.
			if f := p.checkMesh(next); f != nil {
				continue
			}
			key := next.configKey()
			if visited[key] || p.rbFail[key] {
				continue
			}
			// A layout with a known rollback gives one for this start too:
			// splice the current prefix onto the cached suffix.
			if cached, ok := p.rbCache[key]; ok {
				rb := append(p.rebuildInversePath(cur), inv)
				rb = append(rb, cached...)
				p.rbCache[startKey] = rb
				return rb, true
			}
			visited[key] = true
			spent := map[string]int{}
			for k, v := range cur.spent {
				spent[k] = v
			}
			spent[inv.Service]++
			if !p.withinBudgetLowerBound(next, spent) {
				continue
			}
			child := &rbVisitNode{st: next, parent: cur, action: inv, spent: spent, depth: cur.depth + 1}
			if key == p.initialConfigKey {
				rb := p.rebuildInversePath(child)
				p.rbCache[startKey] = rb
				return rb, true
			}
			queue = append(queue, child)
		}
	}
	p.rbFail[startKey] = true
	return nil, false
}

// withinBudgetLowerBound applies a necessary (not sufficient) condition: the
// number of config actions already spent on a service plus the minimum still
// needed to restore that service's initial layout cannot exceed its budget.
// The minimum remaining work counts mismatched replica versions/profiles and
// any replica-count difference.
func (p *Planner) withinBudgetLowerBound(st *State, spent map[string]int) bool {
	initByService := map[string][]*Replica{}
	for _, r := range p.initial.Replicas {
		initByService[r.Service] = append(initByService[r.Service], r)
	}
	for name, svc := range p.spec.Services {
		cur := st.serviceReplicas(name)
		initial := initByService[name]
		need := 0
		// A differing replica count needs at least one add/remove per unit.
		if d := len(cur) - len(initial); d > 0 {
			need += d
		} else {
			need -= d
		}
		// Pair up replicas by id; a version or profile mismatch needs at least
		// one replacement/profile action.
		curByID := map[string]*Replica{}
		for _, r := range cur {
			curByID[r.ID] = r
		}
		for _, ir := range initial {
			if cr, ok := curByID[ir.ID]; ok {
				if cr.Version != ir.Version || cr.Profile != ir.Profile {
					need++
				}
			}
		}
		if spent[name]+need > svc.RollbackBudget {
			return false
		}
	}
	return true
}

// rebuildInversePath walks the reverse BFS tree back to its start node and
// returns the inverse actions in execution order.
func (p *Planner) rebuildInversePath(n *rbVisitNode) []Action {
	var rev []Action
	for n.parent != nil {
		rev = append(rev, n.action)
		n = n.parent
	}
	for i, j := 0, len(rev)-1; i < j; i, j = i+1, j-1 {
		rev[i], rev[j] = rev[j], rev[i]
	}
	return rev
}

// rollbackMoves enumerates the concrete config/replica-only moves available
// from one layout, expressed directly as the action to apply next while
// rolling back (replace/profile inversions, add, remove).
func (p *Planner) rollbackMoves(st *State) []Action {
	var out []Action
	svcNames := make([]string, 0, len(p.spec.Services))
	for name := range p.spec.Services {
		svcNames = append(svcNames, name)
	}
	sort.Strings(svcNames)
	for _, name := range svcNames {
		svc := p.spec.Services[name]
		reps := st.serviceReplicas(name)
		versions := make([]string, 0, len(svc.Versions))
		for vn := range svc.Versions {
			versions = append(versions, vn)
		}
		sort.Strings(versions)
		for _, r := range reps {
			for _, vn := range versions {
				if vn == r.Version {
					continue
				}
				for _, np := range svc.Versions[vn].Profiles {
					out = append(out, Action{
						Kind: ActReplace, Service: name, Replica: r.ID,
						FromVer: r.Version, ToVer: vn, FromProf: r.Profile, ToProf: np,
					})
				}
			}
			for _, np := range svc.Versions[r.Version].Profiles {
				if np != r.Profile {
					out = append(out, Action{
						Kind: ActProfile, Service: name, Replica: r.ID,
						FromProf: r.Profile, ToProf: np,
					})
				}
			}
			if len(reps) > 1 && !p.pinTouchesReplica(st, r.ID) {
				out = append(out, Action{Kind: ActRemove, Service: name, Replica: r.ID, FromVer: r.Version})
			}
		}
		if len(reps) < svc.MaxReplicas {
			for _, vn := range versions {
				for _, prof := range svc.Versions[vn].Profiles {
					out = append(out, Action{Kind: ActAdd, Service: name, ToVer: vn, ToProf: prof})
				}
			}
		}
	}
	return out
}

// reconstruct turns the goal node back into forward steps and attaches the
// reserved rollback sequence to every successful prefix.
func (p *Planner) reconstruct(n *planNode, visited map[string]bool) *Plan {
	var revAct []Action
	for n.parent != nil {
		revAct = append(revAct, n.action)
		n = n.parent
	}
	for i, j := 0, len(revAct)-1; i < j; i, j = i+1, j-1 {
		revAct[i], revAct[j] = revAct[j], revAct[i]
	}
	steps := make([]Step, 0, len(revAct))
	cur := p.initial
	for i, a := range revAct {
		cur = p.apply(cur, a)
		rb, ok := p.reserveRollback(cur)
		if !ok {
			// Every plan prefix passed the reservation gate before enqueueing,
			// so this is an internal inconsistency, not a user-facing frontier.
			rb = nil
		}
		steps = append(steps, Step{Index: i + 1, Action: a, Rollback: RollbackReserve{
			PrefixLen: i + 1, Actions: rb,
		}})
	}
	return &Plan{Steps: steps, GoalReached: true, VisitedStates: len(visited), FrontierReached: len(visited)}
}
