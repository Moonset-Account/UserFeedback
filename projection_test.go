package projection

import (
	"errors"
	"fmt"
	"net/netip"
	"reflect"
	"testing"
	"time"
)

var base = time.Date(2026, 11, 24, 12, 0, 0, 0, time.UTC)

func win(notBefore, until time.Time) Window { return Window{NotBefore: notBefore, Until: until} }

func open() Window { return Window{} }

func mustAddr(t *testing.T, s string) netip.Addr {
	t.Helper()
	a, err := netip.ParseAddr(s)
	if err != nil {
		t.Fatalf("parse addr %q: %v", s, err)
	}
	return a
}

func mustProject(t *testing.T, a Announcement, asOf time.Time) Result {
	t.Helper()
	r, err := Project(a, asOf)
	if err != nil {
		t.Fatalf("Project: unexpected error: %v", err)
	}
	return r
}

func mustZone(t *testing.T, r Result, name string) ZoneResult {
	t.Helper()
	z, ok := r.Zone(name)
	if !ok {
		t.Fatalf("zone %q missing from result", name)
	}
	return z
}

func recordStrings(z ZoneResult) []string {
	out := make([]string, len(z.Records))
	for i, rec := range z.Records {
		out[i] = rec.String()
	}
	return out
}

func serviceByID(z ZoneResult, id string) (ServiceResult, bool) {
	for _, s := range z.Services {
		if s.ID == id {
			return s, true
		}
	}
	return ServiceResult{}, false
}

func mustService(t *testing.T, z ZoneResult, id string) ServiceResult {
	t.Helper()
	s, ok := serviceByID(z, id)
	if !ok {
		t.Fatalf("zone %q: service %q not reported", z.Zone, id)
	}
	return s
}

// sharedFixture is a two-service announcement that shares one address record
// between a public web service and a public API service.
func sharedFixture(t *testing.T) Announcement {
	t.Helper()
	return Announcement{
		Services: []Service{
			{ID: "web", Purpose: "web", Validity: open(), EntryNames: []string{"www.factory.example.com"}},
			{ID: "api", Purpose: "api", Validity: open(), EntryNames: []string{"api.factory.example.com"}},
		},
		Aliases: []Alias{
			{Name: "www.factory.example.com", Owner: "web", Target: Target{Kind: TargetAddress, Ref: "addr-edge"}, Validity: open()},
			{Name: "api.factory.example.com", Owner: "api", Target: Target{Kind: TargetAddress, Ref: "addr-edge"}, Validity: open()},
		},
		Addresses: []Address{
			{ID: "addr-edge", Name: "edge.example.com", IP: mustAddr(t, "10.0.0.10"), Validity: open()},
		},
		Zones: []Zone{
			{Name: "factory", ApprovedPurposes: []string{"web", "api"}},
			{Name: "lab", ApprovedPurposes: []string{"web"}},
		},
	}
}

func TestProjectBasicZones(t *testing.T) {
	a := sharedFixture(t)
	res := mustProject(t, a, base)

	if !res.AsOf.Equal(base) {
		t.Errorf("AsOf = %v, want %v", res.AsOf, base)
	}

	factory := mustZone(t, res, "factory")
	wantFactory := []string{
		"api.factory.example.com CNAME edge.example.com",
		"edge.example.com A 10.0.0.10",
		"www.factory.example.com CNAME edge.example.com",
	}
	if got := recordStrings(factory); !reflect.DeepEqual(got, wantFactory) {
		t.Errorf("factory records = %#v, want %#v", got, wantFactory)
	}
	if n := len(factory.Services); n != 2 {
		t.Fatalf("factory services = %d, want 2", n)
	}
	for _, id := range []string{"web", "api"} {
		s := mustService(t, factory, id)
		if s.Status != StatusPublished {
			t.Errorf("%s status = %q, want published", id, s.Status)
		}
		if s.RecordCount != 2 {
			t.Errorf("%s RecordCount = %d, want 2", id, s.RecordCount)
		}
	}

	lab := mustZone(t, res, "lab")
	wantLab := []string{
		"edge.example.com A 10.0.0.10",
		"www.factory.example.com CNAME edge.example.com",
	}
	if got := recordStrings(lab); !reflect.DeepEqual(got, wantLab) {
		t.Errorf("lab records = %#v, want %#v", got, wantLab)
	}
	if _, ok := serviceByID(lab, "api"); ok {
		t.Errorf("lab must not report unapproved service api")
	}
}

