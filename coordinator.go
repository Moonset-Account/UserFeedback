// Package shutdowncoord implements a restart-safe coordinator for draining a
// group of containers during shutdown.
//
// # Model
//
// A container group is described by a topology of instances (each carrying an
// instance generation) and drain edges. An edge {Consumer: C, Upstream: U}
// means C is reading U's buffer: C may still read already-buffered data after
// it stops accepting new input, and U must not enter its closing phase (run
// its shutdown hook and exit) until C has reported that it finished reading
// AND has exited.
//
// Actions emitted by the coordinator:
//
//	stop_accepting  - every live instance, at shutdown request time
//	execute_hook    - an instance whose drain dependencies are satisfied
//	request_exit    - an instance whose hook returned while still running
//	force_kill      - an instance that has not exited when the shared grace
//	                  deadline is reached
//
// Events consumed by the coordinator:
//
//	DrainReport  - a consumer finished reading its upstream buffers
//	HookReturn   - a shutdown hook returned (late returns are ignored)
//	ExitEvent    - an instance exited (possibly spontaneously)
//	ActionAck    - an emitted action was durably confirmed by the executor
//
// The coordinator is deterministic for a given event order, is safe for
// concurrent use, and persists its state through a Store after every input.
// A restarted coordinator resumes from the persisted snapshot, never re-emits
// an acknowledged action, and re-emits only still-relevant unacknowledged
// actions through Resume.
package shutdowncoord

import (
	"fmt"
	"strings"
	"sync"
	"time"
)

// ContainerID identifies a container within the group.
type ContainerID string

// InstanceGen is the generation of a concrete container instance. Events and
// actions carry it so that stale events from a previous instance are ignored
// and action identities are stable across controller restarts.
type InstanceGen uint64

// Instance is one container instance in the group.
type Instance struct {
	Container ContainerID
	Gen       InstanceGen
}

// Edge declares a drain dependency: Consumer is reading Upstream's buffer.
// Upstream may only enter its closing phase after Consumer reported drain
// completion and exited.
type Edge struct {
	Consumer ContainerID
	Upstream ContainerID
}

// Topology is the static description of the container group.
type Topology struct {
	Instances []Instance
	Edges     []Edge
}

// Phase is the lifecycle phase of one container instance.
type Phase string

const (
	// PhasePending is the state before the shutdown request.
	PhasePending Phase = "pending"
	// PhaseDraining means stop-accepting was issued and the instance is
	// draining its input or waiting for its consumers to drain.
	PhaseDraining Phase = "draining"
	// PhaseHookRunning means the shutdown hook was issued and has not
	// returned yet.
	PhaseHookRunning Phase = "hook_running"
	// PhaseExitRequested means the hook returned and exit was requested.
	PhaseExitRequested Phase = "exit_requested"
	// PhaseExited means a normal exit event was observed.
	PhaseExited Phase = "exited"
	// PhaseForceKilled means the shared grace deadline expired while the
	// instance was still running and a force kill was issued.
	PhaseForceKilled Phase = "force_killed"
)

// ActionKind enumerates the actions the coordinator emits.
type ActionKind string

const (
	ActionStopAccepting ActionKind = "stop_accepting"
	ActionExecuteHook   ActionKind = "execute_hook"
	ActionRequestExit   ActionKind = "request_exit"
	ActionForceKill     ActionKind = "force_kill"
)

// Action is one instruction for the executor. ID is stable for a given
// (kind, container, generation) triple, which makes delivery idempotent.
type Action struct {
	ID        string
	Kind      ActionKind
	Container ContainerID
	Gen       InstanceGen
	Reason    string
}

// Event is one input to the coordinator.
type Event interface {
	isEvent()
}

// DrainReport reports that the consumer finished reading its upstream buffers.
type DrainReport struct {
	Container ContainerID
	Gen       InstanceGen
}

// HookReturn reports that the shutdown hook returned. It is ignored unless the
// matching instance is currently in its hook phase; in particular a hook that
// returns after a spontaneous exit must not move the instance back into
// processing.
type HookReturn struct {
	Container ContainerID
	Gen       InstanceGen
}

// ExitEvent reports that an instance exited, with or without a prior hook
// return.
type ExitEvent struct {
	Container ContainerID
	Gen       InstanceGen
}

// ActionAck confirms that an emitted action was durably delivered/performed.
type ActionAck struct {
	ActionID string
}

func (DrainReport) isEvent() {}
func (HookReturn) isEvent()  {}
func (ExitEvent) isEvent()   {}
func (ActionAck) isEvent()   {}

// EdgeFailure describes an edge that had not fully drained when the grace
// deadline expired.
type EdgeFailure struct {
	Edge
	ConsumerDrained bool
	ConsumerExited  bool
}

