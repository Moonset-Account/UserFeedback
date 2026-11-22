package shutdowncoord_test

import (
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"shutdowncoord"
)

// fakeClock is a manually advanced clock so deadline behavior is deterministic.
type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func newFakeClock() *fakeClock {
	return &fakeClock{t: time.Date(2026, 11, 23, 0, 0, 0, 0, time.UTC)}
}

func (f *fakeClock) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.t
}

func (f *fakeClock) Advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.t = f.t.Add(d)
}

const testGrace = 10 * time.Second

func mustNew(t *testing.T, topo shutdowncoord.Topology, clock shutdowncoord.Clock, store shutdowncoord.Store) *shutdowncoord.Coordinator {
	t.Helper()
	c, err := shutdowncoord.New(topo, testGrace, clock, store)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return c
}

func kinds(actions []shutdowncoord.Action) []string {
	out := make([]string, len(actions))
	for i, a := range actions {
		out[i] = string(a.Kind) + ":" + string(a.Container)
	}
	return out
}

func hasAction(actions []shutdowncoord.Action, kind shutdowncoord.ActionKind, id shutdowncoord.ContainerID) bool {
	for _, a := range actions {
		if a.Kind == kind && a.Container == id {
			return true
		}
	}
	return false
}

func mustPhase(t *testing.T, c *shutdowncoord.Coordinator, id shutdowncoord.ContainerID, want shutdowncoord.Phase) {
	t.Helper()
	got, ok := c.Phase(id)
	if !ok {
		t.Fatalf("unknown container %q", id)
	}
	if got != want {
		t.Fatalf("phase of %q = %q, want %q", id, got, want)
	}
}

// drainTopology models two parallel consumer branches A and B reading the same
// upstream U. Both must report drain completion and exit before U may close.
func drainTopology() shutdowncoord.Topology {
	return shutdowncoord.Topology{
		Instances: []shutdowncoord.Instance{
			{Container: "U", Gen: 1},
			{Container: "A", Gen: 1},
			{Container: "B", Gen: 1},
		},
		Edges: []shutdowncoord.Edge{
			{Consumer: "A", Upstream: "U"},
			{Consumer: "B", Upstream: "U"},
		},
	}
}

func TestValidateTopologyErrors(t *testing.T) {
	cases := []struct {
		name string
		topo shutdowncoord.Topology
	}{
		{"empty", shutdowncoord.Topology{}},
		{"empty id", shutdowncoord.Topology{Instances: []shutdowncoord.Instance{{Gen: 1}}}},
		{"duplicate id", shutdowncoord.Topology{Instances: []shutdowncoord.Instance{
			{Container: "x", Gen: 1}, {Container: "x", Gen: 2},
		}}},
		{"unknown consumer", shutdowncoord.Topology{
			Instances: []shutdowncoord.Instance{{Container: "u", Gen: 1}},
			Edges:     []shutdowncoord.Edge{{Consumer: "ghost", Upstream: "u"}},
		}},
		{"unknown upstream", shutdowncoord.Topology{
			Instances: []shutdowncoord.Instance{{Container: "c", Gen: 1}},
			Edges:     []shutdowncoord.Edge{{Consumer: "c", Upstream: "ghost"}},
		}},
		{"self edge", shutdowncoord.Topology{
			Instances: []shutdowncoord.Instance{{Container: "c", Gen: 1}},
			Edges:     []shutdowncoord.Edge{{Consumer: "c", Upstream: "c"}},
		}},
		{"duplicate edge", shutdowncoord.Topology{
			Instances: []shutdowncoord.Instance{{Container: "u", Gen: 1}, {Container: "c", Gen: 1}},
			Edges: []shutdowncoord.Edge{
				{Consumer: "c", Upstream: "u"},
				{Consumer: "c", Upstream: "u"},
			},
		}},
		{"cycle", shutdowncoord.Topology{
			Instances: []shutdowncoord.Instance{{Container: "a", Gen: 1}, {Container: "b", Gen: 1}},
			Edges: []shutdowncoord.Edge{
				{Consumer: "a", Upstream: "b"},
				{Consumer: "b", Upstream: "a"},
			},
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := shutdowncoord.New(tc.topo, testGrace, newFakeClock(), nil); err == nil {
				t.Fatalf("New(%s) accepted an invalid topology", tc.name)
			}
		})
	}
	if _, err := shutdowncoord.New(drainTopology(), 0, newFakeClock(), nil); err == nil {
		t.Fatal("New accepted a zero grace period")
	}
}

