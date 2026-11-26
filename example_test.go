package barrier_test

import (
	"fmt"

	"barrier"
)

func ExampleManager() {
	m := barrier.NewManager()
	_, err := m.CreateRun(barrier.RunConfig{
		ID: "run-42",
		Participants: []barrier.ParticipantConfig{
			{ID: "a"}, {ID: "b"}, {ID: "d"},
		},
		Groups: []barrier.GroupConfig{
			{ID: "signers", Threshold: 2, Members: []barrier.ParticipantID{"a", "b"}},
			{ID: "witnesses", Threshold: 1, Members: []barrier.ParticipantID{"d"}},
		},
	})
	if err != nil {
		panic(err)
	}

	initial, err := m.DeclareBarrier("run-42", barrier.BarrierConfig{
		ID:           "cp-7",
		CheckpointID: "checkpoint-7",
	})
	if err != nil {
		panic(err)
	}
	fmt.Printf("initial ready=%v missingGroups=%d\n", initial.Ready, len(initial.Missing))

	receipts := []barrier.Receipt{
		{RunID: "run-42", BarrierID: "cp-7", CheckpointID: "checkpoint-7", GroupID: "signers", SubmitterID: "a", Proof: "synthetic-a"},
		{RunID: "run-42", BarrierID: "cp-7", CheckpointID: "checkpoint-7", GroupID: "signers", SubmitterID: "b", Proof: "synthetic-b"},
		{RunID: "run-42", BarrierID: "cp-7", CheckpointID: "checkpoint-7", GroupID: "witnesses", SubmitterID: "d", Proof: "synthetic-d"},
	}
	for _, r := range receipts {
		if _, err := m.SubmitReceipt(r); err != nil {
			panic(err)
		}
	}

	out, err := m.TryReleaseBarrier("run-42", "cp-7")
	if err != nil {
		panic(err)
	}
	fmt.Printf("release ready=%v sources=%d\n", out.Released, len(out.Release.SourceReceipts))

	// Output:
	// initial ready=false missingGroups=2
	// release ready=true sources=3
}
