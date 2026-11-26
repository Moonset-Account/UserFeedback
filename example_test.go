package relaygate_test

import (
	"fmt"
	"time"

	"relaygate"
)

func ExampleAdjudicator_delayedConfirmation() {
	base := time.Date(2026, 11, 27, 0, 0, 0, 0, time.UTC)
	session := relaygate.SessionKey{ConnectionGeneration: 7, PeerStartNumber: 3}
	window := relaygate.LeaseWindow{
		NotBefore: base,
		ExpiresAt: base.Add(10 * time.Minute),
	}
	proof := relaygate.KeepaliveProof{
		Session:  session,
		Sequence: 4,
		Grants: []relaygate.DirectionGrant{{
			Direction: relaygate.Uplink,
			Window:    window,
		}},
	}

	gate := relaygate.New()
	_, _ = gate.SubmitKeepalive(proof)

	record := relaygate.Record{
		ID:        "first-uplink-record",
		Session:   session,
		Direction: relaygate.Uplink,
		TrustedAt: base.Add(2 * time.Minute),
	}
	before := gate.Admit(record)[0]
	fmt.Printf("before: %s", before.Status)
	for _, missing := range before.MissingEvidence {
		if missing.Code == relaygate.NeedWakeConfirmation {
			fmt.Printf(" missing=%s\n", missing.Code)
		}
	}

	_, _ = gate.SubmitWakeConfirmation(relaygate.ConfirmDirections(proof, relaygate.Uplink))
	after, _ := gate.Verdict(record.ID)
	seq := uint64(0)
	if after.Attribution != nil && after.Attribution.KeepaliveSequence != nil {
		seq = *after.Attribution.KeepaliveSequence
	}
	fmt.Printf("after: %s deliverable=%v seq=%d\n", after.Status, after.Deliverable, seq)

	// Output:
	// before: pending missing=missing_wake_confirmation
	// after: admitted deliverable=true seq=4
}