// TestParallelBranchesShareUpstreamStuckHook is the headline scenario: B ends
// normally, A blocks inside its shutdown hook, and U must keep waiting.
func TestParallelBranchesShareUpstreamStuckHook(t *testing.T) {
	clock := newFakeClock()
	c := mustNew(t, drainTopology(), clock, nil)

	t0 := clock.Now()
	start := c.Start(t0)
	if got := kinds(start); len(got) != 3 {
		t.Fatalf("Start actions = %v, want 3 stop_accepting actions", got)
	}
	for _, id := range []shutdowncoord.ContainerID{"U", "A", "B"} {
		if !hasAction(start, shutdowncoord.ActionStopAccepting, id) {
			t.Fatalf("Start missing stop_accepting for %q: %v", id, kinds(start))
		}
	}

	// Both consumers finish reading their input buffers.
	if got := c.Handle(shutdowncoord.DrainReport{Container: "A", Gen: 1}); !hasAction(got, shutdowncoord.ActionExecuteHook, "A") {
		t.Fatalf("drain A produced %v, want execute_hook:A", kinds(got))
	}
	if got := c.Handle(shutdowncoord.DrainReport{Container: "B", Gen: 1}); !hasAction(got, shutdowncoord.ActionExecuteHook, "B") {
		t.Fatalf("drain B produced %v, want execute_hook:B", kinds(got))
	}

	// Neither consumer has exited yet, so U must not close.
	mustPhase(t, c, "U", shutdowncoord.PhaseDraining)

	// B's hook returns promptly; A's hook is stuck (no HookReturn).
	if got := c.Handle(shutdowncoord.HookReturn{Container: "B", Gen: 1}); !hasAction(got, shutdowncoord.ActionRequestExit, "B") {
		t.Fatalf("hook return B produced %v, want request_exit:B", kinds(got))
	}
	exitB := c.Handle(shutdowncoord.ExitEvent{Container: "B", Gen: 1})
	if len(exitB) != 0 {
		t.Fatalf("B exit unexpectedly produced actions %v; U must still wait for A", kinds(exitB))
	}

	// Normal branch B is fully done, long before the shared deadline.
	mustPhase(t, c, "B", shutdowncoord.PhaseExited)
	mustPhase(t, c, "A", shutdowncoord.PhaseHookRunning)
	mustPhase(t, c, "U", shutdowncoord.PhaseDraining)

	// No hook for U was ever issued: A drained but never exited.
	if got := c.Resume(); hasAction(got, shutdowncoord.ActionExecuteHook, "U") {
		t.Fatalf("U hook was issued while A still runs: %v", kinds(got))
	}

	// Deadline arrives: A is force killed, U is force killed because it never
	// closed, B is left untouched.
	clock.Advance(testGrace)
	killed := c.Tick(clock.Now())
	if !hasAction(killed, shutdowncoord.ActionForceKill, "A") || !hasAction(killed, shutdowncoord.ActionForceKill, "U") {
		t.Fatalf("deadline actions = %v, want force_kill for A and U", kinds(killed))
	}
	if hasAction(killed, shutdowncoord.ActionForceKill, "B") {
		t.Fatalf("deadline force-killed already exited B: %v", kinds(killed))
	}

	failures := c.IncompleteEdges()
	if len(failures) != 1 {
		t.Fatalf("incomplete edges = %v, want exactly the A->U edge", failures)
	}
	f := failures[0]
	if f.Consumer != "A" || f.Upstream != "U" {
		t.Fatalf("incomplete edge = %+v, want A->U", f)
	}
	if !f.ConsumerDrained || f.ConsumerExited {
		t.Fatalf("incomplete edge state = %+v, want drained=true exited=false", f)
	}
	if c.Finalized() != true {
		t.Fatal("coordinator should be finalized at the deadline")
	}
}

