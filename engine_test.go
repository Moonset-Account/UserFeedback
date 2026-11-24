package tlsresume

import (
	"testing"
	"time"
)

var base = time.Date(2026, 11, 24, 12, 0, 0, 0, time.UTC)

func fixedClock(t time.Time) func() time.Time {
	return func() time.Time { return t }
}

func baseConfig(now time.Time) Config {
	return Config{
		Entries: map[EntryName]EntryPolicy{
			"edge-a": {
				IsolationDomain: "domain-1",
				AllowedSources:  map[SourceID]bool{"issuer-ok": true},
				Profiles: map[ProfileID]ProfilePolicy{
					"profile-1": {SupportsEarlyData: true},
				},
				Classes: map[RequestClass]ClassPolicy{
					"catalog-read": {ReadOnly: true, ConfirmationLevel: 0},
					"order-write":  {ReadOnly: false, ConfirmationLevel: 0},
					"admin-read":   {ReadOnly: true, ConfirmationLevel: 2},
				},
				EarlyDataMaxConfirmation: 1,
			},
		},
		Aliases: map[EntryName]Alias{},
		Now:     fixedClock(now),
	}
}

func validTicket() Ticket {
	return Ticket{
		Source:          "issuer-ok",
		Entry:           "edge-a",
		IsolationDomain: "domain-1",
		Profile:         "profile-1",
		NotAfter:        base.Add(time.Hour),
	}
}

func TestEarlyDataAccepted(t *testing.T) {
	e := New(baseConfig(base))
	d := e.Adjudicate(validTicket(), Request{Class: "catalog-read"})
	if d.Outcome != OutcomeAcceptEarlyData {
		t.Fatalf("outcome = %v, want %v (%s)", d.Outcome, OutcomeAcceptEarlyData, d.Reason)
	}
	if d.Code != CodeEarlyDataAccepted {
		t.Fatalf("code = %v", d.Code)
	}
	if d.Entry != "edge-a" {
		t.Fatalf("entry = %q", d.Entry)
	}
	if d.Reason == "" {
		t.Fatal("empty reason")
	}
}

func TestWaitReasons(t *testing.T) {
	now := base
	cfg := baseConfig(now)
	// A profile that explicitly opts out of early data.
	cfg.Entries["edge-a"].Profiles["profile-no0rtt"] = ProfilePolicy{SupportsEarlyData: false}

	cases := []struct {
		name   string
		ticket Ticket
		class  RequestClass
		code   ReasonCode
	}{
		{"not read only", validTicket(), "order-write", CodeClassNotReadOnly},
		{"confirmation too high", validTicket(), "admin-read", CodeConfirmationTooHigh},
		{"profile no early data", func() Ticket {
			tk := validTicket()
			tk.Profile = "profile-no0rtt"
			return tk
		}(), "catalog-read", CodeEarlyDataUnsupported},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := New(cfg).Adjudicate(tc.ticket, Request{Class: tc.class})
			if d.Outcome != OutcomeWaitForHandshake {
				t.Fatalf("outcome = %v, want wait", d.Outcome)
			}
			if d.Code != tc.code {
				t.Fatalf("code = %v, want %v", d.Code, tc.code)
			}
			if d.Reason == "" {
				t.Fatal("empty reason")
			}
		})
	}
}

func TestConfirmationAtLimitIsAllowed(t *testing.T) {
	cfg := baseConfig(base)
	entry := cfg.Entries["edge-a"]
	entry.Classes["at-limit"] = ClassPolicy{ReadOnly: true, ConfirmationLevel: 1}
	cfg.Entries["edge-a"] = entry

	d := New(cfg).Adjudicate(validTicket(), Request{Class: "at-limit"})
	if d.Outcome != OutcomeAcceptEarlyData {
		t.Fatalf("level equal to max should be accepted, got %v (%s)", d.Outcome, d.Reason)
	}
}

