package routeauth

import (
	"errors"
	"testing"
	"time"
)

// Fixed UTC timeline used across the tests.
var (
	t0 = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	t1 = time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)
	t2 = time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)
	t3 = time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC)
	t4 = time.Date(2027, 1, 1, 0, 0, 0, 0, time.UTC)
)

func mustAdd(t *testing.T, e *Engine, a Authorization) {
	t.Helper()
	if err := e.Add(a); err != nil {
		t.Fatalf("Add(%q) unexpected error: %v", a.ID, err)
	}
}

func assertState(t *testing.T, d Decision, want State) {
	t.Helper()
	if d.State != want {
		t.Fatalf("decision state = %q, want %q (reason: %s)", d.State, want, d.Reason)
	}
}

func assertAssociated(t *testing.T, d Decision, want []string) {
	t.Helper()
	if len(d.Associated) != len(want) {
		t.Fatalf("associated = %v, want %v", d.Associated, want)
	}
	got := map[string]bool{}
	for _, id := range d.Associated {
		got[id] = true
	}
	for _, id := range want {
		if !got[id] {
			t.Fatalf("associated = %v, want to contain %q", d.Associated, id)
		}
	}
}

// baseROA is a 10.1.0.0/16 authorization permitting announcements up to /24
// from AS 65001, valid between t1 and t3.
func baseROA() Authorization {
	return Authorization{
		ID:              "roa-1",
		Prefix:          "10.1.0.0/16",
		MaxPrefixLength: 24,
		OriginAS:        65001,
		NotBefore:       t1,
		NotAfter:        t3,
	}
}

// TestValidAccepted covers the acceptable-result branch: an announcement
// inside the authorized prefix, within the max length, with matching origin.
func TestValidAccepted(t *testing.T) {
	e := NewEngine()
	mustAdd(t, e, baseROA())

	// More-specific announcement still within max length.
	d, err := e.EvaluateAt(Announcement{Prefix: "10.1.2.0/24", OriginAS: 65001}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateValid)
	assertAssociated(t, d, []string{"roa-1"})

	// Exact-prefix-length announcement.
	d, err = e.EvaluateAt(Announcement{Prefix: "10.1.0.0/16", OriginAS: 65001}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateValid)
}

// TestInvalidWrongOrigin is a blocked result: coverage exists but the origin
// AS does not match. It must be Invalid rather than NotFound.
func TestInvalidWrongOrigin(t *testing.T) {
	e := NewEngine()
	mustAdd(t, e, baseROA())

	d, err := e.EvaluateAt(Announcement{Prefix: "10.1.2.0/24", OriginAS: 65002}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateInvalid)
	// The covering-but-mismatched record is still associated with the decision.
	assertAssociated(t, d, []string{"roa-1"})
}

// TestInvalidPrefixTooSpecific covers the max-prefix-length boundary:
// length == max is accepted, length == max+1 is rejected as Invalid.
func TestInvalidPrefixTooSpecific(t *testing.T) {
	e := NewEngine()
	mustAdd(t, e, baseROA())

	ok, err := e.EvaluateAt(Announcement{Prefix: "10.1.2.0/24", OriginAS: 65001}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, ok, StateValid)

	bad, err := e.EvaluateAt(Announcement{Prefix: "10.1.2.128/25", OriginAS: 65001}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, bad, StateInvalid)
	assertAssociated(t, bad, []string{"roa-1"})
}

// TestNotFoundNoCoverage proves the "no covering authorization at all" branch
// is kept distinct from the Invalid branch.
func TestNotFoundNoCoverage(t *testing.T) {
	e := NewEngine()
	mustAdd(t, e, baseROA())

	d, err := e.EvaluateAt(Announcement{Prefix: "192.0.2.0/24", OriginAS: 65001}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateNotFound)
	if d.Associated != nil {
		t.Fatalf("NotFound associated = %v, want nil", d.Associated)
	}
}

// TestValidityWindowBoundaries exercises the interleaving of the evaluation
// instant with the record validity window:
//   - t == NotBefore  -> active (inclusive lower bound),
//   - t just before    -> NotFound,
//   - t == NotAfter    -> NotFound (exclusive upper bound),
//   - t just inside    -> Valid.
func TestValidityWindowBoundaries(t *testing.T) {
	e := NewEngine()
	mustAdd(t, e, baseROA())

	ann := Announcement{Prefix: "10.1.0.0/24", OriginAS: 65001}

	d, err := e.EvaluateAt(ann, t1)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateValid)

	d, err = e.EvaluateAt(ann, t1.Add(-time.Second))
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateNotFound)

	d, err = e.EvaluateAt(ann, t3)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateNotFound)

	d, err = e.EvaluateAt(ann, t3.Add(-time.Second))
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateValid)
}