// TestParallelBranchesShareUpstreamResolvesWhenStuckConsumerExits shows that U
// proceeds as soon as the second consumer also exits, and that the stuck
// branch's late hook return cannot resurrect it.
func TestParallelBranchesShareUpstreamResolvesWhenStuckConsumerExits(t *testing.T) {
	clock := newFakeClock()
	c := mustNew(t, drainTopology(), clock, nil)
	c.Start(clock.Now())
	c.Handle(shutdowncoord.DrainReport{Container: "A", Gen: 1})
	c.Handle(shutdowncoord.DrainReport{Container: "B", Gen: 1})
	c.Handle(shutdowncoord.HookReturn{Container: "B", Gen: 1})
	c.Handle(shutdowncoord.ExitEvent{Container: "B", Gen: 1})
	mustPhase(t, c, "U", shutdowncoord.PhaseDraining)

	// A eventually reports exit while still nominally inside its hook.
	got := c.Handle(shutdowncoord.ExitEvent{Container: "A", Gen: 1})
	if !hasAction(got, shutdowncoord.ActionExecuteHook, "U") {
		t.Fatalf("A exit produced %v, want execute_hook:U", kinds(got))
	}
	mustPhase(t, c, "A", shutdowncoord.PhaseExited)
	mustPhase(t, c, "U", shutdowncoord.PhaseHookRunning)

	// A's hook finally returns after its exit: inert, no request_exit.
	late := c.Handle(shutdowncoord.HookReturn{Container: "A", Gen: 1})
	if len(late) != 0 {
		t.Fatalf("late hook return produced %v, want no actions", kinds(late))
	}
	mustPhase(t, c, "A", shutdowncoord.PhaseExited)
	st, _ := c.State("A")
	if st.HookReturned {
		t.Fatal("late hook return must not be recorded as the hook result")
	}

	// U completes normally.
	c.Handle(shutdowncoord.HookReturn{Container: "U", Gen: 1})
	c.Handle(shutdowncoord.ExitEvent{Container: "U", Gen: 1})
	mustPhase(t, c, "U", shutdowncoord.PhaseExited)

	clock.Advance(testGrace)
	if got := c.Tick(clock.Now()); len(got) != 0 {
		t.Fatalf("Tick after clean drain produced %v, want none", kinds(got))
	}
	if f := c.IncompleteEdges(); len(f) != 0 {
		t.Fatalf("IncompleteEdges = %v, want none", f)
	}
}

// TestSpontaneousExitBeforeHookReturn covers the explicit requirement that a
// container may exit on its own before its hook returns.
func TestSpontaneousExitBeforeHookReturn(t *testing.T) {
	clock := newFakeClock()
	topo := shutdowncoord.Topology{
		Instances: []shutdowncoord.Instance{{Container: "solo", Gen: 7}},
	}
	c := mustNew(t, topo, clock, nil)
	c.Start(clock.Now())
	mustPhase(t, c, "solo", shutdowncoord.PhaseHookRunning)

	if got := c.Handle(shutdowncoord.ExitEvent{Container: "solo", Gen: 7}); len(got) != 0 {
		t.Fatalf("spontaneous exit produced %v, want no actions", kinds(got))
	}
	mustPhase(t, c, "solo", shutdowncoord.PhaseExited)

	late := c.Handle(shutdowncoord.HookReturn{Container: "solo", Gen: 7})
	if len(late) != 0 {
		t.Fatalf("late hook return produced %v, want no actions", kinds(late))
	}
	mustPhase(t, c, "solo", shutdowncoord.PhaseExited)

	// The shared deadline must not force-kill an instance that already exited.
	clock.Advance(testGrace)
	if got := c.Tick(clock.Now()); len(got) != 0 {
		t.Fatalf("Tick force-killed an exited instance: %v", kinds(got))
	}
}

// TestStaleGenerationIgnored proves events from a previous instance generation
// cannot drive the current instance.
func TestStaleGenerationIgnored(t *testing.T) {
	clock := newFakeClock()
	topo := shutdowncoord.Topology{
		Instances: []shutdowncoord.Instance{{Container: "svc", Gen: 2}},
	}
	c := mustNew(t, topo, clock, nil)
	c.Start(clock.Now())

	c.Handle(shutdowncoord.DrainReport{Container: "svc", Gen: 1})
	c.Handle(shutdowncoord.HookReturn{Container: "svc", Gen: 1})
	c.Handle(shutdowncoord.ExitEvent{Container: "svc", Gen: 1})
	mustPhase(t, c, "svc", shutdowncoord.PhaseHookRunning)

	if got := c.Handle(shutdowncoord.HookReturn{Container: "svc", Gen: 2}); !hasAction(got, shutdowncoord.ActionRequestExit, "svc") {
		t.Fatalf("current-generation hook return produced %v, want request_exit", kinds(got))
	}
}