func TestCompatibilityWindowStillResumes(t *testing.T) {
	now := base
	cfg := baseConfig(now)
	cfg.ProfileCompatibilityWindows = map[ProfileID]time.Duration{"profile-1": 24 * time.Hour}
	entry := cfg.Entries["edge-a"]
	entry.Profiles["profile-1"] = ProfilePolicy{RetiredAt: now.Add(-time.Hour), SupportsEarlyData: true}
	cfg.Entries["edge-a"] = entry

	// Ordinary read-only class: resume but wait; early data is refused.
	d := New(cfg).Adjudicate(validTicket(), Request{Class: "catalog-read"})
	if d.Outcome != OutcomeWaitForHandshake || d.Code != CodeProfileCompatibility {
		t.Fatalf("got %v/%v (%s)", d.Outcome, d.Code, d.Reason)
	}

	// Higher confirmation class: also wait, distinct reason.
	d = New(cfg).Adjudicate(validTicket(), Request{Class: "admin-read"})
	if d.Outcome != OutcomeWaitForHandshake || d.Code != CodeProfileCompatibilityHigh {
		t.Fatalf("got %v/%v (%s)", d.Outcome, d.Code, d.Reason)
	}
}

func TestCompatibilityWindowBoundaries(t *testing.T) {
	cfg := baseConfig(base)
	cfg.ProfileCompatibilityWindows = map[ProfileID]time.Duration{"profile-1": time.Hour}
	entry := cfg.Entries["edge-a"]

	// Exactly at retirement: still resumable via compatibility.
	entry.Profiles["profile-1"] = ProfilePolicy{RetiredAt: base}
	cfg.Entries["edge-a"] = entry
	d := New(cfg).Adjudicate(validTicket(), Request{Class: "catalog-read"})
	if d.Outcome != OutcomeWaitForHandshake || d.Code != CodeProfileCompatibility {
		t.Fatalf("at retirement got %v/%v (%s)", d.Outcome, d.Code, d.Reason)
	}

	// Exactly at the end of the window: refused. The ticket stays valid so
	// the profile, not the ticket, is what fails.
	cfg.Now = fixedClock(base.Add(time.Hour))
	tk := validTicket()
	tk.NotAfter = base.Add(2 * time.Hour)
	d = New(cfg).Adjudicate(tk, Request{Class: "catalog-read"})
	if d.Outcome != OutcomeFullHandshake || d.Code != CodeCompatibilityExpired {
		t.Fatalf("at window end got %v/%v (%s)", d.Outcome, d.Code, d.Reason)
	}
}

func TestZeroWindowDisablesCompatibility(t *testing.T) {
	cfg := baseConfig(base)
	entry := cfg.Entries["edge-a"]
	entry.Profiles["profile-1"] = ProfilePolicy{RetiredAt: base.Add(-time.Second)}
	cfg.Entries["edge-a"] = entry
	// Default and per-profile windows are both zero.

	d := New(cfg).Adjudicate(validTicket(), Request{Class: "catalog-read"})
	if d.Outcome != OutcomeFullHandshake || d.Code != CodeCompatibilityExpired {
		t.Fatalf("got %v/%v (%s)", d.Outcome, d.Code, d.Reason)
	}
}

func TestFullHandshakeRejections(t *testing.T) {
	type mutator func(*Config, *Ticket)

	cases := []struct {
		name string
		code ReasonCode
		mut  mutator
	}{
		{"unknown entry", CodeEntryUnknown, func(c *Config, tk *Ticket) { tk.Entry = "edge-x" }},
		{"source denied", CodeSourceDenied, func(c *Config, tk *Ticket) { tk.Source = "issuer-bad" }},
		{"empty sources denies all", CodeSourceDenied, func(c *Config, tk *Ticket) {
			e := c.Entries["edge-a"]
			e.AllowedSources = nil
			c.Entries["edge-a"] = e
		}},
		{"domain unset", CodeEntryDomainUnset, func(c *Config, tk *Ticket) {
			e := c.Entries["edge-a"]
			e.IsolationDomain = ""
			c.Entries["edge-a"] = e
		}},
		{"domain mismatch", CodeDomainMismatch, func(c *Config, tk *Ticket) { tk.IsolationDomain = "domain-2" }},
		{"ticket expired", CodeTicketExpired, func(c *Config, tk *Ticket) { tk.NotAfter = base }},
		{"ticket without expiry", CodeTicketInvalid, func(c *Config, tk *Ticket) { tk.NotAfter = time.Time{} }},
		{"profile unknown", CodeProfileUnknown, func(c *Config, tk *Ticket) { tk.Profile = "profile-x" }},
		{"class unregistered", CodeClassUnregistered, func(c *Config, tk *Ticket) { tk.Entry = "edge-a" }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := baseConfig(base)
			tk := validTicket()
			tc.mut(&cfg, &tk)
			req := Request{Class: "catalog-read"}
			if tc.name == "class unregistered" {
				req.Class = "ghost"
			}
			d := New(cfg).Adjudicate(tk, req)
			if d.Outcome != OutcomeFullHandshake {
				t.Fatalf("outcome = %v, want full handshake (%s)", d.Outcome, d.Reason)
			}
			if d.Code != tc.code {
				t.Fatalf("code = %v, want %v", d.Code, tc.code)
			}
			if d.Reason == "" {
				t.Fatal("empty reason")
			}
		})
	}
}

