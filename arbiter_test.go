package arbiter

import (
	"errors"
	"testing"
)

// Function names used by the synthetic composite devices below.
const (
	fnKeyboard Function = "usb.hid.keyboard"
	fnMouse    Function = "usb.hid.mouse"
	fnAudio    Function = "usb.audio"
	fnStorage  Function = "usb.mass-storage"
	fnNetwork  Function = "usb.cdc.network"
	fnVideo    Function = "usb.uvc.video"
	fnSerial   Function = "usb.cdc.serial"
	fnVendor   Function = "usb.vendor"
)

func approved(fns ...Function) map[Function]bool {
	m := make(map[Function]bool, len(fns))
	for _, fn := range fns {
		m[fn] = true
	}
	return m
}

func assertStringSet(t *testing.T, name string, got, want []string) {
	t.Helper()
	gs := map[string]bool{}
	for _, s := range got {
		if gs[s] {
			t.Fatalf("%s: duplicate value %q in %v", name, s, got)
		}
		gs[s] = true
	}
	ws := map[string]bool{}
	for _, s := range want {
		ws[s] = true
	}
	if len(gs) != len(ws) {
		t.Fatalf("%s mismatch:\n got %v\nwant %v", name, got, want)
	}
	for s := range ws {
		if !gs[s] {
			t.Fatalf("%s mismatch:\n got %v\nwant %v (missing %q)", name, got, want, s)
		}
	}
}

func assertErrorIs(t *testing.T, name string, got, want error) {
	t.Helper()
	if !errors.Is(got, want) {
		t.Fatalf("%s: expected error %v, got %v", name, want, got)
	}
}

// TestSharedIsolationGroupComposite models a composite device whose
// non-isolatable interfaces all live in one shared hardware isolation group.
// One unapproved function must hold the whole shared group closed, while an
// independently isolatable approved interface can still come up.
func TestSharedIsolationGroupComposite(t *testing.T) {
	// Acceptable case: everything advertised is approved.
	t.Run("all_approved_group_comes_up", func(t *testing.T) {
		ar := New()
		enum := Enumeration{
			InsertionID:   1,
			EnumerationID: 1,
			Interfaces: []Interface{
				{ID: "kbd", Function: fnKeyboard, IsolationGroup: "G-shared"},
				{ID: "mouse", Function: fnMouse, IsolationGroup: "G-shared"},
				{ID: "nic", Function: fnNetwork, IsolationGroup: "G-shared", IndependentlyIsolatable: true},
			},
			ApprovedFunctions: approved(fnKeyboard, fnMouse, fnNetwork),
		}
		dec, err := ar.Adjudicate(enum)
		if err != nil {
			t.Fatalf("adjudicate: %v", err)
		}
		assertStringSet(t, "permitted", dec.Permitted, []string{"kbd", "mouse", "nic"})
		if len(dec.Denied) != 0 {
			t.Fatalf("expected no denials, got %v", dec.Denied)
		}

		for _, id := range dec.Permitted {
			if err := ar.AckEnable(1, 1, id); err != nil {
				t.Fatalf("ack %s: %v", id, err)
			}
		}
		assertStringSet(t, "active", ar.ActiveInterfaces(), []string{"kbd", "mouse", "nic"})
	})

	// Blocked case: an unapproved mass-storage function shares the group.
	// The shared group closes; only the isolatable approved NIC survives.
	t.Run("unapproved_function_holds_shared_group_closed", func(t *testing.T) {
		ar := New()
		enum := Enumeration{
			InsertionID:   2,
			EnumerationID: 1,
			Interfaces: []Interface{
				{ID: "kbd", Function: fnKeyboard, IsolationGroup: "G-shared"},
				{ID: "mouse", Function: fnMouse, IsolationGroup: "G-shared"},
				{ID: "storage", Function: fnStorage, IsolationGroup: "G-shared"},
				{ID: "nic", Function: fnNetwork, IsolationGroup: "G-shared", IndependentlyIsolatable: true},
			},
			ApprovedFunctions: approved(fnKeyboard, fnMouse, fnNetwork),
		}
		dec, err := ar.Adjudicate(enum)
		if err != nil {
			t.Fatalf("adjudicate: %v", err)
		}
		assertStringSet(t, "permitted", dec.Permitted, []string{"nic"})
		assertStringSet(t, "denied", dec.Denied, []string{"kbd", "mouse", "storage"})

		// An enable attempt against a held-closed interface must be refused.
		assertErrorIs(t, "ack kbd", ar.AckEnable(2, 1, "kbd"), ErrInterfaceDenied)
		assertErrorIs(t, "ack storage", ar.AckEnable(2, 1, "storage"), ErrInterfaceDenied)
		if err := ar.AckEnable(2, 1, "nic"); err != nil {
			t.Fatalf("ack nic: %v", err)
		}
		assertStringSet(t, "active", ar.ActiveInterfaces(), []string{"nic"})
	})

	// Boundary: the isolatable interface itself is unapproved. Isolation
	// does not grant it permission — it must still be denied, while the
	// clean shared group stays open.
	t.Run("unapproved_isolatable_interface_denied_but_group_open", func(t *testing.T) {
		ar := New()
		enum := Enumeration{
			InsertionID:   3,
			EnumerationID: 1,
			Interfaces: []Interface{
				{ID: "kbd", Function: fnKeyboard, IsolationGroup: "G-shared"},
				{ID: "vendor", Function: fnVendor, IsolationGroup: "G-shared", IndependentlyIsolatable: true},
			},
			ApprovedFunctions: approved(fnKeyboard),
		}
		dec, err := ar.Adjudicate(enum)
		if err != nil {
			t.Fatalf("adjudicate: %v", err)
		}
		assertStringSet(t, "permitted", dec.Permitted, []string{"kbd"})
		assertStringSet(t, "denied", dec.Denied, []string{"vendor"})
	})
}