func TestSharedAddressEmittedOncePerZone(t *testing.T) {
	a := sharedFixture(t)
	// Add a third service that also points at the same address.
	a.Services = append(a.Services, Service{ID: "admin", Purpose: "admin", Validity: open(), EntryNames: []string{"admin.factory.example.com"}})
	a.Aliases = append(a.Aliases, Alias{Name: "admin.factory.example.com", Owner: "admin", Target: Target{Kind: TargetAddress, Ref: "addr-edge"}, Validity: open()})
	a.Zones[0].ApprovedPurposes = append(a.Zones[0].ApprovedPurposes, "admin")

	z := mustZone(t, mustProject(t, a, base), "factory")
	count := 0
	for _, rec := range z.Records {
		if rec == (Record{Name: "edge.example.com", Type: RecordA, Value: "10.0.0.10"}) {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("shared A record emitted %d times, want once; records=%v", count, recordStrings(z))
	}
}

func TestPrivateServiceNeedsExplicitApproval(t *testing.T) {
	a := Announcement{
		Services: []Service{
			{ID: "portal", Purpose: "portal", Validity: open(), EntryNames: []string{"portal.example.com"}},
			{ID: "vault", Purpose: "portal", Private: true, Validity: open(), EntryNames: []string{"vault.internal.example.com"}},
		},
		Aliases: []Alias{
			{Name: "portal.example.com", Owner: "portal", Target: Target{Kind: TargetAddress, Ref: "addr-p"}, Validity: open()},
			{Name: "vault.internal.example.com", Owner: "vault", Target: Target{Kind: TargetAddress, Ref: "addr-v"}, Validity: open()},
		},
		Addresses: []Address{
			{ID: "addr-p", Name: "p.example.com", IP: mustAddr(t, "10.0.1.1"), Validity: open()},
			{ID: "addr-v", Name: "v.example.com", IP: mustAddr(t, "10.0.1.2"), Validity: open()},
		},
		Zones: []Zone{
			// Same purpose label does not reveal a private service.
			{Name: "lobby", ApprovedPurposes: []string{"portal"}},
			{Name: "ops", ApprovedPurposes: []string{"portal"}, ApprovedServices: []string{"vault"}},
		},
	}
	res := mustProject(t, a, base)

	lobby := mustZone(t, res, "lobby")
	if _, ok := serviceByID(lobby, "vault"); ok {
		t.Errorf("lobby must not see private vault via purpose label")
	}
	for _, rec := range lobby.Records {
		if rec.Name == "vault.internal.example.com" || rec.Name == "v.example.com" {
			t.Errorf("lobby leaked vault record %v", rec)
		}
	}

	ops := mustZone(t, res, "ops")
	v := mustService(t, ops, "vault")
	if v.Status != StatusPublished || !v.Private {
		t.Errorf("ops vault = %+v, want published private", v)
	}
}

func TestDeniedServiceWinsOverApprovals(t *testing.T) {
	a := sharedFixture(t)
	a.Zones[0].DeniedServices = []string{"api"}

	factory := mustZone(t, mustProject(t, a, base), "factory")
	if _, ok := serviceByID(factory, "api"); ok {
		t.Fatalf("denied service api must not be reported")
	}
	for _, rec := range factory.Records {
		if rec.Name == "api.factory.example.com" {
			t.Fatalf("denied service leaked record %v", rec)
		}
	}
	// The shared address is still needed by web.
	found := false
	for _, rec := range factory.Records {
		if rec == (Record{Name: "edge.example.com", Type: RecordA, Value: "10.0.0.10"}) {
			found = true
		}
	}
	if !found {
		t.Fatalf("shared address used by web must remain; records=%v", recordStrings(factory))
	}
}

func TestWithdrawPrivateServiceKeepsSharedAddress(t *testing.T) {
	a := sharedFixture(t)
	// Make web private so withdrawal is the private-service case from the brief.
	a.Services[0].Private = true
	a.Zones[0].ApprovedServices = []string{"web"}
	a.Zones[1].ApprovedServices = []string{"web"}
	a.Zones[1].ApprovedPurposes = []string{"web", "api"}
	a.Withdrawn = []string{"web"}

	res := mustProject(t, a, base)

	factory := mustZone(t, res, "factory")
	web := mustService(t, factory, "web")
	if web.Status != StatusWithdrawn {
		t.Errorf("web status = %q, want withdrawn", web.Status)
	}
	if web.RecordCount != 0 {
		t.Errorf("withdrawn web RecordCount = %d, want 0", web.RecordCount)
	}
	wantFactory := []string{
		"api.factory.example.com CNAME edge.example.com",
		"edge.example.com A 10.0.0.10",
	}
	if got := recordStrings(factory); !reflect.DeepEqual(got, wantFactory) {
		t.Errorf("factory records after withdrawal = %#v, want %#v (alias gone, shared address kept)", got, wantFactory)
	}

	lab := mustZone(t, res, "lab")
	wantLab := []string{
		"api.factory.example.com CNAME edge.example.com",
		"edge.example.com A 10.0.0.10",
	}
	if got := recordStrings(lab); !reflect.DeepEqual(got, wantLab) {
		t.Errorf("lab records after withdrawal = %#v, want %#v (shared address kept for api)", got, wantLab)
	}
	if w := mustService(t, lab, "web"); w.Status != StatusWithdrawn {
		t.Errorf("lab web status = %q, want withdrawn", w.Status)
	}
}

func TestMissingAddressThenComplete(t *testing.T) {
	a := Announcement{
		Services: []Service{
			{ID: "db", Purpose: "db", Validity: open(), EntryNames: []string{"db.example.com"}},
		},
		Aliases: []Alias{
			{Name: "db.example.com", Owner: "db", Target: Target{Kind: TargetAddress, Ref: "addr-db"}, Validity: open()},
		},
		Zones: []Zone{{Name: "factory", ApprovedPurposes: []string{"db"}}},
	}

	first := mustZone(t, mustProject(t, a, base), "factory")
	db := mustService(t, first, "db")
	if db.Status != StatusIncomplete {
		t.Fatalf("missing address: status = %q, want incomplete", db.Status)
	}
	if len(first.Records) != 0 {
		t.Fatalf("missing address must emit no records, got %v", recordStrings(first))
	}
	if len(db.Issues) != 1 || db.Issues[0].Reason != MissingAddress || db.Issues[0].Ref != "addr-db" {
		t.Fatalf("issues = %+v, want one missing_address for addr-db", db.Issues)
	}

	// The address dependency arrives in a later announcement.
	a.Addresses = []Address{{ID: "addr-db", Name: "db-host.example.com", IP: mustAddr(t, "10.0.2.5"), Validity: open()}}
	second := mustZone(t, mustProject(t, a, base), "factory")
	db = mustService(t, second, "db")
	if db.Status != StatusPublished {
		t.Fatalf("after dependency arrives: status = %q, want published; issues=%+v", db.Status, db.Issues)
	}
	want := []string{
		"db-host.example.com A 10.0.2.5",
		"db.example.com CNAME db-host.example.com",
	}
	if got := recordStrings(second); !reflect.DeepEqual(got, want) {
		t.Errorf("records = %#v, want %#v", got, want)
	}
}

func TestExpiredAddressBlocksService(t *testing.T) {
	a := sharedFixture(t)
	a.Addresses[0].Validity = win(base.Add(-2*time.Hour), base) // expired exactly at base

	z := mustZone(t, mustProject(t, a, base), "factory")
	for _, id := range []string{"web", "api"} {
		s := mustService(t, z, id)
		if s.Status != StatusIncomplete {
			t.Errorf("%s status = %q, want incomplete", id, s.Status)
		}
		if len(s.Issues) != 1 || s.Issues[0].Reason != AddressExpired {
			t.Errorf("%s issues = %+v, want address_expired", id, s.Issues)
		}
	}
	if len(z.Records) != 0 {
		t.Errorf("expired address must emit no records, got %v", recordStrings(z))
	}
}

func TestWindowBoundaries(t *testing.T) {
	start := base
	end := base.Add(time.Hour)
	a := Announcement{
		Services: []Service{
			{ID: "svc", Purpose: "p", Validity: win(start, end), EntryNames: []string{"s.example.com"}},
		},
		Aliases: []Alias{
			{Name: "s.example.com", Owner: "svc", Target: Target{Kind: TargetAddress, Ref: "addr-s"}, Validity: open()},
		},
		Addresses: []Address{
			{ID: "addr-s", Name: "h.example.com", IP: mustAddr(t, "10.0.3.1"), Validity: open()},
		},
		Zones: []Zone{{Name: "z", ApprovedPurposes: []string{"p"}}},
	}

	cases := []struct {
		name   string
		asOf   time.Time
		status ServiceStatus
	}{
		{"before start", start.Add(-time.Nanosecond), StatusPending},
		{"at start", start, StatusPublished},
		{"just before end", end.Add(-time.Nanosecond), StatusPublished},
		{"at end", end, StatusExpired},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			z := mustZone(t, mustProject(t, a, tc.asOf), "z")
			if got := mustService(t, z, "svc").Status; got != tc.status {
				t.Errorf("status = %q, want %q", got, tc.status)
			}
		})
	}
}

