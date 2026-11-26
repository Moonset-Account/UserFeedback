// Package machine implements the pre-issuance validation state machine used by
// the download-key service when an employee rebinds their authentication
// device in the middle of a segmented transfer.
//
// The state machine is deliberately defensive: every input that originates
// outside the trust boundary (receipts, segment numbers, binding versions) is
// re-validated here even though registration receipts are described as already
// verified by the registration subsystem. No network access, credential
// storage or key material is involved.
package machine

import (
	"errors"
	"fmt"
	"math"
	"sort"
	"sync"
)

// Decision classifies the outcome of a single validation call.
type Decision int

const (
	// Approved means the whole requested range is governed by the receipt's
	// binding and has been recorded as approved (or was already approved).
	Approved Decision = iota
	// PartialApproval means the request crosses a binding boundary. Only the
	// sub-range governed by the presented receipt is approved; the rest stays
	// untouched and the caller must retry it with the correct device.
	PartialApproval
	// Rejected means nothing in the request was approved. See Reason.
	Rejected
)

// ReasonCode is a stable, machine-readable failure explanation returned to the
// login module. Rejected and PartialApproval results always carry one.
type ReasonCode string

const (
	ReasonOK                        ReasonCode = "OK"
	ReasonInvalidSegmentRange       ReasonCode = "INVALID_SEGMENT_RANGE"
	ReasonEmployeeMismatch          ReasonCode = "EMPLOYEE_MISMATCH"
	ReasonUnknownBindingVersion     ReasonCode = "UNKNOWN_BINDING_VERSION"
	ReasonDeviceMismatch            ReasonCode = "DEVICE_MISMATCH"
	ReasonBindingHasNoGovernedRange ReasonCode = "BINDING_HAS_NO_GOVERNED_RANGE"
	ReasonOldBindingAfterCutover    ReasonCode = "OLD_BINDING_AFTER_CUTOVER"
	ReasonStaleBindingBeforeCutover ReasonCode = "STALE_BINDING_BEFORE_CUTOVER"
	ReasonNewBindingGap             ReasonCode = "NEW_BINDING_GAP"
	ReasonCutoverConfirmedImmutable ReasonCode = "CUTOVER_CONFIRMED_IMMUTABLE"
	ReasonRebindEmployeeMismatch    ReasonCode = "REBIND_EMPLOYEE_MISMATCH"
	ReasonRebindUnknownBinding      ReasonCode = "REBIND_UNKNOWN_BINDING_VERSION"
	ReasonRebindVersionNotCurrent   ReasonCode = "REBIND_VERSION_NOT_CURRENT"
	ReasonRebindDeviceMismatch      ReasonCode = "REBIND_DEVICE_MISMATCH"
)

// Sentinel errors returned for malformed API usage (as opposed to a valid
// request that fails business validation, which is reported via Result).
var (
	ErrCutoverNotEstablished = errors.New("machine: no rebind has been submitted, cutover is not established")
	ErrNoBindings            = errors.New("machine: at least one binding is required")
)

// Binding is one registered device binding in version order. Version numbers
// are assigned by the registration subsystem and must be unique within a
// transfer. Only the binding with the highest version is current.
type Binding struct {
	Version  int
	DeviceID string
}

// Receipt is the already-authenticated registration receipt presented with a
// segment request. The state machine still checks that it belongs to this
// transfer and matches the binding version it claims.
type Receipt struct {
	EmployeeID     string
	BindingVersion int
	DeviceID       string
}

// SegmentRange is a closed, inclusive interval of 1-based transfer segment
// numbers.
type SegmentRange struct {
	First int
	Last  int
}

// Valid reports whether the range is well-formed.
func (r SegmentRange) Valid() bool {
	return r.First >= 1 && r.Last >= r.First
}

// Overlaps reports whether two closed intervals share at least one segment.
func (r SegmentRange) Overlaps(other SegmentRange) bool {
	return r.First <= other.Last && other.First <= r.Last
}

// Intersect returns the closed intersection of two intervals.
func (r SegmentRange) Intersect(other SegmentRange) SegmentRange {
	first := r.First
	if other.First > first {
		first = other.First
	}
	last := r.Last
	if other.Last < last {
		last = other.Last
	}
	return SegmentRange{First: first, Last: last}
}

func (r SegmentRange) String() string {
	return fmt.Sprintf("[%d..%d]", r.First, r.Last)
}

