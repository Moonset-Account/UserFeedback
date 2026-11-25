package sampling

import (
	"reflect"
	"testing"
	"time"
)

// All data below is synthetic and local; no real targets are contacted.

var base = time.Date(2026, 11, 26, 10, 0, 0, 0, time.UTC)

func at(sec int) time.Time { return base.Add(time.Duration(sec) * time.Second) }

func iv(start, end int) Interval { return Interval{Start: at(start), End: at(end)} }

func fw(start, end int, fields ...DataField) FieldWindow {
	return FieldWindow{Interval: iv(start, end), Fields: fields}
}

func procWindows(t *testing.T, r Result, pid int) []FieldWindow {
	t.Helper()
	for _, p := range r.Processes {
		if p.PID == pid {
			return p.Windows
		}
	}
	t.Fatalf("process %d missing from result", pid)
	return nil
}

// Normal scenario: machine consent covers device health for the whole plan;
// the starter's user consent covers process fields for a sub-range.
func TestBuildPlanNormalScenario(t *testing.T) {
	procs := []Process{{PID: 101, StartUser: "alice", LoginID: "L1"}}
	consents := []Consent{
		{ID: "m1", GrantedBy: "alice", Scope: ScopeMachine,
			Fields: []DataField{FieldDeviceHealth}, Window: iv(0, 100)},
		{ID: "u1", GrantedBy: "alice", Scope: ScopeUser, Subject: "alice",
			Fields: []DataField{FieldProcessCPU, FieldProcessMemory}, Window: iv(10, 50)},
	}
	got := BuildPlan(procs, nil, consents, nil, iv(0, 100))

	wantMachine := []FieldWindow{fw(0, 100, FieldDeviceHealth)}
	if !reflect.DeepEqual(got.Machine, wantMachine) {
		t.Errorf("machine windows = %+v, want %+v", got.Machine, wantMachine)
	}
	wantProc := []FieldWindow{fw(10, 50, FieldProcessCPU, FieldProcessMemory)}
	if got := procWindows(t, got, 101); !reflect.DeepEqual(got, wantProc) {
		t.Errorf("process windows = %+v, want %+v", got, wantProc)
	}
}

// Consent window is clipped to the requested plan window on both sides.
func TestConsentClippedToPlanWindow(t *testing.T) {
	procs := []Process{{PID: 1, StartUser: "alice", LoginID: "L1"}}
	consents := []Consent{
		{ID: "u1", GrantedBy: "alice", Scope: ScopeUser, Subject: "alice",
			Fields: []DataField{FieldProcessCPU}, Window: iv(0, 100)},
	}
	got := BuildPlan(procs, nil, consents, nil, iv(20, 40))
	want := []FieldWindow{fw(20, 40, FieldProcessCPU)}
	if got := procWindows(t, got, 1); !reflect.DeepEqual(got, want) {
		t.Errorf("windows = %+v, want %+v", got, want)
	}
}

// A mid-plan revocation narrows the schedule from the revocation moment.
func TestRevocationNarrowsPlan(t *testing.T) {
	procs := []Process{{PID: 1, StartUser: "alice", LoginID: "L1"}}
	consents := []Consent{
		{ID: "m1", GrantedBy: "alice", Scope: ScopeMachine,
			Fields: []DataField{FieldDeviceHealth}, Window: iv(0, 100)},
		{ID: "u1", GrantedBy: "alice", Scope: ScopeUser, Subject: "alice",
			Fields: []DataField{FieldProcessCPU}, Window: iv(0, 100)},
	}
	revs := []Revocation{
		{ConsentID: "u1", At: at(30)},
		{By: "alice", At: at(70)}, // revokes everything else alice granted (m1)
	}
	got := BuildPlan(procs, nil, consents, revs, iv(0, 100))

	if want := []FieldWindow{fw(0, 70, FieldDeviceHealth)}; !reflect.DeepEqual(got.Machine, want) {
		t.Errorf("machine windows = %+v, want %+v", got.Machine, want)
	}
	if got, want := procWindows(t, got, 1), []FieldWindow{fw(0, 30, FieldProcessCPU)}; !reflect.DeepEqual(got, want) {
		t.Errorf("process windows = %+v, want %+v", got, want)
	}
}