func TestAliasChain(t *testing.T) {
	a := Announcement{
		Services: []Service{
			{ID: "svc", Purpose: "p", Validity: open(), EntryNames: []string{"public.example.com"}},
		},
		Aliases: []Alias{
			{Name: "public.example.com", Owner: "svc", Target: Target{Kind: TargetAlias, Ref: "mid.example.com"}, Validity: open()},
			{Name: "mid.example.com", Owner: "svc", Target: Target{Kind: TargetAddress, Ref: "addr-x"}, Validity: open()},
		},
		Addresses: []Address{
			{ID: "addr-x", Name: "host.example.com", IP: mustAddr(t, "10.0.4.7"), Validity: open()},
		},
		Zones: []Zone{{Name: "z", ApprovedPurposes: []string{"p"}}},
	}
	z := mustZone(t, mustProject(t, a, base), "z")
	want := []string{
		"host.example.com A 10.0.4.7",
		"mid.example.com CNAME host.example.com",
		"public.example.com CNAME mid.example.com",
	}
	if got := recordStrings(z); !reflect.DeepEqual(got, want) {
		t.Errorf("records = %#v, want %#v", got, want)
	}
}

func TestChainFailuresAreAtomic(t *testing.T) {
	// Two entry names: one resolves, one has a missing address. The service must
	// publish nothing and stay incomplete.
	a := Announcement{
		Services: []Service{
			{ID: "svc", Purpose: "p", Validity: open(), EntryNames: []string{"ok.example.com", "broken.example.com"}},
		},
		Aliases: []Alias{
			{Name: "ok.example.com", Owner: "svc", Target: Target{Kind: TargetAddress, Ref: "addr-ok"}, Validity: open()},
			{Name: "broken.example.com", Owner: "svc", Target: Target{Kind: TargetAddress, Ref: "addr-missing"}, Validity: open()},
		},
		Addresses: []Address{
			{ID: "addr-ok", Name: "okhost.example.com", IP: mustAddr(t, "10.0.5.1"), Validity: open()},
		},
		Zones: []Zone{{Name: "z", ApprovedPurposes: []string{"p"}}},
	}
	z := mustZone(t, mustProject(t, a, base), "z")
	s := mustService(t, z, "svc")
	if s.Status != StatusIncomplete {
		t.Fatalf("status = %q, want incomplete", s.Status)
	}
	if len(z.Records) != 0 {
		t.Fatalf("partial records leaked: %v", recordStrings(z))
	}
	if len(s.Issues) != 1 || s.Issues[0].Entry != "broken.example.com" || s.Issues[0].Reason != MissingAddress {
		t.Fatalf("issues = %+v", s.Issues)
	}
}

