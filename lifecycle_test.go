package barrier_test

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"
	"testing"
	"time"

	"barrier"
)

func receiptSubjects(rec *barrier.ReleaseRecord) []barrier.ParticipantID {
	var ids []barrier.ParticipantID
	for _, r := range rec.SourceReceipts {
		ids = append(ids, r.SubmitterID)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	return ids
}

func TestHandoffRecomputesUnpublishedBarrier(t *testing.T) {
	m := barrier.NewManager()
	mustCreateRun(t, m, barrier.RunConfig{
		ID:           "r",
		Participants: []barrier.ParticipantConfig{{ID: "a"}, {ID: "b"}, {ID: "c"}},
		Groups:       []barrier.GroupConfig{{ID: "g", Threshold: 2, Members: []barrier.ParticipantID{"a", "b", "c"}}},
	})
	declareBarrier(t, m, "r", "b")
	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "a", Proof: "pa"})
	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "b", Proof: "pb"})

	ho, err := m.HandOver(barrier.HandOverRequest{RunID: "r", From: "a", To: "d"})
	if err != nil {
		t.Fatal(err)
	}
	if ho.TopologyVersion != 1 || len(ho.Groups) != 1 || ho.Groups[0] != "g" {
		t.Fatalf("bad handover record: %+v", ho)
	}

	info, err := m.GetRun("r")
	if err != nil {
		t.Fatal(err)
	}
	var members []barrier.ParticipantID
	for _, g := range info.Groups {
		if g.ID == "g" {
			members = g.Members
		}
	}
	if fmt.Sprint(members) != "[b c d]" {
		t.Fatalf("effective members after handoff = %v", members)
	}
	var aInfo *barrier.ParticipantInfo
	for i := range info.Participants {
		if info.Participants[i].ID == "a" {
			aInfo = &info.Participants[i]
		}
	}
	if aInfo == nil || aInfo.Status != barrier.ParticipantHandedOver || aInfo.Successor != "d" {
		t.Fatalf("bad outgoing participant info: %+v", aInfo)
	}

	eval, err := m.Evaluate("r", "b")
	if err != nil {
		t.Fatal(err)
	}
	g := findGroup(t, eval, "g")
	if g.ProofCount != 1 || g.CurrentlySatisfied {
		t.Fatalf("after handoff old a vote should not count: %+v", g)
	}
	if len(g.InvalidReceipts) != 1 || g.InvalidReceipts[0].Code != "submitter_inactive" {
		t.Fatalf("expected inactive old receipt explanation, got %+v", g.InvalidReceipts)
	}
	if g.Missing == nil || g.Missing.MissingCount != 1 {
		t.Fatalf("missing gap after handoff = %+v", g.Missing)
	}
	out, err := m.TryReleaseBarrier("r", "b")
	if err != nil {
		t.Fatal(err)
	}
	if out.Released || out.Evaluation == nil {
		t.Fatalf("unpublished barrier must not release after handoff: %+v", out)
	}

	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "d", Proof: "pd"})
	rec, err := m.ReleaseBarrier("r", "b")
	if err != nil {
		t.Fatal(err)
	}
	subjects := receiptSubjects(rec)
	if fmt.Sprint(subjects) != "[b d]" {
		t.Fatalf("pending barrier should release from b,d after recomputation, got %v", subjects)
	}
}

func TestPublishedGroupSurvivesHandoffAndAnchorsBarrier(t *testing.T) {
	m := barrier.NewManager()
	mustCreateRun(t, m, barrier.RunConfig{
		ID:           "r",
		Participants: []barrier.ParticipantConfig{{ID: "a"}, {ID: "b"}},
		Groups:       []barrier.GroupConfig{{ID: "g", Threshold: 2, Members: []barrier.ParticipantID{"a", "b"}}},
	})
	declareBarrier(t, m, "r", "b")
	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "a", Proof: "pa"})
	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "b", Proof: "pb"})

	pub, err := m.PublishGroup("r", "b", "g")
	if err != nil || !pub.Released || pub.Release == nil {
		t.Fatalf("PublishGroup = %+v, %v", pub, err)
	}
	if len(pub.Release.SourceReceipts) != 2 {
		t.Fatalf("published group provenance should retain a,b")
	}

	if _, err := m.HandOver(barrier.HandOverRequest{RunID: "r", From: "a", To: "c"}); err != nil {
		t.Fatal(err)
	}
	eval, err := m.Evaluate("r", "b")
	if err != nil {
		t.Fatal(err)
	}
	g := findGroup(t, eval, "g")
	if !g.Published || !g.Releasable || g.CurrentlySatisfied {
		t.Fatalf("published group should remain releasable with historical proof: %+v", g)
	}
	if g.PublishedRelease == nil || len(g.PublishedRelease.SourceReceipts) != 2 {
		t.Fatalf("published provenance missing: %+v", g.PublishedRelease)
	}
	if !eval.Ready {
		t.Fatalf("published group certificate should satisfy the one-group barrier")
	}
	rec, err := m.ReleaseBarrier("r", "b")
	if err != nil {
		t.Fatal(err)
	}
	if got := receiptSubjects(rec); fmt.Sprint(got) != "[a b]" {
		t.Fatalf("global release should anchor published a,b sources, got %v", got)
	}
}

