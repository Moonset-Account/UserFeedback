package trustlist_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"example.com/trustlist"
)

// --- exclusion patterns are per-line -------------------------------------

func TestExclusionDropsOnlyItsOwnLine(t *testing.T) {
	// Line 1 pins DBKEY but excludes the backup host. Line 2 pins the same
	// key for the backup host. Excluding line 1 must not disturb line 2.
	list := mustParse(t, `*.example.com,!backup.example.com pin ssh-ed25519:DBKEY
backup.example.com pin ssh-ed25519:DBKEY
`)

	main := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "DBKEY"), Now: testNow,
	})
	expect(t, main, true, trustlist.ReasonPinned, 1)
	if len(main.ExcludedLines) != 0 {
		t.Fatalf("ExcludedLines = %v, want none for db.example.com", main.ExcludedLines)
	}

	backup := list.Evaluate(trustlist.Request{
		Name: "backup.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "DBKEY"), Now: testNow,
	})
	expect(t, backup, true, trustlist.ReasonPinned, 2)
	if len(backup.ExcludedLines) != 1 || backup.ExcludedLines[0] != 1 {
		t.Fatalf("ExcludedLines = %v, want [1]", backup.ExcludedLines)
	}
}

func TestExclusionDropsRevocationAndPinAlike(t *testing.T) {
	// The lone revocation covers md5-key, but it is excluded for that host, so
	// the host falls back to its pin instead of being revoked.
	list := mustParse(t, `*.example.com,!legacy.example.com revoke ssh-ed25519:MD5KEY
legacy.example.com pin ssh-ed25519:MD5KEY
`)
	ev := list.Evaluate(trustlist.Request{
		Name: "legacy.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "MD5KEY"), Now: testNow,
	})
	expect(t, ev, true, trustlist.ReasonPinned, 2)
	if len(ev.ExcludedLines) != 1 || ev.ExcludedLines[0] != 1 {
		t.Fatalf("ExcludedLines = %v, want [1]", ev.ExcludedLines)
	}

	// The same reversed key on a host the exclusion does not name is revoked.
	other := list.Evaluate(trustlist.Request{
		Name: "other.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "MD5KEY"), Now: testNow,
	})
	expect(t, other, false, trustlist.ReasonRevoked, 1)
}

func TestExclusionCannotContaminateAnotherLinesScope(t *testing.T) {
	// Line 1 excludes only "a.example.com". Line 2 trusts the CA broadly. An
	// excluded line grants nothing, so a.example.com is still covered by 2.
	list := mustParse(t, `a.example.com,!a.example.com cert-authority ssh-ed25519:CAKEY
*.example.com cert-authority ssh-ed25519:CAKEY
`)
	ev := list.Evaluate(trustlist.Request{
		Name: "a.example.com", Port: 22,
		Certificate: cert("SUBJ", "CAKEY", "a.example.com"), Now: testNow,
	})
	expect(t, ev, true, trustlist.ReasonCertTrusted, 2)
	if len(ev.ExcludedLines) != 1 || ev.ExcludedLines[0] != 1 {
		t.Fatalf("ExcludedLines = %v, want [1]", ev.ExcludedLines)
	}
}

func TestExcludedRevocationDoesNotOverrideValidIssuerTrust(t *testing.T) {
	list := mustParse(t, `*.example.com,!app.example.com revoke ssh-ed25519:CAKEY
*.example.com cert-authority ssh-ed25519:CAKEY
`)
	ev := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22,
		Certificate: cert("SUBJ", "CAKEY", "app.example.com"), Now: testNow,
	})
	expect(t, ev, true, trustlist.ReasonCertTrusted, 2)
}

// --- revocation outranks pin and issuer trust ----------------------------

func TestRevocationOverridesPinWhenListedFirst(t *testing.T) {
	list := mustParse(t, `db.example.com revoke ssh-ed25519:KEY
db.example.com pin ssh-ed25519:KEY
`)
	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonRevoked, 1)
}

func TestRevocationOverridesPinWhenListedLast(t *testing.T) {
	// Same list, opposite order. Line position must not matter.
	list := mustParse(t, `db.example.com pin ssh-ed25519:KEY
db.example.com revoke ssh-ed25519:KEY
`)
	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonRevoked, 2)
}

func TestRevocationOfIssuerOverridesCertAuthority(t *testing.T) {
	list := mustParse(t, `*.example.com cert-authority ssh-ed25519:CAKEY
*.example.com revoke ssh-ed25519:CAKEY
`)
	ev := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22,
		Certificate: cert("SUBJ", "CAKEY", "app.example.com"), Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonRevoked, 2)
}

