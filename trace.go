package authstack

import (
	"errors"
	"fmt"
	"strings"
)

// Sentinel reasons reported when a layer is denied. They are wrapped by
// *DenialError, so callers can use errors.Is on any returned error.
var (
	// ErrNoSuccess means the layer finished without a single successful item.
	ErrNoSuccess = errors.New("authstack: layer had no successful item")
	// ErrRequiredFailure means at least one "required" item failed.
	ErrRequiredFailure = errors.New("authstack: a required item failed")
	// ErrRequisiteFailure means an "immediate failure" item failed and ended
	// the layer.
	ErrRequisiteFailure = errors.New("authstack: a requisite item failed")
	// ErrCycle means a stack contains itself, directly or through nesting.
	ErrCycle = errors.New("authstack: sub-stack cycle detected")
	// ErrInvalidStack means the stack definition itself is malformed.
	ErrInvalidStack = errors.New("authstack: invalid stack")
)

// DenialError describes a layer decision. It never crosses a layer boundary:
// a nested stack reports failure to its parent as a plain item failure, and
// the parent decides what that failure means.
type DenialError struct {
	// Stack is the name of the layer that made the decision.
	Stack string
	// User is the account the layer was deciding for.
	User string
	// Reason is one of the sentinel errors above (or a context error).
	Reason error
	// Causes holds the item-level errors that led to the decision.
	Causes []error
}

// Error implements error.
func (e *DenialError) Error() string {
	var b strings.Builder
	fmt.Fprintf(&b, "authstack: stack %q denied authentication for user %q: %v", e.Stack, e.User, e.Reason)
	if len(e.Causes) > 0 {
		b.WriteString(" [")
		for i, cause := range e.Causes {
			if i > 0 {
				b.WriteString("; ")
			}
			b.WriteString(cause.Error())
		}
		b.WriteString("]")
	}
	return b.String()
}

// Unwrap exposes the reason and the underlying item errors to errors.Is/As.
func (e *DenialError) Unwrap() []error {
	out := make([]error, 0, len(e.Causes)+1)
	out = append(out, e.Reason)
	out = append(out, e.Causes...)
	return out
}

// Outcome is the recorded result of one stack item.
type Outcome int

const (
	// NotRun marks an item that was never evaluated, either because an
	// earlier item ended the layer or because a prompt made it impossible.
	NotRun Outcome = iota
	// Succeeded marks an item whose provider (or sub-stack) returned success.
	Succeeded
	// Failed marks an item that failed, panicked, was unavailable or whose
	// provider asked for interaction that could not be delivered.
	Failed
)

// String returns a stable name for the outcome.
func (o Outcome) String() string {
	switch o {
	case NotRun:
		return "not-run"
	case Succeeded:
		return "success"
	case Failed:
		return "failure"
	default:
		return "unknown"
	}
}

// PromptEvent records one interaction request. It is appended when the
// provider asks, before the responder is consulted, so a prompt that could
// not be answered is still visible in the trace.
type PromptEvent struct {
	// Provider is the provider that asked the question.
	Provider string
	// Text is the question text.
	Text string
	// Secret reports whether the answer should be masked on input.
	Secret bool
	// Answered reports whether a responder supplied an answer.
	Answered bool
	// Err is the interaction error, if any, as text.
	Err string
}

// ItemTrace records what happened to a single item of one layer.
type ItemTrace struct {
	// Index is the zero-based position of the item inside its layer.
	Index int
	// Name is the provider name, or the sub-stack label.
	Name string
	// Control is the control flag the item was evaluated with.
	Control Control
	// Kind is "provider" or "stack".
	Kind string
	// Outcome is what the evaluation produced.
	Outcome Outcome
	// Err is the item failure text, empty on success.
	Err string
	// Prompts lists the interactions this item actually produced, in order.
	Prompts []*PromptEvent
	// Sub is the nested layer trace when Kind is "stack".
	Sub *LayerTrace
	// Halt explains why this item ended its layer, if it did; otherwise it
	// explains why the item was skipped.
	Halt string
}

// LayerTrace records one layer evaluation, including skipped items.
type LayerTrace struct {
	// Name is the stack name.
	Name string
	// Items holds exactly one entry per declared item, in declaration order.
	Items []ItemTrace
	// Passed is the layer decision.
	Passed bool
	// Successes counts items that returned success, including successes that
	// did not end the layer.
	Successes int
	// RequiredFailed reports whether any required item failed.
	RequiredFailed bool
	// Reason is a short human-readable explanation of the decision.
	Reason string
}

// Result is the outcome of running a stack.
type Result struct {
	// Stack is the top-level stack name.
	Stack string
	// Passed is true only when the top-level layer passed.
	Passed bool
	// Err is nil when Passed is true; otherwise it is a *DenialError.
	Err error
	// Trace is the full, nested record of the evaluation.
	Trace *LayerTrace
	// Prompts is every interaction request, flattened in chronological order
	// across all nesting levels. Slice entries point at the same events as
	// the per-item traces.
	Prompts []*PromptEvent
}

// PromptTexts returns the question texts of every prompt that was actually
// shown, in chronological order. It is a convenience for tests and reports.
func (res *Result) PromptTexts() []string {
	if res == nil {
		return nil
	}
	out := make([]string, 0, len(res.Prompts))
	for _, p := range res.Prompts {
		out = append(out, p.Text)
	}
	return out
}

// ItemTraceFor returns the first top-level item trace whose name matches, or
// nil. It does not descend into sub-stacks.
func (lt *LayerTrace) ItemTraceFor(name string) *ItemTrace {
	if lt == nil {
		return nil
	}
	for i := range lt.Items {
		if lt.Items[i].Name == name {
			return &lt.Items[i]
		}
	}
	return nil
}
