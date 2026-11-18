package orchestrator

import (
	"strings"
	"testing"
)

// leakPlaybook models the drill from the requirements: enable backup access
// first, then isolate the terminal and suspend identity issuance, then
// follow-up containment steps.
func leakPlaybook() Plan {
	return Plan{
		IncidentID: "INC-2026-1119",
		Actions: []Action{
			{ID: "enable-backup-access", Name: "启用备用接入", Reversible: true},
			{ID: "isolate-terminal", Name: "隔离终端", Requires: []string{"enable-backup-access"}, Reversible: true},
			{ID: "suspend-identity-issuance", Name: "暂停身份发放", Requires: []string{"enable-backup-access"}, Reversible: true},
			{ID: "freeze-tokens", Name: "冻结已发令牌", Requires: []string{"suspend-identity-issuance"}, Reversible: true},
			{ID: "revoke-keys", Name: "吊销泄露密钥", Requires: []string{"isolate-terminal", "suspend-identity-issuance"}, Reversible: false},
			{ID: "public-disclosure", Name: "公开通报", Requires: []string{"enable-backup-access"}, MutexGroup: "comms", Reversible: false},
			{ID: "silent-containment", Name: "静默处置", Requires: []string{"enable-backup-access"}, MutexGroup: "comms", Reversible: true},
		},
	}
}

func mustOrchestrator(t *testing.T, p Plan) *Orchestrator {
	t.Helper()
	o, err := NewOrchestrator(p)
	if err != nil {
		t.Fatalf("NewOrchestrator: unexpected error: %v", err)
	}
	return o
}

func succeed(t *testing.T, o *Orchestrator, receiptID, actionID string) {
	t.Helper()
	out := o.SubmitReceipt(Receipt{ReceiptID: receiptID, ActionID: actionID, Success: true})
	if !out.Accepted || out.Duplicate {
		t.Fatalf("success receipt %q for %q not accepted: %+v", receiptID, actionID, out)
	}
}

func mustState(t *testing.T, o *Orchestrator, actionID string, want ActionState) {
	t.Helper()
	got, ok := o.StateOf(actionID)
	if !ok {
		t.Fatalf("unknown action %q", actionID)
	}
	if got != want {
		t.Fatalf("state of %q = %s, want %s", actionID, got, want)
	}
}

func countLog(o *Orchestrator, kind LogKind) int {
	n := 0
	for _, e := range o.DecisionLog() {
		if e.Kind == kind {
			n++
		}
	}
	return n
}

func contains(list []string, item string) bool {
	for _, s := range list {
		if s == item {
			return true
		}
	}
	return false
}

func decisionOf(o *Orchestrator, actionID string) Decision {
	for _, d := range o.Decisions() {
		if d.ActionID == actionID {
			return d
		}
	}
	return Decision{ActionID: actionID}
}

// ---------------------------------------------------------------------------
// Acceptable result: sequenced happy-path execution of the whole drill.
// ---------------------------------------------------------------------------

func TestHappyPathSequencedExecution(t *testing.T) {
	o := mustOrchestrator(t, leakPlaybook())

	if got := o.AvailableActions(); len(got) != 1 || got[0] != "enable-backup-access" {
		t.Fatalf("initially available = %v, want only [enable-backup-access]", got)
	}

	succeed(t, o, "rc-1", "enable-backup-access")
	avail := o.AvailableActions()
	for _, want := range []string{"isolate-terminal", "suspend-identity-issuance", "public-disclosure", "silent-containment"} {
		if !contains(avail, want) {
			t.Fatalf("after backup access, %q should be available; got %v", want, avail)
		}
	}
	if contains(avail, "revoke-keys") || contains(avail, "freeze-tokens") {
		t.Fatalf("downstream actions must wait for their preconditions; got %v", avail)
	}

	succeed(t, o, "rc-2", "isolate-terminal")
	succeed(t, o, "rc-3", "suspend-identity-issuance")
	succeed(t, o, "rc-4", "freeze-tokens")
	succeed(t, o, "rc-5", "revoke-keys")
	succeed(t, o, "rc-6", "public-disclosure")

	for _, id := range []string{"enable-backup-access", "isolate-terminal", "suspend-identity-issuance", "freeze-tokens", "revoke-keys", "public-disclosure"} {
		mustState(t, o, id, StateSucceeded)
	}
	if got := o.AvailableActions(); len(got) != 0 {
		t.Fatalf("after completion nothing should be executable (silent-containment is mutex-blocked); got %v", got)
	}
	if n := countLog(o, LogReceiptAccepted); n != 6 {
		t.Fatalf("accepted receipts logged = %d, want 6", n)
	}
	for _, e := range o.DecisionLog() {
		if e.IncidentID != "INC-2026-1119" {
			t.Fatalf("log entry %+v missing incident id", e)
		}
	}
}