func TestRevocationOfSubjectOverridesCertAuthority(t *testing.T) {
	// The revoked key is the certificate subject, not its issuer: revoking a
	// subject kills that certificate even though its issuer is still trusted.
	list := mustParse(t, `*.example.com cert-authority ssh-ed25519:CAKEY
*.example.com revoke ssh-ed25519:SUBJ
`)
	ev := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22,
		Certificate: cert("SUBJ", "CAKEY", "app.example.com"), Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonRevoked, 2)
}

func TestRevocationIsScopedByNameAndPort(t *testing.T) {
	list := mustParse(t, `db.example.com port=22 revoke ssh-ed25519:KEY
db.example.com port=2222 pin ssh-ed25519:KEY
`)
	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 2222,
		HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
	})
	expect(t, ev, true, trustlist.ReasonPinned, 2)
}

func TestExpiredCertificateCannotBeRescuedByLaterRevocationOrder(t *testing.T) {
	// Revocation wins over trust regardless of order, but a revocation of an
	// unrelated key must not change an expiry denial into anything else.
	list := mustParse(t, `*.example.com cert-authority ssh-ed25519:CAKEY
*.example.com revoke ssh-ed25519:SOMEONE-ELSE
`)
	c := cert("SUBJ", "CAKEY", "app.example.com")
	c.NotAfter = testNow.Add(-time.Hour)
	ev := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22, Certificate: c, Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonCertificateExpired, 1)
}

// --- aliases and ports are judged separately -----------------------------

func TestTwoAliasesOfOneAddressAreIndependent(t *testing.T) {
	// Both names resolve to 203.0.113.7 in DNS. The client never resolves, so
	// trust granted to the name "db" must not transfer to "db-alias".
	list := mustParse(t, `db.example.com pin ssh-ed25519:KEY
`)
	pinned := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
	})
	expect(t, pinned, true, trustlist.ReasonPinned, 1)

	alias := list.Evaluate(trustlist.Request{
		Name: "db-alias.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
	})
	expect(t, alias, false, trustlist.ReasonNoTrustAnchor, 0)
}

func TestAliasSharingRevokedKeyIsStillRevoked(t *testing.T) {
	// The reverse direction: an alias with no line of its own cannot inherit
	// trust, but a broad revocation still reaches it.
	list := mustParse(t, `*.example.com revoke ssh-ed25519:BAD
db.example.com pin ssh-ed25519:GOOD
`)
	alias := list.Evaluate(trustlist.Request{
		Name: "wall.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "BAD"), Now: testNow,
	})
	expect(t, alias, false, trustlist.ReasonRevoked, 1)
}

func TestSameNameDifferentPortsAreIndependent(t *testing.T) {
	list := mustParse(t, `db.example.com port=22 pin ssh-ed25519:SSHKEY
db.example.com port=5432 pin ssh-ed25519:PGKEY
`)
	ssh := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "SSHKEY"), Now: testNow,
	})
	expect(t, ssh, true, trustlist.ReasonPinned, 1)

	pg := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 5432,
		HostKey: pkey("ssh-ed25519", "PGKEY"), Now: testNow,
	})
	expect(t, pg, true, trustlist.ReasonPinned, 2)

	// The SSH key on the database port is not pinned anywhere.
	crossed := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 5432,
		HostKey: pkey("ssh-ed25519", "SSHKEY"), Now: testNow,
	})
	expect(t, crossed, false, trustlist.ReasonKeyMismatch, 2)
}

func TestPortRangeBoundaries(t *testing.T) {
	list := mustParse(t, `db.example.com port=22-22 pin ssh-ed25519:KEY`)
	for _, tc := range []struct {
		port  int
		allow bool
	}{{21, false}, {22, true}, {23, false}} {
		ev := list.Evaluate(trustlist.Request{
			Name: "db.example.com", Port: tc.port,
			HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
		})
		if ev.Allowed != tc.allow {
			t.Fatalf("port %d Allowed = %v, want %v (%s)", tc.port, ev.Allowed, tc.allow, ev.Detail)
		}
	}
}

func TestPortOutOfScopeIsDistinctFromNoAnchor(t *testing.T) {
	list := mustParse(t, `db.example.com port=22 pin ssh-ed25519:KEY`)
	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 2022,
		HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonPortOutOfScope, 1)
}

// --- names ---------------------------------------------------------------

