package causalcodec

import (
	"errors"
	"fmt"
	"strings"
)

var (
	// ErrInvalidReport is returned when referential or required-field validation fails.
	ErrInvalidReport = errors.New("invalid causal report")
	// ErrInvalidAnchorRef means an anchor reference does not identify an exact context.
	ErrInvalidAnchorRef = errors.New("invalid anchor reference")
	// ErrAnchorNotFound means no exact ID/version/digest anchor is available.
	ErrAnchorNotFound = errors.New("context anchor not available")
	// ErrAnchorDigestMismatch means a same name/version anchor exists but its fingerprint differs.
	ErrAnchorDigestMismatch = errors.New("context anchor digest mismatch")
	// ErrMalformedPacket means the binary framing or checksum is invalid.
	ErrMalformedPacket = errors.New("malformed causal packet")
	// ErrBudgetExceeded means even the minimal recoverable packet is larger than MaxBytes.
	ErrBudgetExceeded = errors.New("causal packet budget exceeded")
	// ErrGapNotFound means Extend was given an unknown or already healed gap.
	ErrGapNotFound = errors.New("causal gap not found")
)

// BudgetError reports the fixed-size limit and the smallest size observed.
type BudgetError struct {
	MaxBytes     int
	ObservedSize int
	MinimumBytes int
}

func (e *BudgetError) Error() string {
	return fmt.Sprintf("%s: max=%d observed=%d minimum_required=%d",
		ErrBudgetExceeded, e.MaxBytes, e.ObservedSize, e.MinimumBytes)
}

func (e *BudgetError) Is(target error) bool { return target == ErrBudgetExceeded }

// MissingAnchorError includes same-name anchors that were deliberately not
// used. This makes name-collision behavior observable.
type MissingAnchorError struct {
	Ref               AnchorRef
	AvailableSameName []AnchorRef
	DigestMismatch    bool
}

func (e *MissingAnchorError) Error() string {
	var b strings.Builder
	if e.DigestMismatch {
		fmt.Fprintf(&b, "%s: requested %s", ErrAnchorDigestMismatch, e.Ref)
	} else {
		fmt.Fprintf(&b, "%s: %s", ErrAnchorNotFound, e.Ref)
	}
	if len(e.AvailableSameName) > 0 {
		refs := make([]string, 0, len(e.AvailableSameName))
		for _, r := range e.AvailableSameName {
			refs = append(refs, r.String())
		}
		b.WriteString("; same-name local anchors not used: ")
		b.WriteString(strings.Join(refs, ", "))
	}
	return b.String()
}

func (e *MissingAnchorError) Is(target error) bool {
	if e.DigestMismatch && target == ErrAnchorDigestMismatch {
		return true
	}
	return target == ErrAnchorNotFound
}

// BranchError associates a decode/resolve failure with one causal path.
type BranchError struct {
	PathID string
	Stage  string
	Err    error
}

func (e *BranchError) Error() string {
	return fmt.Sprintf("path %q stage %q: %v", e.PathID, e.Stage, e.Err)
}

func (e *BranchError) Unwrap() error { return e.Err }

// PartialDecodeError is returned together with a non-nil Report when some
// independently framed branches could still be recovered.
type PartialDecodeError struct {
	Errors []error
}

func (e *PartialDecodeError) Error() string {
	parts := make([]string, 0, len(e.Errors))
	for _, err := range e.Errors {
		parts = append(parts, err.Error())
	}
	return "partial causal decode: " + strings.Join(parts, "; ")
}

func (e *PartialDecodeError) Unwrap() []error { return e.Errors }

func malformed(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrMalformedPacket, fmt.Sprintf(format, args...))
}
