package relaygate_test

import (
	"errors"
	"testing"
	"time"

	"relaygate"
)

var (
	testBase    = time.Date(2026, time.November, 27, 0, 0, 0, 0, time.UTC)
	testSession = relaygate.SessionKey{ConnectionGeneration: 42, PeerStartNumber: 7}
)

func atSecond(second int) time.Time {
	return testBase.Add(time.Duration(second) * time.Second)
}

func testWindow(startSecond, endSecond int) relaygate.LeaseWindow {
	return relaygate.LeaseWindow{NotBefore: atSecond(startSecond), ExpiresAt: atSecond(endSecond)}
}

func testGrant(direction relaygate.Direction, startSecond, endSecond int) relaygate.DirectionGrant {
	return relaygate.DirectionGrant{Direction: direction, Window: testWindow(startSecond, endSecond)}
}

func testProof(sequence uint64, grants ...relaygate.DirectionGrant) relaygate.KeepaliveProof {
	return relaygate.KeepaliveProof{Session: testSession, Sequence: sequence, Grants: grants}
}

func testRecord(id string, direction relaygate.Direction, second int) relaygate.Record {
	return relaygate.Record{
		ID:        id,
		Session:   testSession,
		Direction: direction,
		TrustedAt: atSecond(second),
	}
}

func submitProof(t *testing.T, gate *relaygate.Adjudicator, proof relaygate.KeepaliveProof) {
	t.Helper()
	if _, err := gate.SubmitKeepalive(proof); err != nil {
		t.Fatalf("SubmitKeepalive(seq=%d) returned error: %v", proof.Sequence, err)
	}
}

func submitConfirmation(t *testing.T, gate *relaygate.Adjudicator, confirmation relaygate.WakeConfirmation) {
	t.Helper()
	if _, err := gate.SubmitWakeConfirmation(confirmation); err != nil {
		t.Fatalf("SubmitWakeConfirmation(seq=%d) returned error: %v", confirmation.KeepaliveSequence, err)
	}
}

func requireStatus(t *testing.T, verdict relaygate.Verdict, want relaygate.Status) {
	t.Helper()
	if verdict.Status != want {
		t.Fatalf("record %s status = %q, want %q (issues=%+v missing=%+v)",
			verdict.RecordID, verdict.Status, want, verdict.Issues, verdict.MissingEvidence)
	}
	if verdict.Deliverable != (want == relaygate.StatusAdmitted) {
		t.Fatalf("record %s Deliverable=%v, want %v for status %q",
			verdict.RecordID, verdict.Deliverable, want == relaygate.StatusAdmitted, want)
	}
}

func requireNeedCode(t *testing.T, verdict relaygate.Verdict, want relaygate.NeedCode) relaygate.MissingEvidence {
	t.Helper()
	for _, need := range verdict.MissingEvidence {
		if need.Code == want {
			return need
		}
	}
	t.Fatalf("record %s missing evidence codes = %+v, want %q",
		verdict.RecordID, verdict.MissingEvidence, want)
	return relaygate.MissingEvidence{}
}

func requireIssueCode(t *testing.T, verdict relaygate.Verdict, want relaygate.IssueCode) relaygate.Issue {
	t.Helper()
	for _, issue := range verdict.Issues {
		if issue.Code == want {
			return issue
		}
	}
	t.Fatalf("record %s issue codes = %+v, want %q", verdict.RecordID, verdict.Issues, want)
	return relaygate.Issue{}
}

func requireAttributedSequence(t *testing.T, verdict relaygate.Verdict, want uint64) {
	t.Helper()
	if verdict.Attribution == nil {
		t.Fatalf("record %s has nil attribution", verdict.RecordID)
	}
	if verdict.Attribution.KeepaliveSequence == nil {
		t.Fatalf("record %s attribution has nil proof sequence, want %d", verdict.RecordID, want)
	}
	if got := *verdict.Attribution.KeepaliveSequence; got != want {
		t.Fatalf("record %s attributed sequence = %d, want %d", verdict.RecordID, got, want)
	}
}

func changedVerdict(verdicts []relaygate.Verdict, recordID string) (relaygate.Verdict, bool) {
	for _, verdict := range verdicts {
		if verdict.RecordID == recordID {
			return verdict, true
		}
	}
	return relaygate.Verdict{}, false
}

