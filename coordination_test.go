package podresize

import (
	"errors"
	"path/filepath"
	"sync"
	"testing"
)

// ---- small test helpers ---------------------------------------------------

func mustController(t *testing.T) *Controller {
	t.Helper()
	c, err := NewController(NewMemoryStore())
	if err != nil {
		t.Fatalf("NewController: %v", err)
	}
	return c
}

func mustAddNode(t *testing.T, c *Controller, name string, cap Resources) {
	t.Helper()
	if err := c.AddNode(name, cap); err != nil {
		t.Fatalf("AddNode(%s): %v", name, err)
	}
}

func mustAddPod(t *testing.T, c *Controller, uid, node string, spec Resources) {
	t.Helper()
	if err := c.AddPod(uid, node, spec); err != nil {
		t.Fatalf("AddPod(%s): %v", uid, err)
	}
}

func findNode(t *testing.T, v View, name string) NodeView {
	t.Helper()
	for _, n := range v.Nodes {
		if n.Name == name {
			return n
		}
	}
	t.Fatalf("node %s not found in view", name)
	return NodeView{}
}

func findPod(n NodeView, uid string) PodView {
	for _, p := range n.Pods {
		if p.UID == uid {
			return p
		}
	}
	return PodView{}
}

func findGen(p PodView, gen int64) GenView {
	for _, g := range p.Generations {
		if g.Gen == gen {
			return g
		}
	}
	return GenView{}
}

func assertRes(t *testing.T, label string, got, want Resources) {
	t.Helper()
	if got != want {
		t.Fatalf("%s: got cpu=%d mem=%d, want cpu=%d mem=%d",
			label, got.CPU, got.Memory, want.CPU, want.Memory)
	}
}

func assertActionKinds(t *testing.T, p PodView, want []string) {
	t.Helper()
	if len(p.NextActions) != len(want) {
		t.Fatalf("actions length for %s: got %d (%v), want %d (%v)",
			p.UID, len(p.NextActions), p.NextActions, len(want), want)
	}
	for i, a := range p.NextActions {
		if a.Kind != want[i] {
			t.Fatalf("action %d for %s: got %s, want %s (full: %v)",
				i, p.UID, a.Kind, want[i], p.NextActions)
		}
	}
}

func hasAction(a []Action, kind, resource string, gen int64) bool {
	for _, x := range a {
		if x.Kind == kind && x.Resource == resource && x.Gen == gen {
			return true
		}
	}
	return false
}

// ---- 1. Happy path: 3/5 -> 5/3, max(old,new)=5/5 while preparing ---------

func TestHappyPath35To53(t *testing.T) {
	c := mustController(t)
	mustAddNode(t, c, "node-1", Resources{CPU: 8, Memory: 8})
	mustAddPod(t, c, "pod-a", "node-1", Resources{CPU: 3, Memory: 5})

	view, err := c.Submit(ResizeRequest{
		PodUID: "pod-a", Version: 1, Target: Resources{CPU: 5, Memory: 3},
	})
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	node := findNode(t, view, "node-1")
	pod := findPod(node, "pod-a")

	if pod.ActiveGen != 1 {
		t.Fatalf("active gen: got %d, want 1", pod.ActiveGen)
	}
	// During preparation both resources reserve max(old,new): 5 CPU and 5 mem.
	assertRes(t, "reservation after submit", pod.Used, Resources{CPU: 5, Memory: 5})
	assertRes(t, "node used after submit", node.Used, Resources{CPU: 5, Memory: 5})
	assertRes(t, "node available after submit", node.Available, Resources{CPU: 3, Memory: 3})
	// Visible spec stays at the old value until BOTH resources are ready.
	assertRes(t, "visible unchanged after submit", pod.Visible, Resources{CPU: 3, Memory: 5})
	assertActionKinds(t, pod, []string{ActionPrepare, ActionPrepare})
	if !hasAction(pod.NextActions, ActionPrepare, CPU, 1) ||
		!hasAction(pod.NextActions, ActionPrepare, Memory, 1) {
		t.Fatalf("missing per-resource prepare actions: %v", pod.NextActions)
	}

	// CPU prepared first: visible spec must not move yet.
	view, err = c.PrepareAck(PrepareReceipt{PodUID: "pod-a", Gen: 1, Resource: CPU})
	if err != nil {
		t.Fatalf("PrepareAck cpu: %v", err)
	}
	node = findNode(t, view, "node-1")
	pod = findPod(node, "pod-a")
	g := findGen(pod, 1)
	if g.CPUPhase != PhaseReady || g.MemoryPhase != PhasePrepare {
		t.Fatalf("phases after cpu ack: cpu=%s mem=%s", g.CPUPhase, g.MemoryPhase)
	}
	assertRes(t, "visible unchanged after cpu ready", pod.Visible, Resources{CPU: 3, Memory: 5})
	assertRes(t, "used unchanged after cpu ready", pod.Used, Resources{CPU: 5, Memory: 5})

	// Memory prepared second: same-generation barrier flips the visible spec
	// atomically, but the reservation is still 5/5 until the apply receipt.
	view, err = c.PrepareAck(PrepareReceipt{PodUID: "pod-a", Gen: 1, Resource: Memory})
	if err != nil {
		t.Fatalf("PrepareAck memory: %v", err)
	}
	node = findNode(t, view, "node-1")
	pod = findPod(node, "pod-a")
	assertRes(t, "visible switched after both ready", pod.Visible, Resources{CPU: 5, Memory: 3})
	assertRes(t, "reservation still 5/5 while applying", pod.Used, Resources{CPU: 5, Memory: 5})
	g = findGen(pod, 1)
	if g.CPUPhase != PhaseApplying || g.MemoryPhase != PhaseApplying {
		t.Fatalf("phases after both ready: cpu=%s mem=%s", g.CPUPhase, g.MemoryPhase)
	}
	assertActionKinds(t, pod, []string{ActionApply})

	// Apply confirmation releases the reservation; the shrunk memory is now free.
	view, err = c.ApplyAck(ApplyReceipt{PodUID: "pod-a", Gen: 1})
	if err != nil {
		t.Fatalf("ApplyAck: %v", err)
	}
	node = findNode(t, view, "node-1")
	pod = findPod(node, "pod-a")
	assertRes(t, "visible after apply", pod.Visible, Resources{CPU: 5, Memory: 3})
	assertRes(t, "used released after apply", pod.Used, Resources{CPU: 5, Memory: 3})
	assertRes(t, "node available after apply", node.Available, Resources{CPU: 3, Memory: 5})
	if pod.ActiveGen != 0 || len(pod.Generations) != 0 {
		t.Fatalf("generation not pruned after apply: active=%d gens=%d",
			pod.ActiveGen, len(pod.Generations))
	}
	assertActionKinds(t, pod, []string{ActionNone})
}