func TestPublishedBarrierSurvivesRevocationAndHandoff(t *testing.T) {
	m := barrier.NewManager()
	mustCreateRun(t, m, barrier.RunConfig{
		ID:           "r",
		Participants: []barrier.ParticipantConfig{{ID: "a"}, {ID: "b"}},
		Groups:       []barrier.GroupConfig{{ID: "g", Threshold: 2, Members: []barrier.ParticipantID{"a", "b"}}},
	})
	declareBarrier(t, m, "r", "b")
	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "a", Proof: "pa"})
	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "b", Proof: "pb"})
	first, err := m.ReleaseBarrier("r", "b")
	if err != nil {
		t.Fatal(err)
	}

	if _, err := m.RevokeParticipant("r", "a", "key compromise"); err != nil {
		t.Fatal(err)
	}
	if _, err := m.HandOver(barrier.HandOverRequest{RunID: "r", From: "b", To: "b2"}); err != nil {
		t.Fatal(err)
	}
	got, err := m.GetRelease("r", "b")
	if err != nil || got == nil {
		t.Fatalf("GetRelease after changes: %+v %v", got, err)
	}
	if got.ReleaseID != first.ReleaseID || len(got.SourceReceipts) != 2 {
		t.Fatalf("published success changed: first=%+v got=%+v", first, got)
	}
	out, err := m.TryReleaseBarrier("r", "b")
	if err != nil || !out.Released || !out.AlreadyReleased {
		t.Fatalf("idempotent release after topology changes = %+v, %v", out, err)
	}
}

func TestProxyRevocationForcesReproofButDirectPrincipalCanRestore(t *testing.T) {
	m := barrier.NewManager()
	mustCreateRun(t, m, barrier.RunConfig{
		ID:           "r",
		Participants: []barrier.ParticipantConfig{{ID: "p1"}, {ID: "p2"}, {ID: "px"}},
		Groups:       []barrier.GroupConfig{{ID: "g", Threshold: 2, Members: []barrier.ParticipantID{"p1", "p2"}}},
	})
	grant, err := m.AuthorizeProxy(barrier.ProxyAuthorization{RunID: "r", ProxyID: "px", PrincipalID: "p1", Groups: GroupIDSlice("g")})
	if err != nil {
		t.Fatal(err)
	}
	declareBarrier(t, m, "r", "b")
	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "px", SubjectID: "p1", Proof: "proxy"})
	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "p2", Proof: "p2"})
	eval, _ := m.Evaluate("r", "b")
	if !eval.Ready {
		t.Fatalf("expected ready before proxy revocation: %+v", eval.Missing)
	}

	if _, err := m.RevokeProxy("r", grant.ID, "device lost"); err != nil {
		t.Fatal(err)
	}
	eval, _ = m.Evaluate("r", "b")
	g := findGroup(t, eval, "g")
	if g.ProofCount != 1 || len(g.InvalidReceipts) != 1 || g.InvalidReceipts[0].Code != "grant_revoked" {
		t.Fatalf("revoked proxy proof should be excluded: %+v", g)
	}

	// The principal's own direct receipt is a different currently valid path.
	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "p1", Proof: "direct-p1"})
	eval, _ = m.Evaluate("r", "b")
	if g2 := findGroup(t, eval, "g"); g2.ProofCount != 2 || !eval.Ready {
		t.Fatalf("direct principal should restore quorum: %+v eval ready=%v", g2, eval.Ready)
	}
}