// TestOverlappingAuthorizationsEitherMatch covers the migration scenario: an
// old and a new authorization overlap, and any active matching record lets
// the announcement through. It also interleaves coverage where two records
// cover one announcement but only one matches.
func TestOverlappingAuthorizationsEitherMatch(t *testing.T) {
	e := NewEngine()
	old := Authorization{
		ID: "old-as100", Prefix: "10.0.0.0/8", MaxPrefixLength: 24,
		OriginAS: 100, NotBefore: t0, NotAfter: t4,
	}
	new := Authorization{
		ID: "new-as200", Prefix: "10.1.0.0/16", MaxPrefixLength: 24,
		OriginAS: 200, NotBefore: t0, NotAfter: t4,
	}
	mustAdd(t, e, old)
	mustAdd(t, e, new)

	// New AS origin inside the migrated, more-specific prefix: matches new.
	d, err := e.EvaluateAt(Announcement{Prefix: "10.1.0.0/24", OriginAS: 200}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateValid)
	assertAssociated(t, d, []string{"new-as200"})

	// Old AS origin elsewhere in the old prefix: matches old.
	d, err = e.EvaluateAt(Announcement{Prefix: "10.2.0.0/24", OriginAS: 100}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateValid)
	assertAssociated(t, d, []string{"old-as100"})

	// Interleaving: both records cover 10.1.0.0/24, but an AS100 announcement
	// matches only the old record. The mismatch on the new record must not
	// poison the valid decision; only the matching record is associated.
	d, err = e.EvaluateAt(Announcement{Prefix: "10.1.0.0/24", OriginAS: 100}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateValid)
	assertAssociated(t, d, []string{"old-as100"})

	// Neither origin matches: Invalid, and both covering records are returned.
	d, err = e.EvaluateAt(Announcement{Prefix: "10.1.0.0/24", OriginAS: 300}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateInvalid)
	assertAssociated(t, d, []string{"old-as100", "new-as200"})
}

// TestRevocationOnlyWithdrawsTarget verifies that a revocation removes exactly
// the named record and nothing else, and that the revocation instant is a
// boundary: active before At, withdrawn at and after At.
func TestRevocationOnlyWithdrawsTarget(t *testing.T) {
	e := NewEngine()
	mustAdd(t, e, baseROA())
	other := Authorization{
		ID: "roa-2", Prefix: "10.1.0.0/16", MaxPrefixLength: 24,
		OriginAS: 65002, NotBefore: t1, NotAfter: t3,
	}
	mustAdd(t, e, other)

	ann := Announcement{Prefix: "10.1.5.0/24", OriginAS: 65001}

	// Before the revocation instant the record is still authoritative.
	if err := e.Revoke(Revocation{RecordID: "roa-1", At: t2}); err != nil {
		t.Fatalf("Revoke error: %v", err)
	}
	if !e.Revoked("roa-1") {
		t.Fatal("roa-1 should be marked revoked")
	}
	if e.Revoked("roa-2") {
		t.Fatal("roa-2 must not be revoked")
	}
	if got, ok := e.RevokedAt("roa-1"); !ok || !got.Equal(t2) {
		t.Fatalf("RevokedAt = %v (%v), want %v", got, ok, t2)
	}

	d, err := e.EvaluateAt(ann, t2.Add(-time.Second))
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateValid)

	// At the revocation instant (At <= t) the record is gone. roa-2 covers the
	// prefix but has a different origin, so the outcome degrades to Invalid
	// rather than NotFound.
	d, err = e.EvaluateAt(ann, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateInvalid)
	assertAssociated(t, d, []string{"roa-2"})

	// Withdrawing the second record as well leaves no covering authorization.
	if err := e.Revoke(Revocation{RecordID: "roa-2", At: t2}); err != nil {
		t.Fatalf("Revoke error: %v", err)
	}
	d, err = e.EvaluateAt(ann, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateNotFound)
}