func TestHappyPathAdmitsEachExplicitDirection(t *testing.T) {
	gate := relaygate.New()
	proof := testProof(9,
		testGrant(relaygate.Uplink, 0, 100),
		testGrant(relaygate.Downlink, 0, 120),
	)
	submitProof(t, gate, proof)
	submitConfirmation(t, gate, relaygate.ConfirmDirections(proof, relaygate.Uplink, relaygate.Downlink))

	verdicts := gate.Admit(
		testRecord("up-1", relaygate.Uplink, 50),
		testRecord("down-1", relaygate.Downlink, 50),
	)

	if len(verdicts) != 2 {
		t.Fatalf("len(verdicts) = %d, want 2", len(verdicts))
	}
	for _, verdict := range verdicts {
		requireStatus(t, verdict, relaygate.StatusAdmitted)
		if verdict.Attribution == nil || verdict.Attribution.OwnerKey != testSession.String() {
			t.Fatalf("record %s attribution = %+v, want owner %s", verdict.RecordID, verdict.Attribution, testSession)
		}
		requireAttributedSequence(t, verdict, 9)
		if verdict.Attribution.Window == nil || !verdict.Attribution.Window.Contains(atSecond(50)) {
			t.Fatalf("record %s attribution window = %+v does not contain trusted time", verdict.RecordID, verdict.Attribution.Window)
		}
	}
}

func TestLeaseWindowBoundariesAreHalfOpen(t *testing.T) {
	gate := relaygate.New()
	proof := testProof(3, testGrant(relaygate.Uplink, 10, 20))
	submitProof(t, gate, proof)
	submitConfirmation(t, gate, relaygate.ConfirmDirections(proof, relaygate.Uplink))

	cases := []struct {
		name   string
		time   time.Time
		status relaygate.Status
		issue  relaygate.IssueCode
	}{
		{name: "not before inclusive", time: atSecond(10), status: relaygate.StatusAdmitted},
		{name: "inside", time: atSecond(15), status: relaygate.StatusAdmitted},
		{name: "nanosecond before expiry", time: atSecond(20).Add(-time.Nanosecond), status: relaygate.StatusAdmitted},
		{name: "expiry exclusive", time: atSecond(20), status: relaygate.StatusDenied, issue: relaygate.IssueLeaseExpired},
		{name: "after expiry", time: atSecond(21), status: relaygate.StatusDenied, issue: relaygate.IssueLeaseExpired},
		{name: "before start", time: atSecond(9), status: relaygate.StatusDenied, issue: relaygate.IssueLeaseNotStarted},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			record := testRecord("boundary-"+tc.name, relaygate.Uplink, 0)
			record.TrustedAt = tc.time
			verdicts := gate.Admit(record)
			requireStatus(t, verdicts[0], tc.status)
			if tc.issue != "" {
				requireIssueCode(t, verdicts[0], tc.issue)
			}
		})
	}
}

func TestWakeConfirmationCanBeDelayedAndIsScoped(t *testing.T) {
	gate := relaygate.New()
	proof := testProof(4,
		testGrant(relaygate.Uplink, 0, 100),
		testGrant(relaygate.Downlink, 0, 100),
	)
	submitProof(t, gate, proof)

	up := testRecord("up-delayed", relaygate.Uplink, 40)
	down := testRecord("down-delayed", relaygate.Downlink, 40)
	initial := gate.Admit(up, down)
	for _, verdict := range initial {
		requireStatus(t, verdict, relaygate.StatusPending)
		requireNeedCode(t, verdict, relaygate.NeedWakeConfirmation)
	}

	result, err := gate.SubmitWakeConfirmation(relaygate.ConfirmDirections(proof, relaygate.Uplink))
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Changed) != 2 {
		t.Fatalf("len(Changed) = %d, want admitted uplink and a more specific downlink scope need", len(result.Changed))
	}
	changedUp, ok := changedVerdict(result.Changed, "up-delayed")
	if !ok {
		t.Fatal("uplink verdict missing from Changed")
	}
	requireStatus(t, changedUp, relaygate.StatusAdmitted)
	changedDown, ok := changedVerdict(result.Changed, "down-delayed")
	if !ok {
		t.Fatal("downlink verdict missing from Changed")
	}
	requireStatus(t, changedDown, relaygate.StatusPending)
	requireNeedCode(t, changedDown, relaygate.NeedConfirmationScope)

	upVerdict, ok := gate.Verdict("up-delayed")
	if !ok {
		t.Fatal("uplink verdict missing")
	}
	requireStatus(t, upVerdict, relaygate.StatusAdmitted)

	downVerdict, ok := gate.Verdict("down-delayed")
	if !ok {
		t.Fatal("downlink verdict missing")
	}
	requireStatus(t, downVerdict, relaygate.StatusPending)
	need := requireNeedCode(t, downVerdict, relaygate.NeedConfirmationScope)
	if need.Direction != relaygate.Downlink {
		t.Fatalf("missing-evidence direction = %q, want downlink", need.Direction)
	}

	submitConfirmation(t, gate, relaygate.ConfirmDirections(proof, relaygate.Downlink))
	downVerdict, _ = gate.Verdict("down-delayed")
	requireStatus(t, downVerdict, relaygate.StatusAdmitted)
}