// TestGraceDeadlineForceKillAndIncompleteReport checks the whole-group deadline
// with a consumer that never even reported drain.
func TestGraceDeadlineForceKillAndIncompleteReport(t *testing.T) {
	clock := newFakeClock()
	c := mustNew(t, drainTopology(), clock, nil)
	c.Start(clock.Now())

	// Nothing drains at all.
	clock.Advance(testGrace - time.Millisecond)
	if got := c.Tick(clock.Now()); len(got) != 0 {
		t.Fatalf("Tick before deadline produced %v, want none", kinds(got))
	}
	clock.Advance(time.Millisecond)

	// A timer-driven Handle (any event) at the deadline also finalizes.
	got := c.Handle(shutdowncoord.ActionAck{ActionID: "noop"})
	for _, id := range []shutdowncoord.ContainerID{"U", "A", "B"} {
		if !hasAction(got, shutdowncoord.ActionForceKill, id) {
			t.Fatalf("deadline actions %v missing force_kill:%s", kinds(got), id)
		}
	}
	failures := c.IncompleteEdges()
	if len(failures) != 2 {
		t.Fatalf("IncompleteEdges = %v, want both A->U and B->U", failures)
	}
	for _, f := range failures {
		if f.ConsumerDrained || f.ConsumerExited {
			t.Fatalf("failure %+v should report neither drained nor exited", f)
		}
	}

	// Tick is idempotent: no duplicate force kills.
	if again := c.Tick(clock.Now()); len(again) != 0 {
		t.Fatalf("second Tick produced %v, want none", kinds(again))
	}
}

// TestRestartDoesNotRepeatAckedActions restarts the coordinator from the store
// and checks acknowledged actions are never re-emitted while pending ones are.
func TestRestartDoesNotRepeatAckedActions(t *testing.T) {
	clock := newFakeClock()
	store := &shutdowncoord.MemoryStore{}
	topo := shutdowncoord.Topology{
		Instances: []shutdowncoord.Instance{
			{Container: "U", Gen: 3},
			{Container: "C", Gen: 5},
		},
		Edges: []shutdowncoord.Edge{{Consumer: "C", Upstream: "U"}},
	}

	c1 := mustNew(t, topo, clock, store)
	startActions := c1.Start(clock.Now())
	if len(startActions) != 2 {
		t.Fatalf("Start actions = %v, want 2", kinds(startActions))
	}
	for _, a := range startActions {
		c1.Handle(shutdowncoord.ActionAck{ActionID: a.ID})
	}

	hookActions := c1.Handle(shutdowncoord.DrainReport{Container: "C", Gen: 5})
	if len(hookActions) != 1 || hookActions[0].Kind != shutdowncoord.ActionExecuteHook || hookActions[0].Container != "C" {
		t.Fatalf("drain C produced %v, want execute_hook:C", kinds(hookActions))
	}
	// Deliberately leave the hook action unacknowledged.

	// Controller restarts.
	c2 := mustNew(t, topo, clock, store)
	if got := c2.Start(clock.Now()); got != nil {
		t.Fatalf("Start after restart re-emitted %v, want nil", kinds(got))
	}
	resumed := c2.Resume()
	if len(resumed) != 1 || resumed[0].Kind != shutdowncoord.ActionExecuteHook || resumed[0].Container != "C" {
		t.Fatalf("Resume = %v, want only the unacknowledged execute_hook:C", kinds(resumed))
	}
	for _, a := range resumed {
		if a.Kind == shutdowncoord.ActionStopAccepting {
			t.Fatalf("Resume re-emitted a confirmed stop_accepting action %v", a.ID)
		}
	}

	// Ack the hook action, restart again, and confirm Resume is empty.
	c2.Handle(shutdowncoord.ActionAck{ActionID: resumed[0].ID})
	c3 := mustNew(t, topo, clock, store)
	if got := c3.Resume(); len(got) != 0 {
		t.Fatalf("Resume after acking produced %v, want none", kinds(got))
	}

	// The restart preserves generations and progresses from the saved state.
	got := c3.Handle(shutdowncoord.HookReturn{Container: "C", Gen: 5})
	if !hasAction(got, shutdowncoord.ActionRequestExit, "C") {
		t.Fatalf("hook return after restart produced %v, want request_exit:C", kinds(got))
	}
	if got := c3.Handle(shutdowncoord.ExitEvent{Container: "C", Gen: 5}); !hasAction(got, shutdowncoord.ActionExecuteHook, "U") {
		t.Fatalf("consumer exit after restart produced %v, want execute_hook:U", kinds(got))
	}
	mustPhase(t, c3, "U", shutdowncoord.PhaseHookRunning)
}