// Result is the full answer handed back to the login module.
type Result struct {
	// Decision classifies the call.
	Decision Decision
	// Reason is OK for full approval, otherwise a stable failure code.
	Reason ReasonCode
	// Message is a human-readable explanation, always populated when Reason
	// is not OK. It never contains key material or credentials.
	Message string
	// Requested is the range the caller asked for.
	Requested SegmentRange
	// Approvable is the contiguous sub-range that may be approved under the
	// presented receipt. Empty for Rejected.
	Approvable SegmentRange
	// NewlyApproved lists segments transitioned to approved by this call.
	// Retrying an already approved segment does not reappear here.
	NewlyApproved []int
	// MatchedVersion is the binding version that actually matched the
	// receipt, or 0 when no binding matched.
	MatchedVersion int
	// MatchedDeviceID is the device id recorded for MatchedVersion.
	MatchedDeviceID string
	// Cutover is the first segment governed by the new device binding once a
	// rebind has been submitted; 0 otherwise.
	Cutover int
}

// RebindOutcome describes what happened when a rebind was submitted.
type RebindOutcome struct {
	// AlreadyLocked is true when an earlier rebind had already fixed the
	// cutover. In that case Cutover stays unchanged and Reason explains it.
	AlreadyLocked bool
	// Cutover is the first unapproved segment at the moment the (first)
	// rebind took effect.
	Cutover int
	// Reason is ReasonOK on success or the immutable-cutover code otherwise.
	Reason  ReasonCode
	Message string
}

// Machine is the concurrency-safe validation state machine for one transfer.
//
// Invariants maintained across all calls:
//
//   - approved segments form the contiguous prefix 1..HighWater;
//   - once a cutover exists it never moves, regardless of retries;
//   - segments < cutover can only be approved with the old binding receipt;
//   - segments >= cutover can only be approved with the current binding;
//   - the current binding must approve segments without skipping a gap.
type Machine struct {
	mu sync.RWMutex

	employeeID string
	bindings   map[int]Binding // version -> binding
	versions   []int           // sorted binding versions
	current    int             // highest binding version

	approved  map[int]struct{}
	highWater int // largest n such that 1..n are all approved

	cutover       int // first segment governed by the new binding; 0 = none
	cutoverLocked bool
}

// New builds a state machine for one employee transfer. bindings must contain
// at least one entry; the entry with the highest version is the current
// binding. A machine with a single binding models the pre-rebind state.
func New(employeeID string, bindings []Binding) (*Machine, error) {
	if len(bindings) == 0 {
		return nil, ErrNoBindings
	}
	byVersion := make(map[int]Binding, len(bindings))
	versions := make([]int, 0, len(bindings))
	current := math.MinInt
	for _, b := range bindings {
		if _, dup := byVersion[b.Version]; dup {
			return nil, fmt.Errorf("machine: duplicate binding version %d", b.Version)
		}
		byVersion[b.Version] = b
		versions = append(versions, b.Version)
		if b.Version > current {
			current = b.Version
		}
	}
	sort.Ints(versions)
	return &Machine{
		employeeID: employeeID,
		bindings:   byVersion,
		versions:   versions,
		current:    current,
		approved:   make(map[int]struct{}),
	}, nil
}

// CurrentVersion returns the highest registered binding version.
func (m *Machine) CurrentVersion() int {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.current
}

// Cutover returns the confirmed cutover segment. It returns
// ErrCutoverNotEstablished while no rebind has been submitted.
func (m *Machine) Cutover() (int, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	if m.cutover == 0 {
		return 0, ErrCutoverNotEstablished
	}
	return m.cutover, nil
}

// CutoverLocked reports whether the cutover has been fixed by a rebind
// submission. A locked cutover can never change afterwards.
func (m *Machine) CutoverLocked() bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.cutoverLocked
}

// HighWater returns the number of contiguous approved segments 1..n.
func (m *Machine) HighWater() int {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.highWater
}

// IsApproved reports whether segment n has been approved.
func (m *Machine) IsApproved(n int) bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	_, ok := m.approved[n]
	return ok
}

// FirstUnapproved returns the smallest segment number that has not been
// approved. Because approvals are prefix-contiguous it is always HighWater+1.
func (m *Machine) FirstUnapproved() int {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.firstUnapprovedLocked()
}

func (m *Machine) firstUnapprovedLocked() int {
	return m.highWater + 1
}

