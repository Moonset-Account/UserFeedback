// Package sourceselect implements deterministic source-address selection for a
// multi-homed host.
//
// The selection order is:
//
//  1. choose the best active route for the destination;
//  2. use that route's interface;
//  3. filter addresses by family, validity and destination scope;
//  4. prefer non-deprecated addresses;
//  5. compare policy level, common prefix length and finally IP address.
//
// Established connections retain their configured source until that address
// becomes invalid/disappears or their original interface disappears or becomes
// unavailable. Route changes alone do not rewrite an existing connection.
package sourceselect

import (
	"bytes"
	"errors"
	"fmt"
	"math/bits"
	"net"
	"sort"
	"sync"
	"time"
)

// Public sentinel errors. Callers can use errors.Is to classify failures.
var (
	ErrInvalidIP           = errors.New("invalid IP address")
	ErrInvalidCIDR         = errors.New("invalid CIDR")
	ErrInvalidArgument     = errors.New("invalid argument")
	ErrInterfaceNotFound   = errors.New("interface not found")
	ErrInterfaceDown       = errors.New("interface is down")
	ErrAddressNotFound     = errors.New("address not found")
	ErrRouteNotFound       = errors.New("route not found")
	ErrConnectionNotFound  = errors.New("connection not found")
	ErrConnectionExists    = errors.New("connection already exists")
	ErrNoRoute             = errors.New("no effective route")
	ErrNoUsableAddress     = errors.New("no usable source address")
	ErrRebuildUnsuccessful = errors.New("connection rebuild failed")
)

// Family is an IP address family.
type Family uint8

const (
	FamilyUnknown Family = 0
	FamilyIPv4    Family = 4
	FamilyIPv6    Family = 6
)

// FamilyOf returns the canonical family of ip. IPv4-mapped IPv6 addresses are
// treated as IPv4.
func FamilyOf(ip net.IP) Family {
	ip = normalizeIP(ip)
	switch {
	case ip == nil:
		return FamilyUnknown
	case ip.To4() != nil:
		return FamilyIPv4
	case len(ip) == net.IPv6len:
		return FamilyIPv6
	default:
		return FamilyUnknown
	}
}

func normalizeIP(ip net.IP) net.IP {
	if v4 := ip.To4(); v4 != nil {
		return v4
	}
	if len(ip) == net.IPv6len {
		return ip
	}
	return nil
}

func cloneIP(ip net.IP) net.IP {
	n := normalizeIP(ip)
	if n == nil {
		return nil
	}
	out := make(net.IP, len(n))
	copy(out, n)
	return out
}

func compareIP(a, b net.IP) int {
	aa, bb := normalizeIP(a), normalizeIP(b)
	if aa == nil || bb == nil {
		if aa == nil && bb == nil {
			return 0
		}
		if aa == nil {
			return -1
		}
		return 1
	}
	if len(aa) != len(bb) {
		return len(aa) - len(bb)
	}
	return bytes.Compare(aa, bb)
}

// CommonPrefixLength returns the number of leading bits shared by two
// addresses. Both addresses must belong to the same family. It returns -1 for
// invalid or different-family addresses.
func CommonPrefixLength(a, b net.IP) int {
	aa, bb := normalizeIP(a), normalizeIP(b)
	if aa == nil || bb == nil || len(aa) != len(bb) {
		return -1
	}

	total := len(aa) * 8
	prefix := 0
	for i := 0; i < len(aa); i++ {
		if aa[i] == bb[i] {
			prefix += 8
			continue
		}
		xor := aa[i] ^ bb[i]
		prefix += bits.LeadingZeros8(xor)
		return prefix
	}
	return total
}

// ParseCIDR parses a CIDR and returns a canonicalized net.IPNet.
func ParseCIDR(s string) (*net.IPNet, error) {
	_, network, err := net.ParseCIDR(s)
	if err != nil {
		return nil, fmt.Errorf("%w %q: %v", ErrInvalidCIDR, s, err)
	}
	network.IP = cloneIP(network.IP)
	return network, nil
}