func TestConfirmationCannotExpandProofScope(t *testing.T) {
	gate := relaygate.New()
	proof := testProof(5, testGrant(relaygate.Uplink, 0, 100))
	submitProof(t, gate, proof)

	// The confirmation reuses the proof reference but invents a later expiry.
	broad := relaygate.WakeConfirmation{
		Session:           testSession,
		KeepaliveSequence: 5,
		Grants:            []relaygate.DirectionGrant{testGrant(relaygate.Uplink, 0, 200)},
	}
	submitConfirmation(t, gate, broad)

	verdict := gate.Admit(testRecord("scope", relaygate.Uplink, 50))[0]
	requireStatus(t, verdict, relaygate.StatusPending)
	need := requireNeedCode(t, verdict, relaygate.NeedConfirmationScope)
	if need.Window == nil || !need.Window.ExpiresAt.Equal(atSecond(100)) {
		t.Fatalf("missing scope window = %+v, want exact proof window ending at 100", need.Window)
	}

	// A direction absent from the proof cannot be created by a confirmation.
	downAck := relaygate.WakeConfirmation{
		Session:           testSession,
		KeepaliveSequence: 5,
		Grants:            []relaygate.DirectionGrant{testGrant(relaygate.Downlink, 0, 100)},
	}
	submitConfirmation(t, gate, downAck)
	downVerdict := gate.Admit(testRecord("unlisted-direction", relaygate.Downlink, 50))[0]
	requireStatus(t, downVerdict, relaygate.StatusPending)
	requireNeedCode(t, downVerdict, relaygate.NeedDirectionGrant)

	// Exact proof scope is sufficient.
	submitConfirmation(t, gate, relaygate.ConfirmDirections(proof, relaygate.Uplink))
	scopeVerdict, _ := gate.Verdict("scope")
	requireStatus(t, scopeVerdict, relaygate.StatusAdmitted)
}

func TestDuplicateKeepaliveDoesNotExtendWindow(t *testing.T) {
	gate := relaygate.New()
	original := testProof(8, testGrant(relaygate.Uplink, 0, 100))
	submitProof(t, gate, original)
	submitConfirmation(t, gate, relaygate.ConfirmDirections(original, relaygate.Uplink))

	first := gate.Admit(testRecord("before-expiry", relaygate.Uplink, 50))[0]
	requireStatus(t, first, relaygate.StatusAdmitted)

	duplicateResult, err := gate.SubmitKeepalive(original)
	if err != nil {
		t.Fatalf("equal duplicate returned error: %v", err)
	}
	if duplicateResult.Status != relaygate.SubmitDuplicate {
		t.Fatalf("duplicate status = %q, want %q", duplicateResult.Status, relaygate.SubmitDuplicate)
	}

	extended := testProof(8, testGrant(relaygate.Uplink, 0, 200))
	conflictResult, err := gate.SubmitKeepalive(extended)
	if err == nil {
		t.Fatal("different proof with same sequence was accepted")
	}
	if !errors.Is(err, relaygate.ErrConflict) {
		t.Fatalf("error = %v, want errors.Is relaygate.ErrConflict", err)
	}
	if conflictResult.Status != relaygate.SubmitConflict {
		t.Fatalf("conflict result status = %q, want conflict", conflictResult.Status)
	}

	late := gate.Admit(testRecord("after-original-expiry", relaygate.Uplink, 150))[0]
	requireStatus(t, late, relaygate.StatusDenied)
	requireIssueCode(t, late, relaygate.IssueLeaseExpired)
	if late.Attribution == nil || late.Attribution.Window == nil {
		t.Fatalf("late verdict attribution/window missing: %+v", late.Attribution)
	}
	if !late.Attribution.Window.ExpiresAt.Equal(atSecond(100)) {
		t.Fatalf("expiry changed to %v; duplicate/conflicting proof must not extend it", late.Attribution.Window.ExpiresAt)
	}
}

