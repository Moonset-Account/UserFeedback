package reqreplybridge_test

import (
	"errors"
	"testing"

	reqreplybridge "reqreplybridge"
)

// TestRightToLeftDirection exercises the reverse forwarding leg end to end:
// a client on the right bus sends correlation 1, the bridge re-numbers it on
// its left egress session and swaps the address back on replies.
func TestRightToLeftDirection(t *testing.T) {
	left := reqreplybridge.NewBus("left")
	right := reqreplybridge.NewBus("right")
	br, err := reqreplybridge.NewBridge(left, right, reqreplybridge.BridgeConfig{
		Name:            "br",
		LeftIngress:     "br-in-l",
		LeftEgress:      "br-out-l",
		RightIngress:    "br-in-r",
		RightEgress:     "br-out-r",
		LRForwardTarget: "unused",
		RLForwardTarget: "svc-l",
	})
	if err != nil {
		t.Fatal(err)
	}
	defer br.Stop()
	if err := br.Start(); err != nil {
		t.Fatal(err)
	}

	svc := newService()
	svcEP, err := left.Attach("svc-l")
	if err != nil {
		t.Fatal(err)
	}
	svcEP.OnRequest(svc.handler)

	client, err := right.Attach("rclient")
	if err != nil {
		t.Fatal(err)
	}
	rec := newRecorder()
	cs, err := client.Connect(rec.handler)
	if err != nil {
		t.Fatal(err)
	}
	corr := mustCorr(t, cs)
	mustSend(t, right, cs, "br-in-r", corr, 0, 0)

	reqs := svc.snapshot()
	if len(reqs) != 1 {
		t.Fatalf("left service got %d requests, want 1", len(reqs))
	}
	fwd := reqs[0]
	if fwd.Correlation != 1 || fwd.ReplyTo.Endpoint != "br-out-l" || fwd.ReplyTo.Epoch != 1 {
		t.Fatalf("reverse egress addr = %+v, want br-out-l/e1/c1", fwd.ReplyTo)
	}
	if br.EgressSession(reqreplybridge.RightToLeft).Epoch() != 1 {
		t.Fatal("RightToLeft egress session missing")
	}

	// Reply on the left bus comes back through the reverse leg.
	sendReply(t, left, fwd.ReplyTo, 0, 0, u64p(1), u64p(0), "rl-ok")
	got := rec.snapshot()
	if len(got) != 1 || got[0].Addr.Endpoint != "rclient" || got[0].Addr.Correlation != 1 {
		t.Fatalf("reverse reply = %+v, want rclient corr1", got)
	}
	if st := br.Stats(reqreplybridge.RightToLeft); st.FinishedParents != 1 {
		t.Fatalf("reverse stats = %+v, want 1 finished parent", st)
	}
	if br.EgressEndpoint(reqreplybridge.RightToLeft).ID() != "br-out-l" {
		t.Fatal("wrong reverse egress endpoint")
	}
	if br.IngressEndpoint(reqreplybridge.RightToLeft).ID() != "br-in-r" {
		t.Fatal("wrong reverse ingress endpoint")
	}

	// Reconnect the reverse egress and verify short numbers restart at 1.
	if err := br.ReconnectEgress(reqreplybridge.RightToLeft); err != nil {
		t.Fatal(err)
	}
	mustSend(t, right, cs, "br-in-r", mustCorr(t, cs), 0, 0)
	fwd2 := svc.snapshot()[1]
	if fwd2.ReplyTo.Epoch != 2 || fwd2.ReplyTo.Correlation != 1 {
		t.Fatalf("post-reconnect reverse addr = %+v, want e2/c1", fwd2.ReplyTo)
	}

	// Unknown direction is rejected.
	if err := br.ReconnectEgress("sideways"); !errors.Is(err, reqreplybridge.ErrUnknownEndpoint) {
		t.Fatalf("bad direction err = %v, want ErrUnknownEndpoint", err)
	}
}