// TestPointInTimeReplay verifies reproducibility: the same announcement at the
// same instant yields the same decision even after later state changes.
func TestPointInTimeReplay(t *testing.T) {
	e := NewEngine()
	mustAdd(t, e, baseROA())
	ann := Announcement{Prefix: "10.1.9.0/24", OriginAS: 65001}

	first, err := e.EvaluateAt(ann, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	if err := e.Revoke(Revocation{RecordID: "roa-1", At: t3.Add(-time.Hour)}); err != nil {
		t.Fatalf("Revoke error: %v", err)
	}

	// Replaying the historical instant must not see the later revocation.
	again, err := e.EvaluateAt(ann, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	if first.State != again.State {
		t.Fatalf("replay state = %q, want original %q", again.State, first.State)
	}
	assertAssociated(t, again, first.Associated)
}

// TestIPv6AndFamilyMismatch exercises the IPv6 branch and confirms an
// authorization of one family never covers an announcement of the other.
func TestIPv6AndFamilyMismatch(t *testing.T) {
	e := NewEngine()
	mustAdd(t, e, Authorization{
		ID: "v6", Prefix: "2001:db8::/32", MaxPrefixLength: 48,
		OriginAS: 65010, NotBefore: t1, NotAfter: t3,
	})

	d, err := e.EvaluateAt(Announcement{Prefix: "2001:db8:1::/48", OriginAS: 65010}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateValid)

	d, err = e.EvaluateAt(Announcement{Prefix: "2001:db8:1::/49", OriginAS: 65010}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateInvalid)

	d, err = e.EvaluateAt(Announcement{Prefix: "2001:db8:1::/48", OriginAS: 65011}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateInvalid)

	d, err = e.EvaluateAt(Announcement{Prefix: "2001:db9::/48", OriginAS: 65010}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateNotFound)

	// Family mismatch: IPv4 announcement cannot be covered by an IPv6 ROA.
	d, err = e.EvaluateAt(Announcement{Prefix: "2001:db8::/32", OriginAS: 65010}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateValid)

	d, err = e.EvaluateAt(Announcement{Prefix: "10.0.0.0/8", OriginAS: 65010}, t2)
	if err != nil {
		t.Fatalf("EvaluateAt error: %v", err)
	}
	assertState(t, d, StateNotFound)
}

// TestInputValidation covers malformed records and announcements.
func TestInputValidation(t *testing.T) {
	e := NewEngine()
	mustAdd(t, e, baseROA())

	cases := []struct {
		name string
		rec  Authorization
		want error
	}{
		{"duplicate id", baseROA(), ErrDuplicateRecord},
		{"empty id", Authorization{Prefix: "10.0.0.0/8", MaxPrefixLength: 8, OriginAS: 1, NotBefore: t1, NotAfter: t3}, ErrInvalidInput},
		{"bad prefix", Authorization{ID: "x", Prefix: "not-a-prefix", MaxPrefixLength: 24, OriginAS: 1, NotBefore: t1, NotAfter: t3}, ErrInvalidInput},
		{"max below prefix length", Authorization{ID: "x", Prefix: "10.0.0.0/16", MaxPrefixLength: 8, OriginAS: 1, NotBefore: t1, NotAfter: t3}, ErrInvalidInput},
		{"max above family width", Authorization{ID: "x", Prefix: "10.0.0.0/16", MaxPrefixLength: 33, OriginAS: 1, NotBefore: t1, NotAfter: t3}, ErrInvalidInput},
		{"reserved asn zero", Authorization{ID: "x", Prefix: "10.0.0.0/16", MaxPrefixLength: 24, OriginAS: 0, NotBefore: t1, NotAfter: t3}, ErrInvalidInput},
		{"empty window", Authorization{ID: "x", Prefix: "10.0.0.0/16", MaxPrefixLength: 24, OriginAS: 1, NotBefore: t3, NotAfter: t1}, ErrInvalidInput},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := e.Add(tc.rec)
			if !errors.Is(err, tc.want) {
				t.Fatalf("Add error = %v, want wrapping %v", err, tc.want)
			}
		})
	}

	if _, err := e.EvaluateAt(Announcement{Prefix: "garbage", OriginAS: 1}, t2); !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("bad announcement prefix error = %v, want ErrInvalidInput", err)
	}
	if _, err := e.EvaluateAt(Announcement{Prefix: "10.0.0.0/8", OriginAS: 0}, t2); !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("ASN0 announcement error = %v, want ErrInvalidInput", err)
	}
}

// TestRevocationValidation covers unknown and duplicate revocations.
func TestRevocationValidation(t *testing.T) {
	e := NewEngine()
	mustAdd(t, e, baseROA())

	if err := e.Revoke(Revocation{RecordID: "ghost", At: t2}); !errors.Is(err, ErrUnknownRecord) {
		t.Fatalf("unknown revoke error = %v, want ErrUnknownRecord", err)
	}
	if err := e.Revoke(Revocation{RecordID: "", At: t2}); !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("empty id revoke error = %v, want ErrInvalidInput", err)
	}
	if err := e.Revoke(Revocation{RecordID: "roa-1", At: t2}); err != nil {
		t.Fatalf("first Revoke error: %v", err)
	}
	if err := e.Revoke(Revocation{RecordID: "roa-1", At: t2}); !errors.Is(err, ErrAlreadyRevoked) {
		t.Fatalf("double revoke error = %v, want ErrAlreadyRevoked", err)
	}

	// A failed revocation must leave the engine unchanged.
	if _, ok := e.RevokedAt("ghost"); ok {
		t.Fatal("unknown record must not be reported as revoked")
	}
}

// TestGetRecord checks record retrieval helpers.
func TestGetRecord(t *testing.T) {
	e := NewEngine()
	if _, ok := e.Get("roa-1"); ok {
		t.Fatal("empty engine should not contain roa-1")
	}
	mustAdd(t, e, baseROA())
	got, ok := e.Get("roa-1")
	if !ok || got.OriginAS != 65001 || got.MaxPrefixLength != 24 {
		t.Fatalf("Get returned unexpected record: %+v (%v)", got, ok)
	}
}
