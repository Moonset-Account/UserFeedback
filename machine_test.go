package machine

import (
	"errors"
	"fmt"
	"math/rand"
	"sync"
	"testing"
)

// Test fixtures: one employee, two binding versions.
const employee = "emp-42"

func testMachine(t *testing.T) *Machine {
	t.Helper()
	m, err := New(employee, []Binding{
		{Version: 1, DeviceID: "device-old"},
		{Version: 2, DeviceID: "device-new"},
	})
	if err != nil {
		t.Fatalf("New returned unexpected error: %v", err)
	}
	return m
}

func oldReceipt() Receipt {
	return Receipt{EmployeeID: employee, BindingVersion: 1, DeviceID: "device-old"}
}

func newReceipt() Receipt {
	return Receipt{EmployeeID: employee, BindingVersion: 2, DeviceID: "device-new"}
}

func seg(first, last int) SegmentRange { return SegmentRange{First: first, Last: last} }

func assertApproved(t *testing.T, r Result, want SegmentRange) {
	t.Helper()
	if r.Decision != Approved {
		t.Fatalf("Decision = %s, want APPROVED; reason=%s msg=%q", r.Decision, r.Reason, r.Message)
	}
	if r.Reason != ReasonOK {
		t.Fatalf("Reason = %s, want OK", r.Reason)
	}
	if r.Approvable != want {
		t.Fatalf("Approvable = %s, want %s", r.Approvable, want)
	}
}

func assertRejected(t *testing.T, r Result, want ReasonCode) {
	t.Helper()
	if r.Decision != Rejected {
		t.Fatalf("Decision = %s, want REJECTED", r.Decision)
	}
	if r.Reason != want {
		t.Fatalf("Reason = %s, want %s; msg=%q", r.Reason, want, r.Message)
	}
	if r.Message == "" {
		t.Fatalf("rejected result must carry a human-readable message")
	}
	if len(r.NewlyApproved) != 0 {
		t.Fatalf("rejected request must not approve any segment, got %v", r.NewlyApproved)
	}
}

// TestNoRebindFlow covers the normal single-device flow before any rebind.
func TestNoRebindFlow(t *testing.T) {
	m := testMachine(t)

	if _, err := m.Cutover(); !errors.Is(err, ErrCutoverNotEstablished) {
		t.Fatalf("Cutover before rebind error = %v, want ErrCutoverNotEstablished", err)
	}
	if m.CutoverLocked() {
		t.Fatalf("cutover must not be locked before a rebind submission")
	}

	// A not-yet-effective newer binding governs nothing.
	assertRejected(t, m.Validate(newReceipt(), seg(1, 1)), ReasonBindingHasNoGovernedRange)

	r := m.Validate(oldReceipt(), seg(1, 2))
	assertApproved(t, r, seg(1, 2))
	if len(r.NewlyApproved) != 2 || r.NewlyApproved[0] != 1 || r.NewlyApproved[1] != 2 {
		t.Fatalf("NewlyApproved = %v, want [1 2]", r.NewlyApproved)
	}
	if m.HighWater() != 2 || m.FirstUnapproved() != 3 {
		t.Fatalf("HighWater=%d FirstUnapproved=%d, want 2 and 3", m.HighWater(), m.FirstUnapproved())
	}
	if r.MatchedVersion != 1 || r.MatchedDeviceID != "device-old" {
		t.Fatalf("matched binding = v%d/%q, want v1/device-old", r.MatchedVersion, r.MatchedDeviceID)
	}

	// Out-of-order gap attempt is refused while preserving state.
	assertRejected(t, m.Validate(oldReceipt(), seg(4, 4)), ReasonNewBindingGap)
	if m.HighWater() != 2 || m.IsApproved(4) {
		t.Fatalf("rejected gap request mutated state: HighWater=%d approved4=%v", m.HighWater(), m.IsApproved(4))
	}

	// Idempotent retry of an approved range.
	r = m.Validate(oldReceipt(), seg(1, 2))
	assertApproved(t, r, seg(1, 2))
	if len(r.NewlyApproved) != 0 {
		t.Fatalf("retry NewlyApproved = %v, want empty", r.NewlyApproved)
	}
}

