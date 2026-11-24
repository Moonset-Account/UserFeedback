package projection

import (
	"fmt"
	"sort"
	"strings"
	"time"
)

// maxAliasDepth bounds how many alias hops one entry name may traverse. It
// exists so a malformed announcement cannot make projection unbounded.
const maxAliasDepth = 16

// ValidationError is a list of problems found in an announcement.
type ValidationError []string

// Error implements error.
func (e ValidationError) Error() string {
	return "invalid announcement: " + strings.Join(e, "; ")
}

// Validate checks an announcement for structural problems: duplicate names,
// unknown owners, bad target kinds, inverted validity windows and overlapping
// alias/address namespaces. Project calls Validate before projecting and
// returns the same error.
//
// A missing address record or a missing alias record is deliberately NOT a
// validation error: it is a transient dependency gap that Project reports as
// StatusIncomplete so the service can be published once the dependency
// arrives in a later announcement.
func Validate(a Announcement) error {
	_, errs := buildIndex(a)
	if len(errs) > 0 {
		return ValidationError(errs)
	}
	return nil
}

type index struct {
	services  map[string]*Service
	aliases   map[string]*Alias
	addresses map[string]*Address
	withdrawn map[string]bool
}

func buildIndex(a Announcement) (*index, []string) {
	idx := &index{
		services:  make(map[string]*Service, len(a.Services)),
		aliases:   make(map[string]*Alias, len(a.Aliases)),
		addresses: make(map[string]*Address, len(a.Addresses)),
		withdrawn: make(map[string]bool, len(a.Withdrawn)),
	}
	var errs []string

	for i := range a.Services {
		s := &a.Services[i]
		if s.ID == "" {
			errs = append(errs, fmt.Sprintf("services[%d]: empty ID", i))
			continue
		}
		if _, ok := idx.services[s.ID]; ok {
			errs = append(errs, fmt.Sprintf("service %q: duplicate ID", s.ID))
			continue
		}
		if s.Purpose == "" {
			errs = append(errs, fmt.Sprintf("service %q: empty purpose", s.ID))
		}
		if !windowOrdered(s.Validity) {
			errs = append(errs, fmt.Sprintf("service %q: validity ends before it starts", s.ID))
		}
		if len(s.EntryNames) == 0 {
			errs = append(errs, fmt.Sprintf("service %q: no entry names", s.ID))
		}
		seenEntries := make(map[string]bool, len(s.EntryNames))
		for _, raw := range s.EntryNames {
			name, ok := normalizeName(raw)
			if !ok {
				errs = append(errs, fmt.Sprintf("service %q: invalid entry name %q", s.ID, raw))
				continue
			}
			if seenEntries[name] {
				errs = append(errs, fmt.Sprintf("service %q: duplicate entry name %q", s.ID, name))
			}
			seenEntries[name] = true
		}
		idx.services[s.ID] = s
	}

	for i := range a.Aliases {
		al := &a.Aliases[i]
		name, ok := normalizeName(al.Name)
		if !ok {
			errs = append(errs, fmt.Sprintf("aliases[%d]: invalid name %q", i, al.Name))
			continue
		}
		if _, dup := idx.aliases[name]; dup {
			errs = append(errs, fmt.Sprintf("alias %q: duplicate name", name))
			continue
		}
		if al.Owner == "" {
			errs = append(errs, fmt.Sprintf("alias %q: empty owner", name))
		} else if _, known := idx.services[al.Owner]; !known {
			errs = append(errs, fmt.Sprintf("alias %q: unknown owner %q", name, al.Owner))
		}
		if !windowOrdered(al.Validity) {
			errs = append(errs, fmt.Sprintf("alias %q: validity ends before it starts", name))
		}
		switch al.Target.Kind {
		case TargetAddress:
			if al.Target.Ref == "" {
				errs = append(errs, fmt.Sprintf("alias %q: empty address reference", name))
			}
		case TargetAlias:
			if _, ok := normalizeName(al.Target.Ref); !ok {
				errs = append(errs, fmt.Sprintf("alias %q: invalid alias reference %q", name, al.Target.Ref))
			}
		default:
			errs = append(errs, fmt.Sprintf("alias %q: unknown target kind %v", name, al.Target.Kind))
		}
		idx.aliases[name] = al
	}

	// Address namespaces are checked against the fully built alias index, so a
	// name collision is reported regardless of declaration order.
	addressNames := make(map[string]string, len(a.Addresses))
	for i := range a.Addresses {
		ad := &a.Addresses[i]
		if ad.ID == "" {
			errs = append(errs, fmt.Sprintf("addresses[%d]: empty ID", i))
			continue
		}
		if _, dup := idx.addresses[ad.ID]; dup {
			errs = append(errs, fmt.Sprintf("address %q: duplicate ID", ad.ID))
			continue
		}
		name, ok := normalizeName(ad.Name)
		if !ok {
			errs = append(errs, fmt.Sprintf("address %q: invalid name %q", ad.ID, ad.Name))
		} else {
			if _, clash := idx.aliases[name]; clash {
				errs = append(errs, fmt.Sprintf("address %q: name %q collides with an alias", ad.ID, name))
			}
			if other, dup := addressNames[name]; dup {
				errs = append(errs, fmt.Sprintf("address %q: name %q already used by address %q", ad.ID, name, other))
			} else {
				addressNames[name] = ad.ID
			}
		}
		if !ad.IP.IsValid() {
			errs = append(errs, fmt.Sprintf("address %q: invalid IP", ad.ID))
		}
		if !windowOrdered(ad.Validity) {
			errs = append(errs, fmt.Sprintf("address %q: validity ends before it starts", ad.ID))
		}
		idx.addresses[ad.ID] = ad
	}

	seenZones := make(map[string]bool, len(a.Zones))
	for i := range a.Zones {
		z := &a.Zones[i]
		if z.Name == "" {
			errs = append(errs, fmt.Sprintf("zones[%d]: empty name", i))
			continue
		}
		if seenZones[z.Name] {
			errs = append(errs, fmt.Sprintf("zone %q: duplicate name", z.Name))
		}
		seenZones[z.Name] = true
	}

	for _, id := range a.Withdrawn {
		if id == "" {
			errs = append(errs, "withdrawn: empty service ID")
			continue
		}
		idx.withdrawn[id] = true
	}

	return idx, errs
}