func TestAliasResolution(t *testing.T) {
	cfg := baseConfig(base)
	cfg.Aliases["edge-old"] = Alias{NewEntry: "edge-a", NotAfter: base.Add(time.Hour)}

	tk := validTicket()
	tk.Entry = "edge-old"
	d := New(cfg).Adjudicate(tk, Request{Class: "catalog-read"})
	if d.Outcome != OutcomeAcceptEarlyData {
		t.Fatalf("got %v/%v (%s)", d.Outcome, d.Code, d.Reason)
	}
	if d.Entry != "edge-a" {
		t.Fatalf("resolved entry = %q, want edge-a", d.Entry)
	}
}

func TestAliasFailures(t *testing.T) {
	cases := []struct {
		name  string
		alias Alias
		set   bool
		code  ReasonCode
	}{
		{"expired alias", Alias{NewEntry: "edge-a", NotAfter: base}, true, CodeAliasExpired},
		{"alias without expiry", Alias{NewEntry: "edge-a"}, true, CodeAliasInvalid},
		{"alias to self", Alias{NewEntry: "edge-old", NotAfter: base.Add(time.Hour)}, true, CodeAliasInvalid},
		{"alias to missing entry", Alias{NewEntry: "edge-gone", NotAfter: base.Add(time.Hour)}, true, CodeAliasInvalid},
		{"alias chain", Alias{NewEntry: "edge-old2", NotAfter: base.Add(time.Hour)}, true, CodeAliasInvalid},
		{"no alias at all", Alias{}, false, CodeEntryUnknown},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := baseConfig(base)
			if tc.set {
				cfg.Aliases["edge-old"] = tc.alias
			}
			if tc.name == "alias chain" {
				// edge-old -> edge-old2, and edge-old2 -> edge-a.
				cfg.Aliases["edge-old2"] = Alias{NewEntry: "edge-a", NotAfter: base.Add(time.Hour)}
			}
			tk := validTicket()
			tk.Entry = "edge-old"
			d := New(cfg).Adjudicate(tk, Request{Class: "catalog-read"})
			if d.Outcome != OutcomeFullHandshake {
				t.Fatalf("outcome = %v (%s)", d.Outcome, d.Reason)
			}
			if d.Code != tc.code {
				t.Fatalf("code = %v, want %v", d.Code, tc.code)
			}
			if d.Entry != "" {
				t.Fatalf("entry = %q, want empty on failure", d.Entry)
			}
		})
	}
}

func TestNilEngine(t *testing.T) {
	var e *Engine
	d := e.Adjudicate(validTicket(), Request{Class: "catalog-read"})
	if d.Outcome != OutcomeFullHandshake || d.Code != CodeEngineMissing {
		t.Fatalf("got %v/%v", d.Outcome, d.Code)
	}
}

func TestClockCalledOnce(t *testing.T) {
	calls := 0
	cfg := baseConfig(base)
	cfg.Now = func() time.Time { calls++; return base }
	New(cfg).Adjudicate(validTicket(), Request{Class: "catalog-read"})
	if calls != 1 {
		t.Fatalf("clock called %d times, want 1", calls)
	}
}