// SubmitRebind records a device rebind. The rebind takes effect at the first
// segment that is still unapproved when the submission is processed. If a
// rebind already fixed the cutover, the submission is refused and the
// existing cutover is returned: upload retries and duplicate submissions can
// never move a confirmed cutover.
//
// The receipt must identify the employee, must reference the current binding
// version and must carry that binding's device id.
func (m *Machine) SubmitRebind(receipt Receipt) RebindOutcome {
	m.mu.Lock()
	defer m.mu.Unlock()

	if receipt.EmployeeID != m.employeeID {
		return RebindOutcome{
			Cutover: m.cutover,
			Reason:  ReasonRebindEmployeeMismatch,
			Message: fmt.Sprintf("receipt employee %q does not own transfer %q", receipt.EmployeeID, m.employeeID),
		}
	}
	binding, ok := m.bindings[receipt.BindingVersion]
	if !ok {
		return RebindOutcome{
			Cutover: m.cutover,
			Reason:  ReasonRebindUnknownBinding,
			Message: fmt.Sprintf("binding version %d is not registered for this transfer", receipt.BindingVersion),
		}
	}
	if receipt.BindingVersion != m.current {
		return RebindOutcome{
			Cutover: m.cutover,
			Reason:  ReasonRebindVersionNotCurrent,
			Message: fmt.Sprintf("rebind requires current binding v%d, receipt presents v%d", m.current, receipt.BindingVersion),
		}
	}
	if receipt.DeviceID != binding.DeviceID {
		return RebindOutcome{
			Cutover: m.cutover,
			Reason:  ReasonRebindDeviceMismatch,
			Message: fmt.Sprintf("receipt device %q does not match binding v%d device %q", receipt.DeviceID, binding.Version, binding.DeviceID),
		}
	}

	if m.cutoverLocked {
		return RebindOutcome{
			AlreadyLocked: true,
			Cutover:       m.cutover,
			Reason:        ReasonCutoverConfirmedImmutable,
			Message: fmt.Sprintf(
				"cutover already confirmed at segment %d; rebind retries cannot move it",
				m.cutover,
			),
		}
	}

	m.cutover = m.firstUnapprovedLocked()
	m.cutoverLocked = true
	return RebindOutcome{
		Cutover: m.cutover,
		Reason:  ReasonOK,
		Message: fmt.Sprintf("rebind to device %q takes effect at first unapproved segment %d", binding.DeviceID, m.cutover),
	}
}