func TestForeignAliasIsolation(t *testing.T) {
	a := Announcement{
		Services: []Service{
			{ID: "web", Purpose: "p", Validity: open(), EntryNames: []string{"shared.example.com"}},
			{ID: "api", Purpose: "p", Validity: open(), EntryNames: []string{"api.example.com"}},
		},
		Aliases: []Alias{
			{Name: "shared.example.com", Owner: "api", Target: Target{Kind: TargetAddress, Ref: "addr"}, Validity: open()},
			{Name: "api.example.com", Owner: "api", Target: Target{Kind: TargetAddress, Ref: "addr"}, Validity: open()},
		},
		Addresses: []Address{
			{ID: "addr", Name: "h.example.com", IP: mustAddr(t, "10.0.6.1"), Validity: open()},
		},
		Zones: []Zone{{Name: "z", ApprovedPurposes: []string{"p"}}},
	}
	z := mustZone(t, mustProject(t, a, base), "z")

	web := mustService(t, z, "web")
	if web.Status != StatusIncomplete || len(web.Issues) != 1 || web.Issues[0].Reason != ForeignAlias {
		t.Fatalf("web = %+v, want incomplete alias_foreign_owner", web)
	}
	api := mustService(t, z, "api")
	if api.Status != StatusPublished {
		t.Fatalf("api = %+v, want published", api)
	}
	want := []string{
		"api.example.com CNAME h.example.com",
		"h.example.com A 10.0.6.1",
	}
	if got := recordStrings(z); !reflect.DeepEqual(got, want) {
		t.Errorf("records = %#v, want %#v", got, want)
	}
}

func TestAliasCycle(t *testing.T) {
	a := Announcement{
		Services: []Service{
			{ID: "svc", Purpose: "p", Validity: open(), EntryNames: []string{"a.example.com"}},
		},
		Aliases: []Alias{
			{Name: "a.example.com", Owner: "svc", Target: Target{Kind: TargetAlias, Ref: "b.example.com"}, Validity: open()},
			{Name: "b.example.com", Owner: "svc", Target: Target{Kind: TargetAlias, Ref: "a.example.com"}, Validity: open()},
		},
		Zones: []Zone{{Name: "z", ApprovedPurposes: []string{"p"}}},
	}
	z := mustZone(t, mustProject(t, a, base), "z")
	s := mustService(t, z, "svc")
	if s.Status != StatusIncomplete || len(s.Issues) != 1 || s.Issues[0].Reason != AliasCycle {
		t.Fatalf("svc = %+v, want incomplete alias_cycle", s)
	}
	if len(z.Records) != 0 {
		t.Fatalf("cycle must emit no records, got %v", recordStrings(z))
	}
}

func TestAliasExpired(t *testing.T) {
	a := sharedFixture(t)
	a.Aliases[1].Validity = win(base.Add(-2*time.Hour), base) // api alias expired at base

	z := mustZone(t, mustProject(t, a, base), "factory")
	api := mustService(t, z, "api")
	if api.Status != StatusIncomplete || len(api.Issues) != 1 || api.Issues[0].Reason != AliasExpired {
		t.Fatalf("api = %+v, want incomplete alias_expired", api)
	}
	if web := mustService(t, z, "web"); web.Status != StatusPublished {
		t.Fatalf("web = %+v, want published", web)
	}
}