// windowOrdered reports whether w is empty or non-inverted.
func windowOrdered(w Window) bool {
	return w.NotBefore.IsZero() || w.Until.IsZero() || w.NotBefore.Before(w.Until)
}

// normalizeName canonicalizes a host name: surrounding space is trimmed, ASCII
// letters are lower-cased and a single trailing dot is dropped. It returns
// false for names that are empty after normalisation or that contain
// characters a host label cannot hold.
func normalizeName(raw string) (string, bool) {
	s := strings.TrimSpace(raw)
	s = strings.TrimSuffix(s, ".")
	s = strings.ToLower(s)
	if s == "" || len(s) > 253 {
		return "", false
	}
	for _, label := range strings.Split(s, ".") {
		if label == "" || len(label) > 63 {
			return "", false
		}
		if label[0] == '-' || label[len(label)-1] == '-' {
			return "", false
		}
		for i := 0; i < len(label); i++ {
			c := label[i]
			switch {
			case c >= 'a' && c <= 'z', c >= '0' && c <= '9', c == '-', c == '_':
			default:
				return "", false
			}
		}
	}
	return s, true
}

// Project computes the per-zone record projection of a at instant asOf.
//
// Every approved service in a zone is either published atomically (all of its
// entry names resolve and its chain records are emitted) or left out with a
// status explaining why. Records that several published services share are
// emitted once. Records are sorted and services keep announcement order.
func Project(a Announcement, asOf time.Time) (Result, error) {
	idx, errs := buildIndex(a)
	if len(errs) > 0 {
		return Result{}, ValidationError(errs)
	}

	res := Result{AsOf: asOf, Zones: make([]ZoneResult, 0, len(a.Zones))}
	for i := range a.Zones {
		z := &a.Zones[i]
		zr := ZoneResult{Zone: z.Name, Services: []ServiceResult{}}
		recordSet := make(map[Record]struct{})

		for j := range a.Services {
			s := &a.Services[j]
			if !zoneApproves(z, s) {
				continue
			}
			sr, recs := projectService(idx, s, asOf)
			zr.Services = append(zr.Services, sr)
			for _, rec := range recs {
				recordSet[rec] = struct{}{}
			}
		}

		zr.Records = make([]Record, 0, len(recordSet))
		for rec := range recordSet {
			zr.Records = append(zr.Records, rec)
		}
		sortRecords(zr.Records)
		res.Zones = append(res.Zones, zr)
	}
	return res, nil
}

// zoneApproves reports whether the zone may see the service.
func zoneApproves(z *Zone, s *Service) bool {
	for _, id := range z.DeniedServices {
		if id == s.ID {
			return false
		}
	}
	for _, id := range z.ApprovedServices {
		if id == s.ID {
			return true
		}
	}
	if s.Private {
		return false
	}
	for _, p := range z.ApprovedPurposes {
		if p == s.Purpose {
			return true
		}
	}
	return false
}

// projectService classifies one approved service. Records are returned only
// when the service is published, so a partially resolved service can never
// leak records into a zone.
func projectService(idx *index, s *Service, asOf time.Time) (ServiceResult, []Record) {
	sr := ServiceResult{ID: s.ID, Purpose: s.Purpose, Private: s.Private}

	switch {
	case idx.withdrawn[s.ID]:
		sr.Status = StatusWithdrawn
		return sr, nil
	case s.Validity.PendingAt(asOf):
		sr.Status = StatusPending
		return sr, nil
	case !s.Validity.ActiveAt(asOf):
		sr.Status = StatusExpired
		return sr, nil
	}

	recs, issues := resolveService(idx, s, asOf)
	if len(issues) > 0 {
		sr.Status = StatusIncomplete
		sr.Issues = issues
		return sr, nil
	}
	sr.Status = StatusPublished
	sr.RecordCount = len(recs)
	return sr, recs
}