// String renders the failure for logs and reports.
func (f EdgeFailure) String() string {
	var reasons []string
	if !f.ConsumerDrained {
		reasons = append(reasons, "consumer did not report drain complete")
	}
	if !f.ConsumerExited {
		reasons = append(reasons, "consumer did not exit")
	}
	return fmt.Sprintf("edge consumer=%s upstream=%s: %s",
		f.Consumer, f.Upstream, strings.Join(reasons, "; "))
}

// Clock supplies the current time. Tests use a fake clock.
type Clock interface {
	Now() time.Time
}

type realClock struct{}

func (realClock) Now() time.Time { return time.Now() }

// InstanceState is the persisted runtime state of one instance.
type InstanceState struct {
	Gen          InstanceGen
	Phase        Phase
	Drained      bool
	HookReturned bool
}

// Coordinator is the shutdown coordinator. It is safe for concurrent use.
type Coordinator struct {
	mu    sync.Mutex
	clock Clock
	store Store

	topo     Topology
	grace    time.Duration
	order    []ContainerID
	spec     map[ContainerID]Instance
	edges    []Edge
	inEdges  map[ContainerID][]int // incoming drain edges (container is consumer)
	outEdges map[ContainerID][]int // outgoing drain edges (container is upstream)

	started    bool
	finalized  bool
	deadline   time.Time
	states     map[ContainerID]*InstanceState
	acked      map[string]bool
	issued     map[string]bool
	issuedList []Action
	incomplete []EdgeFailure
}

// New builds a coordinator. When store contains a snapshot the coordinator
// resumes from it (the snapshot's topology and grace period win over the
// arguments); otherwise the supplied topology is validated and used.
func New(topo Topology, grace time.Duration, clock Clock, store Store) (*Coordinator, error) {
	if clock == nil {
		clock = realClock{}
	}
	if store == nil {
		store = &MemoryStore{}
	}
	c := &Coordinator{
		clock:  clock,
		store:  store,
		states: map[ContainerID]*InstanceState{},
		acked:  map[string]bool{},
		issued: map[string]bool{},
	}
	if snap, ok, err := store.Load(); err != nil {
		return nil, fmt.Errorf("load snapshot: %w", err)
	} else if ok {
		if err := c.hydrate(snap); err != nil {
			return nil, fmt.Errorf("resume snapshot: %w", err)
		}
		return c, nil
	}
	if grace <= 0 {
		return nil, fmt.Errorf("grace period must be positive, got %s", grace)
	}
	if err := validateTopology(topo); err != nil {
		return nil, err
	}
	c.topo = topo
	c.grace = grace
	c.buildIndexes(topo.Instances, topo.Edges)
	for _, inst := range topo.Instances {
		c.states[inst.Container] = &InstanceState{Gen: inst.Gen, Phase: PhasePending}
	}
	return c, nil
}

// buildIndexes fills order/spec/edge lookups. Callers must have validated the
// topology (or hydrated a validated snapshot).
func (c *Coordinator) buildIndexes(instances []Instance, edges []Edge) {
	c.order = make([]ContainerID, 0, len(instances))
	c.spec = make(map[ContainerID]Instance, len(instances))
	for _, inst := range instances {
		c.order = append(c.order, inst.Container)
		c.spec[inst.Container] = inst
	}
	c.edges = append([]Edge(nil), edges...)
	c.inEdges = make(map[ContainerID][]int, len(instances))
	c.outEdges = make(map[ContainerID][]int, len(instances))
	for i, e := range c.edges {
		c.inEdges[e.Consumer] = append(c.inEdges[e.Consumer], i)
		c.outEdges[e.Upstream] = append(c.outEdges[e.Upstream], i)
	}
}

// Start begins shutdown at now. The shared grace deadline is now+grace. The
// first batch stops intake for every instance, then hooks fire for instances
// whose drain gates are already satisfied. Calling Start again is a no-op.
func (c *Coordinator) Start(now time.Time) []Action {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.started {
		return nil
	}
	var batch []Action
	c.started = true
	c.deadline = now.Add(c.grace)
	for _, id := range c.order {
		s := c.states[id]
		if s.Phase != PhasePending {
			continue
		}
		s.Phase = PhaseDraining
		c.emit(&batch, ActionStopAccepting, id, "shutdown requested; stop accepting new input")
	}
	c.advance(&batch)
	c.saveLocked()
	return batch
}

// Handle applies one event and returns the actions it caused. When the shared
// deadline has already passed according to the clock, it also finalizes the
// group (force kills and the incomplete-edge report).
func (c *Coordinator) Handle(ev Event) []Action {
	c.mu.Lock()
	defer c.mu.Unlock()
	var batch []Action
	switch e := ev.(type) {
	case ActionAck:
		if e.ActionID != "" {
			c.acked[e.ActionID] = true
		}
	case DrainReport:
		c.onDrain(e, &batch)
	case HookReturn:
		c.onHookReturn(e, &batch)
	case ExitEvent:
		c.onExit(e, &batch)
	}
	if c.started && !c.finalized && !c.clock.Now().Before(c.deadline) {
		c.finalizeLocked(&batch)
	}
	c.saveLocked()
	return batch
}

