package barrier_test

import (
	"testing"

	"barrier"
)

func mustCreateRun(t *testing.T, m *barrier.Manager, cfg barrier.RunConfig) *barrier.RunInfo {
	t.Helper()
	info, err := m.CreateRun(cfg)
	if err != nil {
		t.Fatalf("CreateRun(%q): %v", cfg.ID, err)
	}
	return info
}

func declareBarrier(t *testing.T, m *barrier.Manager, run barrier.RunID, id barrier.BarrierID) *barrier.Evaluation {
	t.Helper()
	eval, err := m.DeclareBarrier(run, barrier.BarrierConfig{ID: id, CheckpointID: "cp-" + string(id)})
	if err != nil {
		t.Fatalf("DeclareBarrier(%s): %v", id, err)
	}
	return eval
}

func submitOK(t *testing.T, m *barrier.Manager, r barrier.Receipt) *barrier.ReceiptRecord {
	t.Helper()
	out, err := m.SubmitReceipt(r)
	if err != nil {
		t.Fatalf("SubmitReceipt(%s/%s): %v", r.SubmitterID, r.SubjectID, err)
	}
	if out.Receipt == nil {
		t.Fatal("SubmitReceipt returned nil receipt")
	}
	return out.Receipt
}

func findGroup(t *testing.T, eval *barrier.Evaluation, gid barrier.GroupID) barrier.GroupStatus {
	t.Helper()
	if eval == nil {
		t.Fatal("nil evaluation")
	}
	for _, g := range eval.Groups {
		if g.GroupID == gid {
			return g
		}
	}
	t.Fatalf("group %s not found in evaluation", gid)
	return barrier.GroupStatus{}
}

func findMissing(eval *barrier.Evaluation, gid barrier.GroupID) *barrier.MissingProof {
	for i := range eval.Missing {
		if eval.Missing[i].GroupID == gid {
			return &eval.Missing[i]
		}
	}
	return nil
}

func standardRunConfig() barrier.RunConfig {
	return barrier.RunConfig{
		ID: "run-1",
		Participants: []barrier.ParticipantConfig{
			{ID: "a"}, {ID: "b"}, {ID: "c"}, {ID: "d"}, {ID: "e"},
		},
		Groups: []barrier.GroupConfig{
			{ID: "control", Threshold: 2, Members: []barrier.ParticipantID{"a", "b", "c"}},
			{ID: "data", Threshold: 1, Members: []barrier.ParticipantID{"d", "e"}},
		},
	}
}