// TestRestartPreservesDeadline checks the shared deadline survives a restart.
func TestRestartPreservesDeadline(t *testing.T) {
	clock := newFakeClock()
	store := &shutdowncoord.MemoryStore{}
	topo := shutdowncoord.Topology{Instances: []shutdowncoord.Instance{{Container: "x", Gen: 1}}}

	c1 := mustNew(t, topo, clock, store)
	t0 := clock.Now()
	c1.Start(t0)
	want := t0.Add(testGrace)

	c2 := mustNew(t, topo, clock, store)
	if got := c2.Deadline(); !got.Equal(want) {
		t.Fatalf("deadline after restart = %v, want %v", got, want)
	}
	clock.Advance(testGrace)
	if got := c2.Tick(clock.Now()); !hasAction(got, shutdowncoord.ActionForceKill, "x") {
		t.Fatalf("deadline after restart produced %v, want force_kill:x", kinds(got))
	}
}

// TestConcurrentIndependentContainers exercises concurrent interleaving of
// events and acks. Run with -race.
func TestConcurrentIndependentContainers(t *testing.T) {
	clock := newFakeClock()
	const n = 32
	topo := shutdowncoord.Topology{}
	for i := 0; i < n; i++ {
		topo.Instances = append(topo.Instances, shutdowncoord.Instance{
			Container: shutdowncoord.ContainerID(fmt.Sprintf("c%02d", i)),
			Gen:       shutdowncoord.InstanceGen(i + 1),
		})
	}
	c := mustNew(t, topo, clock, nil)
	start := c.Start(clock.Now())
	if len(start) != 2*n {
		t.Fatalf("Start actions = %d, want %d", len(start), 2*n)
	}
	for _, a := range start {
		c.Handle(shutdowncoord.ActionAck{ActionID: a.ID})
	}

	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			id := shutdowncoord.ContainerID(fmt.Sprintf("c%02d", i))
			gen := shutdowncoord.InstanceGen(i + 1)
			// Duplicate deliveries must be idempotent.
			for k := 0; k < 3; k++ {
				c.Handle(shutdowncoord.HookReturn{Container: id, Gen: gen})
			}
			for k := 0; k < 3; k++ {
				c.Handle(shutdowncoord.ExitEvent{Container: id, Gen: gen})
			}
		}(i)
	}
	wg.Wait()

	for i := 0; i < n; i++ {
		mustPhase(t, c, shutdowncoord.ContainerID(fmt.Sprintf("c%02d", i)), shutdowncoord.PhaseExited)
	}
	clock.Advance(testGrace)
	if got := c.Tick(clock.Now()); len(got) != 0 {
		t.Fatalf("Tick after all exited produced %v, want none", kinds(got))
	}
}

// TestStopAcceptingHappensForWholeGroupEvenWhenDeepChain verifies intake stops
// group-wide immediately, before any hook runs, in a chain.
func TestStopAcceptingHappensForWholeGroupEvenWhenDeepChain(t *testing.T) {
	clock := newFakeClock()
	topo := shutdowncoord.Topology{
		Instances: []shutdowncoord.Instance{
			{Container: "src", Gen: 1},
			{Container: "mid", Gen: 1},
			{Container: "dst", Gen: 1},
		},
		Edges: []shutdowncoord.Edge{
			{Consumer: "mid", Upstream: "src"},
			{Consumer: "dst", Upstream: "mid"},
		},
	}
	c := mustNew(t, topo, clock, nil)
	start := c.Start(clock.Now())
	if len(start) != 3 {
		t.Fatalf("Start = %v, want exactly 3 stop_accepting actions", kinds(start))
	}
	for _, a := range start {
		if a.Kind != shutdowncoord.ActionStopAccepting {
			t.Fatalf("Start emitted %q before any drain report", a.Kind)
		}
	}
	// Leaf consumer drains first, then its upstreams close one by one.
	if got := c.Handle(shutdowncoord.DrainReport{Container: "dst", Gen: 1}); !hasAction(got, shutdowncoord.ActionExecuteHook, "dst") {
		t.Fatalf("drain dst produced %v", kinds(got))
	}
	if got := c.Handle(shutdowncoord.DrainReport{Container: "mid", Gen: 1}); len(got) != 0 {
		t.Fatalf("drain mid produced %v before dst exited", kinds(got))
	}
	c.Handle(shutdowncoord.HookReturn{Container: "dst", Gen: 1})
	got := c.Handle(shutdowncoord.ExitEvent{Container: "dst", Gen: 1})
	if !hasAction(got, shutdowncoord.ActionExecuteHook, "mid") {
		t.Fatalf("dst exit produced %v, want execute_hook:mid", kinds(got))
	}
	if hasAction(got, shutdowncoord.ActionExecuteHook, "src") {
		t.Fatalf("src closed while mid still runs: %v", kinds(got))
	}
}

