package relaygate_test

import (
	"fmt"
	"sync"
	"testing"

	"relaygate"
)

func TestInvalidEvidenceIsRejectedBeforeStorage(t *testing.T) {
	gate := relaygate.New()

	invalidDirection := testProof(1, relaygate.DirectionGrant{
		Direction: "sideways",
		Window:    testWindow(0, 100),
	})
	if _, err := gate.SubmitKeepalive(invalidDirection); err == nil {
		t.Fatal("proof with invalid direction was accepted")
	}

	invalidWindow := testProof(2, relaygate.DirectionGrant{
		Direction: relaygate.Uplink,
		Window:    relaygate.LeaseWindow{NotBefore: atSecond(10), ExpiresAt: atSecond(10)},
	})
	if _, err := gate.SubmitKeepalive(invalidWindow); err == nil {
		t.Fatal("proof with empty lease window was accepted")
	}

	invalidAck := relaygate.WakeConfirmation{
		Session:           testSession,
		KeepaliveSequence: 3,
		Grants:            []relaygate.DirectionGrant{{Direction: relaygate.Downlink}},
	}
	if _, err := gate.SubmitWakeConfirmation(invalidAck); err == nil {
		t.Fatal("confirmation with zero window was accepted")
	}

	if verdicts := gate.Verdicts(); len(verdicts) != 0 {
		t.Fatalf("invalid evidence caused record state: %+v", verdicts)
	}
}

func TestConfirmationDirectionSelectorUsesExactProofWindow(t *testing.T) {
	gate := relaygate.New()
	proof := testProof(7, testGrant(relaygate.Uplink, 10, 90))
	submitProof(t, gate, proof)

	// The confirmation intentionally omits windows. The effective window must
	// come from the explicitly listed proof grant.
	ack := relaygate.WakeConfirmation{
		Session:           testSession,
		KeepaliveSequence: 7,
		Directions:        []relaygate.Direction{relaygate.Uplink},
	}
	submitConfirmation(t, gate, ack)

	verdict := gate.Admit(testRecord("direction-selector", relaygate.Uplink, 50))[0]
	requireStatus(t, verdict, relaygate.StatusAdmitted)
	if verdict.Attribution == nil || verdict.Attribution.Window == nil {
		t.Fatalf("attribution/window missing: %+v", verdict.Attribution)
	}
	if !verdict.Attribution.Window.NotBefore.Equal(atSecond(10)) ||
		!verdict.Attribution.Window.ExpiresAt.Equal(atSecond(90)) {
		t.Fatalf("window = %+v, want proof window [10,90)", verdict.Attribution.Window)
	}
}

func TestAdjudicatorSupportsConcurrentUniqueSessions(t *testing.T) {
	gate := relaygate.New()
	const n = 24
	var wg sync.WaitGroup

	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()

			sequence := uint64(1000 + i)
			proof := relaygate.KeepaliveProof{
				Session: relaygate.SessionKey{
					ConnectionGeneration: uint64(i + 1),
					PeerStartNumber:      uint64(i + 100),
				},
				Sequence: sequence,
				Grants:   []relaygate.DirectionGrant{testGrant(relaygate.Uplink, 0, 100)},
			}
			if _, err := gate.SubmitKeepalive(proof); err != nil {
				t.Errorf("SubmitKeepalive(%d): %v", i, err)
				return
			}
			ack := relaygate.ConfirmDirections(proof, relaygate.Uplink)
			if _, err := gate.SubmitWakeConfirmation(ack); err != nil {
				t.Errorf("SubmitWakeConfirmation(%d): %v", i, err)
				return
			}

			record := relaygate.Record{
				ID:        fmt.Sprintf("concurrent-%02d", i),
				Session:   proof.Session,
				Direction: relaygate.Uplink,
				TrustedAt: atSecond(50),
			}
			verdicts := gate.Admit(record)
			if len(verdicts) != 1 || verdicts[0].Status != relaygate.StatusAdmitted {
				t.Errorf("Admit(%d) = %+v, want one admitted verdict", i, verdicts)
			}
		}(i)
	}

	wg.Wait()

	all := gate.Verdicts()
	if len(all) != n {
		t.Fatalf("stored verdict count = %d, want %d", len(all), n)
	}
}
