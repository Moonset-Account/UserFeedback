package reqreplybridge_test

import (
	"errors"
	"testing"

	reqreplybridge "reqreplybridge"
)

// TestStaleReplyInvokesObserver verifies a stale bridge-level reply still fires
// the observer with ErrStaleReply.
func TestStaleReplyInvokesObserver(t *testing.T) {
	top := newSingleBridge(t)
	defer top.bridge.Stop()

	var observed int
	top.bridge.OnBridgedReplyLeg(reqreplybridge.LeftToRight, func(e reqreplybridge.ReplyEvent) {
		if errors.Is(e.Error, reqreplybridge.ErrStaleReply) {
			observed++
		}
	})
	out := top.bridge.HandleEgressReply(reqreplybridge.LeftToRight, reqreplybridge.ReplyMsg{
		Addr: reqreplybridge.ReplyAddr{Endpoint: "br-out-r", Epoch: 9, Correlation: 9},
	})
	if !errors.Is(out.Error, reqreplybridge.ErrStaleReply) || observed != 1 {
		t.Fatalf("stale event = %+v observed=%d, want ErrStaleReply and 1 callback", out, observed)
	}

	// Reverse-leg direct injection covers the other HandleEgressReply branch.
	out = top.bridge.HandleEgressReply(reqreplybridge.RightToLeft, reqreplybridge.ReplyMsg{
		Addr: reqreplybridge.ReplyAddr{Endpoint: "br-out-l", Epoch: 9, Correlation: 9},
	})
	if !errors.Is(out.Error, reqreplybridge.ErrStaleReply) {
		t.Fatalf("reverse stale event = %+v, want ErrStaleReply", out)
	}

	// The both-direction setters also wire the reverse leg without panicking.
	top.bridge.OnForward(func(reqreplybridge.ForwardEvent) {})
	top.bridge.OnBridgedReply(func(reqreplybridge.ReplyEvent) {})
}

// TestNewBridgeRejectsDuplicateEndpoints covers the attachment error paths.
func TestNewBridgeRejectsDuplicateEndpoints(t *testing.T) {
	left := reqreplybridge.NewBus("l")
	right := reqreplybridge.NewBus("r")
	if _, err := reqreplybridge.NewBridge(left, right, reqreplybridge.BridgeConfig{
		Name:            "dup",
		LeftIngress:     "same",
		LeftEgress:      "same", // duplicate on the left bus
		RightIngress:    "ri",
		RightEgress:     "re",
		LRForwardTarget: "t",
		RLForwardTarget: "t",
	}); err == nil {
		t.Fatal("duplicate left endpoint should fail")
	}

	// Left endpoints already exist on this bus, so attaching again fails too.
	if _, err := reqreplybridge.NewBridge(left, right, reqreplybridge.BridgeConfig{
		Name:            "dup2",
		LeftIngress:     "same",
		LeftEgress:      "other",
		RightIngress:    "ri",
		RightEgress:     "re",
		LRForwardTarget: "t",
		RLForwardTarget: "t",
	}); err == nil {
		t.Fatal("reusing an existing endpoint id should fail")
	}
}

// TestConnectWithoutDisconnect covers replacing the current session directly.
func TestConnectWithoutDisconnect(t *testing.T) {
	bus := reqreplybridge.NewBus("B")
	ep, err := bus.Attach("ep")
	if err != nil {
		t.Fatal(err)
	}
	s1, err := ep.Connect(nil)
	if err != nil {
		t.Fatal(err)
	}
	s2, err := ep.Connect(nil)
	if err != nil {
		t.Fatal(err)
	}
	if s1.Epoch() != 1 || s2.Epoch() != 2 {
		t.Fatalf("epochs = %d,%d, want 1,2", s1.Epoch(), s2.Epoch())
	}
	if ep.Session() != s2 {
		t.Fatal("current session should be the newest incarnation")
	}
	if old, ok := ep.SessionByEpoch(1); !ok || old != s1 {
		t.Fatal("old incarnation must remain reachable by epoch")
	}
	if _, ok := ep.SessionByEpoch(99); ok {
		t.Fatal("missing epoch should not resolve")
	}

	// Reply to an unknown endpoint and to a closed session.
	err = bus.SendReply(reqreplybridge.ReplyMsg{
		Addr: reqreplybridge.ReplyAddr{Endpoint: "ghost", Epoch: 1, Correlation: 1},
	})
	if !errors.Is(err, reqreplybridge.ErrUnknownEndpoint) {
		t.Fatalf("unknown reply endpoint err = %v", err)
	}
	// After Close all incarnations are discarded, so the epoch no longer
	// resolves; the bus reports it as an unknown session incarnation.
	ep.Close()
	err = bus.SendReply(reqreplybridge.ReplyMsg{
		Addr: reqreplybridge.ReplyAddr{Endpoint: "ep", Epoch: 1, Correlation: 1},
	})
	if !errors.Is(err, reqreplybridge.ErrUnknownEndpoint) {
		t.Fatalf("reply after close err = %v, want ErrUnknownEndpoint", err)
	}
}

// TestReplyToClosedIngressErrors covers the bridge path where swapping back
// fails because the original ingress session incarnation is gone.
func TestReplyToClosedIngressErrors(t *testing.T) {
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
	mustSend(t, top.left, cs, "br-in", mustCorr(t, cs), 0, 0)
	to := top.svc.snapshot()[0].ReplyTo

	// Closing the client removes the ingress incarnation the bridge would
	// deliver to; the bridge reports the delivery error on the event.
	client.Close()
	ev := top.bridge.HandleEgressReply(reqreplybridge.LeftToRight, reqreplybridge.ReplyMsg{
		Addr:   to,
		Branch: 0,
		Seq:    0,
	})
	if !errors.Is(ev.Error, reqreplybridge.ErrUnknownEndpoint) {
		t.Fatalf("delivery to closed ingress err = %v, want ErrUnknownEndpoint", ev.Error)
	}
}
