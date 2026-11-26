package slotcheck

import (
	"encoding/json"
	"os"
	"testing"
)

func TestSyntheticCompliantLedgerFile(t *testing.T) {
	data, err := os.ReadFile("testdata/compliant_ledger.json")
	if err != nil {
		t.Fatal(err)
	}
	var in Input
	if err := json.Unmarshal(data, &in); err != nil {
		t.Fatal(err)
	}
	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Summary.Slots != 12 {
		t.Fatalf("expected 2 generations x 3 slots x 2 directions = 12, got %d", rep.Summary.Slots)
	}
	if rep.Summary.ProvenCompliant != 12 ||
		rep.Summary.ExceededVisibilityTier != 0 ||
		rep.Summary.InsufficientEvidence != 0 {
		t.Fatalf("synthetic compliant ledger produced unexpected summary: %+v", rep.Summary)
	}
	if rep.Summary.LengthFindings != 0 || rep.Summary.ConfirmationGaps != 0 || rep.Summary.EvidenceGaps != 0 {
		t.Fatalf("unexpected findings: %+v", rep.Summary)
	}
}