// ---------------------------------------------------------------------------
// Blocked results: precondition violation, mutex conflict, unknown action.
// ---------------------------------------------------------------------------

func TestReceiptRejectedWhenPreconditionsUnmet(t *testing.T) {
	o := mustOrchestrator(t, leakPlaybook())

	out := o.SubmitReceipt(Receipt{ReceiptID: "rc-x", ActionID: "isolate-terminal", Success: true})
	if out.Accepted || out.Duplicate {
		t.Fatalf("receipt without satisfied preconditions must be rejected, got %+v", out)
	}
	if !strings.Contains(out.RejectedReason, "enable-backup-access") {
		t.Fatalf("rejection reason should name the missing precondition, got %q", out.RejectedReason)
	}
	mustState(t, o, "isolate-terminal", StatePending)
	if n := countLog(o, LogReceiptRejected); n != 1 {
		t.Fatalf("rejected receipts logged = %d, want 1", n)
	}

	// Boundary: a rejected receipt must not consume its idempotency key, so the
	// same receipt ID is accepted once preconditions are satisfied.
	succeed(t, o, "rc-1", "enable-backup-access")
	out2 := o.SubmitReceipt(Receipt{ReceiptID: "rc-x", ActionID: "isolate-terminal", Success: true})
	if !out2.Accepted || out2.Duplicate {
		t.Fatalf("reused receipt id after rejection should be accepted, got %+v", out2)
	}
	mustState(t, o, "isolate-terminal", StateSucceeded)
}

func TestMutexSiblingBlocked(t *testing.T) {
	o := mustOrchestrator(t, leakPlaybook())
	succeed(t, o, "rc-1", "enable-backup-access")
	succeed(t, o, "rc-2", "public-disclosure")

	if contains(o.AvailableActions(), "silent-containment") {
		t.Fatalf("silent-containment must be blocked once public-disclosure succeeded")
	}
	out := o.SubmitReceipt(Receipt{ReceiptID: "rc-3", ActionID: "silent-containment", Success: true})
	if out.Accepted {
		t.Fatalf("mutex-conflicting receipt must be rejected, got %+v", out)
	}
	if !strings.Contains(out.RejectedReason, "mutex") || !strings.Contains(out.RejectedReason, "public-disclosure") {
		t.Fatalf("rejection reason should explain the mutex conflict, got %q", out.RejectedReason)
	}
	mustState(t, o, "silent-containment", StatePending)
}

func TestUnknownActionAndMissingReceiptIDRejected(t *testing.T) {
	o := mustOrchestrator(t, leakPlaybook())

	out := o.SubmitReceipt(Receipt{ReceiptID: "rc-9", ActionID: "nope", Success: true})
	if out.Accepted || !strings.Contains(out.RejectedReason, "unknown action") {
		t.Fatalf("unknown action must be rejected, got %+v", out)
	}
	out = o.SubmitReceipt(Receipt{ReceiptID: "", ActionID: "enable-backup-access", Success: true})
	if out.Accepted || !strings.Contains(out.RejectedReason, "missing receipt id") {
		t.Fatalf("missing receipt id must be rejected, got %+v", out)
	}
	mustState(t, o, "enable-backup-access", StatePending)
}

// ---------------------------------------------------------------------------
// Interleaved core: failure rolls back only the unshared reversible branch.
// ---------------------------------------------------------------------------