// TestGroupableComposite models a composite device split into separate
// hardware isolation groups that can be fenced independently. A tainted
// group only closes itself; clean groups keep working.
func TestGroupableComposite(t *testing.T) {
	ar := New()
	enum := Enumeration{
		InsertionID:   1,
		EnumerationID: 1,
		Interfaces: []Interface{
			// Group G1: HID pair hardware-bonded (must enable together).
			{ID: "kbd", Function: fnKeyboard, IsolationGroup: "G1"},
			{ID: "mouse", Function: fnMouse, IsolationGroup: "G1"},
			// Group G2: unapproved storage taints G2; audio is approved but
			// non-isolatable, so G2's shared part closes.
			{ID: "storage", Function: fnStorage, IsolationGroup: "G2"},
			{ID: "audio", Function: fnAudio, IsolationGroup: "G2"},
			// Group G3: independently isolatable functions.
			{ID: "nic", Function: fnNetwork, IsolationGroup: "G3", IndependentlyIsolatable: true},
			{ID: "vendor", Function: fnVendor, IsolationGroup: "G3", IndependentlyIsolatable: true},
		},
		CoEnableSets:      [][]string{{"kbd", "mouse"}},
		ApprovedFunctions: approved(fnKeyboard, fnMouse, fnAudio, fnNetwork),
	}
	dec, err := ar.Adjudicate(enum)
	if err != nil {
		t.Fatalf("adjudicate: %v", err)
	}

	// G1 clean and bonded -> both up. G2 tainted -> both down.
	// G3: approved isolatable NIC up; unapproved vendor down without
	// affecting anything else.
	assertStringSet(t, "permitted", dec.Permitted, []string{"kbd", "mouse", "nic"})
	assertStringSet(t, "denied", dec.Denied, []string{"storage", "audio", "vendor"})

	for _, id := range dec.Permitted {
		if err := ar.AckEnable(1, 1, id); err != nil {
			t.Fatalf("ack %s: %v", id, err)
		}
	}
	assertStringSet(t, "active", ar.ActiveInterfaces(), []string{"kbd", "mouse", "nic"})

	// Attempting to enable a denied interface from the tainted group fails.
	assertErrorIs(t, "ack audio", ar.AckEnable(1, 1, "audio"), ErrInterfaceDenied)
}

// TestCoEnableBundleAllOrNothing covers the boundary where a co-enable
// bundle spans an otherwise clean group: one inadmissible member must hold
// back every bonded member, even members whose own function is approved.
func TestCoEnableBundleAllOrNothing(t *testing.T) {
	ar := New()
	enum := Enumeration{
		InsertionID:   1,
		EnumerationID: 1,
		Interfaces: []Interface{
			{ID: "kbd", Function: fnKeyboard, IsolationGroup: "G1"},
			{ID: "storage", Function: fnStorage, IsolationGroup: "G1"},
			{ID: "nic", Function: fnNetwork, IsolationGroup: "G2", IndependentlyIsolatable: true},
		},
		// Bonding crosses groups; storage is unapproved so the whole
		// transitive bundle kbd+storage is withheld.
		CoEnableSets:      [][]string{{"kbd", "storage"}},
		ApprovedFunctions: approved(fnKeyboard, fnNetwork),
	}
	dec, err := ar.Adjudicate(enum)
	if err != nil {
		t.Fatalf("adjudicate: %v", err)
	}
	assertStringSet(t, "permitted", dec.Permitted, []string{"nic"})
	assertStringSet(t, "denied", dec.Denied, []string{"kbd", "storage"})
	assertErrorIs(t, "ack kbd", ar.AckEnable(1, 1, "kbd"), ErrInterfaceDenied)

	// Transitive bonding: A-B and B-C collapse into one bundle; denial of C
	// must also remove A and B.
	ar2 := New()
	enum2 := Enumeration{
		InsertionID:   1,
		EnumerationID: 1,
		Interfaces: []Interface{
			{ID: "a", Function: fnKeyboard, IsolationGroup: "G1"},
			{ID: "b", Function: fnMouse, IsolationGroup: "G1"},
			{ID: "c", Function: fnStorage, IsolationGroup: "G1"},
		},
		CoEnableSets:      [][]string{{"a", "b"}, {"b", "c"}},
		ApprovedFunctions: approved(fnKeyboard, fnMouse),
	}
	dec2, err := ar2.Adjudicate(enum2)
	if err != nil {
		t.Fatalf("adjudicate: %v", err)
	}
	if len(dec2.Permitted) != 0 {
		t.Fatalf("transitive bundle should be fully denied, permitted=%v", dec2.Permitted)
	}
	assertStringSet(t, "denied", dec2.Denied, []string{"a", "b", "c"})
}

