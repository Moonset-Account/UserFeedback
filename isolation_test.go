package barrier_test

import (
	"errors"
	"testing"

	"barrier"
)

func TestGeneratedReceiptIDAvoidsCustomIDCollision(t *testing.T) {
	m := barrier.NewManager()
	mustCreateRun(t, m, barrier.RunConfig{
		ID:           "r",
		Participants: []barrier.ParticipantConfig{{ID: "a"}},
		Groups:       []barrier.GroupConfig{{ID: "g", Threshold: 1, Members: []barrier.ParticipantID{"a"}}},
	})
	declareBarrier(t, m, "r", "b")

	custom := submitOK(t, m, barrier.Receipt{
		RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g",
		SubmitterID: "a", ReceiptID: "receipt-1", Proof: "custom",
	})
	auto1 := submitOK(t, m, barrier.Receipt{
		RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g",
		SubmitterID: "a", Proof: "auto-1",
	})
	auto2 := submitOK(t, m, barrier.Receipt{
		RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g",
		SubmitterID: "a", Proof: "auto-2",
	})
	if auto1.ReceiptID == custom.ReceiptID || auto2.ReceiptID == custom.ReceiptID || auto1.ReceiptID == auto2.ReceiptID {
		t.Fatalf("receipt ids must be unique: custom=%s auto1=%s auto2=%s", custom.ReceiptID, auto1.ReceiptID, auto2.ReceiptID)
	}

	retry, err := m.SubmitReceipt(barrier.Receipt{
		RunID: "r", BarrierID: "b", CheckpointID: "cp-b", GroupID: "g",
		SubmitterID: "a", ReceiptID: auto1.ReceiptID, Proof: "auto-1",
	})
	if err != nil || !retry.Duplicate || retry.Receipt.Order != auto1.Order {
		t.Fatalf("generated id should still identify original receipt: %+v %v", retry, err)
	}
}

func TestBarrierThresholdOverrideAndRunIsolation(t *testing.T) {
	m := barrier.NewManager()
	base := barrier.RunConfig{
		Participants: []barrier.ParticipantConfig{{ID: "a"}, {ID: "b"}},
		Groups:       []barrier.GroupConfig{{ID: "g", Threshold: 2, Members: []barrier.ParticipantID{"a", "b"}}},
	}
	base.ID = "r1"
	mustCreateRun(t, m, base)
	base.ID = "r2"
	mustCreateRun(t, m, base)

	_, err := m.DeclareBarrier("r1", barrier.BarrierConfig{
		ID: "b", CheckpointID: "cp",
		Groups: []barrier.GroupRequirement{{GroupID: "g", Threshold: 1}},
	})
	if err != nil {
		t.Fatal(err)
	}
	declareBarrier(t, m, "r2", "b")

	receipt := barrier.Receipt{BarrierID: "b", CheckpointID: "cp", GroupID: "g", SubmitterID: "a", Proof: "a"}
	receipt.RunID = "r1"
	submitOK(t, m, receipt)
	receipt.RunID = "r2"
	receipt.CheckpointID = "cp-b" // declareBarrier helper uses cp-<barrier id>
	submitOK(t, m, receipt)

	eval1, err := m.Evaluate("r1", "b")
	if err != nil {
		t.Fatal(err)
	}
	eval2, err := m.Evaluate("r2", "b")
	if err != nil {
		t.Fatal(err)
	}
	if !eval1.Ready {
		t.Fatalf("threshold override 1 should make r1 ready: %+v", eval1.Missing)
	}
	if eval2.Ready {
		t.Fatalf("r2 should retain default threshold 2 and remain pending")
	}

	if _, err := m.GetRun("missing"); !errors.Is(err, barrier.ErrRunNotFound) {
		t.Fatalf("missing run error = %v", err)
	}
}