// ---- 2. Capacity boundary: exact fit, over-subscription, stale versions ----

func TestCapacityBoundaryAndVersioning(t *testing.T) {
	c := mustController(t)
	// Pod starts at 3/5. A 5/5 reservation needs 2 extra CPU and 0 extra
	// memory, so a node with only 1 free CPU must reject it.
	mustAddNode(t, c, "node-1", Resources{CPU: 6, Memory: 7})
	mustAddPod(t, c, "pod-a", "node-1", Resources{CPU: 3, Memory: 5})

	// Free is 3/2; reservation 5/5 needs 2 more CPU and fits memory, but the
	// CPU headroom of 3 is exactly enough here — shrink the node further to
	// prove rejection: use a dedicated tight node below instead.
	mustAddNode(t, c, "node-tight", Resources{CPU: 4, Memory: 5})
	mustAddPod(t, c, "pod-tight", "node-tight", Resources{CPU: 3, Memory: 5})
	_, err := c.Submit(ResizeRequest{PodUID: "pod-tight", Version: 1, Target: Resources{CPU: 5, Memory: 3}})
	if !errors.Is(err, ErrInsufficient) {
		t.Fatalf("want ErrInsufficient, got %v", err)
	}
	// The original node-1 request does fit (3 free CPU >= 2 needed).
	if _, err := c.Submit(ResizeRequest{PodUID: "pod-a", Version: 1, Target: Resources{CPU: 5, Memory: 3}}); err != nil {
		t.Fatalf("fitting Submit on node-1: %v", err)
	}

	// Exact-fit capacity: node 5/5, pod reservation 5/5 leaves zero available.
	mustAddNode(t, c, "node-2", Resources{CPU: 5, Memory: 5})
	mustAddPod(t, c, "pod-b", "node-2", Resources{CPU: 3, Memory: 5})
	view, err := c.Submit(ResizeRequest{PodUID: "pod-b", Version: 7, Target: Resources{CPU: 5, Memory: 3}})
	if err != nil {
		t.Fatalf("exact-fit Submit: %v", err)
	}
	node2 := findNode(t, view, "node-2")
	assertRes(t, "exact-fit available", node2.Available, Resources{CPU: 0, Memory: 0})

	// Another Pod on the same node cannot take even 1 CPU while 5/5 is reserved.
	mustAddPod(t, c, "pod-c", "node-2", Resources{CPU: 0, Memory: 0})
	_, err = c.Submit(ResizeRequest{PodUID: "pod-c", Version: 1, Target: Resources{CPU: 1, Memory: 0}})
	if !errors.Is(err, ErrInsufficient) {
		t.Fatalf("admission into reserved capacity: want ErrInsufficient, got %v", err)
	}

	// Version must strictly increase.
	_, err = c.Submit(ResizeRequest{PodUID: "pod-b", Version: 7, Target: Resources{CPU: 4, Memory: 4}})
	if !errors.Is(err, ErrStaleVersion) {
		t.Fatalf("same version: want ErrStaleVersion, got %v", err)
	}
	_, err = c.Submit(ResizeRequest{PodUID: "pod-b", Version: 6, Target: Resources{CPU: 4, Memory: 4}})
	if !errors.Is(err, ErrStaleVersion) {
		t.Fatalf("older version: want ErrStaleVersion, got %v", err)
	}

	// Negative quantities are rejected.
	if err := c.AddNode("bad", Resources{CPU: -1, Memory: 1}); !errors.Is(err, ErrNegativeResource) {
		t.Fatalf("negative capacity: want ErrNegativeResource, got %v", err)
	}
	_, err = c.Submit(ResizeRequest{PodUID: "pod-b", Version: 8, Target: Resources{CPU: -1, Memory: 4}})
	if !errors.Is(err, ErrNegativeResource) {
		t.Fatalf("negative target: want ErrNegativeResource, got %v", err)
	}
}