func TestFailureRollsBackOnlyUnsharedReversibleBranches(t *testing.T) {
	o := mustOrchestrator(t, leakPlaybook())
	succeed(t, o, "rc-1", "enable-backup-access")
	succeed(t, o, "rc-2", "isolate-terminal")
	succeed(t, o, "rc-3", "suspend-identity-issuance")
	succeed(t, o, "rc-4", "freeze-tokens") // successful sibling branch sharing suspend-identity-issuance

	out := o.SubmitReceipt(Receipt{ReceiptID: "rc-5", ActionID: "revoke-keys", Success: false, Detail: "key service unreachable"})
	if !out.Accepted {
		t.Fatalf("failure receipt must be accepted, got %+v", out)
	}

	// Only isolate-terminal is exclusive to the failed branch.
	if len(out.RolledBack) != 1 || out.RolledBack[0] != "isolate-terminal" {
		t.Fatalf("RolledBack = %v, want [isolate-terminal]", out.RolledBack)
	}
	// Protections still needed by the succeeded freeze-tokens branch stay in effect.
	if !contains(out.Retained, "suspend-identity-issuance") || !contains(out.Retained, "enable-backup-access") {
		t.Fatalf("Retained = %v, want suspend-identity-issuance and enable-backup-access kept", out.Retained)
	}
	if len(out.Escalated) != 0 {
		t.Fatalf("no irreversible completed dependency here, Escalated = %v", out.Escalated)
	}

	mustState(t, o, "isolate-terminal", StateRolledBack)
	mustState(t, o, "suspend-identity-issuance", StateSucceeded)
	mustState(t, o, "enable-backup-access", StateSucceeded)
	mustState(t, o, "freeze-tokens", StateSucceeded)
	mustState(t, o, "revoke-keys", StateFailed)

	// The failed action cannot be retried until its rolled-back prerequisite
	// is re-executed; the rolled-back action itself becomes executable again.
	if d := decisionOf(o, "revoke-keys"); d.Available {
		t.Fatalf("revoke-keys must wait for isolate-terminal to be re-executed; reasons %v", d.Reasons)
	}
	if d := decisionOf(o, "isolate-terminal"); !d.Available {
		t.Fatalf("rolled-back isolate-terminal should be re-enabled; reasons %v", d.Reasons)
	}
}

func TestRollbackDecisionsTraceableToOriginalIncident(t *testing.T) {
	o := mustOrchestrator(t, leakPlaybook())
	succeed(t, o, "rc-1", "enable-backup-access")
	succeed(t, o, "rc-2", "isolate-terminal")
	succeed(t, o, "rc-3", "suspend-identity-issuance")
	succeed(t, o, "rc-4", "freeze-tokens")
	o.SubmitReceipt(Receipt{ReceiptID: "rc-5", ActionID: "revoke-keys", Success: false})

	// Rollback records point back to the failing step and its receipt.
	var rollbackEntries int
	for _, e := range o.DecisionLog() {
		if e.Kind != LogRollback {
			continue
		}
		rollbackEntries++
		if e.Cause != "failure-of:revoke-keys" {
			t.Fatalf("rollback entry cause = %q, want failure-of:revoke-keys", e.Cause)
		}
		if e.ReceiptID != "rc-5" || e.IncidentID != "INC-2026-1119" {
			t.Fatalf("rollback entry not traceable to receipt/incident: %+v", e)
		}
	}
	if rollbackEntries != 1 {
		t.Fatalf("rollback entries = %d, want 1", rollbackEntries)
	}

	// The retention record names the succeeded branch that keeps the protection.
	var retainedFound bool
	for _, e := range o.DecisionLog() {
		if e.Kind == LogRetained && e.ActionID == "suspend-identity-issuance" {
			retainedFound = true
			if !strings.Contains(e.Detail, "freeze-tokens") {
				t.Fatalf("retention detail should name the retaining branch, got %q", e.Detail)
			}
		}
	}
	if !retainedFound {
		t.Fatalf("expected a RETAINED record for suspend-identity-issuance")
	}

	// The operator-facing decision for the re-enabled action cites the cause.
	d := decisionOf(o, "isolate-terminal")
	joined := strings.Join(d.Reasons, "; ")
	if !d.Available || !strings.Contains(joined, "revoke-keys") || !strings.Contains(joined, "INC-2026-1119") {
		t.Fatalf("re-enabled decision must trace to the original failure, got %+v", d)
	}

	// Trace() reconstructs the full history of the rolled-back action.
	trace := o.Trace("isolate-terminal")
	if len(trace) == 0 || trace[len(trace)-1].Kind != LogRollback {
		t.Fatalf("trace of isolate-terminal should end with its rollback, got %+v", trace)
	}
}

