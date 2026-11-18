// Package forwardguard decides which jump-host forwarding channels are allowed
// to become active inside a managed session.
//
// The evaluator is deliberately a pure, in-memory state machine so that the
// decision can be re-checked at confirmation time: applications are submitted,
// the binding adapter reports the addresses/ports it actually obtained, and the
// current (possibly tightened) session policy is then applied to every pending
// channel in one pass.
package forwardguard

import (
	"errors"
	"fmt"
	"net"
	"sort"
)

// Direction is the forwarding direction requested for a channel.
type Direction string

const (
	// ForwardRemote binds locally and forwards to a remote target resource.
	ForwardRemote Direction = "forward_remote"
	// ReverseRemote causes the remote side to listen and forward to the local
	// side. Reverse channels are only enabled when the policy explicitly lists
	// this direction.
	ReverseRemote Direction = "reverse_remote"
)

// PortRange is an inclusive range of permitted target/bound ports.
type PortRange struct {
	From int
	To   int
}

// Contains reports whether p falls inside the inclusive range.
func (r PortRange) Contains(p int) bool {
	return p >= r.From && p <= r.To
}

// TargetRule describes one allowed target resource of a session.
type TargetRule struct {
	// Name is the target resource identifier referenced by applications.
	Name string
	// AllowedCIDRs bounds every target IP address an application may select.
	AllowedCIDRs []*net.IPNet
	// AllowedPorts bounds every target port an application may select.
	AllowedPorts []PortRange
}

// SessionPolicy is the currently approved forwarding envelope of a session.
type SessionPolicy struct {
	// SessionID ties applications and policy revisions to one managed session.
	SessionID string
	// AllowedDirections is the direction allow list in effect.
	AllowedDirections []Direction
	// Targets enumerates the approved target resources.
	Targets []TargetRule
	// ListenCIDRs bounds the addresses the binding adapter may listen on.
	ListenCIDRs []*net.IPNet
	// TemporaryLoopback marks an approval whose listening scope is temporarily
	// restricted to loopback addresses only. While set, a dual-stack binding
	// that enlarges reachability on either address family must be blocked.
	TemporaryLoopback bool
	// Revision monotonically identifies policy versions; tightening requires a
	// strictly higher revision.
	Revision int
}

// ReasonCode classifies why a pending channel cannot be enabled.
type ReasonCode string

const (
	ReasonBindingMissing         ReasonCode = "binding_missing"
	ReasonDirectionDenied        ReasonCode = "direction_denied"
	ReasonTargetUnknown          ReasonCode = "target_unknown"
	ReasonTargetOutOfRange       ReasonCode = "target_out_of_range"
	ReasonPortNotApproved        ReasonCode = "port_not_approved"
	ReasonStaticPortMismatch     ReasonCode = "static_port_mismatch"
	ReasonDynamicPortReused      ReasonCode = "dynamic_port_reused"
	ReasonListenerOutOfRange     ReasonCode = "listener_out_of_range"
	ReasonDualStackExpansion     ReasonCode = "dual_stack_expansion"
	ReasonLoopbackOnlyViolation  ReasonCode = "loopback_only_violation"
	ReasonPolicyTighteningDenied ReasonCode = "policy_tightening_denied"
)

// BlockReason records one concrete boundary that was crossed.
type BlockReason struct {
	Code ReasonCode
	// Detail is a human-readable description naming the offending value.
	Detail string
	// Boundary names the approval edge that was crossed, e.g. an approved
	// listen CIDR or the approved port range.
	Boundary string
	// Actual is the observed value violating the boundary, when applicable.
	Actual string
	// Family is "ipv4"/"ipv6" for address-family specific boundaries.
	Family string
}

// Channel is the materialized view of one forwarding application.
type Channel struct {
	ApplicationID string
	SessionID     string
	Direction     Direction
	TargetName    string
	TargetIP      net.IP
	TargetPort    int
	// Dynamic means the bound port is ephemeral and valid only for this
	// application instance.
	Dynamic bool
	// RequestedPort is the fixed port for static requests; zero for dynamic.
	RequestedPort int
	Bound         *BindingResult
}

// BoundAddress is one concrete socket the adapter actually bound.
type BoundAddress struct {
	IP   net.IP
	Port int
}