// TestRebindCutoverAndInterleaving is the headline scenario from the task:
// an out-of-order old-version retry interleaved with new-version continuation
// must not move the confirmed cutover.
func TestRebindCutoverAndInterleaving(t *testing.T) {
	m := testMachine(t)

	// Segments 1..3 approved on the old device; 4 and 5 were already in
	// flight when the employee rebinds.
	m.Validate(oldReceipt(), seg(1, 3))

	outcome := m.SubmitRebind(newReceipt())
	if outcome.Reason != ReasonOK || outcome.Cutover != 4 {
		t.Fatalf("SubmitRebind = %+v, want OK cutover 4", outcome)
	}
	if !m.CutoverLocked() {
		t.Fatalf("cutover must be locked after the first rebind")
	}
	cut, err := m.Cutover()
	if err != nil || cut != 4 {
		t.Fatalf("Cutover() = (%d, %v), want (4, nil)", cut, err)
	}

	// Old device's in-flight retry of segment 4 arrives late and is refused:
	// the cutover already assigned segment 4 to the new device.
	assertRejected(t, m.Validate(oldReceipt(), seg(4, 4)), ReasonOldBindingAfterCutover)

	// The new device must perform the continuation; starting at cutover 4.
	r := m.Validate(newReceipt(), seg(4, 5))
	assertApproved(t, r, seg(4, 5))
	if r.MatchedVersion != 2 || r.MatchedDeviceID != "device-new" {
		t.Fatalf("matched binding = v%d/%q, want v2/device-new", r.MatchedVersion, r.MatchedDeviceID)
	}
	if r.Cutover != 4 {
		t.Fatalf("result Cutover = %d, want 4", r.Cutover)
	}
	if m.HighWater() != 5 {
		t.Fatalf("HighWater = %d, want 5", m.HighWater())
	}

	// Interleaved: an older segment (2..3) is re-sent with the ORIGINAL
	// receipt. It remains serviceable on the old binding and must not change
	// anything.
	before := m.HighWater()
	r = m.Validate(oldReceipt(), seg(2, 3))
	assertApproved(t, r, seg(2, 3))
	if len(r.NewlyApproved) != 0 {
		t.Fatalf("old retry NewlyApproved = %v, want empty (idempotent)", r.NewlyApproved)
	}
	if m.HighWater() != before {
		t.Fatalf("old retry moved HighWater from %d to %d", before, m.HighWater())
	}
	cut, _ = m.Cutover()
	if cut != 4 {
		t.Fatalf("cutover moved to %d, want 4", cut)
	}

	// New device retry of already continued segments is idempotent.
	r = m.Validate(newReceipt(), seg(4, 5))
	assertApproved(t, r, seg(4, 5))
	if len(r.NewlyApproved) != 0 {
		t.Fatalf("new retry NewlyApproved = %v, want empty", r.NewlyApproved)
	}

	// Old device can never reach new segments: a wide retry overlapping the
	// boundary is clipped to its old-side sub-range instead of approving it.
	r = m.Validate(oldReceipt(), seg(3, 6))
	if r.Decision != PartialApproval || r.Reason != ReasonOldBindingAfterCutover || r.Approvable != seg(3, 3) {
		t.Fatalf("wide old retry = %s/%s approvable=%s, want PARTIAL OLD_BINDING_AFTER_CUTOVER [3..3]", r.Decision, r.Reason, r.Approvable)
	}
	if len(r.NewlyApproved) != 0 || m.HighWater() != 5 {
		t.Fatalf("boundary retry must not approve new segments: newly=%v highWater=%d", r.NewlyApproved, m.HighWater())
	}
	// An old retry wholly beyond the cutover is a hard rejection.
	assertRejected(t, m.Validate(oldReceipt(), seg(6, 6)), ReasonOldBindingAfterCutover)
}