// TestSpontaneousExitWhileStillDraining stops an instance from accepting input
// and sees it exit before its hook ever runs; nothing must be re-emitted.
func TestSpontaneousExitWhileStillDraining(t *testing.T) {
	clock := newFakeClock()
	c := mustNew(t, drainTopology(), clock, nil)
	c.Start(clock.Now())

	// A exits without draining and without a hook return.
	if got := c.Handle(shutdowncoord.ExitEvent{Container: "A", Gen: 1}); len(got) != 0 {
		t.Fatalf("A exit produced %v, want none", kinds(got))
	}
	mustPhase(t, c, "A", shutdowncoord.PhaseExited)
	if st, _ := c.State("A"); st.Drained {
		t.Fatal("A must not be marked drained by an exit event")
	}

	// A late drain report after exit is inert.
	if got := c.Handle(shutdowncoord.DrainReport{Container: "A", Gen: 1}); len(got) != 0 {
		t.Fatalf("late drain report produced %v, want none", kinds(got))
	}
	if st, _ := c.State("A"); st.Drained {
		t.Fatal("late drain report must not mark an exited instance drained")
	}

	// U still waits on B.
	mustPhase(t, c, "U", shutdowncoord.PhaseDraining)

	clock.Advance(testGrace)
	killed := c.Tick(clock.Now())
	if hasAction(killed, shutdowncoord.ActionForceKill, "A") {
		t.Fatalf("force-killed an instance that already exited: %v", kinds(killed))
	}
	failures := c.IncompleteEdges()
	if len(failures) != 2 {
		t.Fatalf("IncompleteEdges = %v, want both edges incomplete", failures)
	}
	for _, f := range failures {
		// An exit without a drain report still leaves the edge incomplete: the
		// coordinator cannot confirm the consumer finished reading.
		if f.Consumer == "A" && f.ConsumerDrained {
			t.Fatalf("A never reported drain; edge state = %+v", f)
		}
	}
}

// TestRestartWithinDeadlineWindow checks that finalization after a restart still
// force-kills the right instances, does not duplicate an already-acked force
// kill, and keeps the incomplete-edge report.
func TestRestartWithinDeadlineWindow(t *testing.T) {
	clock := newFakeClock()
	store := &shutdowncoord.MemoryStore{}
	c1 := mustNew(t, drainTopology(), clock, store)
	c1.Start(clock.Now())
	c1.Handle(shutdowncoord.DrainReport{Container: "B", Gen: 1})
	c1.Handle(shutdowncoord.HookReturn{Container: "B", Gen: 1})
	c1.Handle(shutdowncoord.ExitEvent{Container: "B", Gen: 1})

	// Restart before the deadline.
	c2 := mustNew(t, drainTopology(), clock, store)
	mustPhase(t, c2, "B", shutdowncoord.PhaseExited)

	clock.Advance(testGrace)
	killed := c2.Tick(clock.Now())
	if !hasAction(killed, shutdowncoord.ActionForceKill, "A") || !hasAction(killed, shutdowncoord.ActionForceKill, "U") {
		t.Fatalf("post-restart deadline produced %v, want force_kill A and U", kinds(killed))
	}
	if hasAction(killed, shutdowncoord.ActionForceKill, "B") {
		t.Fatalf("force-killed B, which already exited: %v", kinds(killed))
	}
	for _, a := range killed {
		c2.Handle(shutdowncoord.ActionAck{ActionID: a.ID})
	}
	if got := c2.IncompleteEdges(); len(got) != 1 || got[0].Consumer != "A" {
		t.Fatalf("IncompleteEdges = %v, want only A->U", got)
	}

	// Restart after finalization: acked force kills never reappear.
	c3 := mustNew(t, drainTopology(), clock, store)
	if got := c3.Resume(); len(got) != 0 {
		t.Fatalf("Resume after acked finalization produced %v", kinds(got))
	}
	if got := c3.Tick(clock.Now()); len(got) != 0 {
		t.Fatalf("Tick after acked finalization produced %v", kinds(got))
	}
	if got := c3.IncompleteEdges(); len(got) != 1 || got[0].Consumer != "A" || got[0].ConsumerDrained || got[0].ConsumerExited {
		t.Fatalf("IncompleteEdges after restart = %v, want the A->U edge neither drained nor exited", got)
	}
}