func TestOneDirectionTakeoverDoesNotInterruptOtherValidDirection(t *testing.T) {
	gate := relaygate.New()

	oldProof := testProof(1,
		testGrant(relaygate.Uplink, 0, 100),
		testGrant(relaygate.Downlink, 0, 100),
	)
	submitProof(t, gate, oldProof)
	submitConfirmation(t, gate, relaygate.ConfirmDirections(oldProof, relaygate.Uplink, relaygate.Downlink))

	// Takeover proof has a newer sequence and lists both directions, but its
	// confirmation covers only uplink.
	takeoverProof := testProof(2,
		testGrant(relaygate.Uplink, 40, 200),
		testGrant(relaygate.Downlink, 40, 200),
	)
	submitProof(t, gate, takeoverProof)
	submitConfirmation(t, gate, relaygate.ConfirmDirections(takeoverProof, relaygate.Uplink))

	verdicts := gate.Admit(
		testRecord("uplink-takeover", relaygate.Uplink, 60),
		testRecord("downlink-continuing", relaygate.Downlink, 60),
	)
	requireStatus(t, verdicts[0], relaygate.StatusAdmitted)
	requireAttributedSequence(t, verdicts[0], 2)

	requireStatus(t, verdicts[1], relaygate.StatusAdmitted)
	requireAttributedSequence(t, verdicts[1], 1)

	// After the old downlink window expires, the unconfirmed new downlink
	// scope cannot take over implicitly.
	lateDown := gate.Admit(testRecord("downlink-after-old-window", relaygate.Downlink, 150))[0]
	requireStatus(t, lateDown, relaygate.StatusPending)
	requireAttributedSequence(t, lateDown, 2)
	requireNeedCode(t, lateDown, relaygate.NeedConfirmationScope)
}

func TestConnectionGenerationAndPeerStartMustMatch(t *testing.T) {
	gate := relaygate.New()
	proof := relaygate.KeepaliveProof{
		Session:  relaygate.SessionKey{ConnectionGeneration: 1, PeerStartNumber: 1},
		Sequence: 1,
		Grants:   []relaygate.DirectionGrant{testGrant(relaygate.Uplink, 0, 100)},
	}
	submitProof(t, gate, proof)
	submitConfirmation(t, gate, relaygate.ConfirmDirections(proof, relaygate.Uplink))

	matching := relaygate.Record{
		ID: "matching", Session: proof.Session, Direction: relaygate.Uplink, TrustedAt: atSecond(50),
	}
	generationMismatch := matching
	generationMismatch.ID = "generation-mismatch"
	generationMismatch.Session.ConnectionGeneration = 2

	peerMismatch := matching
	peerMismatch.ID = "peer-mismatch"
	peerMismatch.Session.PeerStartNumber = 2

	verdicts := gate.Admit(matching, generationMismatch, peerMismatch)
	requireStatus(t, verdicts[0], relaygate.StatusAdmitted)

	requireStatus(t, verdicts[1], relaygate.StatusPending)
	requireNeedCode(t, verdicts[1], relaygate.NeedKeepaliveProof)
	requireIssueCode(t, verdicts[1], relaygate.IssueGenerationMismatch)

	requireStatus(t, verdicts[2], relaygate.StatusPending)
	requireNeedCode(t, verdicts[2], relaygate.NeedKeepaliveProof)
	requireIssueCode(t, verdicts[2], relaygate.IssuePeerStartMismatch)
}