func TestNameNormalisation(t *testing.T) {
	list := mustParse(t, `DB.Example.COM. pin ssh-ed25519:KEY
[::1] pin ssh-ed25519:LOOPKEY
10.0.0.0/8 pin ssh-ed25519:NETKEY
`)
	for _, name := range []string{"db.example.com", "DB.EXAMPLE.COM", "db.example.com."} {
		ev := list.Evaluate(trustlist.Request{
			Name: name, Port: 22,
			HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
		})
		expect(t, ev, true, trustlist.ReasonPinned, 1)
	}

	loop := list.Evaluate(trustlist.Request{
		Name: "::1", Port: 22,
		HostKey: pkey("ssh-ed25519", "LOOPKEY"), Now: testNow,
	})
	expect(t, loop, true, trustlist.ReasonPinned, 2)

	net := list.Evaluate(trustlist.Request{
		Name: "10.4.5.6", Port: 22,
		HostKey: pkey("ssh-ed25519", "NETKEY"), Now: testNow,
	})
	expect(t, net, true, trustlist.ReasonPinned, 3)

	off := list.Evaluate(trustlist.Request{
		Name: "192.0.2.1", Port: 22,
		HostKey: pkey("ssh-ed25519", "NETKEY"), Now: testNow,
	})
	expect(t, off, false, trustlist.ReasonNoTrustAnchor, 0)
}

func TestGlobQuestionMarkDoesNotCrossLabels(t *testing.T) {
	list := mustParse(t, `db?.example.com pin ssh-ed25519:KEY`)
	one := list.Evaluate(trustlist.Request{
		Name: "db1.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
	})
	expect(t, one, true, trustlist.ReasonPinned, 1)

	two := list.Evaluate(trustlist.Request{
		Name: "db12.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
	})
	expect(t, two, false, trustlist.ReasonNoTrustAnchor, 0)
}

// --- line numbers --------------------------------------------------------

func TestLineNumbersMatchPhysicalFile(t *testing.T) {
	// Comments and blank lines consume numbers; the pin is on physical line 4.
	src := `# trust list
# second comment

db.example.com pin ssh-ed25519:DBKEY
`
	dir := t.TempDir()
	path := filepath.Join(dir, "trusted_hosts")
	if err := os.WriteFile(path, []byte(src), 0o600); err != nil {
		t.Fatalf("write fixture: %v", err)
	}

	list, err := trustlist.LoadFile(path)
	if err != nil {
		t.Fatalf("LoadFile() error = %v", err)
	}
	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "DBKEY"), Now: testNow,
	})
	expect(t, ev, true, trustlist.ReasonPinned, 4)
}

func TestCRLFLineNumbersArePreserved(t *testing.T) {
	list := mustParse(t, "# c\r\ndb.example.com pin ssh-ed25519:KEY\r\n")
	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
	})
	expect(t, ev, true, trustlist.ReasonPinned, 2)
}

// --- fail-closed parsing -------------------------------------------------

func TestMalformedLinesAreReportedAndSkipped(t *testing.T) {
	list := mustParse(t, `db.example.com pin
db.example.com pin ssh-ed25519:KEY extra-token
db.example.com sideways ssh-ed25519:KEY
db.example.com port=99999 pin ssh-ed25519:KEY
db.example.com port=9000-80 pin ssh-ed25519:KEY
,!db.example.com pin ssh-ed25519:KEY
db.example.com pin ssh-ed25519:GOOD
`)
	if list.Len() != 1 {
		t.Fatalf("Len = %d, want 1 accepted entry", list.Len())
	}
	issues := list.Issues()
	if len(issues) != 6 {
		t.Fatalf("got %d issues, want 6: %+v", len(issues), issues)
	}
	for i, issue := range issues {
		if issue.Kind != trustlist.IssueSyntax {
			t.Fatalf("issue %d Kind = %q, want %q", i, issue.Kind, trustlist.IssueSyntax)
		}
		if issue.Line != i+1 {
			t.Fatalf("issue %d Line = %d, want %d", i, issue.Line, i+1)
		}
	}

	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "GOOD"), Now: testNow,
	})
	expect(t, ev, true, trustlist.ReasonPinned, 7)
	if len(ev.Issues) != 6 {
		t.Fatalf("Evaluation issues = %d, want 6", len(ev.Issues))
	}
}

func TestBadPatternNeverWidensScope(t *testing.T) {
	// An entry with only an exclusion is rejected, so it can never behave as
	// an implicit wildcard.
	list := mustParse(t, `!other.example.com pin ssh-ed25519:KEY`)
	if list.Len() != 0 {
		t.Fatalf("Len = %d, want 0", list.Len())
	}
	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonNoTrustAnchor, 0)
}

