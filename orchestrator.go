// Package orchestrator implements a local, fully simulated drill orchestrator
// for leak-response playbooks (泄露处置演练编排器).
//
// The orchestrator never touches real infrastructure: execution outcomes
// arrive as synthetic Receipt values supplied by the on-duty operator or by
// tests. It enforces action preconditions and mutual-exclusion constraints,
// tells the operator which actions are currently executable, and on a failed
// step rolls back only the reversible, already-completed dependency branches
// that are no longer shared with other successful branches. Irreversible
// actions are never auto-rolled-back: their records are kept and they are
// escalated to manual handling. Receipt processing is idempotent so a
// duplicated receipt can never trigger a rollback twice, and every decision
// is written to an incident-scoped log so post-rollback decisions remain
// traceable to the original response.
package orchestrator

import (
	"fmt"
	"sort"
	"strings"
)

// ActionState is the lifecycle state of a playbook action.
type ActionState string

const (
	StatePending    ActionState = "PENDING"
	StateSucceeded  ActionState = "SUCCEEDED"
	StateFailed     ActionState = "FAILED"
	StateRolledBack ActionState = "ROLLED_BACK"
)

// Action describes one step of a response playbook.
type Action struct {
	ID         string
	Name       string
	Requires   []string // IDs that must be SUCCEEDED before this action may run
	MutexGroup string   // non-empty: at most one action of the group may reach SUCCEEDED
	Reversible bool     // false: never auto-rolled-back; escalated to a human instead
}

// Plan is a static playbook for one incident drill.
type Plan struct {
	IncidentID string
	Actions    []Action
}

// Receipt is a simulated execution receipt (执行回执). ReceiptID is the
// idempotency key provided by the (simulated) executor.
type Receipt struct {
	ReceiptID string
	ActionID  string
	Success   bool
	Detail    string
}

// LogKind classifies entries of the decision log.
type LogKind string

const (
	LogReceiptAccepted  LogKind = "RECEIPT_ACCEPTED"
	LogReceiptRejected  LogKind = "RECEIPT_REJECTED"
	LogReceiptDuplicate LogKind = "RECEIPT_DUPLICATE"
	LogRollback         LogKind = "ROLLBACK"
	LogRetained         LogKind = "RETAINED"
	LogEscalation       LogKind = "ESCALATION"
)

// LogEntry is one traceable decision record. Every entry carries the
// incident ID, and rollback-related entries carry the causing failure so a
// decision can be traced back to the original response step.
type LogEntry struct {
	Seq        int
	IncidentID string
	Kind       LogKind
	ActionID   string
	ReceiptID  string
	Cause      string
	Detail     string
}

// Decision tells the on-duty operator whether an action is currently
// executable, and why (or why not).
type Decision struct {
	ActionID  string
	Available bool
	Reasons   []string
}

// ReceiptOutcome reports how a receipt was processed.
type ReceiptOutcome struct {
	Accepted       bool
	Duplicate      bool     // receipt ID was already processed; nothing changed
	RejectedReason string   // non-empty when the receipt was rejected
	RolledBack     []string // actions rolled back because of a failure
	Retained       []string // reversible actions kept: still needed by succeeded branches
	Escalated      []string // irreversible actions transferred to manual handling
}

// Orchestrator holds the mutable drill state.
type Orchestrator struct {
	incidentID      string
	actions         map[string]Action
	order           []string
	state           map[string]ActionState
	processed       map[string]bool // processed receipt IDs (idempotency)
	escalated       map[string]bool
	rolledBackDueTo map[string]string // action -> failed action that caused the rollback
	depth           map[string]int
	reqClosure      map[string]map[string]bool
	log             []LogEntry
}

// NewOrchestrator validates the plan and returns a ready orchestrator.
func NewOrchestrator(p Plan) (*Orchestrator, error) {
	if strings.TrimSpace(p.IncidentID) == "" {
		return nil, fmt.Errorf("incident id must not be empty")
	}
	if len(p.Actions) == 0 {
		return nil, fmt.Errorf("plan must contain at least one action")
	}
	o := &Orchestrator{
		incidentID:      p.IncidentID,
		actions:         make(map[string]Action, len(p.Actions)),
		state:           make(map[string]ActionState, len(p.Actions)),
		processed:       map[string]bool{},
		escalated:       map[string]bool{},
		rolledBackDueTo: map[string]string{},
		depth:           map[string]int{},
		reqClosure:      map[string]map[string]bool{},
	}
	for _, a := range p.Actions {
		if strings.TrimSpace(a.ID) == "" {
			return nil, fmt.Errorf("plan contains an action with an empty id")
		}
		if _, dup := o.actions[a.ID]; dup {
			return nil, fmt.Errorf("duplicate action id %q", a.ID)
		}
		o.actions[a.ID] = a
		o.order = append(o.order, a.ID)
		o.state[a.ID] = StatePending
	}
	for _, a := range p.Actions {
		for _, r := range a.Requires {
			if _, ok := o.actions[r]; !ok {
				return nil, fmt.Errorf("action %q requires unknown action %q", a.ID, r)
			}
			if r == a.ID {
				return nil, fmt.Errorf("action %q requires itself", a.ID)
			}
		}
	}
	// Cycle check and depth computation (depth = longest prerequisite chain).
	visiting := map[string]int{} // 0=unvisited, 1=in stack, 2=done
	var visit func(id string) (int, error)
	visit = func(id string) (int, error) {
		switch visiting[id] {
		case 1:
			return 0, fmt.Errorf("dependency cycle involving %q", id)
		case 2:
			return o.depth[id], nil
		}
		visiting[id] = 1
		d := 0
		for _, r := range o.actions[id].Requires {
			rd, err := visit(r)
			if err != nil {
				return 0, err
			}
			if rd+1 > d {
				d = rd + 1
			}
		}
		visiting[id] = 2
		o.depth[id] = d
		return d, nil
	}
	for _, id := range o.order {
		if _, err := visit(id); err != nil {
			return nil, err
		}
	}
	return o, nil
}