// Boundary: revocation exactly at consent start kills the consent entirely;
// revocation at/after consent end changes nothing.
func TestRevocationBoundaries(t *testing.T) {
	procs := []Process{{PID: 1, StartUser: "alice", LoginID: "L1"}}
	consents := []Consent{
		{ID: "u1", GrantedBy: "alice", Scope: ScopeUser, Subject: "alice",
			Fields: []DataField{FieldProcessCPU}, Window: iv(10, 50)},
		{ID: "u2", GrantedBy: "alice", Scope: ScopeUser, Subject: "alice",
			Fields: []DataField{FieldProcessMemory}, Window: iv(10, 50)},
	}
	revs := []Revocation{
		{ConsentID: "u1", At: at(10)}, // at start: consent never effective
		{ConsentID: "u2", At: at(50)}, // at end: no effect
	}
	got := BuildPlan(procs, nil, consents, revs, iv(0, 100))
	want := []FieldWindow{fw(10, 50, FieldProcessMemory)}
	if got := procWindows(t, got, 1); !reflect.DeepEqual(got, want) {
		t.Errorf("windows = %+v, want %+v", got, want)
	}
}

// Foreground switches must not reattribute background processes: bob's
// consent never covers alice's background process, and alice's consent
// keeps working after bob becomes the foreground user.
func TestForegroundSwitchPreservesOwnership(t *testing.T) {
	procs := []Process{
		{PID: 201, StartUser: "alice", LoginID: "L1"}, // background process
		{PID: 202, StartUser: "bob", LoginID: "L2"},
	}
	switches := []ForegroundSwitch{
		{At: at(20), UserID: "bob", LoginID: "L2"}, // bob takes the foreground
	}
	consents := []Consent{
		{ID: "u-alice", GrantedBy: "alice", Scope: ScopeUser, Subject: "alice",
			Fields: []DataField{FieldProcessCPU}, Window: iv(0, 100)},
		{ID: "u-bob", GrantedBy: "bob", Scope: ScopeUser, Subject: "bob",
			Fields: []DataField{FieldProcessMemory}, Window: iv(0, 100)},
	}
	got := BuildPlan(procs, switches, consents, nil, iv(0, 100))

	// alice's background process keeps alice's fields across the switch at t=20,
	// and never gains bob's process.memory.
	wantAlice := []FieldWindow{fw(0, 100, FieldProcessCPU)}
	if got := procWindows(t, got, 201); !reflect.DeepEqual(got, wantAlice) {
		t.Errorf("alice's background process windows = %+v, want %+v", got, wantAlice)
	}
	wantBob := []FieldWindow{fw(0, 100, FieldProcessMemory)}
	if got := procWindows(t, got, 202); !reflect.DeepEqual(got, wantBob) {
		t.Errorf("bob's process windows = %+v, want %+v", got, wantBob)
	}
}