func TestCrossGroupReceiptsReturnReleasableGroupsAndMissingProofs(t *testing.T) {
	m := barrier.NewManager()
	mustCreateRun(t, m, standardRunConfig())

	eval, err := m.DeclareBarrier("run-1", barrier.BarrierConfig{ID: "cp1", CheckpointID: "checkpoint-1"})
	if err != nil {
		t.Fatal(err)
	}
	if eval.Ready || len(eval.ReleasableGroups) != 0 || len(eval.Missing) != 2 {
		t.Fatalf("initial eval = ready=%v releasable=%v missing=%d", eval.Ready, eval.ReleasableGroups, len(eval.Missing))
	}
	miss := findMissing(eval, "control")
	if miss == nil || miss.MissingCount != 2 || miss.Impossible {
		t.Fatalf("control missing = %+v", miss)
	}
	if len(miss.Requests) != 3 {
		t.Fatalf("expected three proof requests, got %d", len(miss.Requests))
	}

	receiptA := submitOK(t, m, barrier.Receipt{
		RunID: "run-1", BarrierID: "cp1", CheckpointID: "checkpoint-1",
		GroupID: "control", SubmitterID: "a", Proof: "proof-a",
	})
	if receiptA.SubjectID != "a" || receiptA.Order == 0 {
		t.Fatalf("bad normalized receipt: %+v", receiptA)
	}

	eval, err = m.Evaluate("run-1", "cp1")
	if err != nil {
		t.Fatal(err)
	}
	control := findGroup(t, eval, "control")
	if control.ProofCount != 1 || control.Releasable {
		t.Fatalf("after a: count=%d releasable=%v", control.ProofCount, control.Releasable)
	}
	if miss := findMissing(eval, "control"); miss == nil || miss.MissingCount != 1 {
		t.Fatalf("control gap after a = %+v", miss)
	}

	submitOK(t, m, barrier.Receipt{
		RunID: "run-1", BarrierID: "cp1", CheckpointID: "checkpoint-1",
		GroupID: "control", SubmitterID: "b", ReceiptID: "receipt-b", Proof: "proof-b",
	})
	eval, err = m.Evaluate("run-1", "cp1")
	if err != nil {
		t.Fatal(err)
	}
	control = findGroup(t, eval, "control")
	if !control.CurrentlySatisfied || !control.Releasable {
		t.Fatalf("control should be releasable: %+v", control)
	}
	if len(eval.ReleasableGroups) != 1 || eval.ReleasableGroups[0] != "control" {
		t.Fatalf("releasable groups = %v", eval.ReleasableGroups)
	}
	if eval.Ready || len(eval.Missing) != 1 || eval.Missing[0].GroupID != "data" {
		t.Fatalf("only data should remain missing: ready=%v missing=%+v", eval.Ready, eval.Missing)
	}

	submitOK(t, m, barrier.Receipt{
		RunID: "run-1", BarrierID: "cp1", CheckpointID: "checkpoint-1",
		GroupID: "data", SubmitterID: "d", Proof: "proof-d",
	})
	out, err := m.TryReleaseBarrier("run-1", "cp1")
	if err != nil {
		t.Fatal(err)
	}
	if !out.Released || out.Release == nil {
		t.Fatalf("expected release, got %+v eval=%+v", out, out.Evaluation)
	}
	rec := out.Release
	if rec.CheckpointID != "checkpoint-1" || len(rec.Groups) != 2 || len(rec.SourceReceipts) != 3 {
		t.Fatalf("bad release record: %+v", rec)
	}
	if rec.Groups[0].GroupID != "control" || len(rec.Groups[0].SourceReceipts) != 2 {
		t.Fatalf("control provenance wrong: %+v", rec.Groups[0])
	}

	got, err := m.GetRelease("run-1", "cp1")
	if err != nil || got == nil || got.ReleaseID != rec.ReleaseID {
		t.Fatalf("GetRelease = %+v, %v", got, err)
	}
	poll, err := m.PollRun("run-1")
	if err != nil || len(poll.Barriers) != 1 || !poll.Barriers[0].Released {
		t.Fatalf("PollRun = %+v, %v", poll, err)
	}
}

func TestProxyAndPrincipalReceiptsCountOnce(t *testing.T) {
	m := barrier.NewManager()
	mustCreateRun(t, m, barrier.RunConfig{
		ID: "r",
		Participants: []barrier.ParticipantConfig{
			{ID: "p1"}, {ID: "p2"}, {ID: "proxy-1"},
		},
		Groups: []barrier.GroupConfig{{ID: "g", Threshold: 2, Members: []barrier.ParticipantID{"p1", "p2"}}},
	})
	grant, err := m.AuthorizeProxy(barrier.ProxyAuthorization{
		RunID: "r", ProxyID: "proxy-1", PrincipalID: "p1", Groups: GroupIDSlice("g"),
	})
	if err != nil {
		t.Fatal(err)
	}
	if !grant.Active || len(grant.Groups) != 1 {
		t.Fatalf("bad grant: %+v", grant)
	}
	declareBarrier(t, m, "r", "b")

	proxyRec := submitOK(t, m, barrier.Receipt{
		RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g",
		SubmitterID: "proxy-1", SubjectID: "p1", Proof: "proxy-proof",
	})
	if proxyRec.GrantID != grant.ID {
		t.Fatalf("resolved grant = %q, want %q", proxyRec.GrantID, grant.ID)
	}
	submitOK(t, m, barrier.Receipt{
		RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g",
		SubmitterID: "p1", Proof: "principal-proof",
	})

	eval, err := m.Evaluate("r", "b")
	if err != nil {
		t.Fatal(err)
	}
	g := findGroup(t, eval, "g")
	if g.ProofCount != 1 {
		t.Fatalf("proxy+principal counted %d distinct proofs", g.ProofCount)
	}
	if len(g.Proofs) != 1 || len(g.Proofs[0].Receipts) != 2 {
		t.Fatalf("expected one contribution with two source receipts: %+v", g.Proofs)
	}
	miss := g.Missing
	if miss == nil || miss.MissingCount != 1 {
		t.Fatalf("expected one more distinct proof, got %+v", miss)
	}
	if len(miss.Requests) != 1 || miss.Requests[0].LogicalID != "p2" {
		t.Fatalf("expected p2 proof request, got %+v", miss.Requests)
	}

	submitOK(t, m, barrier.Receipt{
		RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g",
		SubmitterID: "p2", Proof: "p2-proof",
	})
	rec, err := m.ReleaseBarrier("r", "b")
	if err != nil {
		t.Fatalf("ReleaseBarrier: %v", err)
	}
	if len(rec.SourceReceipts) != 3 {
		t.Fatalf("all three source receipts should be preserved, got %d", len(rec.SourceReceipts))
	}
	if len(rec.Groups[0].Proofs) != 2 {
		t.Fatalf("threshold should contain two logical contributions")
	}
}