// transRequires returns the transitive prerequisite closure of id.
func (o *Orchestrator) transRequires(id string) map[string]bool {
	if c, ok := o.reqClosure[id]; ok {
		return c
	}
	c := map[string]bool{}
	for _, r := range o.actions[id].Requires {
		c[r] = true
		for rr := range o.transRequires(r) {
			c[rr] = true
		}
	}
	o.reqClosure[id] = c
	return c
}

// checkAvailable evaluates preconditions and mutual exclusion for id.
func (o *Orchestrator) checkAvailable(id string) (bool, []string) {
	a := o.actions[id]
	available := true
	var reasons []string
	if o.state[id] == StateSucceeded {
		available = false
		reasons = append(reasons, "already SUCCEEDED")
	}
	for _, r := range a.Requires {
		if o.state[r] != StateSucceeded {
			available = false
			reasons = append(reasons, fmt.Sprintf("requires %q to be SUCCEEDED, currently %s", r, o.state[r]))
		}
	}
	if a.MutexGroup != "" {
		for _, sib := range o.order {
			if sib == id || o.actions[sib].MutexGroup != a.MutexGroup {
				continue
			}
			if o.state[sib] == StateSucceeded {
				available = false
				reasons = append(reasons, fmt.Sprintf("mutex group %q already satisfied by %q (SUCCEEDED)", a.MutexGroup, sib))
			}
		}
	}
	if available {
		switch o.state[id] {
		case StateRolledBack:
			reasons = append(reasons, fmt.Sprintf("re-enabled after rollback caused by failure of %q (incident %s)", o.rolledBackDueTo[id], o.incidentID))
		case StateFailed:
			reasons = append(reasons, "retry permitted after failure")
		default:
			reasons = append(reasons, "all preconditions satisfied")
		}
	}
	return available, reasons
}

// Decisions lists, for every action, whether the on-duty operator may execute
// it now, together with the reasons.
func (o *Orchestrator) Decisions() []Decision {
	out := make([]Decision, 0, len(o.order))
	for _, id := range o.order {
		ok, reasons := o.checkAvailable(id)
		out = append(out, Decision{ActionID: id, Available: ok, Reasons: reasons})
	}
	return out
}

// AvailableActions returns the IDs of the currently executable actions.
func (o *Orchestrator) AvailableActions() []string {
	var out []string
	for _, d := range o.Decisions() {
		if d.Available {
			out = append(out, d.ActionID)
		}
	}
	return out
}

// SubmitReceipt processes one simulated execution receipt. Duplicate receipts
// (same ReceiptID) are acknowledged as duplicates and change nothing, so a
// rollback can never be triggered twice by the same receipt.
func (o *Orchestrator) SubmitReceipt(rc Receipt) ReceiptOutcome {
	if _, ok := o.actions[rc.ActionID]; !ok {
		o.appendLog(LogReceiptRejected, rc.ActionID, rc.ReceiptID, "", "unknown action")
		return ReceiptOutcome{RejectedReason: "unknown action " + rc.ActionID}
	}
	if strings.TrimSpace(rc.ReceiptID) == "" {
		o.appendLog(LogReceiptRejected, rc.ActionID, rc.ReceiptID, "", "missing receipt id")
		return ReceiptOutcome{RejectedReason: "missing receipt id"}
	}
	if o.processed[rc.ReceiptID] {
		o.appendLog(LogReceiptDuplicate, rc.ActionID, rc.ReceiptID, "", "receipt already processed; ignored to keep rollback idempotent")
		return ReceiptOutcome{Duplicate: true, RejectedReason: "duplicate receipt"}
	}
	if ok, reasons := o.checkAvailable(rc.ActionID); !ok {
		o.appendLog(LogReceiptRejected, rc.ActionID, rc.ReceiptID, "", strings.Join(reasons, "; "))
		return ReceiptOutcome{RejectedReason: strings.Join(reasons, "; ")}
	}
	o.processed[rc.ReceiptID] = true
	if rc.Success {
		o.state[rc.ActionID] = StateSucceeded
		o.appendLog(LogReceiptAccepted, rc.ActionID, rc.ReceiptID, "", "execution succeeded")
		return ReceiptOutcome{Accepted: true}
	}
	o.state[rc.ActionID] = StateFailed
	o.appendLog(LogReceiptAccepted, rc.ActionID, rc.ReceiptID, "", "execution failed: "+rc.Detail)
	rolled, retained, escalated := o.rollback(rc.ActionID, rc.ReceiptID)
	return ReceiptOutcome{Accepted: true, RolledBack: rolled, Retained: retained, Escalated: escalated}
}