// TestPartialApprovals verifies range clipping on both sides of the cutover.
func TestPartialApprovals(t *testing.T) {
	t.Run("old binding crossing the cutover", func(t *testing.T) {
		m := testMachine(t)
		m.Validate(oldReceipt(), seg(1, 2))
		m.SubmitRebind(newReceipt()) // cutover = 3

		// Request 1..4 with the old receipt: only 1..2 are governable, and
		// those are already approved.
		r := m.Validate(oldReceipt(), seg(1, 4))
		if r.Decision != PartialApproval || r.Reason != ReasonOldBindingAfterCutover {
			t.Fatalf("got decision=%s reason=%s, want PARTIAL/OLD_BINDING_AFTER_CUTOVER", r.Decision, r.Reason)
		}
		if r.Approvable != seg(1, 2) {
			t.Fatalf("Approvable = %s, want [1..2]", r.Approvable)
		}
		if len(r.NewlyApproved) != 0 {
			t.Fatalf("NewlyApproved = %v, want empty", r.NewlyApproved)
		}
		if m.HighWater() != 2 {
			t.Fatalf("HighWater = %d, want 2", m.HighWater())
		}

		// Same shape but including unapproved old-side work.
		m2 := testMachine(t)
		m2.Validate(oldReceipt(), seg(1, 1)) // only segment 1 approved
		m2.SubmitRebind(newReceipt())        // cutover = 2
		r = m2.Validate(oldReceipt(), seg(1, 3))
		if r.Decision != PartialApproval || r.Approvable != seg(1, 1) {
			t.Fatalf("got decision=%s approvable=%s, want PARTIAL [1..1]", r.Decision, r.Approvable)
		}
		if m2.HighWater() != 1 {
			t.Fatalf("HighWater = %d, want 1 (segment beyond cutover untouched)", m2.HighWater())
		}
	})

	t.Run("new binding reaching before the cutover", func(t *testing.T) {
		m := testMachine(t)
		m.Validate(oldReceipt(), seg(1, 3))
		m.SubmitRebind(newReceipt()) // cutover = 4

		// New receipt asking for 3..5: segment 3 belongs to the old device,
		// only 4..5 can be approved.
		r := m.Validate(newReceipt(), seg(3, 5))
		if r.Decision != PartialApproval || r.Reason != ReasonStaleBindingBeforeCutover {
			t.Fatalf("got decision=%s reason=%s, want PARTIAL/STALE_BINDING_BEFORE_CUTOVER", r.Decision, r.Reason)
		}
		if r.Approvable != seg(4, 5) {
			t.Fatalf("Approvable = %s, want [4..5]", r.Approvable)
		}
		if len(r.NewlyApproved) != 2 || m.HighWater() != 5 {
			t.Fatalf("NewlyApproved=%v HighWater=%d, want [4 5] and 5", r.NewlyApproved, m.HighWater())
		}

		// New receipt entirely on the old side is a hard rejection.
		assertRejected(t, m.Validate(newReceipt(), seg(1, 2)), ReasonStaleBindingBeforeCutover)
	})
}

// TestCutoverImmunityToRetries drives every retry form against a locked
// cutover and proves the cutover never moves.
func TestCutoverImmunityToRetries(t *testing.T) {
	m := testMachine(t)
	m.Validate(oldReceipt(), seg(1, 2))
	m.SubmitRebind(newReceipt()) // cutover = 3, locked

	// Duplicate / retried rebind submission is refused with the same cutover.
	out := m.SubmitRebind(newReceipt())
	if !out.AlreadyLocked || out.Cutover != 3 || out.Reason != ReasonCutoverConfirmedImmutable {
		t.Fatalf("duplicate rebind outcome = %+v", out)
	}

	// A forged rebind attempt claiming a different device is a validation
	// failure and leaves the already confirmed cutover untouched.
	forged := Receipt{EmployeeID: employee, BindingVersion: 2, DeviceID: "device-attacker"}
	out = m.SubmitRebind(forged)
	if out.AlreadyLocked || out.Cutover != 3 || out.Reason != ReasonRebindDeviceMismatch {
		t.Fatalf("forged rebind outcome = %+v, want device mismatch with cutover 3 preserved", out)
	}

	// Old-side upload retries, new-side continuations and rejected requests
	// all leave the cutover at 3.
	m.Validate(oldReceipt(), seg(1, 2))   // idempotent old retry
	m.Validate(newReceipt(), seg(3, 3))   // first new segment
	m.Validate(newReceipt(), seg(3, 4))   // retry + continue
	m.Validate(oldReceipt(), seg(9, 9))   // rejected old overreach
	m.Validate(newReceipt(), seg(50, 50)) // rejected gap
	m.Validate(oldReceipt(), seg(3, 3))   // rejected boundary touch
	if cut, _ := m.Cutover(); cut != 3 {
		t.Fatalf("cutover = %d, want 3 after all retries", cut)
	}
	if m.HighWater() != 4 {
		t.Fatalf("HighWater = %d, want 4", m.HighWater())
	}
}