// TestInterleavedAnnouncements walks one gateway feed across several snapshots:
// the address dependency arrives after the service, then expires, then is
// renewed, while an unrelated withdrawn service never disturbs the shared
// address. It asserts the exact contract at every step.
func TestInterleavedAnnouncements(t *testing.T) {
	t0 := base
	t1 := base.Add(30 * time.Minute)
	t2 := base.Add(60 * time.Minute)
	t3 := base.Add(90 * time.Minute)

	type step struct {
		name        string
		ann         Announcement
		asOf        time.Time
		wantRecords []string
		wantStatus  ServiceStatus
		wantReason  IssueReason
	}

	// Stage 1: service announced, address dependency absent.
	s1 := Announcement{
		Services: []Service{
			{ID: "billing", Purpose: "billing", Validity: win(t0, time.Time{}), EntryNames: []string{"pay.example.com"}},
			{ID: "legacy", Purpose: "legacy", Private: true, Validity: win(t0, time.Time{}), EntryNames: []string{"legacy.example.com"}},
		},
		Aliases: []Alias{
			{Name: "pay.example.com", Owner: "billing", Target: Target{Kind: TargetAddress, Ref: "addr-pay"}, Validity: win(t0, time.Time{})},
			{Name: "legacy.example.com", Owner: "legacy", Target: Target{Kind: TargetAddress, Ref: "addr-pay"}, Validity: win(t0, time.Time{})},
		},
		Zones: []Zone{{Name: "finance", ApprovedPurposes: []string{"billing"}, ApprovedServices: []string{"legacy"}}},
	}

	// Stage 2: the shared address shows up, but only until t3.
	s2 := deepCopyAnnouncement(s1)
	s2.Addresses = []Address{
		{ID: "addr-pay", Name: "pay-edge.example.com", IP: mustAddr(t, "10.1.0.9"), Validity: win(t0, t3)},
	}

	// Stage 3: legacy is withdrawn; billing still needs the shared address.
	s3 := deepCopyAnnouncement(s2)
	s3.Withdrawn = []string{"legacy"}

	// Stage 4: the address expires and is renewed in the same snapshot. The
	// renewed window starts at t3, so at t3 the record is already valid again.
	s4 := deepCopyAnnouncement(s3)
	s4.Addresses = []Address{
		{ID: "addr-pay", Name: "pay-edge.example.com", IP: mustAddr(t, "10.1.0.9"), Validity: win(t3, time.Time{})},
	}

	steps := []step{
		{
			name:        "dependency missing",
			ann:         s1,
			asOf:        t0,
			wantRecords: []string{},
			wantStatus:  StatusIncomplete,
			wantReason:  MissingAddress,
		},
		{
			name: "dependency arrives",
			ann:  s2,
			asOf: t1,
			wantRecords: []string{
				"legacy.example.com CNAME pay-edge.example.com",
				"pay-edge.example.com A 10.1.0.9",
				"pay.example.com CNAME pay-edge.example.com",
			},
			wantStatus: StatusPublished,
		},
		{
			name: "private service withdrawn, shared address survives",
			ann:  s3,
			asOf: t2,
			wantRecords: []string{
				"pay-edge.example.com A 10.1.0.9",
				"pay.example.com CNAME pay-edge.example.com",
			},
			wantStatus: StatusPublished,
		},
		{
			name: "renewed address active at its new start",
			ann:  s4,
			asOf: t3,
			wantRecords: []string{
				"pay-edge.example.com A 10.1.0.9",
				"pay.example.com CNAME pay-edge.example.com",
			},
			wantStatus: StatusPublished,
		},
	}

	for _, st := range steps {
		t.Run(st.name, func(t *testing.T) {
			z := mustZone(t, mustProject(t, st.ann, st.asOf), "finance")
			billing := mustService(t, z, "billing")
			if billing.Status != st.wantStatus {
				t.Fatalf("billing status = %q, want %q; issues=%+v", billing.Status, st.wantStatus, billing.Issues)
			}
			if st.wantReason != "" {
				if len(billing.Issues) != 1 || billing.Issues[0].Reason != st.wantReason {
					t.Fatalf("billing issues = %+v, want %q", billing.Issues, st.wantReason)
				}
			}
			if got := recordStrings(z); !reflect.DeepEqual(got, st.wantRecords) {
				t.Errorf("records = %#v, want %#v", got, st.wantRecords)
			}
			// legacy is private: it must be reported (explicit approval) and,
			// once withdrawn, must not contribute an alias anywhere. While the
			// shared address is missing in stage 1 it is incomplete like billing.
			legacy := mustService(t, z, "legacy")
			switch {
			case st.asOf.Equal(t2) || st.asOf.Equal(t3):
				if legacy.Status != StatusWithdrawn {
					t.Errorf("legacy status = %q, want withdrawn", legacy.Status)
				}
			case st.asOf.Equal(t0):
				if legacy.Status != StatusIncomplete {
					t.Errorf("legacy status = %q, want incomplete while address is missing", legacy.Status)
				}
			default:
				if legacy.Status != StatusPublished {
					t.Errorf("legacy status = %q, want published", legacy.Status)
				}
			}
			for _, rec := range z.Records {
				if legacy.Status == StatusWithdrawn && rec.Name == "legacy.example.com" {
					t.Errorf("withdrawn legacy alias leaked: %v", rec)
				}
			}
		})
	}
}