// Tick lets a timer drive the shared deadline. It finalizes the group when now
// is at or past the deadline. Calls before the deadline are no-ops.
func (c *Coordinator) Tick(now time.Time) []Action {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.started || c.finalized || now.Before(c.deadline) {
		return nil
	}
	var batch []Action
	c.finalizeLocked(&batch)
	c.saveLocked()
	return batch
}

// Resume returns still-relevant issued actions that were not acknowledged
// before a controller restart. It must not be used to replay acknowledged
// actions: those are filtered out. Resuming twice is harmless; the executor is
// expected to treat action IDs idempotently.
func (c *Coordinator) Resume() []Action {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.started {
		return nil
	}
	var out []Action
	for _, a := range c.issuedList {
		if c.acked[a.ID] {
			continue
		}
		if !c.relevantLocked(a) {
			continue
		}
		out = append(out, a)
	}
	return out
}

// Phase returns the current phase of a container.
func (c *Coordinator) Phase(id ContainerID) (Phase, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	s, ok := c.states[id]
	if !ok {
		return "", false
	}
	return s.Phase, true
}

// State returns a copy of an instance's runtime state.
func (c *Coordinator) State(id ContainerID) (InstanceState, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	s, ok := c.states[id]
	if !ok {
		return InstanceState{}, false
	}
	return *s, true
}

// Deadline returns the shared grace deadline. The zero time means shutdown has
// not started.
func (c *Coordinator) Deadline() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.deadline
}

// Finalized reports whether the group has been finalized at the deadline.
func (c *Coordinator) Finalized() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.finalized
}

// IncompleteEdges returns the drain edges that had not completed when the grace
// deadline expired. It is empty before finalization.
func (c *Coordinator) IncompleteEdges() []EdgeFailure {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]EdgeFailure(nil), c.incomplete...)
}

func (c *Coordinator) onDrain(e DrainReport, batch *[]Action) {
	s := c.lookupLocked(e.Container, e.Gen)
	if s == nil {
		return
	}
	switch s.Phase {
	case PhaseExited, PhaseForceKilled, PhasePending:
		return
	}
	s.Drained = true
	c.advance(batch)
}

func (c *Coordinator) onHookReturn(e HookReturn, batch *[]Action) {
	s := c.lookupLocked(e.Container, e.Gen)
	if s == nil {
		return
	}
	// The hook only counts while the instance is actually running its hook.
	// This is what makes a late return after a spontaneous exit inert.
	if s.Phase != PhaseHookRunning {
		return
	}
	s.HookReturned = true
	s.Phase = PhaseExitRequested
	c.emit(batch, ActionRequestExit, e.Container, "shutdown hook returned; request exit")
}

func (c *Coordinator) onExit(e ExitEvent, batch *[]Action) {
	s := c.lookupLocked(e.Container, e.Gen)
	if s == nil {
		return
	}
	if s.Phase == PhaseExited || s.Phase == PhaseForceKilled {
		return
	}
	s.Phase = PhaseExited
	c.advance(batch)
}

// lookupLocked returns the state for a live (generation-matching) instance, or
// nil when the event is stale or unknown.
func (c *Coordinator) lookupLocked(id ContainerID, gen InstanceGen) *InstanceState {
	s, ok := c.states[id]
	if !ok || s.Gen != gen {
		return nil
	}
	return s
}

// advance emits hook actions for every instance whose drain gates are met.
// It runs to a fixpoint because an instance that becomes eligible in one pass
// may unlock another (the ordering within a pass is deterministic: topology
// order).
func (c *Coordinator) advance(batch *[]Action) {
	for {
		progressed := false
		for _, id := range c.order {
			s := c.states[id]
			if s.Phase != PhaseDraining {
				continue
			}
			if !c.eligibleLocked(id) {
				continue
			}
			s.Phase = PhaseHookRunning
			c.emit(batch, ActionExecuteHook, id, "drain dependencies satisfied; run shutdown hook")
			progressed = true
		}
		if !progressed {
			return
		}
	}
}

// eligibleLocked reports whether an instance may enter its closing phase.
//
//  1. the shutdown request has been processed and intake stopped;
//  2. if the instance itself consumes something, it reported its own drain;
//  3. every consumer of this instance reported drain completion AND exited.
func (c *Coordinator) eligibleLocked(id ContainerID) bool {
	s := c.states[id]
	if s.Phase != PhaseDraining {
		return false
	}
	if len(c.inEdges[id]) > 0 && !s.Drained {
		return false
	}
	for _, ei := range c.outEdges[id] {
		e := c.edges[ei]
		cons := c.states[e.Consumer]
		if cons == nil {
			return false
		}
		if !cons.Drained || cons.Phase != PhaseExited {
			return false
		}
	}
	return true
}

