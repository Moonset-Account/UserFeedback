package reqreplybridge_test

import (
	"errors"
	"sync"
	"testing"

	reqreplybridge "reqreplybridge"
)

type testTopology struct {
	left   *reqreplybridge.Bus
	right  *reqreplybridge.Bus
	bridge *reqreplybridge.Bridge
	svc    *service
}

func newSingleBridge(t *testing.T) testTopology {
	t.Helper()
	left := reqreplybridge.NewBus("left")
	right := reqreplybridge.NewBus("right")
	br, err := reqreplybridge.NewBridge(left, right, reqreplybridge.BridgeConfig{
		Name:            "br",
		LeftIngress:     "br-in",
		LeftEgress:      "br-out-l",
		RightIngress:    "br-in-r",
		RightEgress:     "br-out-r",
		LRForwardTarget: "svc",
		RLForwardTarget: "svc-l",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := br.Start(); err != nil {
		t.Fatal(err)
	}
	svc := newService()
	svcEP, err := right.Attach("svc")
	if err != nil {
		t.Fatal(err)
	}
	svcEP.OnRequest(svc.handler)
	return testTopology{left: left, right: right, bridge: br, svc: svc}
}

// TestBridgeRenumbersAndSwapsBack verifies the bridge allocates its own
// egress correlation (starting at 1) and restores the original session
// correlation when replies return.
func TestBridgeRenumbersAndSwapsBack(t *testing.T) {
	top := newSingleBridge(t)
	defer top.bridge.Stop()

	client, err := top.left.Attach("client")
	if err != nil {
		t.Fatal(err)
	}
	rec := newRecorder()
	cs, err := client.Connect(rec.handler)
	if err != nil {
		t.Fatal(err)
	}
	corr := mustCorr(t, cs)
	if corr != 1 {
		t.Fatalf("client corr = %d, want 1", corr)
	}
	mustSend(t, top.left, cs, "br-in", corr, 0, 0)

	reqs := top.svc.snapshot()
	if len(reqs) != 1 {
		t.Fatalf("service got %d requests, want 1", len(reqs))
	}
	fwd := reqs[0]
	if fwd.Correlation != 1 {
		t.Fatalf("egress corr = %d, want 1 (fresh per-session numbering)", fwd.Correlation)
	}
	if fwd.ReplyTo.Endpoint != "br-out-r" || fwd.ReplyTo.Epoch != 1 || fwd.ReplyTo.Correlation != 1 {
		t.Fatalf("forwarded ReplyTo = %+v, want br-out-r/e1/c1", fwd.ReplyTo)
	}

	sendReply(t, top.right, fwd.ReplyTo, 0, 0, u64p(1), u64p(0), "ok")
	got := rec.snapshot()
	if len(got) != 1 {
		t.Fatalf("client got %d replies, want 1", len(got))
	}
	if got[0].Addr.Endpoint != "client" || got[0].Addr.Epoch != 1 || got[0].Addr.Correlation != 1 {
		t.Fatalf("restored addr = %+v, want client/e1/c1", got[0].Addr)
	}
}

// TestInterleavedBranchesAndParentFinishing covers the headline rule: a branch
// may finish early and still receive an earlier fragment afterwards; it only
// counts as ended when seq 0..tail is continuous, and the parent waits for
// every declared branch.
func TestInterleavedBranchesAndParentFinishing(t *testing.T) {
	top := newSingleBridge(t)
	defer top.bridge.Stop()

	var mu sync.Mutex
	var branchEnds, parentEnds int
	var gapErrs int
	top.bridge.OnBridgedReply(func(e reqreplybridge.ReplyEvent) {
		mu.Lock()
		defer mu.Unlock()
		if e.BranchEnded {
			branchEnds++
		}
		if e.ParentEnded {
			parentEnds++
		}
		if errors.Is(e.Error, reqreplybridge.ErrGap) {
			gapErrs++
		}
	})

	client, err := top.left.Attach("client")
	if err != nil {
		t.Fatal(err)
	}
	rec := newRecorder()
	cs, err := client.Connect(rec.handler)
	if err != nil {
		t.Fatal(err)
	}
	mustSend(t, top.left, cs, "br-in", mustCorr(t, cs), 0, 0)
	to := top.svc.snapshot()[0].ReplyTo

	// Two branches declared. Branch 0 has tail 2, branch 1 has tail 0.
	// Deliberately out of order: branch 1 (the short one) completes first.
	sendReply(t, top.right, to, 1, 0, u64p(2), u64p(0), "b1")
	mu.Lock()
	if branchEnds != 1 || parentEnds != 0 {
		mu.Unlock()
		t.Fatalf("after short branch: branchEnds=%d parentEnds=%d, want 1/0", branchEnds, parentEnds)
	}
	mu.Unlock()

	// Branch 0 tail arrives before its earlier fragments: not continuous yet.
	sendReply(t, top.right, to, 0, 2, nil, u64p(2), "b0-tail")
	mu.Lock()
	if branchEnds != 1 || gapErrs != 1 || parentEnds != 0 {
		mu.Unlock()
		t.Fatalf("tail-first: branchEnds=%d gapErrs=%d parentEnds=%d, want 1/1/0",
			branchEnds, gapErrs, parentEnds)
	}
	mu.Unlock()

	// Earlier fragments now arrive; branch 0 becomes continuous and ends.
	sendReply(t, top.right, to, 0, 0, nil, nil, "b0-0")
	sendReply(t, top.right, to, 0, 1, nil, nil, "b0-1")

	mu.Lock()
	if branchEnds != 2 || parentEnds != 1 {
		mu.Unlock()
		t.Fatalf("after continuity: branchEnds=%d parentEnds=%d, want 2/1", branchEnds, parentEnds)
	}
	mu.Unlock()

	// Client observed the raw arrival order, with its own corr/branch restored.
	got := rec.snapshot()
	wantPayload := []string{"b1", "b0-tail", "b0-0", "b0-1"}
	if len(got) != len(wantPayload) {
		t.Fatalf("client got %d fragments, want %d", len(got), len(wantPayload))
	}
	for i, want := range wantPayload {
		if got[i].Payload != want {
			t.Fatalf("fragment %d payload = %q, want %q (arrival order must be preserved)",
				i, got[i].Payload, want)
		}
		if got[i].Addr.Correlation != 1 {
			t.Fatalf("fragment %d restored corr = %d, want 1", i, got[i].Addr.Correlation)
		}
	}
	if got[0].Branch != 1 || got[1].Branch != 0 {
		t.Fatalf("restored branches = %d,%d, want 1,0", got[0].Branch, got[1].Branch)
	}

	st := top.bridge.Stats(reqreplybridge.LeftToRight)
	if st.FinishedBranches != 2 || st.FinishedParents != 1 || st.ActiveParents != 0 {
		t.Fatalf("stats = %+v, want 2 finished branches, 1 parent, 0 active", st)
	}
}

// TestParentWaitsForAllBranches ensures a parent cannot finish while a declared
// branch has never appeared.
func TestParentWaitsForAllBranches(t *testing.T) {
	top := newSingleBridge(t)
	defer top.bridge.Stop()

	parentEnded := 0
	top.bridge.OnBridgedReply(func(e reqreplybridge.ReplyEvent) {
		if e.ParentEnded {
			parentEnded++
		}
	})

	client, err := top.left.Attach("client")
	if err != nil {
		t.Fatal(err)
	}
	rec := newRecorder()
	cs, err := client.Connect(rec.handler)
	if err != nil {
		t.Fatal(err)
	}
	mustSend(t, top.left, cs, "br-in", mustCorr(t, cs), 0, 0)
	to := top.svc.snapshot()[0].ReplyTo

	// Two branches declared, but only branch 0 completes.
	sendReply(t, top.right, to, 0, 0, u64p(2), u64p(0), "only-b0")
	if parentEnded != 0 {
		t.Fatalf("parent ended with missing branch, parentEnded=%d", parentEnded)
	}
	if st := top.bridge.Stats(reqreplybridge.LeftToRight); st.ActiveParents != 1 {
		t.Fatalf("ActiveParents = %d, want 1 while a branch is missing", st.ActiveParents)
	}

	// The missing branch arrives and completes; now the parent may finish.
	sendReply(t, top.right, to, 1, 0, nil, u64p(0), "b1")
	if parentEnded != 1 {
		t.Fatalf("parentEnded = %d, want 1 once all branches ended", parentEnded)
	}
	if st := top.bridge.Stats(reqreplybridge.LeftToRight); st.ActiveParents != 0 {
		t.Fatalf("ActiveParents = %d, want 0 after parent finished", st.ActiveParents)
	}
}

// TestEgressReconnectReusesShortNumbersAndIsolatesLatePackets covers reuse of
// short numbers on a new egress incarnation and isolation of late packets from
// the old incarnation.
func TestEgressReconnectReusesShortNumbersAndIsolatesLatePackets(t *testing.T) {
	top := newSingleBridge(t)
	defer top.bridge.Stop()

	client, err := top.left.Attach("client")
	if err != nil {
		t.Fatal(err)
	}
	rec := newRecorder()
	cs, err := client.Connect(rec.handler)
	if err != nil {
		t.Fatal(err)
	}

	// First request on epoch 1; keep it outstanding so its state survives.
	mustSend(t, top.left, cs, "br-in", mustCorr(t, cs), 0, 0)
	oldReq := top.svc.snapshot()[0]
	if oldReq.ReplyTo.Epoch != 1 {
		t.Fatalf("first egress epoch = %d, want 1", oldReq.ReplyTo.Epoch)
	}

	// Reconnect: short numbers restart at 1 on epoch 2.
	if err := top.bridge.ReconnectEgress(reqreplybridge.LeftToRight); err != nil {
		t.Fatal(err)
	}
	mustSend(t, top.left, cs, "br-in", mustCorr(t, cs), 0, 0)
	newReq := top.svc.snapshot()[1]
	if newReq.ReplyTo.Epoch != 2 || newReq.ReplyTo.Correlation != 1 {
		t.Fatalf("post-reconnect egress addr = %+v, want epoch2 corr1 (reused short number)",
			newReq.ReplyTo)
	}

	// A late packet from the old connection, even though it reuses corr 1,
	// must map to the old session's parent and the original client corr.
	sendReply(t, top.right, oldReq.ReplyTo, 0, 0, u64p(1), u64p(0), "late-old")
	got := rec.snapshot()
	if len(got) != 1 {
		t.Fatalf("after late old packet, replies = %d, want 1", len(got))
	}
	if got[0].Addr.Correlation != 1 || got[0].Payload != "late-old" {
		t.Fatalf("late packet delivery = %+v, want original client corr 1", got[0])
	}
	if st := top.bridge.Stats(reqreplybridge.LeftToRight); st.ActiveParents != 1 {
		t.Fatalf("ActiveParents = %d, want 1 (old parent finalized, new still open)", st.ActiveParents)
	}

	// The new incarnation's reply maps independently to the second client corr.
	sendReply(t, top.right, newReq.ReplyTo, 0, 0, u64p(1), u64p(0), "new")
	got = rec.snapshot()
	if len(got) != 2 || got[1].Addr.Correlation != 2 || got[1].Payload != "new" {
		t.Fatalf("new-session reply = %+v, want client corr 2 payload new", got)
	}

	// A reply claiming an epoch/correlation the bridge never mapped is stale at
	// the bridge layer. Feed it directly: the bus session layer would reject an
	// unallocated correlation with ErrBadCorrelation first (covered separately
	// in TestSessionCorrelationRestartsAtOne).
	ev := top.bridge.EgressEndpoint(reqreplybridge.LeftToRight)
	ghost := reqreplybridge.ReplyAddr{Endpoint: ev.ID(), Epoch: 2, Correlation: 42}
	out := top.bridge.HandleEgressReply(reqreplybridge.LeftToRight,
		reqreplybridge.ReplyMsg{Addr: ghost})
	if !errors.Is(out.Error, reqreplybridge.ErrStaleReply) {
		t.Fatalf("ghost reply err = %v, want ErrStaleReply", out.Error)
	}

	// And the bus itself rejects a correlation the egress session never issued.
	err = top.right.SendReply(reqreplybridge.ReplyMsg{Addr: ghost})
	if !errors.Is(err, reqreplybridge.ErrBadCorrelation) {
		t.Fatalf("bus ghost reply err = %v, want ErrBadCorrelation", err)
	}
}

// TestEgressDownRejectsForwarding verifies the bridge fails closed while its
// egress is disconnected and resumes after reconnect.
func TestEgressDownRejectsForwarding(t *testing.T) {
	top := newSingleBridge(t)
	defer top.bridge.Stop()

	client, err := top.left.Attach("client")
	if err != nil {
		t.Fatal(err)
	}
	cs, err := client.Connect(nil)
	if err != nil {
		t.Fatal(err)
	}
	corr := mustCorr(t, cs)

	top.bridge.EgressEndpoint(reqreplybridge.LeftToRight).Disconnect()
	err = top.left.SendRequest(cs, reqreplybridge.RequestMsg{
		Target: "br-in", Correlation: corr,
	})
	if !errors.Is(err, reqreplybridge.ErrEgressDown) {
		t.Fatalf("forward while down err = %v, want ErrEgressDown", err)
	}

	if err := top.bridge.ReconnectEgress(reqreplybridge.LeftToRight); err != nil {
		t.Fatal(err)
	}
	mustSend(t, top.left, cs, "br-in", mustCorr(t, cs), 0, 0)
	if len(top.svc.snapshot()) != 1 {
		t.Fatalf("service requests after reconnect = %d, want 1", len(top.svc.snapshot()))
	}
}
