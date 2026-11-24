// Package blockcw implements a file-backed block-device simulator with
// atomic group compare-and-write and durable request idempotency.
package blockcw

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
)

// Sentinel errors allow callers to use errors.Is for broad failure classes.
var (
	// ErrInvalidRequest is wrapped by every request-shape validation error.
	ErrInvalidRequest = errors.New("blockcw: invalid request")

	// ErrVolumeNotFound means the named volume does not exist.
	ErrVolumeNotFound = errors.New("blockcw: volume not found")

	// ErrVolumeExists means a volume could not be created because its ID is taken.
	ErrVolumeExists = errors.New("blockcw: volume already exists")

	// ErrBlockOutOfRange means a block index is outside the volume.
	ErrBlockOutOfRange = errors.New("blockcw: block index out of range")

	// ErrRequestIDReused means a request ID was first used with different content.
	ErrRequestIDReused = errors.New("blockcw: request ID reused with different content")

	// ErrCorruptState means the durable state file exists but cannot be interpreted.
	ErrCorruptState = errors.New("blockcw: corrupt durable state")
)

// Decision is the original, immutable outcome of a compare-and-write request.
type Decision string

const (
	// DecisionCommitted means every comparison matched and all replacements were published.
	DecisionCommitted Decision = "COMMITTED"

	// DecisionConflict means at least one comparison failed and no block was changed.
	DecisionConflict Decision = "CONFLICT"
)

// Operation is one block-level compare-and-replace operation.
type Operation struct {
	// Index is the zero-based block number.
	Index int `json:"index"`

	// Expected must exactly equal the current complete block.
	Expected []byte `json:"expected"`

	// Replacement is the complete block published only if the whole group commits.
	Replacement []byte `json:"replacement"`
}

// Request is an atomic group compare-and-write.
type Request struct {
	// VolumeID selects the volume.
	VolumeID string `json:"volume_id"`

	// RequestID is the durable idempotency key. It is currently global across
	// volumes in one state file; VolumeID and all operations are part of its
	// fingerprint.
	RequestID string `json:"request_id"`

	// Operations are the blocks compared and, if all comparisons pass, replaced.
	Operations []Operation `json:"operations"`
}

// Mismatch describes one failed block comparison and preserves the actual bytes
// observed when the original decision was made.
type Mismatch struct {
	Index    int    `json:"index"`
	Expected []byte `json:"expected"`
	Actual   []byte `json:"actual"`
}

// Result is a committed or conflict decision.
type Result struct {
	VolumeID    string     `json:"volume_id"`
	RequestID   string     `json:"request_id"`
	Decision    Decision   `json:"decision"`
	Mismatches  []Mismatch `json:"mismatches,omitempty"`
	Replayed    bool       `json:"replayed"`
	CompletedAt time.Time  `json:"completed_at"`
}

// Committed reports whether the decision was DecisionCommitted.
func (r *Result) Committed() bool {
	return r != nil && r.Decision == DecisionCommitted
}

// RequestReuseError gives details when an existing request ID is reused with
// different canonical content.
type RequestReuseError struct {
	VolumeID            string
	RequestID           string
	ExistingFingerprint string
	ProvidedFingerprint string
}

func (e *RequestReuseError) Error() string {
	return fmt.Sprintf(
		"blockcw: request ID %q already has a different request body (existing=%s, provided=%s)",
		e.RequestID, shortFingerprint(e.ExistingFingerprint), shortFingerprint(e.ProvidedFingerprint),
	)
}

// Unwrap enables errors.Is(err, ErrRequestIDReused).
func (e *RequestReuseError) Unwrap() error { return ErrRequestIDReused }

func shortFingerprint(fp string) string {
	if len(fp) <= 12 {
		return fp
	}
	return fp[:12]
}

// Validate checks request fields that can be checked without loading a volume.
// Block length and range checks additionally require durable volume metadata.
func (r Request) Validate() error {
	if strings.TrimSpace(r.VolumeID) == "" {
		return fmt.Errorf("%w: volume id is empty", ErrInvalidRequest)
	}
	if strings.TrimSpace(r.RequestID) == "" {
		return fmt.Errorf("%w: request id is empty", ErrInvalidRequest)
	}
	if len(r.Operations) == 0 {
		return fmt.Errorf("%w: at least one operation is required", ErrInvalidRequest)
	}

	seen := make(map[int]struct{}, len(r.Operations))
	for i, op := range r.Operations {
		if op.Index < 0 {
			return fmt.Errorf("%w: operations[%d] has negative block index %d", ErrInvalidRequest, i, op.Index)
		}
		if _, duplicated := seen[op.Index]; duplicated {
			return fmt.Errorf("%w: block %d appears more than once", ErrInvalidRequest, op.Index)
		}
		seen[op.Index] = struct{}{}

		if len(op.Expected) == 0 {
			return fmt.Errorf("%w: block %d expected bytes must not be empty", ErrInvalidRequest, op.Index)
		}
		if len(op.Replacement) == 0 {
			return fmt.Errorf("%w: block %d replacement bytes must not be empty", ErrInvalidRequest, op.Index)
		}
		if len(op.Expected) != len(op.Replacement) {
			return fmt.Errorf(
				"%w: block %d expected length %d does not match replacement length %d",
				ErrInvalidRequest, op.Index, len(op.Expected), len(op.Replacement),
			)
		}
	}
	return nil
}

// canonicalOperations returns a sorted copy so operation order is not part of
// request identity. Duplicate indices are rejected by Validate.
func (r Request) canonicalOperations() []Operation {
	ops := append([]Operation(nil), r.Operations...)
	sort.SliceStable(ops, func(i, j int) bool { return ops[i].Index < ops[j].Index })
	return ops
}

// Fingerprint returns a deterministic SHA-256 fingerprint of all request
// content except the request-ID map key itself. Length prefixes prevent
// concatenation collisions, and operations are canonically sorted by index.
func Fingerprint(r Request) string {
	h := sha256.New()

	writeBytes := func(b []byte) {
		var length [8]byte
		binary.BigEndian.PutUint64(length[:], uint64(len(b)))
		_, _ = h.Write(length[:])
		_, _ = h.Write(b)
	}
	writeString := func(s string) { writeBytes([]byte(s)) }

	var version [8]byte
	binary.BigEndian.PutUint64(version[:], 1)
	_, _ = h.Write(version[:])

	writeString(r.VolumeID)
	writeString(r.RequestID)

	ops := r.canonicalOperations()
	var count [8]byte
	binary.BigEndian.PutUint64(count[:], uint64(len(ops)))
	_, _ = h.Write(count[:])

	for _, op := range ops {
		var index [8]byte
		binary.BigEndian.PutUint64(index[:], uint64(op.Index))
		_, _ = h.Write(index[:])
		writeBytes(op.Expected)
		writeBytes(op.Replacement)
	}

	return hex.EncodeToString(h.Sum(nil))
}

func cloneBytes(b []byte) []byte {
	if b == nil {
		return nil
	}
	out := make([]byte, len(b))
	copy(out, b)
	return out
}

func cloneMismatches(in []Mismatch) []Mismatch {
	if in == nil {
		return nil
	}
	out := make([]Mismatch, len(in))
	for i := range in {
		out[i] = Mismatch{
			Index:    in[i].Index,
			Expected: cloneBytes(in[i].Expected),
			Actual:   cloneBytes(in[i].Actual),
		}
	}
	return out
}