func TestKeepaliveSequenceBindingAndSelection(t *testing.T) {
	gate := relaygate.New()
	oldProof := testProof(1, testGrant(relaygate.Uplink, 0, 100))
	newProof := testProof(2, testGrant(relaygate.Uplink, 0, 200))
	submitProof(t, gate, oldProof)
	submitProof(t, gate, newProof)
	submitConfirmation(t, gate, relaygate.ConfirmDirections(oldProof, relaygate.Uplink))
	submitConfirmation(t, gate, relaygate.ConfirmDirections(newProof, relaygate.Uplink))

	oldRequired := testRecord("explicit-old", relaygate.Uplink, 50)
	oldRequired.KeepaliveSequence = relaygate.SequencePtr(1)

	newRequiredLate := testRecord("explicit-new-late", relaygate.Uplink, 150)
	newRequiredLate.KeepaliveSequence = relaygate.SequencePtr(2)

	missingSequence := testRecord("missing-sequence", relaygate.Uplink, 50)
	missingSequence.KeepaliveSequence = relaygate.SequencePtr(9)

	implicit := testRecord("implicit-newest", relaygate.Uplink, 50)

	verdicts := gate.Admit(oldRequired, newRequiredLate, missingSequence, implicit)
	requireStatus(t, verdicts[0], relaygate.StatusAdmitted)
	requireAttributedSequence(t, verdicts[0], 1)

	requireStatus(t, verdicts[1], relaygate.StatusAdmitted)
	requireAttributedSequence(t, verdicts[1], 2)

	requireStatus(t, verdicts[2], relaygate.StatusPending)
	need := requireNeedCode(t, verdicts[2], relaygate.NeedKeepaliveProof)
	if need.KeepaliveSequence == nil || *need.KeepaliveSequence != 9 {
		t.Fatalf("missing sequence = %+v, want 9", need.KeepaliveSequence)
	}

	requireStatus(t, verdicts[3], relaygate.StatusAdmitted)
	requireAttributedSequence(t, verdicts[3], 2)
}

func TestEvidenceAndConfirmationCanInterleaveWithRecords(t *testing.T) {
	gate := relaygate.New()
	r1 := testRecord("interleave-1", relaygate.Uplink, 50)
	r2 := testRecord("interleave-2", relaygate.Downlink, 50)

	beforeEvidence := gate.Admit(r1, r2)
	for _, verdict := range beforeEvidence {
		requireStatus(t, verdict, relaygate.StatusPending)
		requireNeedCode(t, verdict, relaygate.NeedKeepaliveProof)
	}

	proof := testProof(11,
		testGrant(relaygate.Uplink, 0, 100),
		testGrant(relaygate.Downlink, 0, 100),
	)
	proofResult, err := gate.SubmitKeepalive(proof)
	if err != nil {
		t.Fatal(err)
	}
	if len(proofResult.Changed) != 2 {
		t.Fatalf("proof changed %d verdicts, want 2", len(proofResult.Changed))
	}
	for _, verdict := range proofResult.Changed {
		requireStatus(t, verdict, relaygate.StatusPending)
		requireNeedCode(t, verdict, relaygate.NeedWakeConfirmation)
	}

	upAckResult, err := gate.SubmitWakeConfirmation(relaygate.ConfirmDirections(proof, relaygate.Uplink))
	if err != nil {
		t.Fatal(err)
	}
	if len(upAckResult.Changed) != 2 {
		t.Fatalf("uplink ack changed %d verdicts, want interleave-1 admission and interleave-2 scope refinement", len(upAckResult.Changed))
	}
	admittedUp, ok := changedVerdict(upAckResult.Changed, "interleave-1")
	if !ok {
		t.Fatalf("interleave-1 missing from uplink ack changes: %+v", upAckResult.Changed)
	}
	requireStatus(t, admittedUp, relaygate.StatusAdmitted)
	refinedDown, ok := changedVerdict(upAckResult.Changed, "interleave-2")
	if !ok {
		t.Fatalf("interleave-2 missing from uplink ack changes: %+v", upAckResult.Changed)
	}
	requireStatus(t, refinedDown, relaygate.StatusPending)
	requireNeedCode(t, refinedDown, relaygate.NeedConfirmationScope)

	downAckResult, err := gate.SubmitWakeConfirmation(relaygate.ConfirmDirections(proof, relaygate.Downlink))
	if err != nil {
		t.Fatal(err)
	}
	if len(downAckResult.Changed) != 1 || downAckResult.Changed[0].RecordID != "interleave-2" {
		t.Fatalf("downlink ack changed = %+v, want only interleave-2", downAckResult.Changed)
	}

	all := gate.Verdicts()
	if len(all) != 2 || all[0].RecordID != "interleave-1" || all[1].RecordID != "interleave-2" {
		t.Fatalf("Verdicts order/content = %+v", all)
	}
	for _, verdict := range all {
		requireStatus(t, verdict, relaygate.StatusAdmitted)
	}
}