// Validate checks a pre-issuance request carrying an authenticated receipt
// for the closed segment range [first, last]. On success the governed,
// contiguous sub-range is atomically recorded as approved and the full
// decision (approvable range, matched binding, failure reason) is returned
// for the login module.
func (m *Machine) Validate(receipt Receipt, requested SegmentRange) Result {
	m.mu.Lock()
	defer m.mu.Unlock()

	result := Result{Requested: requested, Cutover: m.cutover}
	reject := func(reason ReasonCode, msg string) Result {
		result.Decision = Rejected
		result.Reason = reason
		result.Message = msg
		return result
	}

	if !requested.Valid() {
		return reject(
			ReasonInvalidSegmentRange,
			fmt.Sprintf("segment range %s is invalid: segments are 1-based closed intervals", requested),
		)
	}
	if receipt.EmployeeID != m.employeeID {
		return reject(
			ReasonEmployeeMismatch,
			fmt.Sprintf("receipt employee %q does not own transfer %q", receipt.EmployeeID, m.employeeID),
		)
	}
	binding, ok := m.bindings[receipt.BindingVersion]
	if !ok {
		return reject(
			ReasonUnknownBindingVersion,
			fmt.Sprintf("binding version %d is not registered for this transfer", receipt.BindingVersion),
		)
	}
	result.MatchedVersion = binding.Version
	result.MatchedDeviceID = binding.DeviceID
	if receipt.DeviceID != binding.DeviceID {
		return reject(
			ReasonDeviceMismatch,
			fmt.Sprintf("receipt device %q does not match device %q registered for binding v%d", receipt.DeviceID, binding.DeviceID, binding.Version),
		)
	}

	allowed, hasRange := m.allowedIntervalLocked(binding.Version)
	if !hasRange {
		return reject(
			ReasonBindingHasNoGovernedRange,
			fmt.Sprintf("binding v%d governs no segments for this transfer", binding.Version),
		)
	}

	// Reject outright when the request lies entirely outside this binding's
	// governed interval, explaining which side of the cutover it landed on.
	if !requested.Overlaps(allowed) {
		if m.cutover != 0 && binding.Version < m.current {
			return reject(
				ReasonOldBindingAfterCutover,
				fmt.Sprintf("old binding v%d cannot approve %s: segments >= %d belong to the new device", binding.Version, requested, m.cutover),
			)
		}
		return reject(
			ReasonStaleBindingBeforeCutover,
			fmt.Sprintf("binding v%d cannot approve %s: segments < %d still belong to the previous device", binding.Version, requested, m.cutover),
		)
	}

	approvable := requested.Intersect(allowed)
	// crossesBoundary means part of the request can never be served by this
	// receipt's binding and must be presented with another device.
	crossesBoundary := approvable != requested

	// The active binding must continue from the first unapproved segment
	// without skipping a gap. Retired bindings only re-serve already handled
	// prefix segments, so they have no contiguity requirement. newWork is
	// the unapproved part of the governed range; approvable keeps the full
	// governed range so callers see everything the receipt can approve.
	newWork := approvable
	if binding.Version == m.activeVersionLocked() {
		firstUnapproved := m.firstUnapprovedLocked()
		switch {
		case newWork.First > firstUnapproved:
			return reject(
				ReasonNewBindingGap,
				fmt.Sprintf(
					"binding v%d cannot approve %s: segment %d must be approved first (contiguous prefix required)",
					binding.Version, newWork, firstUnapproved,
				),
			)
		case newWork.Last < firstUnapproved:
			// The whole governed sub-range was already approved: this is an
			// idempotent retry and stays Approved below.
			newWork = SegmentRange{}
		default:
			// Drop the already-approved prefix so only new work is recorded.
			newWork.First = firstUnapproved
		}
	}

	// Mark every newly governed segment, distinguishing them from idempotent
	// retries. An active-binding request whose governed range was already
	// fully approved is reported as an idempotent Approved.
	idempotentRetry := false
	if !newWork.Valid() {
		idempotentRetry = true
	} else {
		for n := newWork.First; n <= newWork.Last; n++ {
			if _, done := m.approved[n]; !done {
				m.approved[n] = struct{}{}
				result.NewlyApproved = append(result.NewlyApproved, n)
			}
		}
		if newWork.Last > m.highWater {
			// Contiguity is guaranteed for the active binding and retired
			// bindings only touch the prefix, so extending is safe.
			m.highWater = newWork.Last
		}
	}

	result.Approvable = approvable
	switch {
	case idempotentRetry:
		result.Decision = Approved
		result.Reason = ReasonOK
		result.Message = fmt.Sprintf("range %s was already approved under binding v%d (device %q); idempotent retry", requested, binding.Version, binding.DeviceID)
	case !crossesBoundary:
		result.Decision = Approved
		result.Reason = ReasonOK
		result.Message = fmt.Sprintf("range %s approved under binding v%d (device %q); newly approved %s", requested, binding.Version, binding.DeviceID, approvable)
	default:
		result.Decision = PartialApproval
		result.Reason = m.partialReasonLocked(binding.Version)
		result.Message = fmt.Sprintf(
			"only %s of requested %s is governed by binding v%d (device %q); present the other device's receipt for the remaining segments",
			approvable, requested, binding.Version, binding.DeviceID,
		)
	}
	return result
}

// activeVersionLocked returns the binding version that currently governs new
// segments: the original (and only) binding before any rebind, or the highest
// version afterwards.
func (m *Machine) activeVersionLocked() int {
	if m.cutover == 0 {
		return m.versions[0]
	}
	return m.current
}

// allowedIntervalLocked returns the closed segment interval governed by the
// given binding version. The second result is false when the binding governs
// no segments at all. A newer binding that has not taken effect yet (no
// rebind submitted) governs nothing.
func (m *Machine) allowedIntervalLocked(version int) (SegmentRange, bool) {
	if m.cutover == 0 {
		if version != m.versions[0] {
			return SegmentRange{}, false
		}
		return SegmentRange{First: 1, Last: math.MaxInt}, true
	}
	if version == m.current {
		return SegmentRange{First: m.cutover, Last: math.MaxInt}, true
	}
	if m.cutover == 1 {
		return SegmentRange{}, false
	}
	return SegmentRange{First: 1, Last: m.cutover - 1}, true
}

func (m *Machine) partialReasonLocked(version int) ReasonCode {
	if version < m.current {
		return ReasonOldBindingAfterCutover
	}
	return ReasonStaleBindingBeforeCutover
}

func (d Decision) String() string {
	switch d {
	case Approved:
		return "APPROVED"
	case PartialApproval:
		return "PARTIAL_APPROVAL"
	case Rejected:
		return "REJECTED"
	default:
		return "UNKNOWN"
	}
}

func (c ReasonCode) String() string { return string(c) }