// TestRebindValidation covers malformed rebind submissions.
func TestRebindValidation(t *testing.T) {
	m := testMachine(t)

	cases := []struct {
		name    string
		receipt Receipt
		reason  ReasonCode
	}{
		{"wrong employee", Receipt{EmployeeID: "someone-else", BindingVersion: 2, DeviceID: "device-new"}, ReasonRebindEmployeeMismatch},
		{"unknown version", Receipt{EmployeeID: employee, BindingVersion: 99, DeviceID: "device-new"}, ReasonRebindUnknownBinding},
		{"stale version", Receipt{EmployeeID: employee, BindingVersion: 1, DeviceID: "device-old"}, ReasonRebindVersionNotCurrent},
		{"wrong device", Receipt{EmployeeID: employee, BindingVersion: 2, DeviceID: "device-old"}, ReasonRebindDeviceMismatch},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			out := m.SubmitRebind(tc.receipt)
			if out.Reason != tc.reason {
				t.Fatalf("reason = %s, want %s (%s)", out.Reason, tc.reason, out.Message)
			}
			if m.CutoverLocked() {
				t.Fatalf("invalid rebind must not lock the cutover")
			}
		})
	}
}

// TestRequestValidation covers receipt and range validation failures.
func TestRequestValidation(t *testing.T) {
	m := testMachine(t)
	m.Validate(oldReceipt(), seg(1, 1))
	m.SubmitRebind(newReceipt()) // cutover = 2

	cases := []struct {
		name    string
		receipt Receipt
		r       SegmentRange
		reason  ReasonCode
	}{
		{"zero first", oldReceipt(), seg(0, 2), ReasonInvalidSegmentRange},
		{"negative segment", oldReceipt(), seg(-1, 2), ReasonInvalidSegmentRange},
		{"inverted range", oldReceipt(), seg(3, 2), ReasonInvalidSegmentRange},
		{"wrong employee", Receipt{EmployeeID: "x", BindingVersion: 1, DeviceID: "device-old"}, seg(1, 1), ReasonEmployeeMismatch},
		{"unknown version", Receipt{EmployeeID: employee, BindingVersion: 7, DeviceID: "device-old"}, seg(1, 1), ReasonUnknownBindingVersion},
		{"device mismatch v1", Receipt{EmployeeID: employee, BindingVersion: 1, DeviceID: "device-new"}, seg(1, 1), ReasonDeviceMismatch},
		{"old binding overreach", oldReceipt(), seg(2, 2), ReasonOldBindingAfterCutover},
		{"new binding gap", newReceipt(), seg(3, 3), ReasonNewBindingGap},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			before := m.HighWater()
			assertRejected(t, m.Validate(tc.receipt, tc.r), tc.reason)
			if m.HighWater() != before {
				t.Fatalf("rejected request changed HighWater %d -> %d", before, m.HighWater())
			}
		})
	}
}

// TestCutoverAtFirstSegment covers the boundary where nothing was approved
// before the rebind: the old binding immediately governs no segments.
func TestCutoverAtFirstSegment(t *testing.T) {
	m := testMachine(t)
	out := m.SubmitRebind(newReceipt())
	if out.Reason != ReasonOK || out.Cutover != 1 {
		t.Fatalf("rebind outcome = %+v, want OK cutover 1", out)
	}

	assertRejected(t, m.Validate(oldReceipt(), seg(1, 1)), ReasonBindingHasNoGovernedRange)

	r := m.Validate(newReceipt(), seg(1, 2))
	assertApproved(t, r, seg(1, 2))
	if m.HighWater() != 2 {
		t.Fatalf("HighWater = %d, want 2", m.HighWater())
	}

	// Continuation cannot skip segment 3.
	assertRejected(t, m.Validate(newReceipt(), seg(4, 4)), ReasonNewBindingGap)
}