// LifetimeState describes an address at one observation time.
type LifetimeState string

const (
	// LifetimePreferred means now is strictly before PreferredUntil.
	LifetimePreferred LifetimeState = "preferred"
	// LifetimeDeprecated means the preferred lifetime has ended, but the
	// valid lifetime has not. The address remains eligible.
	LifetimeDeprecated LifetimeState = "deprecated"
	// LifetimeInvalid means the valid lifetime has ended.
	LifetimeInvalid LifetimeState = "invalid"
)

// Address is an interface source address.
//
// A zero PreferredUntil means the address never becomes deprecated; a zero
// ValidUntil means it never expires. Otherwise lifetime intervals are
// half-open: [start, PreferredUntil) is preferred, [PreferredUntil,
// ValidUntil) is deprecated, and at or after ValidUntil the address is invalid.
type Address struct {
	IP             net.IP
	PrefixLength   int
	PolicyLevel    int
	Scope          Scope
	PreferredUntil time.Time
	ValidUntil     time.Time
}

// EffectiveScope returns the explicitly configured scope or an inferred scope.
func (a Address) EffectiveScope() Scope {
	if a.Scope != ScopeAuto {
		return a.Scope
	}
	return DeriveScope(a.IP)
}

// LifetimeAt classifies the address at at.
func (a Address) LifetimeAt(at time.Time) LifetimeState {
	if !a.ValidUntil.IsZero() && !at.Before(a.ValidUntil) {
		return LifetimeInvalid
	}
	if a.PreferredUntil.IsZero() || at.Before(a.PreferredUntil) {
		return LifetimePreferred
	}
	return LifetimeDeprecated
}

func (a Address) validate() error {
	ip := normalizeIP(a.IP)
	if ip == nil {
		return ErrInvalidIP
	}
	maxBits := len(ip) * 8
	if a.PrefixLength < 0 || a.PrefixLength > maxBits {
		return fmt.Errorf("%w: prefix length %d outside [0,%d]", ErrInvalidArgument, a.PrefixLength, maxBits)
	}
	if !a.PreferredUntil.IsZero() && !a.ValidUntil.IsZero() && a.PreferredUntil.After(a.ValidUntil) {
		return fmt.Errorf("%w: PreferredUntil %s is after ValidUntil %s",
			ErrInvalidArgument, a.PreferredUntil, a.ValidUntil)
	}
	return nil
}

func (a Address) clone() Address {
	a.IP = cloneIP(a.IP)
	return a
}

// InterfaceState is the administrative/operational usability of an interface.
type InterfaceState string

const (
	InterfaceUp   InterfaceState = "up"
	InterfaceDown InterfaceState = "down"
)

// Interface identifies a local network interface.
type Interface struct {
	ID    string
	State InterfaceState
}

func (i Interface) normalized() Interface {
	if i.State == "" {
		i.State = InterfaceUp
	}
	return i
}

func (i Interface) validate() error {
	if i.ID == "" {
		return fmt.Errorf("%w: interface ID is empty", ErrInvalidArgument)
	}
	if i.State != "" && i.State != InterfaceUp && i.State != InterfaceDown {
		return fmt.Errorf("%w: unknown interface state %q", ErrInvalidArgument, i.State)
	}
	return nil
}

// InterfaceView is returned by read APIs and includes configured addresses.
type InterfaceView struct {
	Interface
	Addresses []Address
}

// RouteState controls whether a route participates in selection.
type RouteState string

const (
	RouteActive    RouteState = "active"
	RouteInactive  RouteState = "inactive"
	RouteWithdrawn RouteState = "withdrawn"
)

// Effective reports whether the route state is allowed to select an interface.
func (s RouteState) Effective() bool {
	return s == RouteActive
}

// Route is a destination prefix route. Equal prefix lengths are broken by
// lower Metric and then stable lexicographic Route.ID.
type Route struct {
	ID          string
	Destination *net.IPNet
	InterfaceID string
	State       RouteState
	Metric      int
}