// BindingResult is everything the adapter returned for one application.
type BindingResult struct {
	Addresses []BoundAddress
}

// EnabledChannel is a channel that passed every current-policy check.
type EnabledChannel struct {
	Channel    Channel
	ListenAddr []BoundAddress
}

// BlockedChannel carries the channel together with every crossed boundary.
type BlockedChannel struct {
	Channel Channel
	Reasons []BlockReason
}

// Decision is the output of one confirmation pass over all pending channels.
type Decision struct {
	Enabled []EnabledChannel
	Blocked []BlockedChannel
}

type applicationState struct {
	channel Channel
	bound   *BindingResult
	// dynamicClaimed is the port claimed for this dynamic application.
	dynamicClaimed int
}

// Evaluator holds session policy and the applications pending confirmation.
// All decisions are computed from the current policy at Confirm time, so a
// tightened policy is retroactively applied to every channel awaiting enable.
type Evaluator struct {
	policy SessionPolicy
	apps   map[string]*applicationState
	order  []string
}

// NewEvaluator builds an evaluator for the initial, approved session policy and
// validates that the policy itself is internally consistent.
func NewEvaluator(policy SessionPolicy) (*Evaluator, error) {
	if policy.SessionID == "" {
		return nil, errors.New("session policy requires a session id")
	}
	if len(policy.AllowedDirections) == 0 {
		return nil, errors.New("session policy requires at least one allowed direction")
	}
	if len(policy.Targets) == 0 {
		return nil, errors.New("session policy requires at least one target rule")
	}
	if len(policy.ListenCIDRs) == 0 {
		return nil, errors.New("session policy requires at least one listen cidr")
	}
	for _, d := range policy.AllowedDirections {
		if d != ForwardRemote && d != ReverseRemote {
			return nil, fmt.Errorf("unknown direction %q", d)
		}
	}
	for _, t := range policy.Targets {
		if t.Name == "" {
			return nil, errors.New("target rule requires a name")
		}
		if len(t.AllowedCIDRs) == 0 {
			return nil, fmt.Errorf("target %q requires allowed cidrs", t.Name)
		}
		if len(t.AllowedPorts) == 0 {
			return nil, fmt.Errorf("target %q requires allowed port ranges", t.Name)
		}
		for _, c := range t.AllowedCIDRs {
			if c == nil {
				return nil, fmt.Errorf("target %q contains a nil cidr", t.Name)
			}
		}
		for _, pr := range t.AllowedPorts {
			if pr.From <= 0 || pr.To < pr.From || pr.To > 65535 {
				return nil, fmt.Errorf("target %q contains invalid port range %d-%d", t.Name, pr.From, pr.To)
			}
		}
	}
	for _, c := range policy.ListenCIDRs {
		if c == nil {
			return nil, errors.New("listen cidr list contains a nil cidr")
		}
	}
	if policy.TemporaryLoopback {
		for _, c := range policy.ListenCIDRs {
			if !cidrIsLoopback(c) {
				return nil, fmt.Errorf("temporary loopback approval cannot include non-loopback listen cidr %s", c.String())
			}
		}
	}
	return &Evaluator{
		policy: clonePolicy(policy),
		apps:   map[string]*applicationState{},
	}, nil
}