// TestInterleavedChainAndBranch mixes a chain with a shared upstream and drives
// events out of their nominal order to confirm the coordinator never closes a
// node early and does not deadlock.
func TestInterleavedChainAndBranch(t *testing.T) {
	clock := newFakeClock()
	topo := shutdowncoord.Topology{
		Instances: []shutdowncoord.Instance{
			{Container: "src", Gen: 2},
			{Container: "mid", Gen: 2},
			{Container: "leaf", Gen: 2},
			{Container: "side", Gen: 2},
		},
		Edges: []shutdowncoord.Edge{
			{Consumer: "mid", Upstream: "src"},
			{Consumer: "leaf", Upstream: "mid"},
			{Consumer: "side", Upstream: "src"},
		},
	}
	c := mustNew(t, topo, clock, nil)
	c.Start(clock.Now())

	// Out-of-order: src, mid and side drain before their consumers are done.
	c.Handle(shutdowncoord.DrainReport{Container: "src", Gen: 2})
	c.Handle(shutdowncoord.DrainReport{Container: "mid", Gen: 2})
	got := c.Handle(shutdowncoord.DrainReport{Container: "side", Gen: 2})
	if hasAction(got, shutdowncoord.ActionExecuteHook, "src") {
		t.Fatalf("src closed before mid/side exited: %v", kinds(got))
	}
	mustPhase(t, c, "src", shutdowncoord.PhaseDraining)
	mustPhase(t, c, "mid", shutdowncoord.PhaseDraining) // waiting on leaf exit

	// side finishes on its own branch.
	c.Handle(shutdowncoord.HookReturn{Container: "side", Gen: 2})
	c.Handle(shutdowncoord.ExitEvent{Container: "side", Gen: 2})
	mustPhase(t, c, "src", shutdowncoord.PhaseDraining) // still waiting on mid

	// leaf chain: leaf drains, then exits, unlocking mid.
	if got := c.Handle(shutdowncoord.DrainReport{Container: "leaf", Gen: 2}); !hasAction(got, shutdowncoord.ActionExecuteHook, "leaf") {
		t.Fatalf("drain leaf produced %v", kinds(got))
	}
	c.Handle(shutdowncoord.HookReturn{Container: "leaf", Gen: 2})
	if got := c.Handle(shutdowncoord.ExitEvent{Container: "leaf", Gen: 2}); !hasAction(got, shutdowncoord.ActionExecuteHook, "mid") {
		t.Fatalf("leaf exit produced %v, want execute_hook:mid", kinds(got))
	}
	mustPhase(t, c, "src", shutdowncoord.PhaseDraining) // mid not exited yet

	c.Handle(shutdowncoord.HookReturn{Container: "mid", Gen: 2})
	got = c.Handle(shutdowncoord.ExitEvent{Container: "mid", Gen: 2})
	if !hasAction(got, shutdowncoord.ActionExecuteHook, "src") {
		t.Fatalf("mid exit produced %v, want execute_hook:src", kinds(got))
	}

	c.Handle(shutdowncoord.HookReturn{Container: "src", Gen: 2})
	c.Handle(shutdowncoord.ExitEvent{Container: "src", Gen: 2})
	clock.Advance(testGrace)
	if got := c.Tick(clock.Now()); len(got) != 0 {
		t.Fatalf("clean interleaved drain force-killed: %v", kinds(got))
	}
	if got := c.IncompleteEdges(); len(got) != 0 {
		t.Fatalf("IncompleteEdges = %v, want none", got)
	}
}

// flakyStore fails Load or Save on demand.
type flakyStore struct {
	loadErr error
	saveErr error
	inner   shutdowncoord.MemoryStore
}

func (f *flakyStore) Load() (shutdowncoord.Snapshot, bool, error) {
	if f.loadErr != nil {
		return shutdowncoord.Snapshot{}, false, f.loadErr
	}
	return f.inner.Load()
}