// ---------------------------------------------------------------------------
// Irreversible dependencies: record kept, transferred to manual handling.
// ---------------------------------------------------------------------------

func TestIrreversibleDependencyEscalatedToManual(t *testing.T) {
	p := Plan{
		IncidentID: "INC-ESC",
		Actions: []Action{
			{ID: "enable-backup-access", Reversible: true},
			{ID: "revoke-keys", Requires: []string{"enable-backup-access"}, Reversible: false},
			{ID: "notify-key-consumers", Requires: []string{"revoke-keys"}, Reversible: true},
		},
	}
	o := mustOrchestrator(t, p)
	succeed(t, o, "rc-1", "enable-backup-access")
	succeed(t, o, "rc-2", "revoke-keys")

	out := o.SubmitReceipt(Receipt{ReceiptID: "rc-3", ActionID: "notify-key-consumers", Success: false, Detail: "gateway timeout"})

	if len(out.RolledBack) != 0 {
		t.Fatalf("nothing may be auto-rolled-back here, got %v", out.RolledBack)
	}
	if len(out.Escalated) != 1 || out.Escalated[0] != "revoke-keys" {
		t.Fatalf("Escalated = %v, want [revoke-keys]", out.Escalated)
	}
	// enable-backup-access is reversible but still required by the escalated
	// (still in-effect) revoke-keys, so it must be retained.
	if len(out.Retained) != 1 || out.Retained[0] != "enable-backup-access" {
		t.Fatalf("Retained = %v, want [enable-backup-access]", out.Retained)
	}
	mustState(t, o, "revoke-keys", StateSucceeded) // protection remains in effect
	mustState(t, o, "enable-backup-access", StateSucceeded)
	if !contains(o.Escalations(), "revoke-keys") {
		t.Fatalf("Escalations() = %v, want revoke-keys listed for manual handling", o.Escalations())
	}

	var esc *LogEntry
	for i, e := range o.DecisionLog() {
		if e.Kind == LogEscalation {
			e := e
			esc = &e
			_ = i
		}
	}
	if esc == nil {
		t.Fatalf("expected an ESCALATION record")
	}
	if esc.ActionID != "revoke-keys" || esc.Cause != "failure-of:notify-key-consumers" || esc.ReceiptID != "rc-3" {
		t.Fatalf("escalation record not traceable: %+v", esc)
	}
	if !strings.Contains(esc.Detail, "manual") {
		t.Fatalf("escalation detail should mention manual handling, got %q", esc.Detail)
	}
}

// ---------------------------------------------------------------------------
// Idempotency: duplicate receipts never trigger a second rollback and never
// resurrect a rolled-back action.
// ---------------------------------------------------------------------------