func TestNoEvaluationClockIsRejected(t *testing.T) {
	list := mustParse(t, `*.example.com cert-authority ssh-ed25519:CAKEY`)
	ev := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22,
		Certificate: cert("SUBJ", "CAKEY", "app.example.com"),
	})
	expect(t, ev, false, trustlist.ReasonInvalidRequest, 0)
}

func TestDefaultClockAppliesWhenRequestHasNone(t *testing.T) {
	list := mustParse(t, `*.example.com cert-authority ssh-ed25519:CAKEY`).SetNow(testNow)
	ev := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22,
		Certificate: cert("SUBJ", "CAKEY", "app.example.com"),
	})
	expect(t, ev, true, trustlist.ReasonCertTrusted, 1)
}

func TestConflictingCredentialsRejected(t *testing.T) {
	list := mustParse(t, `*.example.com cert-authority ssh-ed25519:CAKEY`)
	ev := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22,
		HostKey:     pkey("ssh-ed25519", "SUBJ"),
		Certificate: cert("SUBJ", "CAKEY", "app.example.com"),
		Now:         testNow,
	})
	expect(t, ev, false, trustlist.ReasonInvalidRequest, 0)
}

// --- certificate coverage is not inherited from the list pattern ---------

func TestListPatternDoesNotSubstituteForCertificateCoverage(t *testing.T) {
	// The list says "*.example.com", which matches the connection name, but
	// the certificate is for a different name. The second check is independent.
	list := mustParse(t, `*.example.com cert-authority ssh-ed25519:CAKEY`)
	ev := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22,
		Certificate: cert("SUBJ", "CAKEY", "*.internal.example.net"), Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonNameNotCovered, 1)
}

func TestCertificatePrincipalMayBeGlob(t *testing.T) {
	list := mustParse(t, `*.example.com cert-authority ssh-ed25519:CAKEY`)
	ev := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22,
		Certificate: cert("SUBJ", "CAKEY", "*.example.com"), Now: testNow,
	})
	expect(t, ev, true, trustlist.ReasonCertTrusted, 1)
}

// --- end-to-end audit ----------------------------------------------------

func TestCombinedScenarioKeepsProvenance(t *testing.T) {
	// Every comment below states the physical line number of the entry it
	// describes, so a mismatch with the reported provenance is a real bug.
	list := mustParse(t, `*.example.com revoke ssh-ed25519:ROGUE-CA
# line 2 excluded for the bastion, so bastion's own pin is what counts
*.example.com,!bastion.example.com pin ssh-ed25519:SHARED
bastion.example.com port=22 pin ssh-ed25519:BASTION
bastion.example.com port=2222 revoke ssh-ed25519:BASTION
`)
	ssh := list.Evaluate(trustlist.Request{
		Name: "bastion.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "BASTION"), Now: testNow,
	})
	expect(t, ssh, true, trustlist.ReasonPinned, 4)
	if len(ssh.ExcludedLines) != 1 || ssh.ExcludedLines[0] != 3 {
		t.Fatalf("ExcludedLines = %v, want [3]", ssh.ExcludedLines)
	}

	admin := list.Evaluate(trustlist.Request{
		Name: "bastion.example.com", Port: 2222,
		HostKey: pkey("ssh-ed25519", "BASTION"), Now: testNow,
	})
	expect(t, admin, false, trustlist.ReasonRevoked, 5)

	rogue := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22,
		Certificate: cert("SUBJ", "ROGUE-CA", "app.example.com"), Now: testNow,
	})
	expect(t, rogue, false, trustlist.ReasonRevoked, 1)
}

func TestAllowedShorthandAgreesWithEvaluate(t *testing.T) {
	list := mustParse(t, `db.example.com pin ssh-ed25519:KEY`)
	req := trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
	}
	if got, want := list.Allowed(req), list.Evaluate(req).Allowed; got != want {
		t.Fatalf("Allowed() = %v, Evaluate().Allowed = %v", got, want)
	}
	if !list.Allowed(req) {
		t.Fatal("Allowed() = false, want true")
	}
	req.HostKey = pkey("ssh-ed25519", "OTHER")
	if list.Allowed(req) {
		t.Fatal("Allowed() = true for a mismatched key")
	}
}

func TestParseNeverReturnsNilEntryOnIOError(t *testing.T) {
	if _, err := trustlist.Parse(nil); err == nil {
		t.Fatal("Parse(nil) error = nil, want error")
	}
	if _, err := trustlist.Parse(strings.NewReader(`db pin ssh-ed25519:K`)); err != nil {
		t.Fatalf("well-formed list error = %v", err)
	}
}