// Machine-level consent stays valid across foreground switches and never
// leaks process-level fields.
func TestMachineConsentAcrossSwitchAndFieldScope(t *testing.T) {
	procs := []Process{{PID: 1, StartUser: "alice", LoginID: "L1"}}
	switches := []ForegroundSwitch{{At: at(50), UserID: "bob", LoginID: "L2"}}
	consents := []Consent{
		// Granted by alice, but machine-wide: survives her leaving the foreground.
		// The process.cpu entry must be ignored: machine consent cannot unlock it.
		{ID: "m1", GrantedBy: "alice", Scope: ScopeMachine,
			Fields: []DataField{FieldDeviceHealth, FieldProcessCPU}, Window: iv(0, 100)},
		// User consent must not unlock device.health either.
		{ID: "u1", GrantedBy: "alice", Scope: ScopeUser, Subject: "alice",
			Fields: []DataField{FieldDeviceHealth, FieldProcessMemory}, Window: iv(0, 100)},
	}
	got := BuildPlan(procs, switches, consents, nil, iv(0, 100))

	if want := []FieldWindow{fw(0, 100, FieldDeviceHealth)}; !reflect.DeepEqual(got.Machine, want) {
		t.Errorf("machine windows = %+v, want %+v", got.Machine, want)
	}
	want := []FieldWindow{fw(0, 100, FieldProcessMemory)}
	if got := procWindows(t, got, 1); !reflect.DeepEqual(got, want) {
		t.Errorf("process windows = %+v, want %+v", got, want)
	}
}

// A consent pinned to a login session only covers processes from that
// session, even for the same user.
func TestLoginSessionScoping(t *testing.T) {
	procs := []Process{
		{PID: 1, StartUser: "alice", LoginID: "L1"},
		{PID: 2, StartUser: "alice", LoginID: "L2"},
	}
	consents := []Consent{
		{ID: "u1", GrantedBy: "alice", Scope: ScopeUser, Subject: "alice", LoginID: "L1",
			Fields: []DataField{FieldProcessCPU}, Window: iv(0, 100)},
	}
	got := BuildPlan(procs, nil, consents, nil, iv(0, 100))

	if want := []FieldWindow{fw(0, 100, FieldProcessCPU)}; !reflect.DeepEqual(procWindows(t, got, 1), want) {
		t.Errorf("session L1 windows = %+v, want %+v", procWindows(t, got, 1), want)
	}
	if got := procWindows(t, got, 2); len(got) != 0 {
		t.Errorf("session L2 windows = %+v, want none", got)
	}
}

// Interleaved scenario: two overlapping consents with different fields and
// different revocation moments produce the exact piecewise segmentation.
func TestInterleavedConsentsAndRevocations(t *testing.T) {
	procs := []Process{{PID: 1, StartUser: "alice", LoginID: "L1"}}
	consents := []Consent{
		{ID: "u1", GrantedBy: "alice", Scope: ScopeUser, Subject: "alice",
			Fields: []DataField{FieldProcessCPU}, Window: iv(10, 80)},
		{ID: "u2", GrantedBy: "alice", Scope: ScopeUser, Subject: "alice",
			Fields: []DataField{FieldProcessMemory}, Window: iv(30, 90)},
	}
	revs := []Revocation{{ConsentID: "u1", At: at(60)}}
	got := BuildPlan(procs, nil, consents, revs, iv(0, 100))

	want := []FieldWindow{
		fw(10, 30, FieldProcessCPU),
		fw(30, 60, FieldProcessCPU, FieldProcessMemory),
		fw(60, 90, FieldProcessMemory),
	}
	if got := procWindows(t, got, 1); !reflect.DeepEqual(got, want) {
		t.Errorf("windows = %+v, want %+v", got, want)
	}
}

// Adjacent consents with identical field sets merge into one window.
func TestAdjacentConsentsMerge(t *testing.T) {
	procs := []Process{{PID: 1, StartUser: "alice", LoginID: "L1"}}
	consents := []Consent{
		{ID: "u1", GrantedBy: "alice", Scope: ScopeUser, Subject: "alice",
			Fields: []DataField{FieldProcessCPU}, Window: iv(0, 40)},
		{ID: "u2", GrantedBy: "alice", Scope: ScopeUser, Subject: "alice",
			Fields: []DataField{FieldProcessCPU}, Window: iv(40, 80)},
	}
	got := BuildPlan(procs, nil, consents, nil, iv(0, 100))
	want := []FieldWindow{fw(0, 80, FieldProcessCPU)}
	if got := procWindows(t, got, 1); !reflect.DeepEqual(got, want) {
		t.Errorf("windows = %+v, want %+v", got, want)
	}
}

