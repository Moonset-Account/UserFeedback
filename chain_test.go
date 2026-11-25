package reqreplybridge_test

import (
	"testing"

	reqreplybridge "reqreplybridge"
)

// chainTopology builds three buses A -> M -> C connected by two bridges, so a
// request from A to C crosses two bridge levels and replies swap correlation
// numbers back twice.
type chainTopology struct {
	a, m, c *reqreplybridge.Bus
	b1, b2  *reqreplybridge.Bridge
	svc     *service
	svcEP   *reqreplybridge.Endpoint
}

func newChain(t *testing.T) chainTopology {
	t.Helper()
	a := reqreplybridge.NewBus("A")
	m := reqreplybridge.NewBus("M")
	c := reqreplybridge.NewBus("C")

	b1, err := reqreplybridge.NewBridge(a, m, reqreplybridge.BridgeConfig{
		Name:            "B1",
		LeftIngress:     "b1-in",
		LeftEgress:      "b1-out-a",
		RightIngress:    "b1-in-m",
		RightEgress:     "b1-out-m",
		LRForwardTarget: "b2-in",
		RLForwardTarget: "b1-in",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := b1.Start(); err != nil {
		t.Fatal(err)
	}
	b2, err := reqreplybridge.NewBridge(m, c, reqreplybridge.BridgeConfig{
		Name:            "B2",
		LeftIngress:     "b2-in",
		LeftEgress:      "b2-out-m",
		RightIngress:    "b2-in-c",
		RightEgress:     "b2-out-c",
		LRForwardTarget: "svc",
		RLForwardTarget: "b1-in-m",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := b2.Start(); err != nil {
		t.Fatal(err)
	}
	svc := newService()
	svcEP, err := c.Attach("svc")
	if err != nil {
		t.Fatal(err)
	}
	svcEP.OnRequest(svc.handler)
	return chainTopology{a: a, m: m, c: c, b1: b1, b2: b2, svc: svc, svcEP: svcEP}
}

// TestTwoLevelChainTwoIngressesBothCorrelationOne is the scenario demanded by
// the statement: two ingress callers each open correlation number 1, one
// request crosses two bridge levels, and interleaved replies return to the
// actual callers with original correlation and branch numbers restored.
func TestTwoLevelChainTwoIngressesBothCorrelationOne(t *testing.T) {
	top := newChain(t)
	defer top.b1.Stop()
	defer top.b2.Stop()

	client1, err := top.a.Attach("client-1")
	if err != nil {
		t.Fatal(err)
	}
	client2, err := top.a.Attach("client-2")
	if err != nil {
		t.Fatal(err)
	}
	rec1, rec2 := newRecorder(), newRecorder()
	s1, err := client1.Connect(rec1.handler)
	if err != nil {
		t.Fatal(err)
	}
	s2, err := client2.Connect(rec2.handler)
	if err != nil {
		t.Fatal(err)
	}

	// Both ingress sessions independently start numbering at 1.
	if c := mustCorr(t, s1); c != 1 {
		t.Fatalf("client1 corr = %d, want 1", c)
	}
	if c := mustCorr(t, s2); c != 1 {
		t.Fatalf("client2 corr = %d, want 1", c)
	}
	mustSend(t, top.a, s1, "b1-in", 1, 0, 0)
	mustSend(t, top.a, s2, "b1-in", 1, 0, 0)

	reqs := top.svc.snapshot()
	if len(reqs) != 2 {
		t.Fatalf("service got %d requests, want 2", len(reqs))
	}
	// On the shared B2 egress session the two requests got distinct numbers,
	// although both ingress callers used correlation 1.
	if reqs[0].Correlation != 1 || reqs[1].Correlation != 2 {
		t.Fatalf("service-side corr = %d,%d, want 1,2", reqs[0].Correlation, reqs[1].Correlation)
	}
	toClient1 := reqs[0].ReplyTo // b2-out-c e1 c1
	toClient2 := reqs[1].ReplyTo // b2-out-c e1 c2

	// Interleaved replies:
	//  1. client1 branch1 (short branch) completes first
	//  2. client2's only branch completes
	//  3-5. client1 branch0 fragments arrive tail-first, then 0,1
	sendReply(t, top.c, toClient1, 1, 0, u64p(2), u64p(0), "c1-b1")
	sendReply(t, top.c, toClient2, 0, 0, u64p(1), u64p(0), "c2-b0")
	sendReply(t, top.c, toClient1, 0, 2, nil, u64p(2), "c1-b0-seq2")
	sendReply(t, top.c, toClient1, 0, 0, nil, nil, "c1-b0-seq0")
	sendReply(t, top.c, toClient1, 0, 1, nil, nil, "c1-b0-seq1")

	// Caller isolation: each recorder only sees its own request's replies.
	got1 := rec1.snapshot()
	got2 := rec2.snapshot()
	if len(got2) != 1 || got2[0].Payload != "c2-b0" {
		t.Fatalf("client2 replies = %+v, want only c2-b0", got2)
	}
	if got2[0].Addr.Endpoint != "client-2" || got2[0].Addr.Correlation != 1 {
		t.Fatalf("client2 restored addr = %+v, want client-2 corr1", got2[0].Addr)
	}

	want1 := []string{"c1-b1", "c1-b0-seq2", "c1-b0-seq0", "c1-b0-seq1"}
	if len(got1) != len(want1) {
		t.Fatalf("client1 got %d replies, want %d", len(got1), len(want1))
	}
	for i, want := range want1 {
		if got1[i].Payload != want {
			t.Fatalf("client1 fragment %d = %q, want %q (arrival order preserved)", i, got1[i].Payload, want)
		}
		if got1[i].Addr.Endpoint != "client-1" || got1[i].Addr.Correlation != 1 {
			t.Fatalf("client1 fragment %d addr = %+v, want client-1 corr1 after two swap-backs", i, got1[i].Addr)
		}
	}
	// Branch numbers restored at each level: first fragment is branch 1.
	if got1[0].Branch != 1 || got1[1].Branch != 0 {
		t.Fatalf("client1 branches = %d,%d, want 1,0", got1[0].Branch, got1[1].Branch)
	}

	// Both bridges finalized one parent per ingress request.
	if st := top.b1.Stats(reqreplybridge.LeftToRight); st.FinishedParents != 2 || st.ActiveParents != 0 {
		t.Fatalf("B1 stats = %+v, want 2 finished parents, 0 active", st)
	}
	if st := top.b2.Stats(reqreplybridge.LeftToRight); st.FinishedParents != 2 || st.ActiveParents != 0 {
		t.Fatalf("B2 stats = %+v, want 2 finished parents, 0 active", st)
	}
}