// ---- 3. Shrunk resource stays reserved until apply; admission reopens -------

func TestShrinkReservationHoldsUntilApply(t *testing.T) {
	c := mustController(t)
	// Memory capacity is fully occupied by the Pod's initial 5 so there is
	// zero memory headroom until the shrinking reservation is released.
	mustAddNode(t, c, "node-1", Resources{CPU: 6, Memory: 5})
	mustAddPod(t, c, "pod-a", "node-1", Resources{CPU: 3, Memory: 5})
	mustAddPod(t, c, "pod-b", "node-1", Resources{CPU: 0, Memory: 0})

	// 3/5 -> 5/3 reserves 5/5 while preparing.
	if _, err := c.Submit(ResizeRequest{PodUID: "pod-a", Version: 1, Target: Resources{CPU: 5, Memory: 3}}); err != nil {
		t.Fatalf("Submit: %v", err)
	}
	// pod-b wants 1 mem: blocked although the *target* memory of pod-a is only
	// 3, because the reservation is still 5 and node memory capacity is 5.
	_, err := c.Submit(ResizeRequest{PodUID: "pod-b", Version: 1, Target: Resources{CPU: 0, Memory: 1}})
	if !errors.Is(err, ErrInsufficient) {
		t.Fatalf("admission before switch: want ErrInsufficient, got %v", err)
	}

	// Even after the visible spec switched to memory=3, the old 5-memory
	// reservation cannot be released before the same-generation apply.
	if _, err := c.PrepareAck(PrepareReceipt{PodUID: "pod-a", Gen: 1, Resource: CPU}); err != nil {
		t.Fatalf("cpu ack: %v", err)
	}
	if _, err := c.PrepareAck(PrepareReceipt{PodUID: "pod-a", Gen: 1, Resource: Memory}); err != nil {
		t.Fatalf("mem ack: %v", err)
	}
	_, err = c.Submit(ResizeRequest{PodUID: "pod-b", Version: 1, Target: Resources{CPU: 0, Memory: 1}})
	if !errors.Is(err, ErrInsufficient) {
		t.Fatalf("admission before apply: want ErrInsufficient, got %v", err)
	}

	view, err := c.ApplyAck(ApplyReceipt{PodUID: "pod-a", Gen: 1})
	if err != nil {
		t.Fatalf("apply: %v", err)
	}
	node := findNode(t, view, "node-1")
	// Visible 5/3 on a 6/5 node -> available 1 CPU, 2 memory.
	assertRes(t, "memory released only after apply", node.Available, Resources{CPU: 1, Memory: 2})

	// Now the 1-memory upscale of pod-b fits.
	if _, err := c.Submit(ResizeRequest{PodUID: "pod-b", Version: 1, Target: Resources{CPU: 0, Memory: 1}}); err != nil {
		t.Fatalf("admission after release: %v", err)
	}
}

// ---- 4. Request replacement, stale replays, revoke-before-release -----------