// Submit registers a forwarding application for the session.
func (e *Evaluator) Submit(app Application) error {
	if app.ApplicationID == "" {
		return errors.New("application requires an id")
	}
	if app.SessionID != e.policy.SessionID {
		return fmt.Errorf("application session %q does not match evaluator session %q", app.SessionID, e.policy.SessionID)
	}
	if app.Direction != ForwardRemote && app.Direction != ReverseRemote {
		return fmt.Errorf("unknown direction %q", app.Direction)
	}
	if app.TargetName == "" {
		return errors.New("application requires a target name")
	}
	if app.TargetIP == nil {
		return errors.New("application requires a target ip")
	}
	if app.TargetIP.To16() == nil {
		return fmt.Errorf("application target ip %q is not parseable", app.TargetIP.String())
	}
	if app.TargetPort <= 0 || app.TargetPort > 65535 {
		return fmt.Errorf("application target port %d out of range", app.TargetPort)
	}
	if !app.Dynamic {
		if app.RequestedPort <= 0 || app.RequestedPort > 65535 {
			return fmt.Errorf("static application requires a valid requested port, got %d", app.RequestedPort)
		}
	}
	if _, exists := e.apps[app.ApplicationID]; exists {
		return fmt.Errorf("application %q already submitted", app.ApplicationID)
	}
	st := &applicationState{channel: Channel{
		ApplicationID: app.ApplicationID,
		SessionID:     app.SessionID,
		Direction:     app.Direction,
		TargetName:    app.TargetName,
		TargetIP:      append(net.IP(nil), app.TargetIP...),
		TargetPort:    app.TargetPort,
		Dynamic:       app.Dynamic,
		RequestedPort: app.RequestedPort,
	}}
	e.apps[app.ApplicationID] = st
	e.order = append(e.order, app.ApplicationID)
	return nil
}

// RegisterBinding records the actual adapter binding for an application. All
// bound sockets share one port. Re-registering replaces the previous result.
// For dynamic applications the first observed port is claimed for this
// application only; the same port seen elsewhere is later rejected.
func (e *Evaluator) RegisterBinding(applicationID string, binding BindingResult) error {
	st, ok := e.apps[applicationID]
	if !ok {
		return fmt.Errorf("unknown application %q", applicationID)
	}
	if len(binding.Addresses) == 0 {
		return errors.New("binding requires at least one address")
	}
	port := 0
	for i, ba := range binding.Addresses {
		if ba.IP == nil || ba.IP.To16() == nil {
			return fmt.Errorf("binding address #%d has an invalid ip", i)
		}
		if ba.Port <= 0 || ba.Port > 65535 {
			return fmt.Errorf("binding port %d out of range", ba.Port)
		}
		if i == 0 {
			port = ba.Port
		} else if ba.Port != port {
			return fmt.Errorf("binding sockets must share one port, saw %d and %d", port, ba.Port)
		}
	}
	copied := make([]BoundAddress, len(binding.Addresses))
	for i, ba := range binding.Addresses {
		copied[i] = BoundAddress{IP: append(net.IP(nil), ba.IP...), Port: ba.Port}
	}
	st.bound = &BindingResult{Addresses: copied}
	if st.channel.Dynamic && st.dynamicClaimed == 0 {
		st.dynamicClaimed = port
	}
	return nil
}

// Tighten replaces the policy with a strictly tighter revision. The new policy
// must keep the same session id, carry a higher revision, and may only remove
// approved surface: directions, target scope, listen scope and the loopback
// flag. Any attempt to widen the envelope (including silently dropping the
// loopback-only restriction) is rejected and leaves the old policy in force.
func (e *Evaluator) Tighten(next SessionPolicy) error {
	if next.SessionID != e.policy.SessionID {
		return fmt.Errorf("%w: session id changed from %q to %q",
			ErrTightening, e.policy.SessionID, next.SessionID)
	}
	if next.Revision <= e.policy.Revision {
		return fmt.Errorf("%w: revision %d must be greater than %d",
			ErrTightening, next.Revision, e.policy.Revision)
	}
	if e.policy.TemporaryLoopback && !next.TemporaryLoopback {
		return fmt.Errorf("%w: cannot drop temporary loopback restriction", ErrTightening)
	}
	if !directionsSubset(next.AllowedDirections, e.policy.AllowedDirections) {
		return fmt.Errorf("%w: new direction list %v is not a subset of %v",
			ErrTightening, next.AllowedDirections, e.policy.AllowedDirections)
	}
	oldByName := map[string]TargetRule{}
	for _, t := range e.policy.Targets {
		oldByName[t.Name] = t
	}
	for _, nt := range next.Targets {
		ot, ok := oldByName[nt.Name]
		if !ok {
			return fmt.Errorf("%w: target %q was not previously approved", ErrTightening, nt.Name)
		}
		if !cidrsSubset(nt.AllowedCIDRs, ot.AllowedCIDRs) {
			return fmt.Errorf("%w: target %q cidr scope widened", ErrTightening, nt.Name)
		}
		if !portRangesSubset(nt.AllowedPorts, ot.AllowedPorts) {
			return fmt.Errorf("%w: target %q port scope widened", ErrTightening, nt.Name)
		}
	}
	if !cidrsSubset(next.ListenCIDRs, e.policy.ListenCIDRs) {
		return fmt.Errorf("%w: listen cidr scope widened", ErrTightening)
	}
	if next.TemporaryLoopback {
		for _, c := range next.ListenCIDRs {
			if !cidrIsLoopback(c) {
				return fmt.Errorf("%w: temporary loopback approval cannot include non-loopback cidr %s",
					ErrTightening, c.String())
			}
		}
	}
	e.policy = clonePolicy(next)
	return nil
}