// TestInFlightGapAtRebind checks the "first unapproved segment" definition
// when approvals are prefix-contiguous by invariant.
func TestInFlightGapAtRebind(t *testing.T) {
	m := testMachine(t)
	m.Validate(oldReceipt(), seg(1, 4))
	m.SubmitRebind(newReceipt()) // cutover = 5

	r := m.Validate(newReceipt(), seg(5, 6))
	assertApproved(t, r, seg(5, 6))
	if len(r.NewlyApproved) != 2 {
		t.Fatalf("NewlyApproved = %v, want [5 6]", r.NewlyApproved)
	}
}

// TestNewBindingPrefixContiguity verifies prefix enforcement before any
// rebind as well, and that retries mixed with new work behave correctly.
func TestNewBindingPrefixContiguity(t *testing.T) {
	m := testMachine(t)
	m.Validate(oldReceipt(), seg(1, 2))
	m.SubmitRebind(newReceipt()) // cutover = 3

	// Request 3..6 while only 3 will be approved after this call: still fine
	// because the range starts exactly at the first unapproved segment.
	r := m.Validate(newReceipt(), seg(3, 6))
	assertApproved(t, r, seg(3, 6))
	if m.HighWater() != 6 {
		t.Fatalf("HighWater = %d, want 6", m.HighWater())
	}

	// Retry spanning already-approved and new segments, all within the new
	// binding's governed interval: whole request stays Approved, and the
	// reported approvable range covers the full governed sub-range.
	r = m.Validate(newReceipt(), seg(5, 8))
	assertApproved(t, r, seg(5, 8))
	if len(r.NewlyApproved) != 2 || r.NewlyApproved[0] != 7 {
		t.Fatalf("NewlyApproved = %v, want [7 8]", r.NewlyApproved)
	}
	if m.HighWater() != 8 {
		t.Fatalf("HighWater = %d, want 8", m.HighWater())
	}
}

// TestConstructorErrors verifies defensive constructor checks.
func TestConstructorErrors(t *testing.T) {
	if _, err := New(employee, nil); !errors.Is(err, ErrNoBindings) {
		t.Fatalf("New(nil) error = %v, want ErrNoBindings", err)
	}
	_, err := New(employee, []Binding{
		{Version: 1, DeviceID: "a"},
		{Version: 1, DeviceID: "b"},
	})
	if err == nil {
		t.Fatalf("New with duplicate versions expected error")
	}
}