// rollback undoes the reversible, already-completed dependency branch of the
// failed action. A completed protection is kept in effect while any other
// succeeded branch still depends on it; irreversible completed actions are
// recorded and escalated to manual handling instead of being rolled back.
func (o *Orchestrator) rollback(failedID, receiptID string) (rolled, retained, escalated []string) {
	cause := "failure-of:" + failedID
	cand := map[string]bool{}
	for id := range o.transRequires(failedID) {
		if o.state[id] == StateSucceeded {
			cand[id] = true
		}
	}
	target := map[string]bool{}
	for id := range cand {
		if o.actions[id].Reversible {
			target[id] = true
		}
	}
	// Fixpoint: retain any rollback candidate that a succeeded action outside
	// the rollback set still (transitively) depends on.
	for changed := true; changed; {
		changed = false
		for a := range target {
			if _, ok := o.retainedBy(a, target); ok {
				delete(target, a)
				changed = true
			}
		}
	}
	for id := range cand {
		switch {
		case !o.actions[id].Reversible:
			o.escalated[id] = true
			escalated = append(escalated, id)
			o.appendLog(LogEscalation, id, receiptID, cause,
				"irreversible action cannot be rolled back automatically; record kept and transferred to manual handling")
		case !target[id]:
			retained = append(retained, id)
			by, _ := o.retainedBy(id, target)
			o.appendLog(LogRetained, id, receiptID, cause,
				fmt.Sprintf("protection kept in effect: still required by succeeded branch %q", by))
		}
	}
	rolled = make([]string, 0, len(target))
	for id := range target {
		rolled = append(rolled, id)
	}
	// Roll back deepest dependencies first; deterministic order for equal depth.
	sort.Slice(rolled, func(i, j int) bool {
		if o.depth[rolled[i]] != o.depth[rolled[j]] {
			return o.depth[rolled[i]] > o.depth[rolled[j]]
		}
		return rolled[i] < rolled[j]
	})
	for _, id := range rolled {
		o.state[id] = StateRolledBack
		o.rolledBackDueTo[id] = failedID
		o.appendLog(LogRollback, id, receiptID, cause, "reversible completed action rolled back")
	}
	sort.Strings(retained)
	sort.Strings(escalated)
	return rolled, retained, escalated
}

// retainedBy reports whether a succeeded action outside the rollback target
// set still depends on a.
func (o *Orchestrator) retainedBy(a string, target map[string]bool) (string, bool) {
	for _, s := range o.order {
		if s == a || target[s] || o.state[s] != StateSucceeded {
			continue
		}
		if o.transRequires(s)[a] {
			return s, true
		}
	}
	return "", false
}

// StateOf returns the current state of an action.
func (o *Orchestrator) StateOf(id string) (ActionState, bool) {
	st, ok := o.state[id]
	return st, ok
}

// Escalations returns the IDs of irreversible actions transferred to manual
// handling, in deterministic order.
func (o *Orchestrator) Escalations() []string {
	var out []string
	for id := range o.escalated {
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

// DecisionLog returns a copy of the full incident-scoped decision log.
func (o *Orchestrator) DecisionLog() []LogEntry {
	out := make([]LogEntry, len(o.log))
	copy(out, o.log)
	return out
}

// Trace returns every log entry related to one action, so a post-rollback
// decision can be traced back to the original response steps.
func (o *Orchestrator) Trace(actionID string) []LogEntry {
	var out []LogEntry
	for _, e := range o.log {
		if e.ActionID == actionID {
			out = append(out, e)
		}
	}
	return out
}

func (o *Orchestrator) appendLog(kind LogKind, actionID, receiptID, cause, detail string) {
	o.log = append(o.log, LogEntry{
		Seq:        len(o.log) + 1,
		IncidentID: o.incidentID,
		Kind:       kind,
		ActionID:   actionID,
		ReceiptID:  receiptID,
		Cause:      cause,
		Detail:     detail,
	})
}