// PolicyRevision exposes the revision currently in force.
func (e *Evaluator) PolicyRevision() int {
	return e.policy.Revision
}

// Confirm re-evaluates every pending channel against the policy currently in
// force. Nothing is cached from earlier policy versions: each channel is
// checked against the tightened envelope at confirmation time.
func (e *Evaluator) Confirm() Decision {
	dec := Decision{Enabled: []EnabledChannel{}, Blocked: []BlockedChannel{}}
	for _, id := range e.order {
		st := e.apps[id]
		ch := st.channel
		ch.Bound = st.bound
		reasons := e.evaluate(st)
		if len(reasons) == 0 {
			dec.Enabled = append(dec.Enabled, EnabledChannel{
				Channel:    ch,
				ListenAddr: append([]BoundAddress(nil), st.bound.Addresses...),
			})
			continue
		}
		dec.Blocked = append(dec.Blocked, BlockedChannel{Channel: ch, Reasons: reasons})
	}
	return dec
}

func (e *Evaluator) evaluate(st *applicationState) []BlockReason {
	ch := st.channel
	var reasons []BlockReason

	// 1. The adapter must have reported an actual binding before confirmation.
	if st.bound == nil {
		reasons = append(reasons, BlockReason{
			Code:   ReasonBindingMissing,
			Detail: "binding adapter returned no address before confirmation",
		})
		// Further checks need concrete sockets; stop here.
		return reasons
	}

	// 2. Direction must still be allowed by the policy in force.
	if !containsDirection(e.policy.AllowedDirections, ch.Direction) {
		reasons = append(reasons, BlockReason{
			Code:     ReasonDirectionDenied,
			Detail:   fmt.Sprintf("direction %s not in session allow list", ch.Direction),
			Boundary: fmt.Sprintf("directions=%v", e.policy.AllowedDirections),
			Actual:   string(ch.Direction),
		})
	}

	// 3. Target resource must exist and the concrete IP/port stay inside it.
	rule, ok := e.findTarget(ch.TargetName)
	if !ok {
		reasons = append(reasons, BlockReason{
			Code:     ReasonTargetUnknown,
			Detail:   fmt.Sprintf("target resource %q is not approved", ch.TargetName),
			Boundary: "session target list",
			Actual:   ch.TargetName,
		})
	} else {
		if !ipInCIDRs(ch.TargetIP, rule.AllowedCIDRs) {
			reasons = append(reasons, BlockReason{
				Code:     ReasonTargetOutOfRange,
				Detail:   fmt.Sprintf("target ip %s outside approved cidrs of %q", ch.TargetIP.String(), ch.TargetName),
				Boundary: cidrsLabel(rule.AllowedCIDRs),
				Actual:   ch.TargetIP.String(),
			})
		}
		if !portInRanges(ch.TargetPort, rule.AllowedPorts) {
			reasons = append(reasons, BlockReason{
				Code:     ReasonPortNotApproved,
				Detail:   fmt.Sprintf("target port %d outside approved ranges of %q", ch.TargetPort, ch.TargetName),
				Boundary: portRangesLabel(rule.AllowedPorts),
				Actual:   fmt.Sprintf("%d", ch.TargetPort),
			})
		}
	}

	// 4. Bound port boundaries.
	boundPort := st.bound.Addresses[0].Port
	if !ch.Dynamic {
		if boundPort != ch.RequestedPort {
			reasons = append(reasons, BlockReason{
				Code:     ReasonStaticPortMismatch,
				Detail:   "adapter bound a different port than the approved static request",
				Boundary: fmt.Sprintf("requested_port=%d", ch.RequestedPort),
				Actual:   fmt.Sprintf("%d", boundPort),
			})
		}
	} else {
		// Dynamic ports are valid only inside the application that obtained
		// them. The first application (in submission order) that claimed a port
		// owns it; any later application reusing the same port is rejected.
		if owner := e.dynamicPortOwner(boundPort); owner != "" && owner != ch.ApplicationID {
			reasons = append(reasons, BlockReason{
				Code:     ReasonDynamicPortReused,
				Detail:   fmt.Sprintf("dynamic port %d already claimed by application %q", boundPort, owner),
				Boundary: fmt.Sprintf("dynamic port owned by %s", owner),
				Actual:   fmt.Sprintf("%d", boundPort),
			})
		}
	}

	// 5. Every actually bound socket must stay inside the approved listen
	// segments. Under a temporary loopback approval, dual-stack bindings are
	// treated as one envelope: if either address family enlarges reachability
	// beyond loopback, the whole channel is blocked with the dual-stack
	// expansion boundary rather than a per-address miss.
	families := map[string]bool{}
	for _, ba := range st.bound.Addresses {
		families[familyOf(ba.IP)] = true
	}
	dualStack := families["ipv4"] && families["ipv6"]

	if e.policy.TemporaryLoopback && dualStack {
		expanded := false
		for _, ba := range st.bound.Addresses {
			if ba.IP.IsUnspecified() || !ba.IP.IsLoopback() {
				expanded = true
				break
			}
		}
		if expanded {
			reasons = append(reasons, BlockReason{
				Code:   ReasonDualStackExpansion,
				Detail: "dual-stack binding enlarges reachability beyond loopback while loopback-only approval is in force",
				Boundary: "loopback only: " +
					loopbackCIDR("ipv4") + "," + loopbackCIDR("ipv6"),
				Actual: boundLabel(st.bound.Addresses),
			})
			return reasons
		}
	}

	for _, ba := range st.bound.Addresses {
		if !ipInCIDRs(ba.IP, e.policy.ListenCIDRs) {
			reasons = append(reasons, BlockReason{
				Code:     ReasonListenerOutOfRange,
				Detail:   fmt.Sprintf("bound address %s is outside approved listen segments", ba.IP.String()),
				Boundary: cidrsLabel(e.policy.ListenCIDRs),
				Actual:   ba.IP.String(),
				Family:   familyOf(ba.IP),
			})
		}
		if e.policy.TemporaryLoopback && (ba.IP.IsUnspecified() || !ba.IP.IsLoopback()) {
			reasons = append(reasons, BlockReason{
				Code:     ReasonLoopbackOnlyViolation,
				Detail:   "temporary loopback approval only permits loopback listeners",
				Boundary: "loopback only",
				Actual:   ba.IP.String(),
				Family:   familyOf(ba.IP),
			})
		}
	}

	return reasons
}