// resolveService walks every entry name of s. If any entry fails, the whole
// service is incomplete and the partial records are discarded so the service is
// published atomically.
func resolveService(idx *index, s *Service, asOf time.Time) ([]Record, []Issue) {
	var issues []Issue
	records := make([]Record, 0, len(s.EntryNames))
	seenRecord := make(map[Record]struct{})

	for _, rawEntry := range s.EntryNames {
		entry, ok := normalizeName(rawEntry)
		if !ok {
			issues = append(issues, Issue{Reason: MissingAlias, Entry: rawEntry, Ref: rawEntry})
			continue
		}
		chain, chainIssues := walkChain(idx, s.ID, entry, asOf)
		issues = append(issues, chainIssues...)
		if len(chainIssues) > 0 {
			continue
		}
		for _, rec := range chain {
			if _, dup := seenRecord[rec]; dup {
				continue
			}
			seenRecord[rec] = struct{}{}
			records = append(records, rec)
		}
	}

	if len(issues) > 0 {
		dedup := dedupeIssues(issues)
		return nil, dedup
	}
	sortRecords(records)
	return records, nil
}

// walkChain follows an entry name through alias hops until it lands on an
// address. It returns the CNAME/A records that make the name resolvable.
func walkChain(idx *index, serviceID, entry string, asOf time.Time) ([]Record, []Issue) {
	var records []Record
	visited := map[string]bool{}
	current := entry
	firstHop := true

	for depth := 0; ; depth++ {
		if depth > maxAliasDepth {
			return nil, []Issue{{Reason: AliasChainTooDeep, Entry: entry, Ref: current}}
		}
		if visited[current] {
			return nil, []Issue{{Reason: AliasCycle, Entry: entry, Ref: current}}
		}
		visited[current] = true

		al, ok := idx.aliases[current]
		if !ok {
			reason := MissingAliasTarget
			if firstHop {
				reason = MissingAlias
			}
			return nil, []Issue{{Reason: reason, Entry: entry, Ref: current}}
		}
		if al.Owner != serviceID {
			return nil, []Issue{{Reason: ForeignAlias, Entry: entry, Ref: current}}
		}
		if al.Validity.PendingAt(asOf) {
			return nil, []Issue{{Reason: AliasNotYetValid, Entry: entry, Ref: current}}
		}
		if !al.Validity.ActiveAt(asOf) {
			return nil, []Issue{{Reason: AliasExpired, Entry: entry, Ref: current}}
		}
		name, _ := normalizeName(al.Name)

		switch al.Target.Kind {
		case TargetAddress:
			addr, ok := idx.addresses[al.Target.Ref]
			if !ok {
				return nil, []Issue{{Reason: MissingAddress, Entry: entry, Ref: al.Target.Ref}}
			}
			if addr.Validity.PendingAt(asOf) {
				return nil, []Issue{{Reason: AddressNotYetValid, Entry: entry, Ref: addr.ID}}
			}
			if !addr.Validity.ActiveAt(asOf) {
				return nil, []Issue{{Reason: AddressExpired, Entry: entry, Ref: addr.ID}}
			}
			addrName, _ := normalizeName(addr.Name)
			records = append(records,
				Record{Name: name, Type: RecordCNAME, Value: addrName},
				Record{Name: addrName, Type: RecordA, Value: addr.IP.String()},
			)
			return records, nil

		case TargetAlias:
			next, ok := normalizeName(al.Target.Ref)
			if !ok {
				return nil, []Issue{{Reason: InvalidTarget, Entry: entry, Ref: al.Target.Ref}}
			}
			records = append(records, Record{Name: name, Type: RecordCNAME, Value: next})
			current = next
			firstHop = false

		default:
			return nil, []Issue{{Reason: InvalidTarget, Entry: entry, Ref: al.Name}}
		}
	}
}

// dedupeIssues removes duplicate issues and sorts the remainder into a stable
// order so results are reproducible.
func dedupeIssues(issues []Issue) []Issue {
	seen := make(map[Issue]struct{}, len(issues))
	out := make([]Issue, 0, len(issues))
	for _, is := range issues {
		if _, dup := seen[is]; dup {
			continue
		}
		seen[is] = struct{}{}
		out = append(out, is)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Entry != out[j].Entry {
			return out[i].Entry < out[j].Entry
		}
		if out[i].Ref != out[j].Ref {
			return out[i].Ref < out[j].Ref
		}
		return out[i].Reason < out[j].Reason
	})
	return out
}

// sortRecords sorts records by name, then type, then value.
func sortRecords(records []Record) {
	sort.Slice(records, func(i, j int) bool {
		if records[i].Name != records[j].Name {
			return records[i].Name < records[j].Name
		}
		if records[i].Type != records[j].Type {
			return records[i].Type < records[j].Type
		}
		return records[i].Value < records[j].Value
	})
}
