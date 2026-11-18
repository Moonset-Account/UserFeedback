// Package multicastadmission implements a per-terminal multicast admission
// controller.
//
// Multiple terminals may subscribe to the same multicast group, but each
// terminal has its own source-address conditions and is additionally
// constrained by network-segment policies. Upstream subscriptions of the
// same group are merged into one set of sources so that duplicate traffic is
// not requested; every packet delivered to a terminal is nevertheless judged
// against that terminal's own current rules. Joining, updating or leaving a
// session only affects that session, and an emergency withdrawal of a
// protected source immediately overrides every already-merged upstream
// subscription that contains it.
package multicastadmission

import (
	"errors"
	"net"
	"sort"
	"sync"
)

// Sentinel errors returned by the controller operations.
var (
	ErrEmptyTerminalID = errors.New("multicastadmission: terminal id must not be empty")
	ErrEmptyGroup      = errors.New("multicastadmission: multicast group must not be nil")
	ErrNotMulticast    = errors.New("multicastadmission: group address is not a multicast address")
	ErrNilSource       = errors.New("multicastadmission: source address must not be nil")
	ErrSessionExists   = errors.New("multicastadmission: terminal already has an active session")
	ErrSessionNotFound = errors.New("multicastadmission: no active session for terminal")
	ErrGroupMismatch   = errors.New("multicastadmission: update must keep the same multicast group")
	ErrUnknownPolicy   = errors.New("multicastadmission: no segment policy registered for segment")
	ErrNotProtected    = errors.New("multicastadmission: source is not protected and cannot be withdrawn")
	ErrProtectedSource = errors.New("multicastadmission: source is protected and has been withdrawn")
	ErrEmptySegment    = errors.New("multicastadmission: segment id must not be empty")
	ErrInvalidPrefix   = errors.New("multicastadmission: invalid CIDR prefix")
)

// RejectReason classifies why a source is not delivered to a terminal.
type RejectReason int

const (
	// ReasonUnknown is only returned for sources that are actually admitted.
	ReasonUnknown RejectReason = iota
	// ReasonNotRequested: the terminal's subscription does not include the
	// source.
	ReasonNotRequested
	// ReasonWithdrawn: the source is protected and has been emergently
	// withdrawn. This check runs before every other rule.
	ReasonWithdrawn
	// ReasonSegmentDenied: a segment deny prefix matches the source.
	ReasonSegmentDenied
	// ReasonNotInAllowedSegment: no segment allow prefix matches the source.
	// An empty allow list admits nothing (fail closed).
	ReasonNotInAllowedSegment
)

// String returns the stable textual name of a rejection reason.
func (r RejectReason) String() string {
	switch r {
	case ReasonNotRequested:
		return "not_requested"
	case ReasonWithdrawn:
		return "withdrawn"
	case ReasonSegmentDenied:
		return "segment_denied"
	case ReasonNotInAllowedSegment:
		return "not_in_allowed_segment"
	default:
		return "unknown"
	}
}

// SourceCondition is a terminal's own source condition for a group.
// Include lists the sending sources the terminal asks to receive. It is an
// explicit allow list: a source absent from it can never be delivered to the
// terminal, regardless of what other terminals request.
type SourceCondition struct {
	Include []net.IP
}

// SegmentPolicy is the network-segment policy applied to every terminal in
// the segment.
//
//   - AllowedPrefixes: sources must match at least one prefix. An empty list
//     means no source is allowed (fail closed).
//   - DeniedPrefixes: matching sources are always blocked and take precedence
//     over AllowedPrefixes.
//   - ProtectedSources: sources that may be emergently withdrawn. A withdrawal
//     immediately removes them from every merged upstream subscription and
//     from every terminal view.
type SegmentPolicy struct {
	AllowedPrefixes  []*net.IPNet
	DeniedPrefixes   []*net.IPNet
	ProtectedSources []net.IP
}