func TestSupersedeReplayAndRevocation(t *testing.T) {
	c := mustController(t)
	mustAddNode(t, c, "node-1", Resources{CPU: 8, Memory: 8})
	mustAddPod(t, c, "pod-a", "node-1", Resources{CPU: 3, Memory: 5})

	// Generation 1: 3/5 -> 5/3, reservation 5/5.
	if _, err := c.Submit(ResizeRequest{PodUID: "pod-a", Version: 1, Target: Resources{CPU: 5, Memory: 3}}); err != nil {
		t.Fatalf("submit v1: %v", err)
	}
	// CPU of gen-1 becomes ready before the new request arrives.
	view, err := c.PrepareAck(PrepareReceipt{PodUID: "pod-a", Gen: 1, Resource: CPU})
	if err != nil {
		t.Fatalf("gen1 cpu ack: %v", err)
	}
	if findGen(findPod(findNode(t, view, "node-1"), "pod-a"), 1).CPUPhase != PhaseReady {
		t.Fatalf("gen1 cpu should be Ready")
	}

	// Generation 2 replaces gen 1: 3/5 -> 6/2, reservation max = 6/5.
	view, err = c.Submit(ResizeRequest{PodUID: "pod-a", Version: 2, Target: Resources{CPU: 6, Memory: 2}})
	if err != nil {
		t.Fatalf("submit v2: %v", err)
	}
	pod := findPod(findNode(t, view, "node-1"), "pod-a")
	if pod.ActiveGen != 2 {
		t.Fatalf("active gen after supersede: got %d want 2", pod.ActiveGen)
	}
	g1 := findGen(pod, 1)
	if g1.CPUPhase != PhaseReady || g1.MemoryPhase != PhaseRevoking {
		t.Fatalf("gen1 after supersede: cpu=%s mem=%s, want Ready/Revoking",
			g1.CPUPhase, g1.MemoryPhase)
	}
	if len(g1.Actions) != 1 || g1.Actions[0].Kind != ActionRevoke {
		t.Fatalf("gen1 actions: %v, want single Revoke", g1.Actions)
	}
	// Both generations' reservations count until revocation is confirmed:
	// per-pod occupancy is the max, i.e. 6/5.
	assertRes(t, "used while gen1 revoking", pod.Used, Resources{CPU: 6, Memory: 5})
	assertRes(t, "visible still old", pod.Visible, Resources{CPU: 3, Memory: 5})

	// A replayed gen-1 prepare receipt must NOT advance gen 2.
	view, err = c.PrepareAck(PrepareReceipt{PodUID: "pod-a", Gen: 1, Resource: Memory})
	if err != nil {
		t.Fatalf("stale prepare: %v", err)
	}
	if !view.Stale {
		t.Fatalf("gen1 prepare after supersede must be reported stale")
	}
	pod = findPod(findNode(t, view, "node-1"), "pod-a")
	g2 := findGen(pod, 2)
	if g2.MemoryPhase != PhasePrepare {
		t.Fatalf("stale receipt advanced gen2 memory: phase=%s", g2.MemoryPhase)
	}

	// Drive gen 2 to completion while gen 1 is still revoking. Its 6/5
	// reservation keeps occupying capacity the whole time.
	if _, err := c.PrepareAck(PrepareReceipt{PodUID: "pod-a", Gen: 2, Resource: CPU}); err != nil {
		t.Fatalf("gen2 cpu: %v", err)
	}
	if _, err := c.PrepareAck(PrepareReceipt{PodUID: "pod-a", Gen: 2, Resource: Memory}); err != nil {
		t.Fatalf("gen2 mem: %v", err)
	}
	view, err = c.ApplyAck(ApplyReceipt{PodUID: "pod-a", Gen: 2})
	if err != nil {
		t.Fatalf("gen2 apply: %v", err)
	}
	pod = findPod(findNode(t, view, "node-1"), "pod-a")
	assertRes(t, "visible after gen2 apply", pod.Visible, Resources{CPU: 6, Memory: 2})
	// gen1 still revoking: its 5/5 reservation vs visible 6/2 -> occupancy 6/5.
	assertRes(t, "used still held by revoking gen1", pod.Used, Resources{CPU: 6, Memory: 5})
	if findGen(pod, 1).Gen == 0 {
		t.Fatalf("gen1 must remain until revocation confirmed")
	}

	// A replayed gen-1 apply receipt is stale and changes nothing.
	view, err = c.ApplyAck(ApplyReceipt{PodUID: "pod-a", Gen: 1})
	if err != nil {
		t.Fatalf("stale apply: %v", err)
	}
	if !view.Stale {
		t.Fatalf("gen1 apply must be stale")
	}
	pod = findPod(findNode(t, view, "node-1"), "pod-a")
	if findGen(pod, 1).Gen == 0 {
		t.Fatalf("stale apply must not remove gen1")
	}

	// Revocation confirmation finally releases the old reservation.
	view, err = c.RevokeAck(RevokeReceipt{PodUID: "pod-a", Gen: 1})
	if err != nil {
		t.Fatalf("revoke: %v", err)
	}
	pod = findPod(findNode(t, view, "node-1"), "pod-a")
	if findGen(pod, 1).Gen != 0 {
		t.Fatalf("gen1 not removed after revoke")
	}
	assertRes(t, "used after revoke", pod.Used, Resources{CPU: 6, Memory: 2})

	// Duplicate revocation is stale and harmless.
	view, err = c.RevokeAck(RevokeReceipt{PodUID: "pod-a", Gen: 1})
	if err != nil {
		t.Fatalf("duplicate revoke: %v", err)
	}
	if !view.Stale {
		t.Fatalf("duplicate revoke must be stale")
	}
}