func TestRevokedParticipantLosesPendingVoteAndNotReadyErrorCarriesEval(t *testing.T) {
	m := barrier.NewManager()
	mustCreateRun(t, m, barrier.RunConfig{
		ID:           "r",
		Participants: []barrier.ParticipantConfig{{ID: "a"}, {ID: "b"}, {ID: "c"}},
		Groups:       []barrier.GroupConfig{{ID: "g", Threshold: 2, Members: []barrier.ParticipantID{"a", "b", "c"}}},
	})
	declareBarrier(t, m, "r", "b")
	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "a", Proof: "a"})
	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "b", Proof: "b"})

	if _, err := m.RevokeParticipant("r", "a", "revoked"); err != nil {
		t.Fatal(err)
	}
	_, err := m.ReleaseBarrier("r", "b")
	var notReady *barrier.NotReadyError
	if !errors.As(err, &notReady) || !errors.Is(err, barrier.ErrBarrierNotReady) {
		t.Fatalf("expected NotReadyError, got %v", err)
	}
	if notReady.Evaluation == nil || len(notReady.Evaluation.Missing) != 1 {
		t.Fatalf("NotReadyError should carry evaluation: %+v", notReady)
	}

	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "c", Proof: "c"})
	if _, err := m.ReleaseBarrier("r", "b"); err != nil {
		t.Fatalf("c should replace revoked vote: %v", err)
	}
}

func TestReceiptIdempotencyConflictAndCheckpointAuthorizationBoundaries(t *testing.T) {
	m := barrier.NewManager()
	mustCreateRun(t, m, barrier.RunConfig{
		ID:           "r",
		Participants: []barrier.ParticipantConfig{{ID: "a"}, {ID: "outsider"}},
		Groups:       []barrier.GroupConfig{{ID: "g", Threshold: 1, Members: []barrier.ParticipantID{"a"}}},
	})
	declareBarrier(t, m, "r", "b")

	first, err := m.SubmitReceipt(barrier.Receipt{
		RunID: "r", BarrierID: "b", ReceiptID: "idem-1", CheckpointID: "cp-b",
		GroupID: "g", SubmitterID: "a", Proof: "same",
	})
	if err != nil || first.Duplicate {
		t.Fatalf("first submit = %+v %v", first, err)
	}
	second, err := m.SubmitReceipt(barrier.Receipt{
		RunID: "r", BarrierID: "b", ReceiptID: "idem-1", CheckpointID: "cp-b",
		GroupID: "g", SubmitterID: "a", Proof: "same",
	})
	if err != nil || !second.Duplicate || second.Receipt.Order != first.Receipt.Order {
		t.Fatalf("idempotent retry = %+v %v", second, err)
	}
	_, err = m.SubmitReceipt(barrier.Receipt{
		RunID: "r", BarrierID: "b", ReceiptID: "idem-1", CheckpointID: "cp-b",
		GroupID: "g", SubmitterID: "a", Proof: "changed",
	})
	if !errors.Is(err, barrier.ErrConflict) {
		t.Fatalf("payload reuse expected conflict, got %v", err)
	}

	_, err = m.SubmitReceipt(barrier.Receipt{
		RunID: "r", BarrierID: "b", CheckpointID: "wrong", GroupID: "g", SubmitterID: "a", Proof: "x",
	})
	if !errors.Is(err, barrier.ErrCheckpointMismatch) {
		t.Fatalf("checkpoint mismatch expected, got %v", err)
	}
	_, err = m.SubmitReceipt(barrier.Receipt{
		RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "outsider", Proof: "x",
	})
	if !errors.Is(err, barrier.ErrNotMember) {
		t.Fatalf("outsider direct receipt expected not member, got %v", err)
	}
	_, err = m.SubmitReceipt(barrier.Receipt{
		RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g",
		SubmitterID: "outsider", SubjectID: "a", Proof: "x",
	})
	if !errors.Is(err, barrier.ErrUnauthorized) {
		t.Fatalf("unauthorized proxy expected, got %v", err)
	}
	_, err = m.SubmitReceipt(barrier.Receipt{
		RunID: "missing-run", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "a", Proof: "x",
	})
	if !errors.Is(err, barrier.ErrRunNotFound) {
		t.Fatalf("missing run expected, got %v", err)
	}
}