func TestDuplicateFailureReceiptDoesNotRollbackTwice(t *testing.T) {
	o := mustOrchestrator(t, leakPlaybook())
	succeed(t, o, "rc-1", "enable-backup-access")
	succeed(t, o, "rc-2", "isolate-terminal")
	succeed(t, o, "rc-3", "suspend-identity-issuance")

	out := o.SubmitReceipt(Receipt{ReceiptID: "rc-4", ActionID: "revoke-keys", Success: false})
	// No other succeeded branch shares the chain: all three reversible
	// prerequisites roll back, deepest first.
	wantRolled := []string{"isolate-terminal", "suspend-identity-issuance", "enable-backup-access"}
	if len(out.RolledBack) != len(wantRolled) {
		t.Fatalf("RolledBack = %v, want %v", out.RolledBack, wantRolled)
	}
	for i := range wantRolled {
		if out.RolledBack[i] != wantRolled[i] {
			t.Fatalf("RolledBack = %v, want %v (deepest first)", out.RolledBack, wantRolled)
		}
	}
	if n := countLog(o, LogRollback); n != 3 {
		t.Fatalf("rollback entries = %d, want 3", n)
	}

	dup := o.SubmitReceipt(Receipt{ReceiptID: "rc-4", ActionID: "revoke-keys", Success: false})
	if !dup.Duplicate || dup.Accepted {
		t.Fatalf("second delivery of rc-4 must be flagged duplicate and not accepted, got %+v", dup)
	}
	if len(dup.RolledBack) != 0 {
		t.Fatalf("duplicate receipt must not roll anything back, got %v", dup.RolledBack)
	}
	if n := countLog(o, LogRollback); n != 3 {
		t.Fatalf("duplicate receipt triggered another rollback: entries = %d, want 3", n)
	}
	if n := countLog(o, LogReceiptDuplicate); n != 1 {
		t.Fatalf("duplicate receipts logged = %d, want 1", n)
	}
	mustState(t, o, "enable-backup-access", StateRolledBack)
	mustState(t, o, "revoke-keys", StateFailed)
}

func TestDuplicateSuccessReceiptDoesNotResurrectRolledBackAction(t *testing.T) {
	o := mustOrchestrator(t, leakPlaybook())
	succeed(t, o, "rc-1", "enable-backup-access")
	succeed(t, o, "rc-2", "isolate-terminal")
	succeed(t, o, "rc-3", "suspend-identity-issuance")
	o.SubmitReceipt(Receipt{ReceiptID: "rc-4", ActionID: "revoke-keys", Success: false})
	mustState(t, o, "enable-backup-access", StateRolledBack)

	// The executor re-delivers the original success receipt of a now
	// rolled-back action: it must not bring the action back to SUCCEEDED.
	dup := o.SubmitReceipt(Receipt{ReceiptID: "rc-1", ActionID: "enable-backup-access", Success: true})
	if !dup.Duplicate || dup.Accepted {
		t.Fatalf("re-delivered success receipt must be a no-op duplicate, got %+v", dup)
	}
	mustState(t, o, "enable-backup-access", StateRolledBack)

	// The drill can still continue: re-execute the rolled-back chain with new
	// receipts, then retry the failed step.
	succeed(t, o, "rc-5", "enable-backup-access")
	succeed(t, o, "rc-6", "isolate-terminal")
	succeed(t, o, "rc-7", "suspend-identity-issuance")
	succeed(t, o, "rc-8", "revoke-keys")
	mustState(t, o, "revoke-keys", StateSucceeded)
}

// ---------------------------------------------------------------------------
// Plan validation.
// ---------------------------------------------------------------------------

func TestPlanValidation(t *testing.T) {
	cases := map[string]Plan{
		"empty incident":   {IncidentID: "", Actions: []Action{{ID: "a"}}},
		"no actions":       {IncidentID: "INC", Actions: nil},
		"empty action id":  {IncidentID: "INC", Actions: []Action{{ID: " "}}},
		"duplicate id":     {IncidentID: "INC", Actions: []Action{{ID: "a"}, {ID: "a"}}},
		"unknown requires": {IncidentID: "INC", Actions: []Action{{ID: "a", Requires: []string{"ghost"}}}},
		"self requires":    {IncidentID: "INC", Actions: []Action{{ID: "a", Requires: []string{"a"}}}},
		"cycle": {IncidentID: "INC", Actions: []Action{
			{ID: "a", Requires: []string{"b"}},
			{ID: "b", Requires: []string{"a"}},
		}},
	}
	for name, p := range cases {
		if _, err := NewOrchestrator(p); err == nil {
			t.Fatalf("case %q: expected validation error, got nil", name)
		}
	}
	if _, err := NewOrchestrator(Plan{IncidentID: "INC", Actions: []Action{{ID: "a"}}}); err != nil {
		t.Fatalf("valid minimal plan rejected: %v", err)
	}
}
