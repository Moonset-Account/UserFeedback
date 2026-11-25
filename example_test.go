package reqreplybridge_test

import (
	"fmt"
	"strings"

	reqreplybridge "reqreplybridge"
)

// Example_twoLevelChain demonstrates the scenario from the statement: two
// ingress callers on bus A each allocate correlation number 1; the requests
// cross two bridge levels (A->M->C); replies come back interleaved and
// out-of-order. The printed trace shows which egress short numbers were
// allocated per hop, which actual caller receives each fragment, and the
// preserved fragment order.
func Example_twoLevelChain() {
	a := reqreplybridge.NewBus("A")
	m := reqreplybridge.NewBus("M")
	c := reqreplybridge.NewBus("C")

	b1, _ := reqreplybridge.NewBridge(a, m, reqreplybridge.BridgeConfig{
		Name: "B1", LeftIngress: "b1-in", LeftEgress: "b1-out-a",
		RightIngress: "b1-in-m", RightEgress: "b1-out-m",
		LRForwardTarget: "b2-in", RLForwardTarget: "b1-in",
	})
	_ = b1.Start()
	b2, _ := reqreplybridge.NewBridge(m, c, reqreplybridge.BridgeConfig{
		Name: "B2", LeftIngress: "b2-in", LeftEgress: "b2-out-m",
		RightIngress: "b2-in-c", RightEgress: "b2-out-c",
		LRForwardTarget: "svc", RLForwardTarget: "b1-in-m",
	})
	_ = b2.Start()

	fwdLine := func(bridge string, e reqreplybridge.ForwardEvent) {
		fmt.Printf("FWD %s %s: %s/e%d/c%d -> %s/e%d/c%d\n",
			bridge, e.Direction,
			e.Ingress.Endpoint, e.Ingress.Epoch, e.Ingress.Correlation,
			e.Egress.Endpoint, e.Egress.Epoch, e.Egress.Correlation)
	}
	replyLine := func(bridge string, e reqreplybridge.ReplyEvent) {
		fmt.Printf("REPLY %s: %s/e%d/c%d b%d s%d -> %s/e%d/c%d b%d %s\n",
			bridge,
			e.Received.Addr.Endpoint, e.Received.Addr.Epoch, e.Received.Addr.Correlation,
			e.Received.Branch, e.Received.Seq,
			e.Delivered.Addr.Endpoint, e.Delivered.Addr.Epoch, e.Delivered.Addr.Correlation,
			e.Delivered.Branch, endSuffix(e))
	}

	// Separate per-bridge callbacks keep the hop order explicit in the output.
	b1.OnForwardLeg(reqreplybridge.LeftToRight, func(e reqreplybridge.ForwardEvent) { fwdLine("B1", e) })
	b2.OnForwardLeg(reqreplybridge.LeftToRight, func(e reqreplybridge.ForwardEvent) { fwdLine("B2", e) })
	b2.OnBridgedReplyLeg(reqreplybridge.LeftToRight, func(e reqreplybridge.ReplyEvent) { replyLine("B2", e) })
	b1.OnBridgedReplyLeg(reqreplybridge.LeftToRight, func(e reqreplybridge.ReplyEvent) { replyLine("B1", e) })

	svcEP, _ := c.Attach("svc")
	var svcReqs []reqreplybridge.RequestMsg
	svcEP.OnRequest(func(q reqreplybridge.RequestMsg) error {
		svcReqs = append(svcReqs, q)
		return nil
	})

	client1, _ := a.Attach("client-1")
	client2, _ := a.Attach("client-2")
	client1Rec := newRecorder()
	client2Rec := newRecorder()
	s1, _ := client1.Connect(client1Rec.handler)
	s2, _ := client2.Connect(client2Rec.handler)

	c1, _ := s1.AllocateCorrelation()
	c2, _ := s2.AllocateCorrelation()
	fmt.Printf("caller correlations: client-1=%d, client-2=%d\n", c1, c2)
	_ = a.SendRequest(s1, reqreplybridge.RequestMsg{Target: "b1-in", Correlation: c1})
	_ = a.SendRequest(s2, reqreplybridge.RequestMsg{Target: "b1-in", Correlation: c2})

	p := func(v uint64) *uint64 { return &v }
	reply := func(to reqreplybridge.ReplyAddr, branch, seq uint64, branches, tail *uint64, payload string) {
		_ = c.SendReply(reqreplybridge.ReplyMsg{
			Addr: to, Branch: branch, Seq: seq,
			DeclareBranches: branches, DeclareTail: tail, Payload: payload,
		})
	}

	// client-1: two branches; branch 1 finishes early, branch 0 tail-first.
	reply(svcReqs[0].ReplyTo, 1, 0, p(2), p(0), "c1-b1")
	// client-2: one branch.
	reply(svcReqs[1].ReplyTo, 0, 0, p(1), p(0), "c2-b0")
	reply(svcReqs[0].ReplyTo, 0, 2, nil, p(2), "c1-b0-seq2")
	reply(svcReqs[0].ReplyTo, 0, 0, nil, nil, "c1-b0-seq0")
	reply(svcReqs[0].ReplyTo, 0, 1, nil, nil, "c1-b0-seq1")

	for i, m := range client1Rec.snapshot() {
		fmt.Printf("DELIVER client-1[%d]: b%d s%d %q\n", i, m.Branch, m.Seq, m.Payload)
	}
	for i, m := range client2Rec.snapshot() {
		fmt.Printf("DELIVER client-2[%d]: b%d s%d %q\n", i, m.Branch, m.Seq, m.Payload)
	}
	// Output:
	// caller correlations: client-1=1, client-2=1
	// FWD B2 left->right: b1-out-m/e1/c1 -> b2-out-c/e1/c1
	// FWD B1 left->right: client-1/e1/c1 -> b1-out-m/e1/c1
	// FWD B2 left->right: b1-out-m/e1/c2 -> b2-out-c/e1/c2
	// FWD B1 left->right: client-2/e1/c1 -> b1-out-m/e1/c2
	// REPLY B1: b1-out-m/e1/c1 b1 s0 -> client-1/e1/c1 b1 [branch-end]
	// REPLY B2: b2-out-c/e1/c1 b1 s0 -> b1-out-m/e1/c1 b1 [branch-end]
	// REPLY B1: b1-out-m/e1/c2 b0 s0 -> client-2/e1/c1 b0 [branch-end] [parent-end branches=1]
	// REPLY B2: b2-out-c/e1/c2 b0 s0 -> b1-out-m/e1/c2 b0 [branch-end] [parent-end branches=1]
	// REPLY B1: b1-out-m/e1/c1 b0 s2 -> client-1/e1/c1 b0 error=branch fragments are not continuous from zero
	// REPLY B2: b2-out-c/e1/c1 b0 s2 -> b1-out-m/e1/c1 b0 error=branch fragments are not continuous from zero
	// REPLY B1: b1-out-m/e1/c1 b0 s0 -> client-1/e1/c1 b0 -
	// REPLY B2: b2-out-c/e1/c1 b0 s0 -> b1-out-m/e1/c1 b0 -
	// REPLY B1: b1-out-m/e1/c1 b0 s1 -> client-1/e1/c1 b0 [branch-end] [parent-end branches=2]
	// REPLY B2: b2-out-c/e1/c1 b0 s1 -> b1-out-m/e1/c1 b0 [branch-end] [parent-end branches=2]
	// DELIVER client-1[0]: b1 s0 "c1-b1"
	// DELIVER client-1[1]: b0 s2 "c1-b0-seq2"
	// DELIVER client-1[2]: b0 s0 "c1-b0-seq0"
	// DELIVER client-1[3]: b0 s1 "c1-b0-seq1"
	// DELIVER client-2[0]: b0 s0 "c2-b0"
}

func endSuffix(e reqreplybridge.ReplyEvent) string {
	var parts []string
	if e.BranchEnded {
		parts = append(parts, "[branch-end]")
	}
	if e.ParentEnded {
		parts = append(parts, fmt.Sprintf("[parent-end branches=%d]", e.TotalBranches))
	}
	if e.Error != nil {
		parts = append(parts, "error="+e.Error.Error())
	}
	if len(parts) == 0 {
		return "-"
	}
	return strings.Join(parts, " ")
}