func (c *Coordinator) finalizeLocked(batch *[]Action) {
	if c.finalized {
		return
	}
	c.finalized = true
	for _, id := range c.order {
		s := c.states[id]
		if s.Phase == PhaseExited || s.Phase == PhaseForceKilled {
			continue
		}
		s.Phase = PhaseForceKilled
		c.emit(batch, ActionForceKill, id, "shared grace deadline reached; force kill instance")
	}
	c.incomplete = c.incompleteEdgesLocked()
}

func (c *Coordinator) incompleteEdgesLocked() []EdgeFailure {
	out := make([]EdgeFailure, 0)
	for _, e := range c.edges {
		cons := c.states[e.Consumer]
		if cons == nil {
			out = append(out, EdgeFailure{Edge: e})
			continue
		}
		drained := cons.Drained
		exited := cons.Phase == PhaseExited
		if drained && exited {
			continue
		}
		out = append(out, EdgeFailure{Edge: e, ConsumerDrained: drained, ConsumerExited: exited})
	}
	return out
}

func (c *Coordinator) emit(batch *[]Action, kind ActionKind, id ContainerID, reason string) Action {
	inst := c.spec[id]
	a := Action{
		ID:        actionID(kind, id, inst.Gen),
		Kind:      kind,
		Container: id,
		Gen:       inst.Gen,
		Reason:    reason,
	}
	if c.issued[a.ID] || c.acked[a.ID] {
		return a
	}
	c.issued[a.ID] = true
	c.issuedList = append(c.issuedList, a)
	*batch = append(*batch, a)
	return a
}

func (c *Coordinator) relevantLocked(a Action) bool {
	s, ok := c.states[a.Container]
	if !ok || s.Gen != a.Gen {
		return false
	}
	switch a.Kind {
	case ActionStopAccepting:
		return s.Phase == PhaseDraining || s.Phase == PhaseHookRunning || s.Phase == PhaseExitRequested
	case ActionExecuteHook:
		return s.Phase == PhaseHookRunning
	case ActionRequestExit:
		return s.Phase == PhaseExitRequested
	case ActionForceKill:
		return c.finalized && s.Phase == PhaseForceKilled
	}
	return false
}

func actionID(kind ActionKind, id ContainerID, gen InstanceGen) string {
	return fmt.Sprintf("%s/%s/%d", kind, id, gen)
}

// validateTopology rejects malformed or cyclic drain graphs.
func validateTopology(topo Topology) error {
	if len(topo.Instances) == 0 {
		return fmt.Errorf("topology has no instances")
	}
	seen := make(map[ContainerID]bool, len(topo.Instances))
	for _, inst := range topo.Instances {
		if inst.Container == "" {
			return fmt.Errorf("instance with empty container id")
		}
		if seen[inst.Container] {
			return fmt.Errorf("duplicate container %q", inst.Container)
		}
		seen[inst.Container] = true
	}
	edgeSeen := make(map[Edge]bool, len(topo.Edges))
	adj := make(map[ContainerID][]ContainerID, len(topo.Instances))
	for _, e := range topo.Edges {
		if !seen[e.Consumer] {
			return fmt.Errorf("edge references unknown consumer %q", e.Consumer)
		}
		if !seen[e.Upstream] {
			return fmt.Errorf("edge references unknown upstream %q", e.Upstream)
		}
		if e.Consumer == e.Upstream {
			return fmt.Errorf("self drain edge on %q", e.Consumer)
		}
		if edgeSeen[e] {
			return fmt.Errorf("duplicate edge %s -> %s", e.Consumer, e.Upstream)
		}
		edgeSeen[e] = true
		adj[e.Upstream] = append(adj[e.Upstream], e.Consumer)
	}
	// Drain dependencies must form a DAG: a cycle means nobody can ever close.
	const (
		white = 0
		gray  = 1
		black = 2
	)
	color := make(map[ContainerID]int, len(topo.Instances))
	var visit func(ContainerID) error
	visit = func(id ContainerID) error {
		color[id] = gray
		for _, next := range adj[id] {
			switch color[next] {
			case gray:
				return fmt.Errorf("drain dependency cycle involving %q and %q", id, next)
			case white:
				if err := visit(next); err != nil {
					return err
				}
			}
		}
		color[id] = black
		return nil
	}
	for _, inst := range topo.Instances {
		if color[inst.Container] == white {
			if err := visit(inst.Container); err != nil {
				return err
			}
		}
	}
	return nil
}