func (r Route) validate() error {
	if r.ID == "" {
		return fmt.Errorf("%w: route ID is empty", ErrInvalidArgument)
	}
	if r.Destination == nil {
		return fmt.Errorf("%w: route %s has nil destination", ErrInvalidArgument, r.ID)
	}
	if ip := normalizeIP(r.Destination.IP); ip == nil {
		return fmt.Errorf("%w: route %s has invalid destination IP", ErrInvalidCIDR, r.ID)
	}
	ones, bits := r.Destination.Mask.Size()
	if ones < 0 || bits == 0 {
		return fmt.Errorf("%w: route %s has invalid mask", ErrInvalidCIDR, r.ID)
	}
	if r.InterfaceID == "" {
		return fmt.Errorf("%w: route %s has empty interface ID", ErrInvalidArgument, r.ID)
	}
	if r.State == "" {
		r.State = RouteActive
	}
	switch r.State {
	case RouteActive, RouteInactive, RouteWithdrawn:
	default:
		return fmt.Errorf("%w: unknown route state %q", ErrInvalidArgument, r.State)
	}
	return nil
}

func (r Route) clone() Route {
	if r.Destination != nil {
		dup := *r.Destination
		dup.IP = cloneIP(r.Destination.IP)
		if r.Destination.Mask != nil {
			dup.Mask = append(net.IPMask(nil), r.Destination.Mask...)
		}
		r.Destination = &dup
	}
	return r
}

func routeBetter(a, b *Route) bool {
	aOnes, _ := a.Destination.Mask.Size()
	bOnes, _ := b.Destination.Mask.Size()
	if aOnes != bOnes {
		return aOnes > bOnes
	}
	if a.Metric != b.Metric {
		return a.Metric < b.Metric
	}
	return a.ID < b.ID
}

// Candidate is an eligible source address with its comparison keys.
type Candidate struct {
	Address            Address
	ConfiguredScope    Scope
	EffectiveScope     Scope
	Lifetime           LifetimeState
	Preferred          bool
	CommonPrefixLength int
}

// Selection explains the chosen source and interface.
type Selection struct {
	RouteID            string
	InterfaceID        string
	Source             net.IP
	PrefixLength       int
	EffectiveScope     Scope
	Lifetime           LifetimeState
	Preferred          bool
	CommonPrefixLength int
	Candidates         []Candidate
}

func (s Selection) clone() Selection {
	s.Source = cloneIP(s.Source)
	if s.Candidates != nil {
		out := make([]Candidate, len(s.Candidates))
		for i := range s.Candidates {
			out[i] = s.Candidates[i]
			out[i].Address = out[i].Address.clone()
		}
		s.Candidates = out
	}
	return s
}

// ConnectionState records whether an established flow is still considered
// usable or has been asked to rebuild.
type ConnectionState string

const (
	ConnectionEstablished   ConnectionState = "established"
	ConnectionRebuildNeeded ConnectionState = "rebuild-needed"
)

// Connection is an established flow with its retained source.
type Connection struct {
	ID              string
	Destination     net.IP
	InterfaceID     string
	Source          net.IP
	RouteID         string
	OpenedAt        time.Time
	State           ConnectionState
	RebuildReason   RebuildReason
	LastEvaluatedAt time.Time
}

func (c Connection) clone() Connection {
	c.Destination = cloneIP(c.Destination)
	c.Source = cloneIP(c.Source)
	return c
}

// RebuildReason identifies the retention rule that failed.
type RebuildReason string

const (
	RebuildSourceExpired        RebuildReason = "source-valid-lifetime-expired"
	RebuildSourceWithdrawn      RebuildReason = "source-address-withdrawn"
	RebuildInterfaceDeparted    RebuildReason = "interface-departed"
	RebuildInterfaceUnavailable RebuildReason = "interface-unavailable"
)

// RebuildRecommendation describes a connection that must be rebuilt.
type RebuildRecommendation struct {
	ConnectionID  string
	Reason        RebuildReason
	At            time.Time
	InterfaceID   string
	Source        net.IP
	Detail        string
	NewlyDetected bool
}