// Boundary: consent outside the plan, empty plan window, and no consent at
// all all yield empty schedules.
func TestEmptyOutcomes(t *testing.T) {
	procs := []Process{{PID: 1, StartUser: "alice", LoginID: "L1"}}
	consents := []Consent{
		{ID: "m1", GrantedBy: "alice", Scope: ScopeMachine,
			Fields: []DataField{FieldDeviceHealth}, Window: iv(200, 300)},
		{ID: "u1", GrantedBy: "alice", Scope: ScopeUser, Subject: "alice",
			Fields: []DataField{FieldProcessCPU}, Window: iv(200, 300)},
	}
	got := BuildPlan(procs, nil, consents, nil, iv(0, 100))
	if len(got.Machine) != 0 {
		t.Errorf("machine windows = %+v, want none", got.Machine)
	}
	if got := procWindows(t, got, 1); len(got) != 0 {
		t.Errorf("process windows = %+v, want none", got)
	}

	empty := BuildPlan(procs, nil, consents, nil, iv(50, 50))
	if len(empty.Machine) != 0 || len(empty.Processes) != 0 {
		t.Errorf("empty plan window should yield empty result, got %+v", empty)
	}

	none := BuildPlan(procs, nil, nil, nil, iv(0, 100))
	if got := procWindows(t, none, 1); len(got) != 0 {
		t.Errorf("no consents: windows = %+v, want none", got)
	}
}

// A consent from another user never covers the process, even when that user
// is the only one who agreed to anything.
func TestOtherUserConsentNotApplicable(t *testing.T) {
	procs := []Process{{PID: 1, StartUser: "alice", LoginID: "L1"}}
	consents := []Consent{
		{ID: "u1", GrantedBy: "bob", Scope: ScopeUser, Subject: "bob",
			Fields: []DataField{FieldProcessCPU, FieldProcessCmdline}, Window: iv(0, 100)},
	}
	got := BuildPlan(procs, nil, consents, nil, iv(0, 100))
	if got := procWindows(t, got, 1); len(got) != 0 {
		t.Errorf("windows = %+v, want none", got)
	}
}

// Revoking by user (blank ConsentID) only affects consents that user
// granted; other users' consents are untouched.
func TestRevocationByUserOnlyAffectsOwnGrants(t *testing.T) {
	procs := []Process{
		{PID: 1, StartUser: "alice", LoginID: "L1"},
		{PID: 2, StartUser: "bob", LoginID: "L2"},
	}
	consents := []Consent{
		{ID: "u-alice", GrantedBy: "alice", Scope: ScopeUser, Subject: "alice",
			Fields: []DataField{FieldProcessCPU}, Window: iv(0, 100)},
		{ID: "u-bob", GrantedBy: "bob", Scope: ScopeUser, Subject: "bob",
			Fields: []DataField{FieldProcessCPU}, Window: iv(0, 100)},
	}
	revs := []Revocation{{By: "alice", At: at(40)}}
	got := BuildPlan(procs, nil, consents, revs, iv(0, 100))

	if got, want := procWindows(t, got, 1), []FieldWindow{fw(0, 40, FieldProcessCPU)}; !reflect.DeepEqual(got, want) {
		t.Errorf("alice windows = %+v, want %+v", got, want)
	}
	if got, want := procWindows(t, got, 2), []FieldWindow{fw(0, 100, FieldProcessCPU)}; !reflect.DeepEqual(got, want) {
		t.Errorf("bob windows = %+v, want %+v", got, want)
	}
}

func TestIsMachineField(t *testing.T) {
	if !IsMachineField(FieldDeviceHealth) {
		t.Error("device.health should be machine-level")
	}
	for _, f := range []DataField{FieldProcessCPU, FieldProcessMemory, FieldProcessCmdline} {
		if IsMachineField(f) {
			t.Errorf("%s should be process-level", f)
		}
	}
}