func TestProfileWindowPerProfileOverridesDefault(t *testing.T) {
	cfg := baseConfig(base)
	cfg.DefaultCompatibilityWindow = time.Hour
	cfg.ProfileCompatibilityWindows = map[ProfileID]time.Duration{"profile-1": 10 * time.Hour}
	entry := cfg.Entries["edge-a"]
	entry.Profiles["profile-1"] = ProfilePolicy{RetiredAt: base.Add(-2 * time.Hour), SupportsEarlyData: true}
	cfg.Entries["edge-a"] = entry

	// Beyond the default but within the per-profile window.
	d := New(cfg).Adjudicate(validTicket(), Request{Class: "catalog-read"})
	if d.Outcome != OutcomeWaitForHandshake || d.Code != CodeProfileCompatibility {
		t.Fatalf("got %v/%v (%s)", d.Outcome, d.Code, d.Reason)
	}
}

func TestOutcomeString(t *testing.T) {
	cases := map[Outcome]string{
		OutcomeFullHandshake:    "full_handshake",
		OutcomeWaitForHandshake: "wait_for_handshake",
		OutcomeAcceptEarlyData:  "accept_early_data",
		Outcome(99):             "outcome(99)",
	}
	for o, want := range cases {
		if got := o.String(); got != want {
			t.Fatalf("%d.String() = %q, want %q", int(o), got, want)
		}
	}
}

// TestCheckOrder verifies that a failure in an earlier rule wins the reason,
// even when several rules would independently reject the ticket.
func TestCheckOrder(t *testing.T) {
	cfg := baseConfig(base)
	entry := cfg.Entries["edge-a"]
	// Retired far enough back that it is also past its (zero) compatibility
	// window, so profile state would fail too.
	entry.Profiles["profile-1"] = ProfilePolicy{RetiredAt: base.Add(-48 * time.Hour)}
	cfg.Entries["edge-a"] = entry

	tk := validTicket()
	tk.Entry = "edge-unknown" // also unresolvable
	tk.Source = "issuer-bad"  // and its source is denied
	if d := New(cfg).Adjudicate(tk, Request{Class: "ghost"}); d.Code != CodeEntryUnknown {
		t.Fatalf("entry resolution must be checked first, got %v", d.Code)
	}

	tk.Entry = "edge-a"
	if d := New(cfg).Adjudicate(tk, Request{Class: "ghost"}); d.Code != CodeSourceDenied {
		t.Fatalf("source must be checked before profile and class, got %v", d.Code)
	}

	tk.Source = "issuer-ok"
	if d := New(cfg).Adjudicate(tk, Request{Class: "ghost"}); d.Code != CodeCompatibilityExpired {
		t.Fatalf("profile state must be checked before class, got %v", d.Code)
	}
}

// TestCanonicalEntryWinsOverAlias documents that an entry that exists under
// its own name is used directly, never redirected through a same-named alias.
func TestCanonicalEntryWinsOverAlias(t *testing.T) {
	cfg := baseConfig(base)
	cfg.Aliases["edge-a"] = Alias{NewEntry: "edge-b", NotAfter: base.Add(time.Hour)}
	cfg.Entries["edge-b"] = EntryPolicy{
		IsolationDomain:          "domain-2",
		AllowedSources:           map[SourceID]bool{"issuer-ok": true},
		Profiles:                 map[ProfileID]ProfilePolicy{"profile-1": {SupportsEarlyData: true}},
		Classes:                  map[RequestClass]ClassPolicy{"catalog-read": {ReadOnly: true}},
		EarlyDataMaxConfirmation: 1,
	}

	d := New(cfg).Adjudicate(validTicket(), Request{Class: "catalog-read"})
	if d.Outcome != OutcomeAcceptEarlyData || d.Entry != "edge-a" {
		t.Fatalf("got %v/%v entry=%q (%s)", d.Outcome, d.Code, d.Entry, d.Reason)
	}
}

// TestTicketExpiryPrecedesProfile verifies an expired ticket is reported as
// such even when its profile is also retired.
func TestTicketExpiryPrecedesProfile(t *testing.T) {
	cfg := baseConfig(base.Add(2 * time.Hour))
	entry := cfg.Entries["edge-a"]
	entry.Profiles["profile-1"] = ProfilePolicy{RetiredAt: base.Add(-72 * time.Hour)}
	cfg.Entries["edge-a"] = entry

	d := New(cfg).Adjudicate(validTicket(), Request{Class: "catalog-read"})
	if d.Code != CodeTicketExpired {
		t.Fatalf("got %v (%s)", d.Code, d.Reason)
	}
}