func (r RebuildRecommendation) clone() RebuildRecommendation {
	r.Source = cloneIP(r.Source)
	return r
}

type ifaceData struct {
	info      Interface
	addresses map[string]Address
	addrOrder []string
}

// Selector is the concurrency-safe source selection state machine.
type Selector struct {
	mu          sync.RWMutex
	ifaces      map[string]*ifaceData
	routes      map[string]Route
	connOrder   []string
	connections map[string]Connection
}

// NewSelector creates an empty selector.
func NewSelector() *Selector {
	return &Selector{
		ifaces:      make(map[string]*ifaceData),
		routes:      make(map[string]Route),
		connections: make(map[string]Connection),
	}
}

// UpsertInterface inserts an interface or updates its state. Existing
// addresses are preserved when updating an interface.
func (s *Selector) UpsertInterface(iface Interface) error {
	if err := iface.validate(); err != nil {
		return err
	}
	iface = iface.normalized()

	s.mu.Lock()
	defer s.mu.Unlock()
	if existing, ok := s.ifaces[iface.ID]; ok {
		existing.info.State = iface.State
		return nil
	}
	s.ifaces[iface.ID] = &ifaceData{
		info:      iface,
		addresses: make(map[string]Address),
	}
	return nil
}

// SetInterfaceState changes an existing interface's state.
func (s *Selector) SetInterfaceState(id string, state InterfaceState) error {
	if id == "" {
		return fmt.Errorf("%w: interface ID is empty", ErrInvalidArgument)
	}
	if state != InterfaceUp && state != InterfaceDown {
		return fmt.Errorf("%w: unknown interface state %q", ErrInvalidArgument, state)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	iface, ok := s.ifaces[id]
	if !ok {
		return fmt.Errorf("%w: %s", ErrInterfaceNotFound, id)
	}
	iface.info.State = state
	return nil
}

// RemoveInterface deletes an interface. Existing connections using it are
// reported by the next Evaluate/Apply call as RebuildInterfaceDeparted.
func (s *Selector) RemoveInterface(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.ifaces[id]; !ok {
		return fmt.Errorf("%w: %s", ErrInterfaceNotFound, id)
	}
	delete(s.ifaces, id)
	return nil
}

// Interface returns a view including addresses.
func (s *Selector) Interface(id string) (InterfaceView, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	iface, ok := s.ifaces[id]
	if !ok {
		return InterfaceView{}, false
	}
	view := InterfaceView{Interface: iface.info}
	for _, key := range iface.addrOrder {
		view.Addresses = append(view.Addresses, iface.addresses[key].clone())
	}
	return view, true
}

// Interfaces returns all interfaces in ID order.
func (s *Selector) Interfaces() []InterfaceView {
	s.mu.RLock()
	defer s.mu.RUnlock()
	ids := make([]string, 0, len(s.ifaces))
	for id := range s.ifaces {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	out := make([]InterfaceView, 0, len(ids))
	for _, id := range ids {
		iface := s.ifaces[id]
		view := InterfaceView{Interface: iface.info}
		for _, key := range iface.addrOrder {
			view.Addresses = append(view.Addresses, iface.addresses[key].clone())
		}
		out = append(out, view)
	}
	return out
}

// UpsertAddress adds or replaces an address on an existing interface.
func (s *Selector) UpsertAddress(interfaceID string, addr Address) error {
	if interfaceID == "" {
		return fmt.Errorf("%w: interface ID is empty", ErrInvalidArgument)
	}
	if err := addr.validate(); err != nil {
		return err
	}
	addr = addr.clone()
	key := addr.IP.String()

	s.mu.Lock()
	defer s.mu.Unlock()
	iface, ok := s.ifaces[interfaceID]
	if !ok {
		return fmt.Errorf("%w: %s", ErrInterfaceNotFound, interfaceID)
	}
	if _, exists := iface.addresses[key]; !exists {
		iface.addrOrder = append(iface.addrOrder, key)
	}
	iface.addresses[key] = addr
	return nil
}

// RemoveAddress removes one address from an interface.
func (s *Selector) RemoveAddress(interfaceID string, ip net.IP) error {
	normalized := cloneIP(ip)
	if normalized == nil {
		return ErrInvalidIP
	}
	key := normalized.String()

	s.mu.Lock()
	defer s.mu.Unlock()
	iface, ok := s.ifaces[interfaceID]
	if !ok {
		return fmt.Errorf("%w: %s", ErrInterfaceNotFound, interfaceID)
	}
	if _, ok := iface.addresses[key]; !ok {
		return fmt.Errorf("%w: %s on %s", ErrAddressNotFound, key, interfaceID)
	}
	delete(iface.addresses, key)
	for i, existing := range iface.addrOrder {
		if existing == key {
			iface.addrOrder = append(iface.addrOrder[:i], iface.addrOrder[i+1:]...)
			break
		}
	}
	return nil
}

// UpsertRoute inserts or replaces a route. Referencing a not-yet-created
// interface is allowed so event streams can interleave routing and interface
// state.
func (s *Selector) UpsertRoute(route Route) error {
	if err := route.validate(); err != nil {
		return err
	}
	if route.State == "" {
		route.State = RouteActive
	}
	route = route.clone()

	s.mu.Lock()
	defer s.mu.Unlock()
	s.routes[route.ID] = route
	return nil
}

// SetRouteState updates a route state.
func (s *Selector) SetRouteState(id string, state RouteState) error {
	switch state {
	case RouteActive, RouteInactive, RouteWithdrawn:
	default:
		return fmt.Errorf("%w: unknown route state %q", ErrInvalidArgument, state)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	route, ok := s.routes[id]
	if !ok {
		return fmt.Errorf("%w: %s", ErrRouteNotFound, id)
	}
	route.State = state
	s.routes[id] = route
	return nil
}

// RemoveRoute deletes a route.
func (s *Selector) RemoveRoute(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.routes[id]; !ok {
		return fmt.Errorf("%w: %s", ErrRouteNotFound, id)
	}
	delete(s.routes, id)
	return nil
}

// Route returns a cloned route by ID.
func (s *Selector) Route(id string) (Route, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	r, ok := s.routes[id]
	return r.clone(), ok
}

// Routes returns all routes in ID order.
func (s *Selector) Routes() []Route {
	s.mu.RLock()
	defer s.mu.RUnlock()
	ids := make([]string, 0, len(s.routes))
	for id := range s.routes {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	out := make([]Route, 0, len(ids))
	for _, id := range ids {
		out = append(out, s.routes[id].clone())
	}
	return out
}

// SelectSource chooses the current source for destination at at.
func (s *Selector) SelectSource(destination net.IP, at time.Time) (Selection, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	sel, err := s.selectLocked(destination, at)
	if err != nil {
		return Selection{}, err
	}
	return sel.clone(), nil
}

func (s *Selector) selectLocked(destination net.IP, at time.Time) (Selection, error) {
	dst := normalizeIP(destination)
	if dst == nil {
		return Selection{}, ErrInvalidIP
	}
	family := FamilyOf(dst)

	route, err := s.bestRouteLocked(dst)
	if err != nil {
		return Selection{}, err
	}

	iface, ok := s.ifaces[route.InterfaceID]
	if !ok {
		return Selection{}, fmt.Errorf("%w: route %s points to %s",
			ErrInterfaceNotFound, route.ID, route.InterfaceID)
	}
	if iface.info.State != InterfaceUp {
		return Selection{}, fmt.Errorf("%w: %s", ErrInterfaceDown, iface.info.ID)
	}

	dstScope := DeriveScope(dst)
	candidates := make([]Candidate, 0)
	for _, key := range iface.addrOrder {
		addr := iface.addresses[key]
		if FamilyOf(addr.IP) != family {
			continue
		}
		lifetime := addr.LifetimeAt(at)
		if lifetime == LifetimeInvalid {
			continue
		}
		scope := addr.EffectiveScope()
		if !scope.Covers(dstScope) {
			continue
		}
		cp := CommonPrefixLength(addr.IP, dst)
		if cp < 0 {
			continue
		}
		candidates = append(candidates, Candidate{
			Address:            addr.clone(),
			ConfiguredScope:    addr.Scope,
			EffectiveScope:     scope,
			Lifetime:           lifetime,
			Preferred:          lifetime == LifetimePreferred,
			CommonPrefixLength: cp,
		})
	}

	if len(candidates) == 0 {
		return Selection{}, fmt.Errorf("%w: interface %s has no %s address covering %s scope for %s at %s",
			ErrNoUsableAddress, iface.info.ID, familyName(family), dstScope, dst, at.Format(time.RFC3339))
	}

	sort.SliceStable(candidates, func(i, j int) bool {
		return compareCandidate(candidates[i], candidates[j]) < 0
	})

	best := candidates[0]
	return Selection{
		RouteID:            route.ID,
		InterfaceID:        route.InterfaceID,
		Source:             cloneIP(best.Address.IP),
		PrefixLength:       best.Address.PrefixLength,
		EffectiveScope:     best.EffectiveScope,
		Lifetime:           best.Lifetime,
		Preferred:          best.Preferred,
		CommonPrefixLength: best.CommonPrefixLength,
		Candidates:         cloneCandidates(candidates),
	}, nil
}

func cloneCandidates(in []Candidate) []Candidate {
	out := make([]Candidate, len(in))
	for i := range in {
		out[i] = in[i]
		out[i].Address = in[i].Address.clone()
	}
	return out
}

func familyName(f Family) string {
	switch f {
	case FamilyIPv4:
		return "IPv4"
	case FamilyIPv6:
		return "IPv6"
	default:
		return "unknown-family"
	}
}

// compareCandidate returns -1 when x should precede y, 1 when y should
// precede x, and 0 only when the stable order should be retained.
func compareCandidate(x, y Candidate) int {
	if x.Preferred != y.Preferred {
		if x.Preferred {
			return -1
		}
		return 1
	}
	if x.Address.PolicyLevel != y.Address.PolicyLevel {
		if x.Address.PolicyLevel > y.Address.PolicyLevel {
			return -1
		}
		return 1
	}
	if x.CommonPrefixLength != y.CommonPrefixLength {
		if x.CommonPrefixLength > y.CommonPrefixLength {
			return -1
		}
		return 1
	}
	if c := compareIP(x.Address.IP, y.Address.IP); c != 0 {
		return c
	}
	return 0
}

func (s *Selector) bestRouteLocked(dst net.IP) (Route, error) {
	var best Route
	found := false
	ids := make([]string, 0, len(s.routes))
	for id := range s.routes {
		ids = append(ids, id)
	}
	sort.Strings(ids)

	for _, id := range ids {
		route := s.routes[id]
		if !route.State.Effective() {
			continue
		}
		if route.Destination == nil || !route.Destination.Contains(dst) {
			continue
		}
		if !found || routeBetter(&route, &best) {
			best = route
			found = true
		}
	}
	if !found {
		return Route{}, fmt.Errorf("%w: destination %s", ErrNoRoute, dst)
	}
	return best.clone(), nil
}

// OpenConnection selects a source and stores an established connection.
func (s *Selector) OpenConnection(id string, destination net.IP, at time.Time) (Connection, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, _, err := s.openConnectionLocked(id, destination, at)
	if err != nil {
		return Connection{}, err
	}
	return c.clone(), nil
}

func (s *Selector) openConnectionLocked(id string, destination net.IP, at time.Time) (Connection, Selection, error) {
	if id == "" {
		return Connection{}, Selection{}, fmt.Errorf("%w: connection ID is empty", ErrInvalidArgument)
	}
	if _, exists := s.connections[id]; exists {
		return Connection{}, Selection{}, fmt.Errorf("%w: %s", ErrConnectionExists, id)
	}
	sel, err := s.selectLocked(destination, at)
	if err != nil {
		return Connection{}, Selection{}, err
	}
	conn := Connection{
		ID:              id,
		Destination:     cloneIP(destination),
		InterfaceID:     sel.InterfaceID,
		Source:          cloneIP(sel.Source),
		RouteID:         sel.RouteID,
		OpenedAt:        at,
		State:           ConnectionEstablished,
		LastEvaluatedAt: at,
	}
	s.connections[id] = conn
	s.connOrder = append(s.connOrder, id)
	return conn.clone(), sel.clone(), nil
}

// Connection returns a cloned connection.
func (s *Selector) Connection(id string) (Connection, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	c, ok := s.connections[id]
	return c.clone(), ok
}

// Connections returns connections in opening order.
func (s *Selector) Connections() []Connection {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]Connection, 0, len(s.connOrder))
	for _, id := range s.connOrder {
		out = append(out, s.connections[id].clone())
	}
	return out
}

// CloseConnection removes a connection.
func (s *Selector) CloseConnection(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.connections[id]; !ok {
		return fmt.Errorf("%w: %s", ErrConnectionNotFound, id)
	}
	delete(s.connections, id)
	for i, existing := range s.connOrder {
		if existing == id {
			s.connOrder = append(s.connOrder[:i], s.connOrder[i+1:]...)
			break
		}
	}
	return nil
}

// RebuildConnection reselection for an existing connection and clears its
// rebuild-needed state. If current state has no valid source, the connection
// is left unchanged and an error wrapping ErrRebuildUnsuccessful is returned.
func (s *Selector) RebuildConnection(id string, at time.Time) (Connection, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, err := s.rebuildConnectionLocked(id, at)
	if err != nil {
		return Connection{}, err
	}
	return c.clone(), nil
}

func (s *Selector) rebuildConnectionLocked(id string, at time.Time) (Connection, error) {
	conn, ok := s.connections[id]
	if !ok {
		return Connection{}, fmt.Errorf("%w: %s", ErrConnectionNotFound, id)
	}
	sel, err := s.selectLocked(conn.Destination, at)
	if err != nil {
		return Connection{}, fmt.Errorf("%w %s: %w", ErrRebuildUnsuccessful, id, err)
	}
	conn.InterfaceID = sel.InterfaceID
	conn.Source = cloneIP(sel.Source)
	conn.RouteID = sel.RouteID
	conn.State = ConnectionEstablished
	conn.RebuildReason = ""
	conn.LastEvaluatedAt = at
	s.connections[id] = conn
	return conn.clone(), nil
}

// Evaluate checks every connection against at and returns all connections that
// currently require rebuild, in opening order. A connection remains in
// rebuild-needed state until RebuildConnection succeeds.
func (s *Selector) Evaluate(at time.Time) []RebuildRecommendation {
	all, _ := s.EvaluateChanges(at)
	return all
}

// EvaluateChanges is Evaluate plus the subset of recommendations whose state
// transitioned from established to rebuild-needed during this evaluation.
func (s *Selector) EvaluateChanges(at time.Time) ([]RebuildRecommendation, []RebuildRecommendation) {
	s.mu.Lock()
	defer s.mu.Unlock()
	all, newly := s.evaluateLocked(at)
	return cloneRecommendations(all), cloneRecommendations(newly)
}

func (s *Selector) evaluateLocked(at time.Time) ([]RebuildRecommendation, []RebuildRecommendation) {
	all := make([]RebuildRecommendation, 0)
	newOnes := make([]RebuildRecommendation, 0)

	for _, id := range s.connOrder {
		conn := s.connections[id]
		rec, bad := diagnoseConnectionLocked(s.ifaces, conn, at)
		if bad {
			wasNeeded := conn.State == ConnectionRebuildNeeded
			rec.NewlyDetected = !wasNeeded
			conn.State = ConnectionRebuildNeeded
			conn.RebuildReason = rec.Reason
			conn.LastEvaluatedAt = at
			s.connections[id] = conn
			all = append(all, rec)
			if !wasNeeded {
				newOnes = append(newOnes, rec)
			}
			continue
		}

		if conn.State == ConnectionRebuildNeeded {
			// Latch: a previously detected broken flow is not silently declared
			// established just because configuration reappeared. It must be
			// explicitly rebuilt/revalidated by the caller.
			rec = RebuildRecommendation{
				ConnectionID:  conn.ID,
				Reason:        conn.RebuildReason,
				At:            at,
				InterfaceID:   conn.InterfaceID,
				Source:        cloneIP(conn.Source),
				Detail:        "connection remains marked for rebuild",
				NewlyDetected: false,
			}
			all = append(all, rec)
			continue
		}

		conn.LastEvaluatedAt = at
		s.connections[id] = conn
	}
	return all, newOnes
}

func diagnoseConnectionLocked(ifaces map[string]*ifaceData, conn Connection, at time.Time) (RebuildRecommendation, bool) {
	base := RebuildRecommendation{
		ConnectionID: conn.ID,
		At:           at,
		InterfaceID:  conn.InterfaceID,
		Source:       cloneIP(conn.Source),
	}

	iface, ok := ifaces[conn.InterfaceID]
	if !ok {
		base.Reason = RebuildInterfaceDeparted
		base.Detail = "original interface is no longer configured"
		return base, true
	}
	if iface.info.State != InterfaceUp {
		base.Reason = RebuildInterfaceUnavailable
		base.Detail = "original interface is not up"
		return base, true
	}

	key := normalizeIP(conn.Source).String()
	addr, ok := iface.addresses[key]
	if !ok {
		base.Reason = RebuildSourceWithdrawn
		base.Detail = "retained source address is no longer on original interface"
		return base, true
	}
	if addr.LifetimeAt(at) == LifetimeInvalid {
		base.Reason = RebuildSourceExpired
		base.Detail = "retained source has reached ValidUntil"
		return base, true
	}

	return RebuildRecommendation{}, false
}

func cloneRecommendations(in []RebuildRecommendation) []RebuildRecommendation {
	out := make([]RebuildRecommendation, len(in))
	for i := range in {
		out[i] = in[i].clone()
	}
	return out
}

// EgressDrift compares a connection's retained path with the path a new
// connection to the same destination would select now.
type EgressDrift struct {
	ConnectionID       string
	CurrentInterfaceID string
	CurrentSource      net.IP
	Available          bool
	BestRouteID        string
	BestInterfaceID    string
	BestSource         net.IP
	InterfaceChanged   bool
	SourceChanged      bool
	Err                string
}

func (d EgressDrift) clone() EgressDrift {
	d.CurrentSource = cloneIP(d.CurrentSource)
	d.BestSource = cloneIP(d.BestSource)
	return d
}

// EgressDrift calculates current selection drift for all connections. It does
// not modify connections or rebuild them.
func (s *Selector) EgressDrift(at time.Time) []EgressDrift {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.driftLocked(at)
}

func (s *Selector) driftLocked(at time.Time) []EgressDrift {
	out := make([]EgressDrift, 0)
	for _, id := range s.connOrder {
		conn := s.connections[id]
		d := EgressDrift{
			ConnectionID:       conn.ID,
			CurrentInterfaceID: conn.InterfaceID,
			CurrentSource:      cloneIP(conn.Source),
		}
		sel, err := s.selectLocked(conn.Destination, at)
		if err != nil {
			d.Available = false
			d.Err = err.Error()
			out = append(out, d)
			continue
		}
		d.Available = true
		d.BestRouteID = sel.RouteID
		d.BestInterfaceID = sel.InterfaceID
		d.BestSource = cloneIP(sel.Source)
		d.InterfaceChanged = sel.InterfaceID != conn.InterfaceID
		d.SourceChanged = compareIP(sel.Source, conn.Source) != 0
		if d.InterfaceChanged || d.SourceChanged {
			out = append(out, d)
		}
	}
	return out
}