// TestModeChangeReenumerationAndLateAcks interleaves two enumeration rounds
// within one insertion: the device changes working mode, old interfaces are
// torn down first, and late confirmations from the previous round cannot
// restore old permissions.
func TestModeChangeReenumerationAndLateAcks(t *testing.T) {
	ar := New()

	// Round 1: full dock in "peripheral" mode, everything approved.
	first := Enumeration{
		InsertionID:   7,
		EnumerationID: 1,
		Interfaces: []Interface{
			{ID: "kbd", Function: fnKeyboard, IsolationGroup: "G1"},
			{ID: "storage", Function: fnStorage, IsolationGroup: "G1"},
			{ID: "nic", Function: fnNetwork, IsolationGroup: "G1", IndependentlyIsolatable: true},
		},
		ApprovedFunctions: approved(fnKeyboard, fnStorage, fnNetwork),
	}
	dec, err := ar.Adjudicate(first)
	if err != nil {
		t.Fatalf("adjudicate round 1: %v", err)
	}
	assertStringSet(t, "round1 permitted", dec.Permitted, []string{"kbd", "storage", "nic"})
	for _, id := range dec.Permitted {
		if err := ar.AckEnable(7, 1, id); err != nil {
			t.Fatalf("ack %s: %v", id, err)
		}
	}
	assertStringSet(t, "round1 active", ar.ActiveInterfaces(), []string{"kbd", "storage", "nic"})

	// Mode change: same insertion, new enumeration ID and topology.
	// Storage use is no longer approved; the shared G1 group becomes
	// tainted so the keyboard goes away too. Only the isolatable NIC
	// remains admissible.
	second := Enumeration{
		InsertionID:   7,
		EnumerationID: 2,
		Interfaces: []Interface{
			{ID: "kbd", Function: fnKeyboard, IsolationGroup: "G1"},
			{ID: "storage", Function: fnStorage, IsolationGroup: "G1"},
			{ID: "nic", Function: fnNetwork, IsolationGroup: "G1", IndependentlyIsolatable: true},
			{ID: "cam", Function: fnVideo, IsolationGroup: "G2", IndependentlyIsolatable: true},
		},
		ApprovedFunctions: approved(fnKeyboard, fnNetwork, fnVideo),
	}
	dec2, err := ar.Adjudicate(second)
	if err != nil {
		t.Fatalf("adjudicate round 2: %v", err)
	}
	assertStringSet(t, "round2 permitted", dec2.Permitted, []string{"nic", "cam"})
	assertStringSet(t, "denied", dec2.Denied, []string{"kbd", "storage"})

	// Old permissions must already be torn down before any new confirmation.
	if got := ar.ActiveInterfaces(); len(got) != 0 {
		t.Fatalf("expected full teardown after re-enumeration, active=%v", got)
	}

	// Late confirmations stamped with the previous round are rejected and
	// never restore the old (now denied) keyboard/storage permissions.
	assertErrorIs(t, "late kbd ack", ar.AckEnable(7, 1, "kbd"), ErrStaleRound)
	assertErrorIs(t, "late storage ack", ar.AckEnable(7, 1, "storage"), ErrStaleRound)
	assertStringSet(t, "active after late acks", ar.ActiveInterfaces(), []string{})

	// A late ack for an interface that is still permitted in round 2 is
	// likewise rejected: confirmations are round-scoped, not interface-scoped.
	assertErrorIs(t, "late nic ack", ar.AckEnable(7, 1, "nic"), ErrStaleRound)

	// Current-round confirmations drive the actual enabled set.
	if err := ar.AckEnable(7, 2, "nic"); err != nil {
		t.Fatalf("ack nic round 2: %v", err)
	}
	if err := ar.AckEnable(7, 2, "cam"); err != nil {
		t.Fatalf("ack cam round 2: %v", err)
	}
	assertStringSet(t, "round2 active", ar.ActiveInterfaces(), []string{"nic", "cam"})

	// Re-adjudicating the same round is idempotent and keeps confirmations.
	dec2Again, err := ar.Adjudicate(second)
	if err != nil {
		t.Fatalf("re-adjudicate: %v", err)
	}
	assertStringSet(t, "idempotent permitted", dec2Again.Permitted, []string{"nic", "cam"})
	assertStringSet(t, "idempotent active", ar.ActiveInterfaces(), []string{"nic", "cam"})
}