// A generation that already passed the same-generation switch barrier has its
// spec in effect. A newer request must not revoke it; the apply receipt still
// has to close it out before its reservation can change hands.
func TestSupersedeDoesNotRevokeApplyingGeneration(t *testing.T) {
	c := mustController(t)
	mustAddNode(t, c, "node-1", Resources{CPU: 10, Memory: 10})
	mustAddPod(t, c, "pod-a", "node-1", Resources{CPU: 3, Memory: 5})

	if _, err := c.Submit(ResizeRequest{PodUID: "pod-a", Version: 1, Target: Resources{CPU: 5, Memory: 3}}); err != nil {
		t.Fatalf("submit v1: %v", err)
	}
	if _, err := c.PrepareAck(PrepareReceipt{PodUID: "pod-a", Gen: 1, Resource: CPU}); err != nil {
		t.Fatalf("gen1 cpu: %v", err)
	}
	if _, err := c.PrepareAck(PrepareReceipt{PodUID: "pod-a", Gen: 1, Resource: Memory}); err != nil {
		t.Fatalf("gen1 mem: %v", err)
	}
	// Gen 1 is now Applying: visible spec already switched to 5/3.
	view := c.View()
	pod := findPod(findNode(t, view, "node-1"), "pod-a")
	g1 := findGen(pod, 1)
	if g1.CPUPhase != PhaseApplying {
		t.Fatalf("gen1 should be Applying, got %s", g1.CPUPhase)
	}

	// Newer request must not mark gen 1 as revoking.
	view, err := c.Submit(ResizeRequest{PodUID: "pod-a", Version: 2, Target: Resources{CPU: 7, Memory: 2}})
	if err != nil {
		t.Fatalf("submit v2: %v", err)
	}
	pod = findPod(findNode(t, view, "node-1"), "pod-a")
	g1 = findGen(pod, 1)
	if g1.Gen == 0 {
		t.Fatalf("applying gen1 must remain until apply receipt")
	}
	if g1.CPUPhase != PhaseApplying || g1.MemoryPhase != PhaseApplying {
		t.Fatalf("gen1 must stay Applying, got %s/%s", g1.CPUPhase, g1.MemoryPhase)
	}
	for _, a := range g1.Actions {
		if a.Kind == ActionRevoke {
			t.Fatalf("applying gen1 must not offer a Revoke action: %v", g1.Actions)
		}
	}
	// Gen 2 is the new active generation, but occupancy keeps the max of both
	// reservations: max(5,7)=7 CPU, max(5,2)=5 memory.
	if pod.ActiveGen != 2 {
		t.Fatalf("active gen should be 2, got %d", pod.ActiveGen)
	}
	assertRes(t, "occupancy while gen1 applying and gen2 preparing",
		pod.Used, Resources{CPU: 7, Memory: 5})
	// Visible spec remains the switched 5/3; gen2 has not crossed its barrier.
	assertRes(t, "visible stays at gen1 target", pod.Visible, Resources{CPU: 5, Memory: 3})

	// The gen-1 apply receipt is still valid and releases gen 1.
	view, err = c.ApplyAck(ApplyReceipt{PodUID: "pod-a", Gen: 1})
	if err != nil {
		t.Fatalf("gen1 apply after v2 submitted: %v", err)
	}
	pod = findPod(findNode(t, view, "node-1"), "pod-a")
	if findGen(pod, 1).Gen != 0 {
		t.Fatalf("gen1 must be pruned after apply")
	}
	// Visible is 5/3; gen2 reserves max(5/3, 7/2) = 7/3.
	assertRes(t, "occupancy after gen1 apply", pod.Used, Resources{CPU: 7, Memory: 3})

	// A late revoke receipt for gen 1 is stale: it was never superseded.
	view, err = c.RevokeAck(RevokeReceipt{PodUID: "pod-a", Gen: 1})
	if err != nil {
		t.Fatalf("late revoke: %v", err)
	}
	if !view.Stale {
		t.Fatalf("revoke of a never-revoked generation must be stale")
	}
}

// ---- 5. Restart recovery from the durable snapshot --------------------------

func newTestFileStore(t *testing.T) *FileStore {
	t.Helper()
	dir := t.TempDir()
	return NewFileStore(filepath.Join(dir, "snapshot.json"))
}