// TestConcurrentInterleaving hammers the machine with concurrent old-receipt
// retries and new-receipt continuations (plus retried rebind submissions) and
// checks that every state-machine invariant holds throughout.
func TestConcurrentInterleaving(t *testing.T) {
	m := testMachine(t)
	m.Validate(oldReceipt(), seg(1, 4))

	// The real rebind happens exactly once, establishing cutover 5. All later
	// rebind calls are retries that must be refused.
	if out := m.SubmitRebind(newReceipt()); out.Cutover != 5 || out.Reason != ReasonOK {
		t.Fatalf("initial rebind = %+v", out)
	}

	const workers = 16
	const iterations = 200
	var wg sync.WaitGroup
	rng := rand.New(rand.NewSource(20261127))
	var rngMu sync.Mutex

	wg.Add(workers)
	for w := 0; w < workers; w++ {
		go func(id int) {
			defer wg.Done()
			for i := 0; i < iterations; i++ {
				rngMu.Lock()
				roll := rng.Intn(6)
				rngMu.Unlock()

				switch roll {
				case 0:
					// Retried rebind submission: must be refused, cutover stays.
					out := m.SubmitRebind(newReceipt())
					if !out.AlreadyLocked || out.Cutover != 5 {
						t.Errorf("retried rebind broke the cutover: %+v", out)
						return
					}
				case 1:
					// Old-side idempotent retry, always within 1..4.
					r := m.Validate(oldReceipt(), seg(1, 4))
					if r.Decision == Rejected || len(r.NewlyApproved) != 0 {
						t.Errorf("old prefix retry unexpected: %+v", r)
						return
					}
				case 2:
					// Old-side overreach, must never approve anything.
					r := m.Validate(oldReceipt(), seg(5, 8))
					if len(r.NewlyApproved) != 0 {
						t.Errorf("old overreach approved segments: %v", r.NewlyApproved)
						return
					}
				case 3:
					// New-side continuation from the current first unapproved.
					first := m.FirstUnapproved()
					if first >= 5 {
						m.Validate(newReceipt(), seg(first, first+1))
					}
				case 4:
					// New-side idempotent retry of the already approved tail.
					hw := m.HighWater()
					if hw >= 5 {
						r := m.Validate(newReceipt(), seg(5, hw))
						if len(r.NewlyApproved) != 0 {
							t.Errorf("new tail retry re-approved segments: %v", r.NewlyApproved)
							return
						}
					}
				case 5:
					// New-side deliberate gap, must be rejected. The segment
					// is far beyond anything reachable here: at most
					// workers*iterations*2 continuation segments can be
					// approved, so HighWater can never approach this.
					r := m.Validate(newReceipt(), seg(10_000_000, 10_000_001))
					if r.Decision != Rejected || r.Reason != ReasonNewBindingGap {
						t.Errorf("gap request not rejected: %+v", r)
						return
					}
				}
			}
		}(w)
	}
	wg.Wait()

	// Invariant 1: cutover fixed at 5 and locked.
	cut, err := m.Cutover()
	if err != nil || cut != 5 {
		t.Fatalf("cutover = (%d, %v), want 5", cut, err)
	}
	if !m.CutoverLocked() {
		t.Fatalf("cutover not locked")
	}

	// Invariant 2: approvals form the contiguous prefix 1..HighWater and
	// nothing beyond it is approved.
	hw := m.HighWater()
	for n := 1; n <= hw; n++ {
		if !m.IsApproved(n) {
			t.Fatalf("segment %d missing inside approved prefix 1..%d", n, hw)
		}
	}
	if m.IsApproved(hw + 1) {
		t.Fatalf("segment %d approved beyond HighWater", hw+1)
	}
	if m.FirstUnapproved() != hw+1 {
		t.Fatalf("FirstUnapproved = %d, want %d", m.FirstUnapproved(), hw+1)
	}

	// Invariant 3: old binding cannot touch anything >= cutover, new binding
	// cannot touch anything < cutover.
	r := m.Validate(oldReceipt(), seg(cut, cut))
	if r.Decision != Rejected {
		t.Fatalf("old binding approved segment at cutover: %+v", r)
	}
	if cut > 1 {
		r = m.Validate(newReceipt(), seg(1, cut-1))
		if r.Decision != Rejected {
			t.Fatalf("new binding approved old-side segment: %+v", r)
		}
	}
}