func (f *flakyStore) Save(s shutdowncoord.Snapshot) error {
	if f.saveErr != nil {
		return f.saveErr
	}
	return f.inner.Save(s)
}

// TestStoreErrorsSurfaced confirms store failures are reported rather than
// silently ignored on load.
func TestStoreErrorsSurfaced(t *testing.T) {
	boom := errors.New("disk on fire")
	_, err := shutdowncoord.New(drainTopology(), testGrace, newFakeClock(), &flakyStore{loadErr: boom})
	if !errors.Is(err, boom) {
		t.Fatalf("New with failing Load = %v, want wrapped %v", err, boom)
	}
}

// TestCorruptSnapshotRejected checks every hydration guard.
func TestCorruptSnapshotRejected(t *testing.T) {
	base := func() shutdowncoord.Snapshot {
		return shutdowncoord.Snapshot{
			Topology: shutdowncoord.Topology{
				Instances: []shutdowncoord.Instance{{Container: "x", Gen: 1}},
			},
			Grace: time.Second,
			Order: []shutdowncoord.ContainerID{"x"},
			States: []shutdowncoord.InstanceSnapshot{
				{Container: "x", State: shutdowncoord.InstanceState{Gen: 1, Phase: shutdowncoord.PhasePending}},
			},
		}
	}

	cases := []struct {
		name   string
		mutate func(*shutdowncoord.Snapshot)
	}{
		{"bad topology", func(s *shutdowncoord.Snapshot) { s.Topology = shutdowncoord.Topology{} }},
		{"zero grace", func(s *shutdowncoord.Snapshot) { s.Grace = 0 }},
		{"short order", func(s *shutdowncoord.Snapshot) { s.Order = nil }},
		{"unknown container state", func(s *shutdowncoord.Snapshot) {
			s.States = append(s.States, shutdowncoord.InstanceSnapshot{Container: "ghost"})
		}},
		{"duplicate state", func(s *shutdowncoord.Snapshot) {
			s.States = append(s.States, s.States[0])
		}},
		{"generation mismatch", func(s *shutdowncoord.Snapshot) { s.States[0].State.Gen = 99 }},
		{"missing state", func(s *shutdowncoord.Snapshot) { s.States = nil }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			snap := base()
			tc.mutate(&snap)
			store := &shutdowncoord.MemoryStore{}
			if err := store.Save(snap); err != nil {
				t.Fatalf("Save: %v", err)
			}
			if _, err := shutdowncoord.New(drainTopology(), testGrace, newFakeClock(), store); err == nil {
				t.Fatalf("New accepted corrupt snapshot %q", tc.name)
			}
		})
	}
}

// TestZeroValueBehavior covers pre-start and unknown-container queries.
func TestZeroValueBehavior(t *testing.T) {
	clock := newFakeClock()
	c := mustNew(t, drainTopology(), clock, nil)

	if got := c.Resume(); got != nil {
		t.Fatalf("Resume before Start = %v, want nil", kinds(got))
	}
	if got := c.Tick(clock.Now()); got != nil {
		t.Fatalf("Tick before Start = %v, want nil", kinds(got))
	}
	if !c.Deadline().IsZero() {
		t.Fatalf("Deadline before Start = %v, want zero", c.Deadline())
	}
	if _, ok := c.Phase("nope"); ok {
		t.Fatal("Phase reported an unknown container")
	}
	if _, ok := c.State("nope"); ok {
		t.Fatal("State reported an unknown container")
	}
	// Events for unknown containers are ignored without panicking.
	if got := c.Handle(shutdowncoord.DrainReport{Container: "nope", Gen: 1}); got != nil {
		t.Fatalf("event for unknown container produced %v", kinds(got))
	}
	// Empty ack ID is ignored.
	c.Start(clock.Now())
	if got := c.Handle(shutdowncoord.ActionAck{}); len(got) != 0 {
		t.Fatalf("empty ack produced %v", kinds(got))
	}

	// Start is idempotent.
	before := c.Resume()
	if got := c.Start(clock.Now().Add(time.Hour)); got != nil {
		t.Fatalf("second Start produced %v, want nil", kinds(got))
	}
	if !c.Deadline().Equal(clock.Now().Add(testGrace)) {
		t.Fatalf("second Start moved the deadline to %v", c.Deadline())
	}
	if after := c.Resume(); len(after) != len(before) {
		t.Fatalf("Resume changed across no-op Start: %d -> %d", len(before), len(after))
	}
}