func TestRestartRecoveryMidPreparation(t *testing.T) {
	store := newTestFileStore(t)
	c, err := NewController(store)
	if err != nil {
		t.Fatalf("new controller: %v", err)
	}
	mustAddNode(t, c, "node-1", Resources{CPU: 8, Memory: 8})
	mustAddPod(t, c, "pod-a", "node-1", Resources{CPU: 3, Memory: 5})
	if _, err := c.Submit(ResizeRequest{PodUID: "pod-a", Version: 1, Target: Resources{CPU: 5, Memory: 3}}); err != nil {
		t.Fatalf("submit: %v", err)
	}
	if _, err := c.PrepareAck(PrepareReceipt{PodUID: "pod-a", Gen: 1, Resource: CPU}); err != nil {
		t.Fatalf("cpu ack: %v", err)
	}

	// Restart: a brand new controller over the same store.
	c2, err := NewController(store)
	if err != nil {
		t.Fatalf("restart: %v", err)
	}
	view := c2.View()
	node := findNode(t, view, "node-1")
	pod := findPod(node, "pod-a")
	assertRes(t, "recovered reservation", pod.Used, Resources{CPU: 5, Memory: 5})
	assertRes(t, "recovered visible spec", pod.Visible, Resources{CPU: 3, Memory: 5})
	g := findGen(pod, 1)
	if g.Gen != 1 || g.CPUPhase != PhaseReady || g.MemoryPhase != PhasePrepare {
		t.Fatalf("recovered gen phases: %+v", g)
	}
	assertActionKinds(t, pod, []string{ActionPrepare})
	if pod.NextActions[0].Resource != Memory {
		t.Fatalf("recovered pending action should target memory, got %v", pod.NextActions)
	}

	// The recovered controller resumes the protocol exactly where it stopped.
	if _, err := c2.PrepareAck(PrepareReceipt{PodUID: "pod-a", Gen: 1, Resource: Memory}); err != nil {
		t.Fatalf("mem ack after restart: %v", err)
	}
	if _, err := c2.ApplyAck(ApplyReceipt{PodUID: "pod-a", Gen: 1}); err != nil {
		t.Fatalf("apply after restart: %v", err)
	}
	view = c2.View()
	pod = findPod(findNode(t, view, "node-1"), "pod-a")
	assertRes(t, "completed after restart", pod.Used, Resources{CPU: 5, Memory: 3})
}

func TestRestartRecoveryDuringRevocation(t *testing.T) {
	store := newTestFileStore(t)
	c, err := NewController(store)
	if err != nil {
		t.Fatalf("new controller: %v", err)
	}
	mustAddNode(t, c, "node-1", Resources{CPU: 8, Memory: 7})
	mustAddPod(t, c, "pod-a", "node-1", Resources{CPU: 3, Memory: 5})
	if _, err := c.Submit(ResizeRequest{PodUID: "pod-a", Version: 1, Target: Resources{CPU: 5, Memory: 3}}); err != nil {
		t.Fatalf("submit v1: %v", err)
	}
	if _, err := c.Submit(ResizeRequest{PodUID: "pod-a", Version: 2, Target: Resources{CPU: 6, Memory: 2}}); err != nil {
		t.Fatalf("submit v2: %v", err)
	}

	// Restart while gen 1 awaits revocation and gen 2 awaits preparation.
	c2, err := NewController(store)
	if err != nil {
		t.Fatalf("restart: %v", err)
	}
	view := c2.View()
	pod := findPod(findNode(t, view, "node-1"), "pod-a")
	if pod.ActiveGen != 2 {
		t.Fatalf("recovered active gen: got %d want 2", pod.ActiveGen)
	}
	g1 := findGen(pod, 1)
	if g1.CPUPhase != PhaseRevoking || g1.MemoryPhase != PhaseRevoking {
		t.Fatalf("recovered gen1 phases: cpu=%s mem=%s", g1.CPUPhase, g1.MemoryPhase)
	}
	assertRes(t, "recovered occupancy with revoking gen", pod.Used, Resources{CPU: 6, Memory: 5})

	// Capacity remains protected across restart: another Pod cannot grab the
	// memory that gen 1 still holds. Node has 7 memory, pod-a occupies 5
	// (revoking reservation), so only 2 memory is free and a 3-memory request
	// must be rejected.
	mustAddPod(t, c2, "pod-b", "node-1", Resources{CPU: 0, Memory: 0})
	_, err = c2.Submit(ResizeRequest{PodUID: "pod-b", Version: 1, Target: Resources{CPU: 0, Memory: 3}})
	if !errors.Is(err, ErrInsufficient) {
		t.Fatalf("recovered protection: want ErrInsufficient, got %v", err)
	}

	// Revoke can be confirmed after restart and then capacity opens up.
	if _, err := c2.RevokeAck(RevokeReceipt{PodUID: "pod-a", Gen: 1}); err != nil {
		t.Fatalf("revoke after restart: %v", err)
	}
	view = c2.View()
	pod = findPod(findNode(t, view, "node-1"), "pod-a")
	assertRes(t, "occupancy after recovered revoke", pod.Used, Resources{CPU: 6, Memory: 5})
}

