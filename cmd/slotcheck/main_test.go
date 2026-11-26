package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"slotcheck"
)

func TestCLISyntheticFileSuccess(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := run(
		[]string{"-in", "../../testdata/compliant_ledger.json", "-fail-on-violation", "-fail-on-insufficient-evidence"},
		strings.NewReader(""),
		&stdout,
		&stderr,
	)
	if code != 0 {
		t.Fatalf("expected exit 0, got %d; stderr=%s stdout=%s", code, stderr.String(), stdout.String())
	}
	var rep slotcheck.Report
	if err := json.Unmarshal(stdout.Bytes(), &rep); err != nil {
		t.Fatalf("stdout was not JSON report: %v\n%s", err, stdout.String())
	}
	if rep.Summary.ProvenCompliant != 12 {
		t.Fatalf("expected 12 compliant slots through CLI, got %+v", rep.Summary)
	}
}

func TestCLIViolationExitCode(t *testing.T) {
	input := `{
		"template": {"slot_count": 1},
		"policies": [{"direction":"out","allowed_lengths":[128]}],
		"records": [{
			"generation": 1, "slot": 0, "direction": "out",
			"observed_visible_bytes": 999, "real_bytes": 999
		}]
	}`
	var stdout, stderr bytes.Buffer
	code := run([]string{"-fail-on-violation"}, strings.NewReader(input), &stdout, &stderr)
	if code != 2 {
		t.Fatalf("expected violation exit 2, got %d; stderr=%s", code, stderr.String())
	}
	var rep slotcheck.Report
	if err := json.Unmarshal(stdout.Bytes(), &rep); err != nil {
		t.Fatal(err)
	}
	if rep.Summary.ExceededVisibilityTier != 1 {
		t.Fatalf("expected one violation through CLI, got %+v", rep.Summary)
	}
}

func TestCLIInsufficientEvidenceExitCode(t *testing.T) {
	input := `{
		"generations": [{"generation": 1}],
		"template": {"slot_count": 1},
		"policies": [{"direction":"out","allowed_lengths":[0,128]}],
		"delayed_chunks": [{
			"chunk_id":"c",
			"bytes":10,
			"origin":{"generation":1,"slot":0,"direction":"out"},
			"reissue":{"generation":1,"slot":1,"direction":"out"}
		}]
	}`
	var stdout, stderr bytes.Buffer
	code := run(
		[]string{"-fail-on-insufficient-evidence"},
		strings.NewReader(input),
		&stdout,
		&stderr,
	)
	if code != 3 {
		t.Fatalf("expected evidence exit 3, got %d; stderr=%s stdout=%s", code, stderr.String(), stdout.String())
	}
	var rep slotcheck.Report
	if err := json.Unmarshal(stdout.Bytes(), &rep); err != nil {
		t.Fatal(err)
	}
	if rep.Summary.InsufficientEvidence == 0 || rep.Summary.ConfirmationGaps == 0 {
		t.Fatalf("expected evidence and confirmation gaps, got %+v", rep.Summary)
	}
}
