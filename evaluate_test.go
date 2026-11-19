package trustlist_test

import (
	"strings"
	"testing"
	"time"

	"example.com/trustlist"
)

// Fixed synthetic clock so nothing in the suite depends on wall time.
var testNow = time.Date(2026, 11, 20, 12, 0, 0, 0, time.UTC)

func pkey(t, blob string) trustlist.PublicKey {
	return trustlist.NewPublicKey(t, blob)
}

// mustParse fails the test if the list does not compile.
func mustParse(t *testing.T, src string) *trustlist.List {
	t.Helper()
	list, err := trustlist.Parse(strings.NewReader(src))
	if err != nil {
		t.Fatalf("Parse() error = %v", err)
	}
	if list == nil {
		t.Fatal("Parse() returned nil list")
	}
	return list
}

// cert builds a certificate valid around testNow.
func cert(subject, issuer string, principals ...string) *trustlist.HostCertificate {
	return &trustlist.HostCertificate{
		Subject:           pkey("ssh-ed25519", subject),
		Issuer:            pkey("ssh-ed25519", issuer),
		Principals:        principals,
		NotBefore:         testNow.Add(-24 * time.Hour),
		NotAfter:          testNow.Add(24 * time.Hour),
		SignatureVerified: true,
	}
}

// expect checks every field of a decision that the callers care about.
func expect(t *testing.T, ev trustlist.Evaluation, allowed bool, reason trustlist.Reason, line int) {
	t.Helper()
	if ev.Allowed != allowed {
		t.Fatalf("Allowed = %v, want %v (reason=%s detail=%q)", ev.Allowed, allowed, ev.Reason, ev.Detail)
	}
	if ev.Reason != reason {
		t.Fatalf("Reason = %q, want %q (detail=%q)", ev.Reason, reason, ev.Detail)
	}
	if ev.Line != line {
		t.Fatalf("Line = %d, want %d (detail=%q)", ev.Line, line, ev.Detail)
	}
}

// --- accepted outcomes ---------------------------------------------------

func TestPinAllowsExactHostKey(t *testing.T) {
	list := mustParse(t, `# comment keeping line numbers honest
db.example.com port=22-2222 pin ssh-ed25519:DBKEY
`)
	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "DBKEY"), Now: testNow,
	})
	expect(t, ev, true, trustlist.ReasonPinned, 2)
	if ev.ExcludedLines != nil {
		t.Fatalf("ExcludedLines = %v, want none", ev.ExcludedLines)
	}
}

func TestWildcardPinOnAnyPort(t *testing.T) {
	list := mustParse(t, `*.example.com pin ssh-ed25519:KEY`)
	for _, port := range []int{1, 22, 65535} {
		ev := list.Evaluate(trustlist.Request{
			Name: "web.example.com", Port: port,
			HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
		})
		expect(t, ev, true, trustlist.ReasonPinned, 1)
	}
}

func TestCertAuthorityAllowsVerifiedCertificate(t *testing.T) {
	list := mustParse(t, `*.example.com cert-authority ssh-ed25519:CAKEY`)
	ev := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22,
		Certificate: cert("SUBJ", "CAKEY", "*.example.com"), Now: testNow,
	})
	expect(t, ev, true, trustlist.ReasonCertTrusted, 1)
}

func TestCertAuthorityReportsFirstTrustingLine(t *testing.T) {
	list := mustParse(t, `not-this.example.org cert-authority ssh-ed25519:CAKEY
*.example.com cert-authority ssh-ed25519:CAKEY
app.example.com cert-authority ssh-ed25519:CAKEY
`)
	ev := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22,
		Certificate: cert("SUBJ", "CAKEY", "app.example.com"), Now: testNow,
	})
	expect(t, ev, true, trustlist.ReasonCertTrusted, 2)
}

// --- blocked / rejected outcomes -----------------------------------------

func TestPinRejectsDifferentHostKey(t *testing.T) {
	list := mustParse(t, `db.example.com pin ssh-ed25519:DBKEY`)
	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "ATTACKER"), Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonKeyMismatch, 1)
}

func TestUntrustedIssuerRejected(t *testing.T) {
	list := mustParse(t, `*.example.com cert-authority ssh-ed25519:CAKEY`)
	ev := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22,
		Certificate: cert("SUBJ", "ROGUE-CA", "app.example.com"), Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonIssuerNotTrusted, 1)
}

func TestUnverifiedSignatureRejected(t *testing.T) {
	list := mustParse(t, `*.example.com cert-authority ssh-ed25519:CAKEY`)
	c := cert("SUBJ", "CAKEY", "app.example.com")
	c.SignatureVerified = false
	ev := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22, Certificate: c, Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonSignatureUnverified, 1)
}

func TestCertificateMustCoverConnectionName(t *testing.T) {
	list := mustParse(t, `*.example.com cert-authority ssh-ed25519:CAKEY`)
	ev := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22,
		Certificate: cert("SUBJ", "CAKEY", "other.example.com"), Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonNameNotCovered, 1)
}

func TestNoMatchingLineDenies(t *testing.T) {
	list := mustParse(t, `db.example.com pin ssh-ed25519:DBKEY`)
	ev := list.Evaluate(trustlist.Request{
		Name: "unknown.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "DBKEY"), Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonNoTrustAnchor, 0)
}

func TestEmptyListDenies(t *testing.T) {
	list := mustParse(t, "# only comments\n\n")
	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "DBKEY"), Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonNoTrustAnchor, 0)
	if list.Len() != 0 {
		t.Fatalf("Len = %d, want 0", list.Len())
	}
}

// --- validity-window boundary -------------------------------------------

func TestCertificateValidityWindowIsInclusive(t *testing.T) {
	cases := []struct {
		name  string
		at    time.Time
		allow bool
	}{
		{"one nanosecond before NotBefore", testNow.Add(-24*time.Hour - time.Nanosecond), false},
		{"exactly NotBefore", testNow.Add(-24 * time.Hour), true},
		{"middle of window", testNow, true},
		{"one nanosecond before NotAfter", testNow.Add(24*time.Hour - time.Nanosecond), true},
		{"exactly NotAfter", testNow.Add(24 * time.Hour), false},
	}
	list := mustParse(t, `*.example.com cert-authority ssh-ed25519:CAKEY`)
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ev := list.Evaluate(trustlist.Request{
				Name: "app.example.com", Port: 22,
				Certificate: cert("SUBJ", "CAKEY", "app.example.com"), Now: tc.at,
			})
			if ev.Allowed != tc.allow {
				t.Fatalf("Allowed at %s = %v, want %v (%s)", tc.at, ev.Allowed, tc.allow, ev.Detail)
			}
			if !tc.allow && ev.Reason != trustlist.ReasonCertificateExpired {
				t.Fatalf("Reason = %q, want %q", ev.Reason, trustlist.ReasonCertificateExpired)
			}
		})
	}
}

func TestZeroNotAfterIsAlwaysExpired(t *testing.T) {
	list := mustParse(t, `*.example.com cert-authority ssh-ed25519:CAKEY`)
	c := cert("SUBJ", "CAKEY", "app.example.com")
	c.NotAfter = time.Time{}
	ev := list.Evaluate(trustlist.Request{
		Name: "app.example.com", Port: 22, Certificate: c, Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonCertificateExpired, 1)
}