// ---- 6. Multiple Pods on one node: isolation and interleaving ----------------

func TestMultiPodInterleaving(t *testing.T) {
	c := mustController(t)
	mustAddNode(t, c, "node-1", Resources{CPU: 10, Memory: 10})
	mustAddPod(t, c, "pod-a", "node-1", Resources{CPU: 3, Memory: 3})
	mustAddPod(t, c, "pod-b", "node-1", Resources{CPU: 2, Memory: 2})

	// pod-a reserves 5/5 (3/3 -> 5/5), pod-b reserves 4/4 (2/2 -> 4/1).
	if _, err := c.Submit(ResizeRequest{PodUID: "pod-a", Version: 1, Target: Resources{CPU: 5, Memory: 5}}); err != nil {
		t.Fatalf("submit a: %v", err)
	}
	if _, err := c.Submit(ResizeRequest{PodUID: "pod-b", Version: 1, Target: Resources{CPU: 4, Memory: 1}}); err != nil {
		t.Fatalf("submit b: %v", err)
	}
	view := c.View()
	node := findNode(t, view, "node-1")
	// Independent Pods sum. pod-a reserves 5/5; pod-b reserves
	// max(2,4)=4 CPU and max(2,1)=2 memory -> 9/7.
	assertRes(t, "node used with two pods", node.Used, Resources{CPU: 9, Memory: 7})

	// Interleave receipts: pod-a cpu, pod-b cpu, pod-a mem, pod-b mem.
	if _, err := c.PrepareAck(PrepareReceipt{PodUID: "pod-a", Gen: 1, Resource: CPU}); err != nil {
		t.Fatalf("a cpu: %v", err)
	}
	if _, err := c.PrepareAck(PrepareReceipt{PodUID: "pod-b", Gen: 1, Resource: CPU}); err != nil {
		t.Fatalf("b cpu: %v", err)
	}
	view, _ = c.PrepareAck(PrepareReceipt{PodUID: "pod-a", Gen: 1, Resource: Memory})
	podA := findPod(findNode(t, view, "node-1"), "pod-a")
	// pod-a fully ready -> switched and applying; pod-b still has mem pending.
	assertRes(t, "pod-a switched", podA.Visible, Resources{CPU: 5, Memory: 5})
	podB := findPod(findNode(t, view, "node-1"), "pod-b")
	assertRes(t, "pod-b not switched yet", podB.Visible, Resources{CPU: 2, Memory: 2})
	if findGen(podB, 1).MemoryPhase != PhasePrepare {
		t.Fatalf("pod-b memory must still be Prepare")
	}

	if _, err := c.PrepareAck(PrepareReceipt{PodUID: "pod-b", Gen: 1, Resource: Memory}); err != nil {
		t.Fatalf("b mem: %v", err)
	}
	// Unknown UID and unknown resource are real errors.
	if _, err := c.PrepareAck(PrepareReceipt{PodUID: "ghost", Gen: 1, Resource: CPU}); !errors.Is(err, ErrPodNotFound) {
		t.Fatalf("unknown pod: want ErrPodNotFound, got %v", err)
	}
	if _, err := c.PrepareAck(PrepareReceipt{PodUID: "pod-a", Gen: 1, Resource: "gpu"}); !errors.Is(err, ErrUnknownResource) {
		t.Fatalf("unknown resource: want ErrUnknownResource, got %v", err)
	}

	// Applying pod-a's release must not touch pod-b's outstanding reservation.
	view, err := c.ApplyAck(ApplyReceipt{PodUID: "pod-a", Gen: 1})
	if err != nil {
		t.Fatalf("apply a: %v", err)
	}
	node = findNode(t, view, "node-1")
	// pod-a now 5/5 released, pod-b still reserves 4/2 (max of its 2/2 and 4/1).
	assertRes(t, "node used after a apply, b pending", node.Used, Resources{CPU: 9, Memory: 7})
	view, _ = c.ApplyAck(ApplyReceipt{PodUID: "pod-b", Gen: 1})
	node = findNode(t, view, "node-1")
	// pod-b visible 4/1 now.
	assertRes(t, "node used after both apply", node.Used, Resources{CPU: 9, Memory: 6})

	// A receipt for an unknown generation is stale, not an error.
	view, err = c.ApplyAck(ApplyReceipt{PodUID: "pod-a", Gen: 99})
	if err != nil {
		t.Fatalf("unknown gen apply: %v", err)
	}
	if !view.Stale {
		t.Fatalf("unknown generation receipt must be stale")
	}
}