func TestConfirmationCanArriveBeforeProof(t *testing.T) {
	gate := relaygate.New()
	confirmation := relaygate.WakeConfirmation{
		Session:           testSession,
		KeepaliveSequence: 12,
		Grants:            []relaygate.DirectionGrant{testGrant(relaygate.Uplink, 0, 100)},
	}
	submitConfirmation(t, gate, confirmation)

	beforeProof := gate.Admit(testRecord("ack-first", relaygate.Uplink, 50))[0]
	requireStatus(t, beforeProof, relaygate.StatusPending)
	requireNeedCode(t, beforeProof, relaygate.NeedKeepaliveProof)

	proof := testProof(12, testGrant(relaygate.Uplink, 0, 100))
	result, err := gate.SubmitKeepalive(proof)
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Changed) != 1 {
		t.Fatalf("proof result changed %d verdicts, want 1", len(result.Changed))
	}
	requireStatus(t, result.Changed[0], relaygate.StatusAdmitted)
}

func TestAdmitReturnsVerdictForEveryInputAndRejectsInvalidMetadata(t *testing.T) {
	gate := relaygate.New()
	proof := testProof(1, testGrant(relaygate.Uplink, 0, 100))
	submitProof(t, gate, proof)
	submitConfirmation(t, gate, relaygate.ConfirmDirections(proof, relaygate.Uplink))

	valid := testRecord("valid", relaygate.Uplink, 50)
	emptyID := valid
	emptyID.ID = ""
	unknownDirection := valid
	unknownDirection.ID = "unknown-direction"
	unknownDirection.Direction = relaygate.Direction("sideways")
	missingTime := valid
	missingTime.ID = "missing-time"
	missingTime.TrustedAt = time.Time{}

	verdicts := gate.Admit(valid, emptyID, unknownDirection, missingTime)
	if len(verdicts) != 4 {
		t.Fatalf("len(verdicts) = %d, want one verdict per input", len(verdicts))
	}
	requireStatus(t, verdicts[0], relaygate.StatusAdmitted)

	// ID is only a correlation key; an empty-ID record is still decided. It is
	// not retained for later evidence-triggered refresh.
	requireStatus(t, verdicts[1], relaygate.StatusAdmitted)
	if _, ok := gate.Verdict(""); ok {
		t.Fatal("empty-ID record should not be retained under the empty key")
	}

	// Genuinely invalid metadata is denied rather than silently dropped.
	requireStatus(t, verdicts[2], relaygate.StatusDenied)
	requireIssueCode(t, verdicts[2], relaygate.IssueUnknownDirection)
	requireStatus(t, verdicts[3], relaygate.StatusDenied)
	requireIssueCode(t, verdicts[3], relaygate.IssueMissingTrustedTime)
	requireNeedCode(t, verdicts[3], relaygate.NeedTrustedTime)
}

func TestReusingRecordIDWithDifferentMetadataIsConflict(t *testing.T) {
	gate := relaygate.New()
	first := testRecord("dup-id", relaygate.Uplink, 50)
	gate.Admit(first)

	second := first
	second.TrustedAt = atSecond(60)
	verdict := gate.Admit(second)[0]
	requireStatus(t, verdict, relaygate.StatusConflict)
	requireIssueCode(t, verdict, relaygate.IssueDuplicateRecordID)

	stored, ok := gate.Verdict("dup-id")
	if !ok {
		t.Fatal("original record ID not stored")
	}
	if stored.Status != relaygate.StatusPending {
		t.Fatalf("stored status = %q, want original pending verdict preserved", stored.Status)
	}
}

func TestWithoutWakeConfirmationPolicyAdmitsOnProofAlone(t *testing.T) {
	gate := relaygate.New(relaygate.WithWakeConfirmationRequired(false))
	proof := testProof(1, testGrant(relaygate.Uplink, 0, 100))
	submitProof(t, gate, proof)

	verdict := gate.Admit(testRecord("no-ack-required", relaygate.Uplink, 50))[0]
	requireStatus(t, verdict, relaygate.StatusAdmitted)
	if verdict.Attribution == nil || verdict.Attribution.Basis != relaygate.BasisConfirmedLease {
		t.Fatalf("attribution = %+v, want confirmed lease basis under disabled ack policy", verdict.Attribution)
	}
}