// TestBusErrorPaths covers the bus-level validation branches.
func TestBusErrorPaths(t *testing.T) {
	bus := reqreplybridge.NewBus("B")
	ep, err := bus.Attach("ep")
	if err != nil {
		t.Fatal(err)
	}
	s, err := ep.Connect(nil)
	if err != nil {
		t.Fatal(err)
	}

	// Sending without a live session.
	if err := bus.SendRequest(nil, reqreplybridge.RequestMsg{Target: "ep"}); !errors.Is(err, reqreplybridge.ErrNotConnected) {
		t.Fatalf("nil sender err = %v, want ErrNotConnected", err)
	}
	// Unknown target.
	if err := bus.SendRequest(s, reqreplybridge.RequestMsg{Target: "ghost"}); !errors.Is(err, reqreplybridge.ErrUnknownEndpoint) {
		t.Fatalf("unknown target err = %v, want ErrUnknownEndpoint", err)
	}
	// Target with no handler.
	bare, err := bus.Attach("bare")
	if err != nil {
		t.Fatal(err)
	}
	if err := bus.SendRequest(s, reqreplybridge.RequestMsg{Target: "bare"}); !errors.Is(err, reqreplybridge.ErrNoHandler) {
		t.Fatalf("no handler err = %v, want ErrNoHandler", err)
	}
	// Handler error aborts fan-out and is propagated.
	bare.OnRequest(func(reqreplybridge.RequestMsg) error { return errors.New("boom") })
	if err := bus.SendRequest(s, reqreplybridge.RequestMsg{Target: "bare"}); err == nil || err.Error() != "boom" {
		t.Fatalf("handler err = %v, want boom", err)
	}
	// Closed target.
	bare.Close()
	if err := bus.SendRequest(s, reqreplybridge.RequestMsg{Target: "bare"}); !errors.Is(err, reqreplybridge.ErrEndpointClosed) {
		t.Fatalf("closed target err = %v, want ErrEndpointClosed", err)
	}
	// Multiple handlers each run until one fails.
	multi, err := bus.Attach("multi")
	if err != nil {
		t.Fatal(err)
	}
	calls := 0
	multi.OnRequest(func(reqreplybridge.RequestMsg) error { calls++; return nil })
	multi.OnRequest(func(reqreplybridge.RequestMsg) error { calls++; return nil })
	if err := bus.SendRequest(s, reqreplybridge.RequestMsg{Target: "multi"}); err != nil {
		t.Fatal(err)
	}
	if calls != 2 {
		t.Fatalf("multi handlers ran %d times, want 2", calls)
	}

	// Disconnect then reconnect without an intervening current session still
	// yields a strictly increasing epoch (covered behavior of the epoch scan).
	ep.Disconnect()
	s2, err := ep.Connect(nil)
	if err != nil {
		t.Fatal(err)
	}
	if s2.Epoch() != 2 {
		t.Fatalf("epoch after disconnect/reconnect = %d, want 2", s2.Epoch())
	}
	// Detached old session still answers late replies.
	oldEP, _ := bus.Attach("late")
	oldS, err := oldEP.Connect(newRecorder().handler)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = oldS.AllocateCorrelation()
	oldEP.Disconnect()
	_, _ = oldEP.Connect(nil)
	if err := bus.SendReply(reqreplybridge.ReplyMsg{
		Addr: reqreplybridge.ReplyAddr{Endpoint: "late", Epoch: 1, Correlation: 1},
	}); err != nil {
		t.Fatalf("late reply to detached epoch err = %v", err)
	}
}

// TestForwardFailureCleansUp covers the branch where forwarding fails after the
// egress correlation was allocated (here the egress bus has no forwarding
// target), ensuring the caller gets the error rather than a silent drop.
func TestForwardFailureCleansUp(t *testing.T) {
	left := reqreplybridge.NewBus("left")
	right := reqreplybridge.NewBus("right")
	br, err := reqreplybridge.NewBridge(left, right, reqreplybridge.BridgeConfig{
		Name:            "br",
		LeftIngress:     "br-in",
		LeftEgress:      "br-out-l",
		RightIngress:    "br-in-r",
		RightEgress:     "br-out-r",
		LRForwardTarget: "missing-svc",
		RLForwardTarget: "unused",
	})
	if err != nil {
		t.Fatal(err)
	}
	defer br.Stop()
	if err := br.Start(); err != nil {
		t.Fatal(err)
	}
	client, err := left.Attach("client")
	if err != nil {
		t.Fatal(err)
	}
	cs, err := client.Connect(nil)
	if err != nil {
		t.Fatal(err)
	}
	err = left.SendRequest(cs, reqreplybridge.RequestMsg{
		Target: "br-in", Correlation: mustCorr(t, cs),
	})
	if !errors.Is(err, reqreplybridge.ErrUnknownEndpoint) {
		t.Fatalf("forward to missing target err = %v, want ErrUnknownEndpoint", err)
	}
}