// ---- 7. Concurrency: serialized submissions keep a single consistent ledger --

func TestConcurrentSubmissions(t *testing.T) {
	c := mustController(t)
	mustAddNode(t, c, "node-1", Resources{CPU: 100, Memory: 100})
	mustAddPod(t, c, "pod-a", "node-1", Resources{CPU: 1, Memory: 1})

	const workers = 16
	const perWorker = 20
	var wg sync.WaitGroup
	errs := make(chan error, workers*perWorker)
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(base int64) {
			defer wg.Done()
			for i := 0; i < perWorker; i++ {
				version := base*int64(perWorker) + int64(i) + 1
				_, err := c.Submit(ResizeRequest{
					PodUID:  "pod-a",
					Version: version,
					Target:  Resources{CPU: 2, Memory: 2},
				})
				if err != nil && !errors.Is(err, ErrStaleVersion) {
					errs <- err
				}
			}
		}(int64(w))
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent submit failed: %v", err)
	}

	// Exactly one active generation survives, carrying the highest version.
	// Every previously accepted generation is retained in the Revoking state
	// because no node revocation receipt has arrived yet.
	view := c.View()
	pod := findPod(findNode(t, view, "node-1"), "pod-a")
	wantVersion := int64(workers * perWorker)
	if pod.LastVersion != wantVersion {
		t.Fatalf("last version: got %d want %d", pod.LastVersion, wantVersion)
	}
	if pod.ActiveGen == 0 {
		t.Fatalf("an active generation must remain")
	}
	var activeCount, revokingCount int
	for _, g := range pod.Generations {
		switch {
		case g.Gen == pod.ActiveGen:
			activeCount++
			if g.CPUPhase != PhasePrepare || g.MemoryPhase != PhasePrepare {
				t.Fatalf("active gen %d in unexpected phases: %s/%s",
					g.Gen, g.CPUPhase, g.MemoryPhase)
			}
		case g.CPUPhase == PhaseRevoking && g.MemoryPhase == PhaseRevoking:
			revokingCount++
		default:
			t.Fatalf("gen %d in unexpected phases: %s/%s",
				g.Gen, g.CPUPhase, g.MemoryPhase)
		}
	}
	if activeCount != 1 {
		t.Fatalf("want exactly 1 active generation, got %d", activeCount)
	}
	if revokingCount != len(pod.Generations)-1 {
		t.Fatalf("all superseded generations must await revocation, got %d of %d",
			revokingCount, len(pod.Generations)-1)
	}
	// The ledger always balances: used can never exceed capacity.
	node := findNode(t, view, "node-1")
	if node.Used.CPU > node.Capacity.CPU || node.Used.Memory > node.Capacity.Memory {
		t.Fatalf("ledger over capacity: used=%+v cap=%+v", node.Used, node.Capacity)
	}
}

// ---- 8. FileStore round-trip durability -------------------------------------

func TestFileStoreRoundTrip(t *testing.T) {
	store := newTestFileStore(t)
	snap := &Snapshot{
		Nodes: map[string]*nodeState{
			"n1": {Name: "n1", Capacity: Resources{CPU: 9, Memory: 9}},
		},
		Pods: map[string]*podState{
			"p1": {
				UID: "p1", NodeName: "n1",
				Visible:     Resources{CPU: 3, Memory: 5},
				LastVersion: 2,
				NextGen:     3,
				ActiveGen:   2,
				Gens: map[int64]*genState{
					2: {
						Gen: 2, Version: 2,
						Base:     Resources{CPU: 3, Memory: 5},
						Target:   Resources{CPU: 5, Memory: 3},
						Reserve:  Resources{CPU: 5, Memory: 5},
						CPUPhase: PhasePrepare,
						MemPhase: PhaseReady,
					},
				},
			},
		},
		PodNode: map[string]string{"p1": "n1"},
	}
	if err := store.Save(snap); err != nil {
		t.Fatalf("save: %v", err)
	}
	loaded, err := store.Load()
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if loaded.Nodes["n1"].Capacity != (Resources{CPU: 9, Memory: 9}) {
		t.Fatalf("node round-trip mismatch: %+v", loaded.Nodes["n1"])
	}
	g := loaded.Pods["p1"].Gens[2]
	if g == nil || g.CPUPhase != PhasePrepare || g.MemPhase != PhaseReady ||
		g.Reserve != (Resources{CPU: 5, Memory: 5}) {
		t.Fatalf("generation round-trip mismatch: %+v", g)
	}

	// Loading from a nonexistent file is a fresh, non-erroring store.
	empty := NewFileStore(filepath.Join(t.TempDir(), "missing.json"))
	snap2, err := empty.Load()
	if err != nil || snap2 != nil {
		t.Fatalf("missing file: snap=%v err=%v", snap2, err)
	}
}