func TestInvalidConfigurationBoundaries(t *testing.T) {
	tests := []struct {
		name string
		cfg  barrier.RunConfig
	}{
		{"missing run id", barrier.RunConfig{Groups: []barrier.GroupConfig{{ID: "g", Threshold: 1}}}},
		{"no groups", barrier.RunConfig{ID: "r"}},
		{"duplicate participant", barrier.RunConfig{
			ID: "r", Participants: []barrier.ParticipantConfig{{ID: "a"}, {ID: "a"}},
			Groups: []barrier.GroupConfig{{ID: "g", Threshold: 1, Members: []barrier.ParticipantID{"a"}}},
		}},
		{"unknown participant member", barrier.RunConfig{
			ID: "r", Groups: []barrier.GroupConfig{{ID: "g", Threshold: 1, Members: []barrier.ParticipantID{"ghost"}}},
		}},
		{"zero threshold", barrier.RunConfig{
			ID: "r", Participants: []barrier.ParticipantConfig{{ID: "a"}},
			Groups: []barrier.GroupConfig{{ID: "g", Threshold: 0, Members: []barrier.ParticipantID{"a"}}},
		}},
		{"threshold above members", barrier.RunConfig{
			ID: "r", Participants: []barrier.ParticipantConfig{{ID: "a"}},
			Groups: []barrier.GroupConfig{{ID: "g", Threshold: 2, Members: []barrier.ParticipantID{"a"}}},
		}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := barrier.NewManager().CreateRun(tc.cfg)
			if !errors.Is(err, barrier.ErrInvalidConfig) {
				t.Fatalf("expected ErrInvalidConfig, got %v", err)
			}
		})
	}
}

func TestConcurrentWaitersAndDuplicateSubmissionsPublishOnce(t *testing.T) {
	m := barrier.NewManager()
	mustCreateRun(t, m, barrier.RunConfig{
		ID:           "r",
		Participants: []barrier.ParticipantConfig{{ID: "p1"}, {ID: "p2"}, {ID: "p3"}},
		Groups:       []barrier.GroupConfig{{ID: "g", Threshold: 3, Members: []barrier.ParticipantID{"p1", "p2", "p3"}}},
	})
	declareBarrier(t, m, "r", "b")

	const waiters = 16
	const submitters = 45
	var wg sync.WaitGroup
	records := make(chan *barrier.ReleaseRecord, waiters)
	errs := make(chan error, waiters+submitters)

	for i := 0; i < waiters; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			rec, err := m.WaitForRelease(ctx, "r", "b")
			if err != nil {
				errs <- err
				return
			}
			records <- rec
		}()
	}

	ids := []barrier.ParticipantID{"p1", "p2", "p3"}
	for i := 0; i < submitters; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			pid := ids[i%len(ids)]
			_, err := m.SubmitReceipt(barrier.Receipt{
				RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g",
				SubmitterID: pid, ReceiptID: barrier.ReceiptID(fmt.Sprintf("receipt-%02d", i)),
				Proof: fmt.Sprintf("proof-%d", i),
			})
			if err != nil && !errors.Is(err, barrier.ErrBarrierReleased) {
				errs <- err
			}
		}(i)
	}

	wg.Wait()
	close(records)
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent operation error: %v", err)
	}
	var releaseID barrier.ReleaseID
	count := 0
	for rec := range records {
		count++
		if rec == nil {
			t.Fatal("nil release record")
		}
		if releaseID == "" {
			releaseID = rec.ReleaseID
		} else if rec.ReleaseID != releaseID {
			t.Fatalf("waiters observed different releases: %s vs %s", releaseID, rec.ReleaseID)
		}
	}
	if count != waiters {
		t.Fatalf("expected %d waiter records, got %d", waiters, count)
	}
}

