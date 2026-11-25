package reqreplybridge_test

import (
	"errors"
	"testing"

	reqreplybridge "reqreplybridge"
)

// TestSessionCorrelationRestartsAtOne verifies every client session numbers
// requests from 1 and a reconnect (new epoch) reuses the short numbers while
// the old incarnation still validates late replies.
func TestSessionCorrelationRestartsAtOne(t *testing.T) {
	bus := reqreplybridge.NewBus("B")
	ep, err := bus.Attach("client")
	if err != nil {
		t.Fatal(err)
	}
	rec := newRecorder()
	s1, err := ep.Connect(rec.handler)
	if err != nil {
		t.Fatal(err)
	}
	if got := mustCorr(t, s1); got != 1 {
		t.Fatalf("first corr = %d, want 1", got)
	}
	if got := mustCorr(t, s1); got != 2 {
		t.Fatalf("second corr = %d, want 2", got)
	}
	if s1.Epoch() != 1 {
		t.Fatalf("epoch = %d, want 1", s1.Epoch())
	}

	ep.Disconnect()
	s2, err := ep.Connect(rec.handler)
	if err != nil {
		t.Fatal(err)
	}
	if s2.Epoch() != 2 {
		t.Fatalf("new epoch = %d, want 2", s2.Epoch())
	}
	if got := mustCorr(t, s2); got != 1 {
		t.Fatalf("post-reconnect corr = %d, want 1", got)
	}

	// A late reply addressed to the old epoch/correlation still lands on the
	// old session and not on the new one.
	oldAddr := reqreplybridge.ReplyAddr{Endpoint: "client", Epoch: 1, Correlation: 1}
	sendReply(t, bus, oldAddr, 0, 0, nil, nil, "late")
	if got := len(rec.snapshot()); got != 1 {
		t.Fatalf("recorder got %d replies, want 1 delivered to old session", got)
	}

	// A correlation the old session never allocated is rejected.
	bad := reqreplybridge.ReplyAddr{Endpoint: "client", Epoch: 1, Correlation: 99}
	err = bus.SendReply(reqreplybridge.ReplyMsg{Addr: bad})
	if !errors.Is(err, reqreplybridge.ErrBadCorrelation) {
		t.Fatalf("late bad corr err = %v, want ErrBadCorrelation", err)
	}
	if s1.LateDropped() != 1 {
		t.Fatalf("LateDropped = %d, want 1", s1.LateDropped())
	}

	// Unknown endpoint/epoch are rejected.
	err = bus.SendReply(reqreplybridge.ReplyMsg{
		Addr: reqreplybridge.ReplyAddr{Endpoint: "ghost", Epoch: 1, Correlation: 1},
	})
	if !errors.Is(err, reqreplybridge.ErrUnknownEndpoint) {
		t.Fatalf("ghost endpoint err = %v, want ErrUnknownEndpoint", err)
	}
}

// TestRequestReplyRouting verifies the bus stamps ReplyTo and routes replies
// back to the exact session incarnation.
func TestRequestReplyRouting(t *testing.T) {
	bus := reqreplybridge.NewBus("B")
	client, err := bus.Attach("client")
	if err != nil {
		t.Fatal(err)
	}
	server, err := bus.Attach("server")
	if err != nil {
		t.Fatal(err)
	}
	var seen reqreplybridge.RequestMsg
	server.OnRequest(func(m reqreplybridge.RequestMsg) error {
		seen = m
		return nil
	})

	rec := newRecorder()
	s, err := client.Connect(rec.handler)
	if err != nil {
		t.Fatal(err)
	}
	corr := mustCorr(t, s)
	mustSend(t, bus, s, "server", corr, 0, 0)

	if seen.ReplyTo.Endpoint != "client" || seen.ReplyTo.Epoch != 1 || seen.ReplyTo.Correlation != 1 {
		t.Fatalf("stamped ReplyTo = %+v, want client/e1/c1", seen.ReplyTo)
	}
	sendReply(t, bus, seen.ReplyTo, 0, 0, u64p(1), u64p(0), "ok")
	got := rec.snapshot()
	if len(got) != 1 || got[0].Payload != "ok" {
		t.Fatalf("recorder = %+v, want one ok reply", got)
	}
}