func (e *Evaluator) findTarget(name string) (TargetRule, bool) {
	for _, t := range e.policy.Targets {
		if t.Name == name {
			return t, true
		}
	}
	return TargetRule{}, false
}

// dynamicPortOwner returns the earliest submitted dynamic application that
// claimed port, or "" when nobody holds it. Submission order decides ownership
// so the first legitimate claim keeps its port at confirmation time.
func (e *Evaluator) dynamicPortOwner(port int) string {
	if port == 0 {
		return ""
	}
	for _, id := range e.order {
		st := e.apps[id]
		if st.channel.Dynamic && st.dynamicClaimed == port {
			return id
		}
	}
	return ""
}

// Application is a forwarding request submitted to the session.
type Application struct {
	ApplicationID string
	SessionID     string
	Direction     Direction
	TargetName    string
	TargetIP      net.IP
	TargetPort    int
	Dynamic       bool
	RequestedPort int
}

// ErrTightening wraps every rejected policy-tightening attempt.
var ErrTightening = errors.New("policy tightening rejected")

// ---------- small network helpers ----------

func familyOf(ip net.IP) string {
	if v4 := ip.To4(); v4 != nil {
		return "ipv4"
	}
	return "ipv6"
}

func loopbackCIDR(family string) string {
	if family == "ipv4" {
		return "127.0.0.0/8"
	}
	return "::1/128"
}