func TestReleaseHandoffInterleavingsAreLinearizable(t *testing.T) {
	for i := 0; i < 60; i++ {
		runID := barrier.RunID(fmt.Sprintf("race-%d", i))
		m := barrier.NewManager()
		mustCreateRun(t, m, barrier.RunConfig{
			ID:           runID,
			Participants: []barrier.ParticipantConfig{{ID: "a"}, {ID: "b"}},
			Groups:       []barrier.GroupConfig{{ID: "g", Threshold: 2, Members: []barrier.ParticipantID{"a", "b"}}},
		})
		bid := barrier.BarrierID("b")
		declareBarrier(t, m, runID, bid)
		cp := "cp-b"
		submitOK(t, m, barrier.Receipt{RunID: runID, BarrierID: bid, CheckpointID: cp, GroupID: "g", SubmitterID: "a", Proof: "a"})
		submitOK(t, m, barrier.Receipt{RunID: runID, BarrierID: bid, CheckpointID: cp, GroupID: "g", SubmitterID: "b", Proof: "b"})

		var wg sync.WaitGroup
		var relOut *barrier.ReleaseOutcome
		var relErr, hoErr error
		wg.Add(2)
		go func() {
			defer wg.Done()
			relOut, relErr = m.TryReleaseBarrier(runID, bid)
		}()
		go func() {
			defer wg.Done()
			_, hoErr = m.HandOver(barrier.HandOverRequest{RunID: runID, From: "a", To: "c"})
		}()
		wg.Wait()
		if relErr != nil || hoErr != nil {
			t.Fatalf("iteration %d errors: release=%v handoff=%v", i, relErr, hoErr)
		}

		if relOut.Released {
			rec, err := m.GetRelease(runID, bid)
			if err != nil || rec == nil {
				t.Fatalf("iteration %d released but GetRelease=%v err=%v", i, rec, err)
			}
			if got := receiptSubjects(rec); fmt.Sprint(got) != "[a b]" {
				t.Fatalf("iteration %d published provenance = %v", i, got)
			}
			continue
		}

		if relOut.Evaluation == nil {
			t.Fatalf("iteration %d unreleased outcome missing evaluation", i)
		}
		g := findGroup(t, relOut.Evaluation, "g")
		if g.ProofCount != 1 {
			t.Fatalf("iteration %d expected only b after handoff, count=%d", i, g.ProofCount)
		}
		submitOK(t, m, barrier.Receipt{RunID: runID, BarrierID: bid, CheckpointID: cp, GroupID: "g", SubmitterID: "c", Proof: "c"})
		rec, err := m.ReleaseBarrier(runID, bid)
		if err != nil {
			t.Fatalf("iteration %d release after c reproof: %v", i, err)
		}
		if got := receiptSubjects(rec); fmt.Sprint(got) != "[b c]" {
			t.Fatalf("iteration %d recomputed provenance = %v", i, got)
		}
	}
}

func TestCanceledWaitDoesNotConsumeLaterNotification(t *testing.T) {
	m := barrier.NewManager()
	mustCreateRun(t, m, barrier.RunConfig{
		ID:           "r",
		Participants: []barrier.ParticipantConfig{{ID: "a"}},
		Groups:       []barrier.GroupConfig{{ID: "g", Threshold: 1, Members: []barrier.ParticipantID{"a"}}},
	})
	declareBarrier(t, m, "r", "b")

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := m.WaitForRelease(ctx, "r", "b"); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled wait = %v", err)
	}

	type waitResult struct {
		rec *barrier.ReleaseRecord
		err error
	}
	done := make(chan waitResult, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		rec, err := m.WaitForRelease(ctx, "r", "b")
		done <- waitResult{rec, err}
	}()
	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "a", Proof: "a"})
	select {
	case res := <-done:
		if res.err != nil || res.rec == nil {
			t.Fatalf("later wait result = %+v", res)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("later WaitForRelease was not notified")
	}
}

func TestInjectedClockIsUsedForEvaluationAndRelease(t *testing.T) {
	fixed := time.Date(2025, time.January, 2, 3, 4, 5, 0, time.UTC)
	m := barrier.NewManager(barrier.WithClock(func() time.Time { return fixed }))
	mustCreateRun(t, m, barrier.RunConfig{
		ID:           "r",
		Participants: []barrier.ParticipantConfig{{ID: "a"}},
		Groups:       []barrier.GroupConfig{{ID: "g", Threshold: 1, Members: []barrier.ParticipantID{"a"}}},
	})
	eval := declareBarrier(t, m, "r", "b")
	if !eval.EvaluatedAt.Equal(fixed) {
		t.Fatalf("EvaluatedAt = %v, want %v", eval.EvaluatedAt, fixed)
	}
	submitOK(t, m, barrier.Receipt{RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g", SubmitterID: "a", Proof: "a"})
	rec, err := m.ReleaseBarrier("r", "b")
	if err != nil {
		t.Fatal(err)
	}
	if !rec.PublishedAt.Equal(fixed) || !rec.Groups[0].PublishedAt.Equal(fixed) {
		t.Fatalf("release times = barrier %v group %v, want %v", rec.PublishedAt, rec.Groups[0].PublishedAt, fixed)
	}
}