// GroupIDSlice is a tiny test helper because composite literals with the
// exported slice type are verbose in table tests.
func GroupIDSlice(ids ...string) []barrier.GroupID {
	out := make([]barrier.GroupID, len(ids))
	for i, id := range ids {
		out[i] = barrier.GroupID(id)
	}
	return out
}

func TestMirrorReceiptsCollapseAndMirrorRevocationRecounts(t *testing.T) {
	m := barrier.NewManager()
	mustCreateRun(t, m, barrier.RunConfig{
		ID: "r",
		Participants: []barrier.ParticipantConfig{
			{ID: "alice"}, {ID: "alice-mirror"}, {ID: "bob"},
		},
		Groups: []barrier.GroupConfig{{ID: "g", Threshold: 2, Members: []barrier.ParticipantID{"alice", "alice-mirror", "bob"}}},
	})
	rel, err := m.DeclareMirror("r", "alice", "alice-mirror")
	if err != nil {
		t.Fatal(err)
	}
	declareBarrier(t, m, "r", "b")

	for _, submitter := range []barrier.ParticipantID{"alice", "alice-mirror"} {
		submitOK(t, m, barrier.Receipt{
			RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g",
			SubmitterID: submitter, Proof: "proof-" + string(submitter),
		})
	}
	eval, _ := m.Evaluate("r", "b")
	g := findGroup(t, eval, "g")
	if g.ProofCount != 1 || len(g.Proofs) != 1 || g.Proofs[0].LogicalID != "alice" {
		t.Fatalf("mirror receipts should collapse to alice: count=%d proofs=%+v", g.ProofCount, g.Proofs)
	}
	if g.Missing == nil || g.Missing.Impossible || g.Missing.MissingCount != 1 {
		t.Fatalf("with bob as second identity, expected one reachable missing vote: %+v", g.Missing)
	}

	if _, err := m.RevokeMirror("r", rel.ID, "split-brain resolved"); err != nil {
		t.Fatal(err)
	}
	eval, _ = m.Evaluate("r", "b")
	g = findGroup(t, eval, "g")
	if g.ProofCount != 2 || !g.CurrentlySatisfied {
		t.Fatalf("after mirror revocation both receipts should count: %+v", g)
	}
}

func TestMirrorMakesThresholdImpossibleUntilRelationRevoked(t *testing.T) {
	m := barrier.NewManager()
	mustCreateRun(t, m, barrier.RunConfig{
		ID:           "r",
		Participants: []barrier.ParticipantConfig{{ID: "p1"}, {ID: "p2"}},
		Groups:       []barrier.GroupConfig{{ID: "g", Threshold: 2, Members: []barrier.ParticipantID{"p1", "p2"}}},
	})
	rel, err := m.DeclareMirror("r", "p1", "p2")
	if err != nil {
		t.Fatal(err)
	}
	declareBarrier(t, m, "r", "b")
	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "p1", Proof: "x"})
	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "p2", Proof: "y"})

	eval, _ := m.Evaluate("r", "b")
	miss := findMissing(eval, "g")
	if miss == nil || !miss.Impossible || miss.MissingCount != 1 {
		t.Fatalf("expected impossible one-logical-identity gap, got %+v", miss)
	}

	if _, err := m.RevokeMirror("r", rel.ID, "not actually mirror"); err != nil {
		t.Fatal(err)
	}
	rec, err := m.ReleaseBarrier("r", "b")
	if err != nil {
		t.Fatalf("release after mirror revoke: %v", err)
	}
	if len(rec.SourceReceipts) != 2 {
		t.Fatalf("expected two sources, got %d", len(rec.SourceReceipts))
	}
}
