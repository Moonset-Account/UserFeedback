package trustlist_test

import (
	"testing"

	"example.com/trustlist"
)

// --- key comparison ------------------------------------------------------

func TestKeyTypeIsCaseInsensitive(t *testing.T) {
	list := mustParse(t, `db.example.com pin SSH-Ed25519:DBKEY`)
	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "DBKEY"), Now: testNow,
	})
	expect(t, ev, true, trustlist.ReasonPinned, 1)
}

func TestKeyBlobIsCaseSensitive(t *testing.T) {
	// Key blobs are base64: changing case changes the key. The library must
	// compare them byte for byte, never case-insensitively.
	list := mustParse(t, `db.example.com pin ssh-ed25519:dbkey`)
	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "DBKEY"), Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonKeyMismatch, 1)
}

func TestDifferentKeyTypeDoesNotMatch(t *testing.T) {
	list := mustParse(t, `db.example.com pin ssh-ed25519:SAME`)
	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-rsa", "SAME"), Now: testNow,
	})
	expect(t, ev, false, trustlist.ReasonKeyMismatch, 1)
}

func TestKeyAcceptsSpaceSeparatedForm(t *testing.T) {
	list := mustParse(t, "db.example.com pin ssh-ed25519 DBKEY\n")
	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "DBKEY"), Now: testNow,
	})
	expect(t, ev, true, trustlist.ReasonPinned, 1)
}

// --- port range parser ---------------------------------------------------

func TestParsePortRangeForms(t *testing.T) {
	cases := []struct {
		spec    string
		port    int
		contain bool
	}{
		{"*", 1, true},
		{"*", 65535, true},
		{"22", 22, true},
		{"22", 23, false},
		{"2200-2222", 2199, false},
		{"2200-2222", 2200, true},
		{"2200-2222", 2222, true},
		{"2200-2222", 2223, false},
	}
	for _, tc := range cases {
		r, err := trustlist.ParsePortRange(tc.spec)
		if err != nil {
			t.Fatalf("ParsePortRange(%q) error = %v", tc.spec, err)
		}
		if got := r.Contains(tc.port); got != tc.contain {
			t.Fatalf("%q.Contains(%d) = %v, want %v", tc.spec, tc.port, got, tc.contain)
		}
	}
}

func TestParsePortRangeRejectsBadInput(t *testing.T) {
	for _, spec := range []string{"", "0", "65536", "abc", "22-", "-22", "9000-80", "22-33-44"} {
		if r, err := trustlist.ParsePortRange(spec); err == nil {
			t.Fatalf("ParsePortRange(%q) = %v, want error", spec, r)
		}
	}
}

func TestZeroPortRangeMatchesNothing(t *testing.T) {
	// The zero value is the "unset" state and must fail closed rather than
	// mean "any port".
	var r trustlist.PortRange
	for _, port := range []int{0, 22, 65535} {
		if r.Contains(port) {
			t.Fatalf("zero PortRange.Contains(%d) = true, want false", port)
		}
	}
	if !trustlist.AnyPort().Contains(22) {
		t.Fatal("AnyPort().Contains(22) = false")
	}
}

func TestPortStarSyntaxInList(t *testing.T) {
	list := mustParse(t, `db.example.com port=* pin ssh-ed25519:KEY`)
	for _, port := range []int{1, 22, 65535} {
		ev := list.Evaluate(trustlist.Request{
			Name: "db.example.com", Port: port,
			HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
		})
		expect(t, ev, true, trustlist.ReasonPinned, 1)
	}
}

// --- request validation --------------------------------------------------

func TestRequestValidation(t *testing.T) {
	list := mustParse(t, `db.example.com pin ssh-ed25519:KEY`)
	cases := []struct {
		name string
		req  trustlist.Request
	}{
		{"no name", trustlist.Request{Port: 22, HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow}},
		{"port zero", trustlist.Request{Name: "db.example.com", Port: 0, HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow}},
		{"port too high", trustlist.Request{Name: "db.example.com", Port: 70000, HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow}},
		{"no credential", trustlist.Request{Name: "db.example.com", Port: 22, Now: testNow}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ev := list.Evaluate(tc.req)
			expect(t, ev, false, trustlist.ReasonInvalidRequest, 0)
		})
	}
}

// --- multiple positive patterns -----------------------------------------

func TestMultiplePositivePatternsWidenOneLine(t *testing.T) {
	list := mustParse(t, `db.example.com,cache.example.com port=22 pin ssh-ed25519:SHARED`)
	for _, name := range []string{"db.example.com", "cache.example.com"} {
		ev := list.Evaluate(trustlist.Request{
			Name: name, Port: 22,
			HostKey: pkey("ssh-ed25519", "SHARED"), Now: testNow,
		})
		expect(t, ev, true, trustlist.ReasonPinned, 1)
	}
	other := list.Evaluate(trustlist.Request{
		Name: "web.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "SHARED"), Now: testNow,
	})
	expect(t, other, false, trustlist.ReasonNoTrustAnchor, 0)
}

// --- empty and whitespace-only input ------------------------------------

func TestBlankLinesAndWhitespaceIgnored(t *testing.T) {
	list := mustParse(t, "\n   \n\t\n# c\n  db.example.com   pin   ssh-ed25519:KEY  \n\n")
	if list.Len() != 1 {
		t.Fatalf("Len = %d, want 1", list.Len())
	}
	if issues := list.Issues(); len(issues) != 0 {
		t.Fatalf("Issues = %+v, want none", issues)
	}
	ev := list.Evaluate(trustlist.Request{
		Name: "db.example.com", Port: 22,
		HostKey: pkey("ssh-ed25519", "KEY"), Now: testNow,
	})
	expect(t, ev, true, trustlist.ReasonPinned, 5)
}