func TestAddressExpiredWithNoRenewal(t *testing.T) {
	t3 := base.Add(time.Hour)
	a := Announcement{
		Services: []Service{
			{ID: "svc", Purpose: "p", Validity: open(), EntryNames: []string{"s.example.com"}},
		},
		Aliases: []Alias{
			{Name: "s.example.com", Owner: "svc", Target: Target{Kind: TargetAddress, Ref: "addr"}, Validity: open()},
		},
		Addresses: []Address{
			{ID: "addr", Name: "h.example.com", IP: mustAddr(t, "10.1.1.1"), Validity: win(base, t3)},
		},
		Zones: []Zone{{Name: "z", ApprovedPurposes: []string{"p"}}},
	}
	if s := mustService(t, mustZone(t, mustProject(t, a, base), "z"), "svc"); s.Status != StatusPublished {
		t.Fatalf("at start status = %q, want published", s.Status)
	}
	z := mustZone(t, mustProject(t, a, t3), "z")
	s := mustService(t, z, "svc")
	if s.Status != StatusIncomplete || len(s.Issues) != 1 || s.Issues[0].Reason != AddressExpired {
		t.Fatalf("svc = %+v, want incomplete address_expired at address end", s)
	}
	if len(z.Records) != 0 {
		t.Fatalf("expired dependency must retract records, got %v", recordStrings(z))
	}
}

func TestDeniedBeatsExplicitApproval(t *testing.T) {
	a := sharedFixture(t)
	a.Zones[0].ApprovedServices = []string{"api"}
	a.Zones[0].DeniedServices = []string{"api"}

	factory := mustZone(t, mustProject(t, a, base), "factory")
	if _, ok := serviceByID(factory, "api"); ok {
		t.Fatalf("denied service must not be reported even when explicitly approved")
	}
}

func TestWithdrawnUnknownServiceIsHarmless(t *testing.T) {
	a := sharedFixture(t)
	a.Withdrawn = []string{"not-in-this-snapshot"}
	res := mustProject(t, a, base)
	factory := mustZone(t, res, "factory")
	if n := len(factory.Services); n != 2 {
		t.Fatalf("services = %d, want 2", n)
	}
}

func TestSharedAddressAcrossZonesKeptPerZone(t *testing.T) {
	// A zone that sees only one of the two sharers still gets the address.
	a := sharedFixture(t)
	a.Zones[0].DeniedServices = []string{"api"}
	a.Zones[1].ApprovedPurposes = []string{"web", "api"}
	a.Zones[1].DeniedServices = []string{"web"}

	res := mustProject(t, a, base)
	factory := mustZone(t, res, "factory")
	lab := mustZone(t, res, "lab")

	for _, z := range []ZoneResult{factory, lab} {
		has := false
		for _, rec := range z.Records {
			if rec == (Record{Name: "edge.example.com", Type: RecordA, Value: "10.0.0.10"}) {
				has = true
			}
		}
		if !has {
			t.Errorf("zone %q lost the shared address", z.Zone)
		}
	}
	if got := recordStrings(factory); !reflect.DeepEqual(got, []string{
		"edge.example.com A 10.0.0.10",
		"www.factory.example.com CNAME edge.example.com",
	}) {
		t.Errorf("factory = %#v", got)
	}
	if got := recordStrings(lab); !reflect.DeepEqual(got, []string{
		"api.factory.example.com CNAME edge.example.com",
		"edge.example.com A 10.0.0.10",
	}) {
		t.Errorf("lab = %#v", got)
	}
}

func TestAliasTargetNormalized(t *testing.T) {
	a := Announcement{
		Services: []Service{
			{ID: "svc", Purpose: "p", Validity: open(), EntryNames: []string{"a.example.com"}},
		},
		Aliases: []Alias{
			{Name: "a.example.com", Owner: "svc", Target: Target{Kind: TargetAlias, Ref: "B.Example.COM."}, Validity: open()},
			{Name: "b.example.com", Owner: "svc", Target: Target{Kind: TargetAddress, Ref: "addr"}, Validity: open()},
		},
		Addresses: []Address{
			{ID: "addr", Name: "H.Example.com", IP: mustAddr(t, "10.1.2.3"), Validity: open()},
		},
		Zones: []Zone{{Name: "z", ApprovedPurposes: []string{"p"}}},
	}
	z := mustZone(t, mustProject(t, a, base), "z")
	want := []string{
		"a.example.com CNAME b.example.com",
		"b.example.com CNAME h.example.com",
		"h.example.com A 10.1.2.3",
	}
	if got := recordStrings(z); !reflect.DeepEqual(got, want) {
		t.Errorf("records = %#v, want %#v", got, want)
	}
}

func TestDuplicateAliasRejected(t *testing.T) {
	a := sharedFixture(t)
	a.Aliases = append(a.Aliases, a.Aliases[0])
	_, err := Project(a, base)
	if err == nil {
		t.Fatal("duplicate alias accepted")
	}
}

func TestServiceWithoutEntryNamesRejected(t *testing.T) {
	a := Announcement{
		Services: []Service{{ID: "s", Purpose: "p"}},
		Zones:    []Zone{{Name: "z"}},
	}
	if _, err := Project(a, base); err == nil {
		t.Fatal("service without entry names accepted")
	}
}