// cidrIsLoopback reports whether every address inside c is a loopback address.
func cidrIsLoopback(c *net.IPNet) bool {
	ones, bits := c.Mask.Size()
	if bits == 32 {
		loopback := mustCIDR(loopbackCIDR("ipv4"))
		return cidrWithin(c, loopback) && ones >= 8
	}
	loopback := mustCIDR(loopbackCIDR("ipv6"))
	return cidrWithin(c, loopback)
}

func mustCIDR(s string) *net.IPNet {
	_, n, err := net.ParseCIDR(s)
	if err != nil {
		panic(err)
	}
	return n
}

// cidrWithin reports whether inner is fully contained in outer.
func cidrWithin(inner, outer *net.IPNet) bool {
	innerOnes, innerBits := inner.Mask.Size()
	outerOnes, outerBits := outer.Mask.Size()
	if innerBits != outerBits {
		return false
	}
	if innerOnes < outerOnes {
		return false
	}
	return outer.Contains(inner.IP)
}

// cidrsSubset reports whether every network in inner is contained by some
// network in outer.
func cidrsSubset(inner, outer []*net.IPNet) bool {
	for _, in := range inner {
		covered := false
		for _, out := range outer {
			if cidrWithin(in, out) {
				covered = true
				break
			}
		}
		if !covered {
			return false
		}
	}
	return true
}

func portRangesSubset(inner, outer []PortRange) bool {
	for _, in := range inner {
		for p := in.From; p <= in.To; p++ {
			if !portInRanges(p, outer) {
				return false
			}
		}
	}
	return true
}

func portInRanges(port int, ranges []PortRange) bool {
	for _, r := range ranges {
		if r.Contains(port) {
			return true
		}
	}
	return false
}

func ipInCIDRs(ip net.IP, cidrs []*net.IPNet) bool {
	for _, c := range cidrs {
		if c.Contains(ip) {
			return true
		}
	}
	return false
}

func containsDirection(want []Direction, d Direction) bool {
	for _, x := range want {
		if x == d {
			return true
		}
	}
	return false
}

func directionsSubset(inner, outer []Direction) bool {
	for _, d := range inner {
		if !containsDirection(outer, d) {
			return false
		}
	}
	return true
}

func cidrsLabel(cidrs []*net.IPNet) string {
	parts := make([]string, 0, len(cidrs))
	for _, c := range cidrs {
		parts = append(parts, c.String())
	}
	sort.Strings(parts)
	out := ""
	for i, p := range parts {
		if i > 0 {
			out += ","
		}
		out += p
	}
	return out
}

func portRangesLabel(ranges []PortRange) string {
	cp := append([]PortRange(nil), ranges...)
	sort.Slice(cp, func(i, j int) bool { return cp[i].From < cp[j].From })
	out := ""
	for i, r := range cp {
		if i > 0 {
			out += ","
		}
		if r.From == r.To {
			out += fmt.Sprintf("%d", r.From)
		} else {
			out += fmt.Sprintf("%d-%d", r.From, r.To)
		}
	}
	return out
}

func boundLabel(addrs []BoundAddress) string {
	out := ""
	for i, ba := range addrs {
		if i > 0 {
			out += ","
		}
		out += net.JoinHostPort(ba.IP.String(), fmt.Sprintf("%d", ba.Port))
	}
	return out
}

func clonePolicy(p SessionPolicy) SessionPolicy {
	cp := SessionPolicy{
		SessionID:         p.SessionID,
		AllowedDirections: append([]Direction(nil), p.AllowedDirections...),
		ListenCIDRs:       append([]*net.IPNet(nil), p.ListenCIDRs...),
		TemporaryLoopback: p.TemporaryLoopback,
		Revision:          p.Revision,
	}
	cp.Targets = make([]TargetRule, len(p.Targets))
	for i, t := range p.Targets {
		cp.Targets[i] = TargetRule{
			Name:         t.Name,
			AllowedCIDRs: append([]*net.IPNet(nil), t.AllowedCIDRs...),
			AllowedPorts: append([]PortRange(nil), t.AllowedPorts...),
		}
	}
	return cp
}
