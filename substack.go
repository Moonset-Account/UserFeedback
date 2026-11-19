package authstack

import "fmt"

// SubStackError is the item-level failure a parent layer sees when a nested
// stack denied authentication.
//
// It deliberately does not implement Unwrap: the inner layer's sentinel reasons
// (ErrRequisiteFailure, ErrNoSuccess, ...) describe *that* layer's decision and
// must not be mistaken for the parent's. The parent decides what the single
// "the sub-stack failed" result means for its own control flag.
//
// Callers that need the details can use errors.As to obtain a *SubStackError
// and inspect Cause, or walk the parent trace down into the nested LayerTrace.
type SubStackError struct {
	// Stack is the nested stack's name.
	Stack string
	// Cause is the nested layer's denial.
	Cause error
}

// Error implements error.
func (e *SubStackError) Error() string {
	if e == nil {
		return "authstack: sub-stack failed"
	}
	if e.Cause == nil {
		return fmt.Sprintf("authstack: sub-stack %q failed", e.Stack)
	}
	return fmt.Sprintf("authstack: sub-stack %q failed: %v", e.Stack, e.Cause)
}

// Denial returns the nested layer's denial error, if the cause is one.
func (e *SubStackError) Denial() (*DenialError, bool) {
	if e == nil {
		return nil, false
	}
	d, ok := e.Cause.(*DenialError)
	return d, ok
}