// TestThreeBindingVersions verifies governance when more than two binding
// versions are registered: only the highest version is current, and every
// retired version shares the pre-cutover interval.
func TestThreeBindingVersions(t *testing.T) {
	m, err := New(employee, []Binding{
		{Version: 1, DeviceID: "device-old"},
		{Version: 2, DeviceID: "device-replaced"},
		{Version: 3, DeviceID: "device-new"},
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if m.CurrentVersion() != 3 {
		t.Fatalf("CurrentVersion = %d, want 3", m.CurrentVersion())
	}

	m.Validate(oldReceipt(), seg(1, 2))
	out := m.SubmitRebind(Receipt{EmployeeID: employee, BindingVersion: 3, DeviceID: "device-new"})
	if out.Reason != ReasonOK || out.Cutover != 3 {
		t.Fatalf("rebind outcome = %+v, want OK cutover 3", out)
	}

	// The intermediate version was never active and is treated as a retired
	// binding: it can only serve the pre-cutover prefix.
	r := m.Validate(Receipt{EmployeeID: employee, BindingVersion: 2, DeviceID: "device-replaced"}, seg(1, 2))
	assertApproved(t, r, seg(1, 2))
	assertRejected(t, m.Validate(Receipt{EmployeeID: employee, BindingVersion: 2, DeviceID: "device-replaced"}, seg(3, 3)), ReasonOldBindingAfterCutover)

	// A rebind receipt on a non-current version is refused even after lock.
	out = m.SubmitRebind(Receipt{EmployeeID: employee, BindingVersion: 2, DeviceID: "device-replaced"})
	if out.Reason != ReasonRebindVersionNotCurrent || out.Cutover != 3 {
		t.Fatalf("stale rebind outcome = %+v", out)
	}

	// The highest version continues the transfer from the cutover.
	r = m.Validate(Receipt{EmployeeID: employee, BindingVersion: 3, DeviceID: "device-new"}, seg(3, 4))
	assertApproved(t, r, seg(3, 4))
	if m.HighWater() != 4 {
		t.Fatalf("HighWater = %d, want 4", m.HighWater())
	}
}

// TestDecisionAndReasonStrings guards the stable diagnostic vocabulary.
func TestDecisionAndReasonStrings(t *testing.T) {
	if Approved.String() != "APPROVED" || PartialApproval.String() != "PARTIAL_APPROVAL" || Rejected.String() != "REJECTED" {
		t.Fatalf("unexpected decision string: %q %q %q", Approved, PartialApproval, Rejected)
	}
	if ReasonOldBindingAfterCutover.String() != "OLD_BINDING_AFTER_CUTOVER" {
		t.Fatalf("reason string changed: %q", ReasonOldBindingAfterCutover)
	}
	if seg(2, 5).String() != "[2..5]" {
		t.Fatalf("range String = %q", seg(2, 5))
	}
	if (SegmentRange{}).Valid() || !seg(1, 1).Valid() {
		t.Fatalf("SegmentRange.Valid broken")
	}
}

// TestSingleBindingMachine documents behavior for a transfer that never
// rebinds.
func TestSingleBindingMachine(t *testing.T) {
	m, err := New(employee, []Binding{{Version: 1, DeviceID: "only-device"}})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	r := m.Validate(Receipt{EmployeeID: employee, BindingVersion: 1, DeviceID: "only-device"}, seg(1, 3))
	assertApproved(t, r, seg(1, 3))
	if m.CurrentVersion() != 1 {
		t.Fatalf("CurrentVersion = %d, want 1", m.CurrentVersion())
	}
}

// ExampleValidate_outOfOrderCutover is executable documentation of the
// headline interleaving scenario.
func ExampleValidate_outOfOrderCutover() {
	m, _ := New("emp-42", []Binding{
		{Version: 1, DeviceID: "device-old"},
		{Version: 2, DeviceID: "device-new"},
	})
	old := Receipt{EmployeeID: "emp-42", BindingVersion: 1, DeviceID: "device-old"}
	next := Receipt{EmployeeID: "emp-42", BindingVersion: 2, DeviceID: "device-new"}

	m.Validate(old, SegmentRange{First: 1, Last: 3})
	out := m.SubmitRebind(next)
	fmt.Println("cutover:", out.Cutover)

	late := m.Validate(old, SegmentRange{First: 4, Last: 4})
	fmt.Println("late old retry:", late.Decision, late.Reason)

	cont := m.Validate(next, SegmentRange{First: 4, Last: 5})
	fmt.Println("new continuation:", cont.Decision, cont.Approvable, cont.MatchedDeviceID)

	retry := m.Validate(old, SegmentRange{First: 2, Last: 3})
	fmt.Println("old prefix retry:", retry.Decision, len(retry.NewlyApproved))

	cut, _ := m.Cutover()
	fmt.Println("confirmed cutover:", cut)
	// Output:
	// cutover: 4
	// late old retry: REJECTED OLD_BINDING_AFTER_CUTOVER
	// new continuation: APPROVED [4..5] device-new
	// old prefix retry: APPROVED 0
	// confirmed cutover: 4
}