func TestAliasChainTooDeep(t *testing.T) {
	const depth = maxAliasDepth + 8
	aliases := make([]Alias, 0, depth)
	for i := 0; i < depth; i++ {
		name := fmt.Sprintf("h%02d.example.com", i)
		next := fmt.Sprintf("h%02d.example.com", i+1)
		if i == depth-1 {
			aliases = append(aliases, Alias{Name: name, Owner: "svc", Target: Target{Kind: TargetAddress, Ref: "addr"}, Validity: open()})
			continue
		}
		aliases = append(aliases, Alias{Name: name, Owner: "svc", Target: Target{Kind: TargetAlias, Ref: next}, Validity: open()})
	}
	a := Announcement{
		Services: []Service{
			{ID: "svc", Purpose: "p", Validity: open(), EntryNames: []string{"h00.example.com"}},
		},
		Aliases: aliases,
		Addresses: []Address{
			{ID: "addr", Name: "end.example.com", IP: mustAddr(t, "10.1.3.1"), Validity: open()},
		},
		Zones: []Zone{{Name: "z", ApprovedPurposes: []string{"p"}}},
	}
	z := mustZone(t, mustProject(t, a, base), "z")
	s := mustService(t, z, "svc")
	if s.Status != StatusIncomplete || len(s.Issues) != 1 || s.Issues[0].Reason != AliasChainTooDeep {
		t.Fatalf("svc = %+v, want incomplete alias_chain_too_deep", s)
	}
	if len(z.Records) != 0 {
		t.Fatalf("over-deep chain must emit no records, got %d", len(z.Records))
	}
}

func TestProjectEmptyAnnouncement(t *testing.T) {
	res := mustProject(t, Announcement{}, base)
	if res.Zones == nil {
		t.Fatalf("Zones must be non-nil empty slice")
	}
	if len(res.Zones) != 0 {
		t.Fatalf("zones = %d, want 0", len(res.Zones))
	}
}

func TestAliasValidityIndependentOfService(t *testing.T) {
	a := sharedFixture(t)
	a.Aliases[1].Validity = win(base.Add(time.Hour), time.Time{}) // api alias not yet valid

	z := mustZone(t, mustProject(t, a, base), "factory")
	api := mustService(t, z, "api")
	if api.Status != StatusIncomplete || len(api.Issues) != 1 || api.Issues[0].Reason != AliasNotYetValid {
		t.Fatalf("api = %+v, want incomplete alias_not_yet_valid", api)
	}
	if web := mustService(t, z, "web"); web.Status != StatusPublished {
		t.Fatalf("web = %+v, want published", web)
	}
}

func TestAddressNotYetValid(t *testing.T) {
	a := sharedFixture(t)
	a.Addresses[0].Validity = win(base.Add(time.Minute), time.Time{})

	z := mustZone(t, mustProject(t, a, base), "factory")
	web := mustService(t, z, "web")
	if web.Status != StatusIncomplete || len(web.Issues) != 1 || web.Issues[0].Reason != AddressNotYetValid {
		t.Fatalf("web = %+v, want incomplete address_not_yet_valid", web)
	}
}

func TestNameNormalization(t *testing.T) {
	a := sharedFixture(t)
	a.Services[0].EntryNames = []string{"WWW.Factory.Example.COM."}
	a.Aliases[0].Name = "www.factory.example.com"
	a.Services[1].EntryNames = []string{"  api.factory.example.com "}

	res := mustProject(t, a, base)
	z := mustZone(t, res, "factory")
	if len(z.Records) != 3 {
		t.Fatalf("records = %v, want 3 after normalising names", recordStrings(z))
	}
	for _, rec := range z.Records {
		if rec.Name != "edge.example.com" && rec.Name != "api.factory.example.com" && rec.Name != "www.factory.example.com" {
			t.Errorf("unexpected un-normalised record %v", rec)
		}
	}
}

func TestProjectDoesNotMutateInput(t *testing.T) {
	a := sharedFixture(t)
	before := deepCopyAnnouncement(a)

	_, err := Project(a, base)
	if err != nil {
		t.Fatalf("Project: %v", err)
	}
	if !reflect.DeepEqual(a, before) {
		t.Fatalf("announcement mutated:\n got %#v\nwant %#v", a, before)
	}
}

func TestProjectDeterministic(t *testing.T) {
	a := sharedFixture(t)
	r1 := mustProject(t, a, base)
	r2 := mustProject(t, a, base)
	if !reflect.DeepEqual(r1, r2) {
		t.Fatalf("projection is not deterministic:\n%#v\n%#v", r1, r2)
	}
	// The result must also be sorted, not merely stable.
	for _, z := range r1.Zones {
		for i := 1; i < len(z.Records); i++ {
			if compareRecord(z.Records[i-1], z.Records[i]) > 0 {
				t.Fatalf("zone %q records not sorted at %d: %v then %v", z.Zone, i, z.Records[i-1], z.Records[i])
			}
		}
	}
}

func TestNoZonesShowsOnlyPolicy(t *testing.T) {
	a := sharedFixture(t)
	a.Zones = nil
	res := mustProject(t, a, base)
	if len(res.Zones) != 0 {
		t.Fatalf("zones = %d, want 0", len(res.Zones))
	}

	a.Zones = []Zone{{Name: "empty"}}
	res = mustProject(t, a, base)
	z := mustZone(t, res, "empty")
	if len(z.Records) != 0 || len(z.Services) != 0 {
		t.Fatalf("empty zone = %+v, want no records or services", z)
	}
}