// TestNewInsertionResetsAllPermissions verifies that a new physical
// insertion starts from a fully closed state and confirmations carrying the
// old insertion number are stale even if their enumeration ID matches.
func TestNewInsertionResetsAllPermissions(t *testing.T) {
	ar := New()
	first := Enumeration{
		InsertionID:       1,
		EnumerationID:     1,
		Interfaces:        []Interface{{ID: "kbd", Function: fnKeyboard, IsolationGroup: "G1"}},
		ApprovedFunctions: approved(fnKeyboard),
	}
	if _, err := ar.Adjudicate(first); err != nil {
		t.Fatalf("adjudicate: %v", err)
	}
	if err := ar.AckEnable(1, 1, "kbd"); err != nil {
		t.Fatalf("ack: %v", err)
	}

	next := Enumeration{
		InsertionID:       2,
		EnumerationID:     1,
		Interfaces:        []Interface{{ID: "kbd", Function: fnKeyboard, IsolationGroup: "G1"}},
		ApprovedFunctions: approved(fnKeyboard),
	}
	if _, err := ar.Adjudicate(next); err != nil {
		t.Fatalf("adjudicate insertion 2: %v", err)
	}
	if got := ar.ActiveInterfaces(); len(got) != 0 {
		t.Fatalf("new insertion must start closed, active=%v", got)
	}
	assertErrorIs(t, "old insertion ack", ar.AckEnable(1, 1, "kbd"), ErrStaleRound)
	assertErrorIs(t, "unknown interface ack", ar.AckEnable(2, 1, "ghost"), ErrUnknownInterface)
}

// TestInvalidEnumerations covers malformed input rejection.
func TestInvalidEnumerations(t *testing.T) {
	goodIface := Interface{ID: "kbd", Function: fnKeyboard, IsolationGroup: "G1"}
	cases := map[string]Enumeration{
		"zero insertion": {
			InsertionID: 0, EnumerationID: 1,
			Interfaces: []Interface{goodIface}, ApprovedFunctions: approved(fnKeyboard),
		},
		"zero enumeration": {
			InsertionID: 1, EnumerationID: 0,
			Interfaces: []Interface{goodIface}, ApprovedFunctions: approved(fnKeyboard),
		},
		"no interfaces": {
			InsertionID: 1, EnumerationID: 1,
			Interfaces: nil, ApprovedFunctions: approved(fnKeyboard),
		},
		"duplicate interface id": {
			InsertionID: 1, EnumerationID: 1,
			Interfaces:        []Interface{goodIface, goodIface},
			ApprovedFunctions: approved(fnKeyboard),
		},
		"empty group": {
			InsertionID: 1, EnumerationID: 1,
			Interfaces:        []Interface{{ID: "x", Function: fnSerial, IsolationGroup: ""}},
			ApprovedFunctions: approved(fnSerial),
		},
		"coenable references unknown interface": {
			InsertionID: 1, EnumerationID: 1,
			Interfaces:        []Interface{goodIface},
			CoEnableSets:      [][]string{{"kbd", "missing"}},
			ApprovedFunctions: approved(fnKeyboard),
		},
		"singleton coenable set": {
			InsertionID: 1, EnumerationID: 1,
			Interfaces:        []Interface{goodIface},
			CoEnableSets:      [][]string{{"kbd"}},
			ApprovedFunctions: approved(fnKeyboard),
		},
	}
	for name, enum := range cases {
		t.Run(name, func(t *testing.T) {
			ar := New()
			_, err := ar.Adjudicate(enum)
			assertErrorIs(t, name, err, ErrInvalidEnumeration)
		})
	}
}

// TestEmptyArbiterRejectsAcks makes sure confirmations before any
// enumeration cannot enable anything.
func TestEmptyArbiterRejectsAcks(t *testing.T) {
	ar := New()
	assertErrorIs(t, "ack before adjudicate", ar.AckEnable(1, 1, "kbd"), ErrStaleRound)
	if got := ar.ActiveInterfaces(); len(got) != 0 {
		t.Fatalf("expected no active interfaces, got %v", got)
	}
}