// session is the internal state of one terminal's current subscription.
type session struct {
	terminalID string
	segmentID  string
	group      net.IP
	condition  SourceCondition
}

// TerminalView is the evaluated outcome for one terminal.
type TerminalView struct {
	TerminalID string
	SegmentID  string
	Group      net.IP
	// AllowedSources is the set of sources actually deliverable to the
	// terminal after every rule (own condition, segment policy, withdrawal).
	AllowedSources []net.IP
	// Rejected records the deciding reason for every candidate source that is
	// not delivered. Candidates are the union of the terminal's own requested
	// sources and the merged upstream sources of its group, so that a source
	// another terminal added can never silently widen this terminal's
	// entitlement: it appears here with an explicit reason.
	Rejected map[string]RejectReason
}

// GroupReport is the admission result for one multicast group.
type GroupReport struct {
	Group net.IP
	// UpstreamSources is the merged upstream receive scope of the group: the
	// union of sources any terminal is currently entitled to receive. It is
	// what would actually be requested upstream.
	UpstreamSources []net.IP
	Terminals       []TerminalView
}

// Controller is the multicast admission module. A Controller is safe for
// concurrent use: every operation is serialized and derived views are rebuilt
// before each operation returns.
type Controller struct {
	mu sync.Mutex

	sessions map[string]*session
	policies map[string]*SegmentPolicy

	// withdrawn holds the canonical string form of every protected source
	// currently under emergency withdrawal.
	withdrawn map[string]struct{}

	groups map[string]*GroupReport
}

// NewController creates an empty admission controller.
func NewController() *Controller {
	return &Controller{
		sessions:  make(map[string]*session),
		policies:  make(map[string]*SegmentPolicy),
		withdrawn: make(map[string]struct{}),
		groups:    make(map[string]*GroupReport),
	}
}

// SetPolicy registers (or replaces) the policy of a network segment. The
// change takes effect immediately: the next Report and every later Join /
// Update reflects it.
func (c *Controller) SetPolicy(segmentID string, policy *SegmentPolicy) error {
	if segmentID == "" {
		return ErrEmptySegment
	}
	if policy == nil {
		return errors.New("multicastadmission: policy must not be nil")
	}
	c.mu.Lock()
	defer c.mu.Unlock()

	c.policies[segmentID] = clonePolicy(policy)
	c.rebuildLocked()
	return nil
}