func TestValidateRejectsBadAnnouncements(t *testing.T) {
	tests := []struct {
		name string
		a    Announcement
		want string
	}{
		{
			name: "duplicate service id",
			a: Announcement{
				Services: []Service{
					{ID: "dup", Purpose: "p", EntryNames: []string{"a.example.com"}},
					{ID: "dup", Purpose: "p", EntryNames: []string{"b.example.com"}},
				},
			},
			want: "duplicate ID",
		},
		{
			name: "alias with unknown owner",
			a: Announcement{
				Services: []Service{{ID: "s", Purpose: "p", EntryNames: []string{"a.example.com"}}},
				Aliases:  []Alias{{Name: "a.example.com", Owner: "ghost", Target: Target{Kind: TargetAddress, Ref: "x"}}},
			},
			want: "unknown owner",
		},
		{
			name: "inverted window",
			a: Announcement{
				Services: []Service{{ID: "s", Purpose: "p", Validity: win(base, base.Add(-time.Hour)), EntryNames: []string{"a.example.com"}}},
			},
			want: "validity ends before it starts",
		},
		{
			name: "invalid ip",
			a: Announcement{
				Services:  []Service{{ID: "s", Purpose: "p", EntryNames: []string{"a.example.com"}}},
				Addresses: []Address{{ID: "x", Name: "x.example.com"}},
			},
			want: "invalid IP",
		},
		{
			name: "address name collides with alias",
			a: Announcement{
				Services:  []Service{{ID: "s", Purpose: "p", EntryNames: []string{"a.example.com"}}},
				Aliases:   []Alias{{Name: "a.example.com", Owner: "s", Target: Target{Kind: TargetAddress, Ref: "x"}}},
				Addresses: []Address{{ID: "x", Name: "a.example.com", IP: mustAddr(t, "10.0.0.1")}},
			},
			want: "collides with an alias",
		},
		{
			name: "unknown target kind",
			a: Announcement{
				Services: []Service{{ID: "s", Purpose: "p", EntryNames: []string{"a.example.com"}}},
				Aliases:  []Alias{{Name: "a.example.com", Owner: "s", Target: Target{Kind: TargetKind(0)}}},
			},
			want: "unknown target kind",
		},
		{
			name: "duplicate zone",
			a: Announcement{
				Services: []Service{{ID: "s", Purpose: "p", EntryNames: []string{"a.example.com"}}},
				Zones:    []Zone{{Name: "z"}, {Name: "z"}},
			},
			want: "duplicate name",
		},
		{
			name: "invalid entry name",
			a: Announcement{
				Services: []Service{{ID: "s", Purpose: "p", EntryNames: []string{"bad name"}}},
			},
			want: "invalid entry name",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Project(tc.a, base)
			if err == nil {
				t.Fatalf("Project accepted invalid announcement")
			}
			var ve ValidationError
			if !errors.As(err, &ve) {
				t.Fatalf("error type = %T, want ValidationError", err)
			}
			if !containsSubstring(ve.Error(), tc.want) {
				t.Errorf("error = %q, want substring %q", ve.Error(), tc.want)
			}
			if err := Validate(tc.a); err == nil {
				t.Errorf("Validate accepted invalid announcement")
			}
		})
	}
}

func TestValidateAcceptsGoodAnnouncement(t *testing.T) {
	if err := Validate(sharedFixture(t)); err != nil {
		t.Fatalf("Validate rejected valid announcement: %v", err)
	}
}

// helpers

func containsSubstring(s, sub string) bool {
	return len(sub) == 0 || (len(s) >= len(sub) && indexOf(s, sub) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

func compareRecord(a, b Record) int {
	if a.Name != b.Name {
		if a.Name < b.Name {
			return -1
		}
		return 1
	}
	if a.Type != b.Type {
		if a.Type < b.Type {
			return -1
		}
		return 1
	}
	if a.Value < b.Value {
		return -1
	}
	if a.Value > b.Value {
		return 1
	}
	return 0
}

func deepCopyAnnouncement(a Announcement) Announcement {
	out := a
	out.Services = append([]Service(nil), a.Services...)
	for i := range out.Services {
		out.Services[i].EntryNames = append([]string(nil), a.Services[i].EntryNames...)
	}
	out.Aliases = append([]Alias(nil), a.Aliases...)
	out.Addresses = append([]Address(nil), a.Addresses...)
	out.Zones = append([]Zone(nil), a.Zones...)
	for i := range out.Zones {
		out.Zones[i].ApprovedPurposes = append([]string(nil), a.Zones[i].ApprovedPurposes...)
		out.Zones[i].ApprovedServices = append([]string(nil), a.Zones[i].ApprovedServices...)
		out.Zones[i].DeniedServices = append([]string(nil), a.Zones[i].DeniedServices...)
	}
	out.Withdrawn = append([]string(nil), a.Withdrawn...)
	return out
}