// Join registers a terminal's subscription. Each terminal may hold at most one
// active session; it must Leave before joining again.
func (c *Controller) Join(terminalID, segmentID string, group net.IP, condition SourceCondition) error {
	if terminalID == "" {
		return ErrEmptyTerminalID
	}
	if group == nil {
		return ErrEmptyGroup
	}
	if !group.IsMulticast() {
		return ErrNotMulticast
	}
	for _, src := range condition.Include {
		if src == nil {
			return ErrNilSource
		}
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	if _, ok := c.sessions[terminalID]; ok {
		return ErrSessionExists
	}
	if _, ok := c.policies[segmentID]; !ok {
		return ErrUnknownPolicy
	}

	c.sessions[terminalID] = &session{
		terminalID: terminalID,
		segmentID:  segmentID,
		group:      cloneIP(group),
		condition:  cloneCondition(condition),
	}
	c.rebuildLocked()
	return nil
}

// Update replaces the source condition of a terminal's existing session. The
// multicast group is fixed for the lifetime of a session; a mismatching group
// returns ErrGroupMismatch and changes nothing.
func (c *Controller) Update(terminalID string, group net.IP, condition SourceCondition) error {
	if terminalID == "" {
		return ErrEmptyTerminalID
	}
	if group == nil {
		return ErrEmptyGroup
	}
	if !group.IsMulticast() {
		return ErrNotMulticast
	}
	for _, src := range condition.Include {
		if src == nil {
			return ErrNilSource
		}
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	s, ok := c.sessions[terminalID]
	if !ok {
		return ErrSessionNotFound
	}
	if !s.group.Equal(group) {
		return ErrGroupMismatch
	}

	s.condition = cloneCondition(condition)
	c.rebuildLocked()
	return nil
}

// Leave removes a terminal's session. Only that terminal's subscription is
// affected; other terminals' views and the merged upstream scopes are
// recomputed without it.
func (c *Controller) Leave(terminalID string) error {
	if terminalID == "" {
		return ErrEmptyTerminalID
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	if _, ok := c.sessions[terminalID]; !ok {
		return ErrSessionNotFound
	}
	delete(c.sessions, terminalID)
	c.rebuildLocked()
	return nil
}

// WithdrawSource emergently withdraws a protected source. The source must be
// listed as protected by at least one registered segment policy; otherwise
// ErrNotProtected is returned and nothing changes. On success the withdrawal
// takes effect immediately: the source is removed from every already-merged
// upstream subscription and denied to every terminal, regardless of the
// terminal's own condition or its segment allow list.
func (c *Controller) WithdrawSource(src net.IP) error {
	if src == nil {
		return ErrNilSource
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	key := canonical(src)
	if !c.isProtectedLocked(key) {
		return ErrNotProtected
	}
	c.withdrawn[key] = struct{}{}
	c.rebuildLocked()
	return nil
}

// RestoreSource cancels a previous emergency withdrawal. It never unblocks a
// source on its own: ordinary segment and condition rules still apply.
func (c *Controller) RestoreSource(src net.IP) error {
	if src == nil {
		return ErrNilSource
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	delete(c.withdrawn, canonical(src))
	c.rebuildLocked()
	return nil
}

// IsWithdrawn reports whether a source is currently under emergency
// withdrawal.
func (c *Controller) IsWithdrawn(src net.IP) bool {
	if src == nil {
		return false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	_, ok := c.withdrawn[canonical(src)]
	return ok
}

// Report returns the current merged upstream receive scope and the evaluated
// source set of every terminal, grouped by multicast group. The returned
// slices and maps are deep copies and may be modified freely.
func (c *Controller) Report() []GroupReport {
	c.mu.Lock()
	defer c.mu.Unlock()

	keys := make([]string, 0, len(c.groups))
	for k := range c.groups {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	out := make([]GroupReport, 0, len(keys))
	for _, k := range keys {
		out = append(out, cloneGroupReport(c.groups[k]))
	}
	return out
}

// GroupReportFor returns a deep copy of the report for one multicast group, or
// nil if no terminal currently subscribes to it.
func (c *Controller) GroupReportFor(group net.IP) *GroupReport {
	if group == nil {
		return nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if r, ok := c.groups[canonical(group)]; ok {
		cp := cloneGroupReport(r)
		return &cp
	}
	return nil
}

// rebuildLocked recomputes every derived view from the current sessions,
// policies and withdrawal set. Callers must hold c.mu.
func (c *Controller) rebuildLocked() {
	// Collect sessions per group in a deterministic terminal order.
	groupSessions := make(map[string][]*session)
	groupIPs := make(map[string]net.IP)
	terminalIDs := make([]string, 0, len(c.sessions))
	for id := range c.sessions {
		terminalIDs = append(terminalIDs, id)
	}
	sort.Strings(terminalIDs)

	for _, id := range terminalIDs {
		s := c.sessions[id]
		key := canonical(s.group)
		groupSessions[key] = append(groupSessions[key], s)
		groupIPs[key] = s.group
	}

	newGroups := make(map[string]*GroupReport, len(groupSessions))
	for gkey, members := range groupSessions {
		report := &GroupReport{Group: cloneIP(groupIPs[gkey])}

		// Candidates cover every source requested by any member of the group,
		// plus each member's own requests. This guarantees a terminal can
		// only lose on a source another terminal introduced, never gain it.
		candidateSet := make(map[string]net.IP)
		for _, s := range members {
			for _, src := range s.condition.Include {
				k := canonical(src)
				candidateSet[k] = cloneIP(src)
			}
		}

		upstream := make(map[string]net.IP)
		views := make([]TerminalView, 0, len(members))
		for _, s := range members {
			view := TerminalView{
				TerminalID:     s.terminalID,
				SegmentID:      s.segmentID,
				Group:          cloneIP(s.group),
				Rejected:       make(map[string]RejectReason),
				AllowedSources: []net.IP{},
			}
			policy := c.policies[s.segmentID]
			ownRequested := make(map[string]struct{}, len(s.condition.Include))
			for _, src := range s.condition.Include {
				ownRequested[canonical(src)] = struct{}{}
			}

			// Deterministic candidate order.
			candKeys := make([]string, 0, len(candidateSet))
			for k := range candidateSet {
				candKeys = append(candKeys, k)
			}
			sort.Strings(candKeys)

			for _, ck := range candKeys {
				src := candidateSet[ck]
				reason := c.evaluateLocked(src, ownRequested, policy)
				if reason == ReasonUnknown {
					view.AllowedSources = append(view.AllowedSources, cloneIP(src))
					upstream[ck] = cloneIP(src)
				} else {
					view.Rejected[ck] = reason
				}
			}
			sortIPs(view.AllowedSources)
			views = append(views, view)
		}

		report.Terminals = views
		report.UpstreamSources = make([]net.IP, 0, len(upstream))
		for _, ip := range upstream {
			report.UpstreamSources = append(report.UpstreamSources, cloneIP(ip))
		}
		sortIPs(report.UpstreamSources)
		newGroups[gkey] = report
	}
	c.groups = newGroups
}

// evaluateLocked applies the admission rules for one source to one terminal
// and returns ReasonUnknown when the source is admissible. Rule order:
// emergency withdrawal, the terminal's own condition, then segment policy.
func (c *Controller) evaluateLocked(src net.IP, ownRequested map[string]struct{}, policy *SegmentPolicy) RejectReason {
	key := canonical(src)

	if _, withdrawn := c.withdrawn[key]; withdrawn {
		return ReasonWithdrawn
	}
	if _, ok := ownRequested[key]; !ok {
		return ReasonNotRequested
	}

	// Deny prefixes win over allow prefixes.
	for _, deny := range policy.DeniedPrefixes {
		if deny.Contains(src) {
			return ReasonSegmentDenied
		}
	}

	allowed := false
	for _, allow := range policy.AllowedPrefixes {
		if allow.Contains(src) {
			allowed = true
			break
		}
	}
	if !allowed {
		return ReasonNotInAllowedSegment
	}
	return ReasonUnknown
}

// isProtectedLocked reports whether a canonical source key is marked
// protected by any registered segment policy.
func (c *Controller) isProtectedLocked(canonicalKey string) bool {
	for _, p := range c.policies {
		for _, src := range p.ProtectedSources {
			if canonical(src) == canonicalKey {
				return true
			}
		}
	}
	return false
}

// canonical returns a stable textual key for an IP. IPv4 and IPv4-mapped IPv6
// forms normalize to the same key.
func canonical(ip net.IP) string {
	if v4 := ip.To4(); v4 != nil {
		return v4.String()
	}
	return ip.String()
}

// sortIPs orders a slice of IPs by their canonical strings.
func sortIPs(ips []net.IP) {
	sort.Slice(ips, func(i, j int) bool {
		return canonical(ips[i]) < canonical(ips[j])
	})
}

func cloneIP(ip net.IP) net.IP {
	if ip == nil {
		return nil
	}
	if v4 := ip.To4(); v4 != nil {
		return append(net.IPv4zero[:0:0], v4...)
	}
	return append(net.IPv6zero[:0:0], ip.To16()...)
}

func cloneCondition(in SourceCondition) SourceCondition {
	out := SourceCondition{Include: make([]net.IP, 0, len(in.Include))}
	for _, ip := range in.Include {
		out.Include = append(out.Include, cloneIP(ip))
	}
	return out
}

func clonePolicy(in *SegmentPolicy) *SegmentPolicy {
	out := &SegmentPolicy{
		AllowedPrefixes:  make([]*net.IPNet, 0, len(in.AllowedPrefixes)),
		DeniedPrefixes:   make([]*net.IPNet, 0, len(in.DeniedPrefixes)),
		ProtectedSources: make([]net.IP, 0, len(in.ProtectedSources)),
	}
	for _, n := range in.AllowedPrefixes {
		if n == nil {
			continue
		}
		out.AllowedPrefixes = append(out.AllowedPrefixes, &net.IPNet{
			IP:   cloneIP(n.IP),
			Mask: append(net.IPv4Mask(0, 0, 0, 0)[:0:0], n.Mask...),
		})
	}
	for _, n := range in.DeniedPrefixes {
		if n == nil {
			continue
		}
		out.DeniedPrefixes = append(out.DeniedPrefixes, &net.IPNet{
			IP:   cloneIP(n.IP),
			Mask: append(net.IPv4Mask(0, 0, 0, 0)[:0:0], n.Mask...),
		})
	}
	for _, ip := range in.ProtectedSources {
		out.ProtectedSources = append(out.ProtectedSources, cloneIP(ip))
	}
	return out
}

func cloneGroupReport(in *GroupReport) GroupReport {
	out := GroupReport{
		Group:           cloneIP(in.Group),
		UpstreamSources: make([]net.IP, 0, len(in.UpstreamSources)),
		Terminals:       make([]TerminalView, 0, len(in.Terminals)),
	}
	for _, ip := range in.UpstreamSources {
		out.UpstreamSources = append(out.UpstreamSources, cloneIP(ip))
	}
	for _, v := range in.Terminals {
		cp := TerminalView{
			TerminalID:     v.TerminalID,
			SegmentID:      v.SegmentID,
			Group:          cloneIP(v.Group),
			AllowedSources: make([]net.IP, 0, len(v.AllowedSources)),
			Rejected:       make(map[string]RejectReason, len(v.Rejected)),
		}
		for _, ip := range v.AllowedSources {
			cp.AllowedSources = append(cp.AllowedSources, cloneIP(ip))
		}
		for k, reason := range v.Rejected {
			cp.Rejected[k] = reason
		}
		out.Terminals = append(out.Terminals, cp)
	}
	return out
}

// ParseSegmentPolicy builds a SegmentPolicy from CIDR strings. It is a
// convenience constructor so callers can configure a controller with plain
// textual fixtures. An invalid prefix yields ErrInvalidPrefix.
func ParseSegmentPolicy(allowed, denied []string, protected []net.IP) (*SegmentPolicy, error) {
	policy := &SegmentPolicy{
		AllowedPrefixes:  make([]*net.IPNet, 0, len(allowed)),
		DeniedPrefixes:   make([]*net.IPNet, 0, len(denied)),
		ProtectedSources: make([]net.IP, 0, len(protected)),
	}
	for _, s := range allowed {
		_, n, err := net.ParseCIDR(s)
		if err != nil {
			return nil, ErrInvalidPrefix
		}
		policy.AllowedPrefixes = append(policy.AllowedPrefixes, n)
	}
	for _, s := range denied {
		_, n, err := net.ParseCIDR(s)
		if err != nil {
			return nil, ErrInvalidPrefix
		}
		policy.DeniedPrefixes = append(policy.DeniedPrefixes, n)
	}
	for _, ip := range protected {
		if ip == nil {
			return nil, ErrNilSource
		}
		policy.ProtectedSources = append(policy.ProtectedSources, cloneIP(ip))
	}
	return policy, nil
}
