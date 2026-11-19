// Package connpool decides which HTTP/2 connections may be shared between
// domains that resolve to the same gateway and present the same certificate.
//
// The pool is fed locally observed handshake records (protocol, verified
// domains, remote address, isolation zone). A domain may reuse an existing
// connection only when all of the following hold at request time:
//
//   - the connection negotiated HTTP/2,
//   - its certificate verifiably covers the domain,
//   - its remote address is still part of the domain's current resolution,
//   - it belongs to the domain's isolation zone, and
//   - the (domain, connection) pair has not been revoked by a 421.
//
// A 421 revokes exactly one (domain, connection) pair. Streams that were
// already opened on that connection keep running; only future acquisitions for
// that domain stop choosing it. Resolution is re-read on every acquisition, so
// an address rotation is evaluated against the fresh value and may force a new
// connection.
package connpool

import (
	"fmt"
	"sort"
	"strings"
	"sync"
)

// Resolver returns the current IP addresses (with optional ports) for a domain.
// Implementations are expected to perform a fresh lookup on every call.
type Resolver interface {
	Resolve(domain string) ([]string, error)
}

// ResolverFunc adapts a function to Resolver.
type ResolverFunc func(domain string) ([]string, error)

// Resolve implements Resolver.
func (f ResolverFunc) Resolve(domain string) ([]string, error) { return f(domain) }

// ZonePolicy maps a domain to its isolation zone. Domains without a zone are
// refused rather than silently grouped together.
type ZonePolicy interface {
	ZoneFor(domain string) (string, error)
}

// ZonePolicyFunc adapts a function to ZonePolicy.
type ZonePolicyFunc func(domain string) (string, error)

// ZoneFor implements ZonePolicy.
func (f ZonePolicyFunc) ZoneFor(domain string) (string, error) { return f(domain) }

// ZoneMap is a static zone policy keyed by lowercase domain.
type ZoneMap map[string]string

// ZoneFor implements ZonePolicy.
func (m ZoneMap) ZoneFor(domain string) (string, error) {
	zone, ok := m[strings.ToLower(strings.TrimSuffix(strings.TrimSpace(domain), "."))]
	if !ok || strings.TrimSpace(zone) == "" {
		return "", fmt.Errorf("%w: %s", ErrUnknownZone, domain)
	}
	return strings.TrimSpace(zone), nil
}

// Dialer opens a new connection for a domain. candidates holds the freshly
// resolved addresses; the returned record must describe the handshake that
// actually happened. A dialer is only consulted when no connection is
// reusable.
type Dialer interface {
	Dial(domain, zone string, candidates []string) (HandshakeRecord, error)
}

// DialerFunc adapts a function to Dialer.
type DialerFunc func(domain, zone string, candidates []string) (HandshakeRecord, error)

// Dial implements Dialer.
func (f DialerFunc) Dial(domain, zone string, candidates []string) (HandshakeRecord, error) {
	return f(domain, zone, candidates)
}

// Options configures a Pool.
type Options struct {
	Resolver Resolver
	Zones    ZonePolicy
	Dialer   Dialer
}

// Action describes what an acquisition did.
type Action string

const (
	// ActionReuse means an existing connection was selected.
	ActionReuse Action = "reuse"
	// ActionDial means no eligible connection existed and a new one was made.
	ActionDial Action = "dial"
)

// Reuse blockers are stable, machine-readable reasons.
const (
	ReasonProtocolMismatch    = "protocol_not_http2"
	ReasonCertificateMismatch = "certificate_mismatch"
	ReasonAddressMismatch     = "address_mismatch"
	ReasonZoneMismatch        = "zone_mismatch"
	ReasonRevokedPair         = "revoked_by_421"
)

// Skip records why one connection was not reused for a domain.
type Skip struct {
	ConnectionID string
	Reason       string
	Detail       string
}

// Decision is the observable outcome of one acquisition. It answers, per
// domain, whether it keeps reusing a connection or needs a new one, and why.
type Decision struct {
	Domain       string
	Zone         string
	Resolved     []string
	Action       Action
	ConnectionID string
	Protocol     string
	Reason       string
	Skipped      []Skip
}

// String renders a decision for logs and README examples.
func (d Decision) String() string {
	var b strings.Builder
	fmt.Fprintf(&b, "%s zone=%s action=%s", d.Domain, d.Zone, d.Action)
	if d.ConnectionID != "" {
		fmt.Fprintf(&b, " conn=%s", d.ConnectionID)
	}
	if d.Reason != "" && d.Reason != string(d.Action) {
		fmt.Fprintf(&b, " reason=%s", d.Reason)
	}
	if len(d.Resolved) > 0 {
		fmt.Fprintf(&b, " resolved=%s", strings.Join(d.Resolved, ","))
	}
	for _, skip := range d.Skipped {
		fmt.Fprintf(&b, " skip(%s=%s)", skip.ConnectionID, skip.Reason)
	}
	return b.String()
}

// Stream is an open HTTP/2 stream lease. Ending it never affects other
// streams on the same connection.
type Stream struct {
	ID           uint64
	Domain       string
	ConnectionID string

	pool  *Pool
	ended bool
}

// End releases the stream. The connection stays open for other streams.
func (s *Stream) End() error {
	if s == nil || s.pool == nil {
		return ErrStreamEnded
	}
	return s.pool.endStream(s)
}

// ConnectionInfo is a read-only snapshot of a registered connection.
type ConnectionInfo struct {
	ID              string
	Protocol        string
	VerifiedDomains []string
	RemoteAddr      string
	Zone            string
	ActiveStreams   int
	RevokedDomains  []string
}

// Pool is a connection selector. It is safe for concurrent use, but resolver
// and dialer callbacks must not call back into the same Pool.
type Pool struct {
	mu     sync.Mutex
	opts   Options
	byID   map[string]*connection
	order  []*connection
	nextID uint64
}

// New validates the options and returns an empty pool.
func New(opts Options) (*Pool, error) {
	if opts.Resolver == nil {
		return nil, ErrNoResolver
	}
	if opts.Zones == nil {
		return nil, ErrNoZonePolicy
	}
	return &Pool{
		opts: opts,
		byID: make(map[string]*connection),
	}, nil
}

// AddConnection registers a locally observed handshake record.
func (p *Pool) AddConnection(record HandshakeRecord) error {
	conn, err := newConnection(record)
	if err != nil {
		return err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if _, exists := p.byID[conn.id]; exists {
		return fmt.Errorf("%w: %s", ErrDuplicateConnection, conn.id)
	}
	p.byID[conn.id] = conn
	p.order = append(p.order, conn)
	return nil
}

// Acquire picks a connection for domain or dials a new one when no connection
// passes every reuse rule. The returned Decision explains the choice even when
// Acquire fails.
func (p *Pool) Acquire(domain string) (*Stream, Decision, error) {
	normalized, err := normalizeDomain(domain, false)
	if err != nil {
		return nil, Decision{Domain: domain}, err
	}

	p.mu.Lock()
	defer p.mu.Unlock()

	zone, err := p.opts.Zones.ZoneFor(normalized)
	if err != nil {
		return nil, Decision{Domain: normalized}, fmt.Errorf("%w: %v", ErrUnknownZone, err)
	}

	rawResolved, err := p.opts.Resolver.Resolve(normalized)
	if err != nil {
		return nil, Decision{Domain: normalized, Zone: zone}, fmt.Errorf("connpool: resolve %s: %w", normalized, err)
	}
	resolved, err := normalizeAddresses(rawResolved)
	if err != nil {
		return nil, Decision{Domain: normalized, Zone: zone}, err
	}

	decision := Decision{
		Domain:   normalized,
		Zone:     zone,
		Resolved: addressStrings(resolved),
	}

	var eligible []*connection
	for _, conn := range p.order {
		reason, detail := reuseBlocker(conn, normalized, zone, resolved)
		if reason != "" {
			decision.Skipped = append(decision.Skipped, Skip{
				ConnectionID: conn.id,
				Reason:       reason,
				Detail:       detail,
			})
			continue
		}
		eligible = append(eligible, conn)
	}

	if len(eligible) > 0 {
		chosen := leastLoaded(eligible)
		stream := p.openStream(chosen, normalized)
		decision.Action = ActionReuse
		decision.ConnectionID = chosen.id
		decision.Protocol = chosen.protocol
		decision.Reason = string(ActionReuse)
		return stream, decision, nil
	}

	if p.opts.Dialer == nil {
		decision.Action = ActionDial
		decision.Reason = "no_dialer"
		return nil, decision, ErrNoDialer
	}

	candidates := addressStrings(resolved)
	record, err := p.opts.Dialer.Dial(normalized, zone, candidates[0:len(candidates):len(candidates)])
	if err != nil {
		decision.Action = ActionDial
		decision.Reason = "dial_failed"
		return nil, decision, fmt.Errorf("connpool: dial %s: %w", normalized, err)
	}

	conn, err := newConnection(record)
	if err != nil {
		decision.Action = ActionDial
		decision.Reason = "invalid_handshake"
		return nil, decision, err
	}
	if _, exists := p.byID[conn.id]; exists {
		decision.Action = ActionDial
		decision.Reason = "duplicate_connection"
		return nil, decision, fmt.Errorf("%w: %s", ErrDuplicateConnection, conn.id)
	}
	if err := validateDialed(conn, normalized, zone, resolved); err != nil {
		decision.Action = ActionDial
		decision.Reason = "dial_rejected"
		return nil, decision, err
	}

	p.byID[conn.id] = conn
	p.order = append(p.order, conn)
	stream := p.openStream(conn, normalized)
	decision.Action = ActionDial
	decision.ConnectionID = conn.id
	decision.Protocol = conn.protocol
	decision.Reason = string(ActionDial)
	return stream, decision, nil
}

// Handle421 revokes only the (domain, connection) pair that received the 421.
// Other domains on the connection, and streams already open on it, continue.
func (p *Pool) Handle421(connectionID, domain string) error {
	normalized, err := normalizeDomain(domain, false)
	if err != nil {
		return err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	conn, ok := p.byID[connectionID]
	if !ok {
		return fmt.Errorf("%w: %s", ErrUnknownConnection, connectionID)
	}
	conn.revoked[normalized] = true
	return nil
}

// ConnectionInfo returns a snapshot of every registered connection in
// registration order.
func (p *Pool) ConnectionInfo() []ConnectionInfo {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]ConnectionInfo, 0, len(p.order))
	for _, conn := range p.order {
		info := ConnectionInfo{
			ID:              conn.id,
			Protocol:        conn.protocol,
			VerifiedDomains: append([]string(nil), conn.verified...),
			RemoteAddr:      conn.remote.String(),
			Zone:            conn.zone,
			ActiveStreams:   len(conn.active),
		}
		for domain := range conn.revoked {
			info.RevokedDomains = append(info.RevokedDomains, domain)
		}
		sort.Strings(info.RevokedDomains)
		out = append(out, info)
	}
	return out
}

func (p *Pool) openStream(conn *connection, domain string) *Stream {
	p.nextID++
	id := p.nextID
	conn.active[id] = struct{}{}
	return &Stream{
		ID:           id,
		Domain:       domain,
		ConnectionID: conn.id,
		pool:         p,
	}
}

func (p *Pool) endStream(stream *Stream) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if stream.ended {
		return ErrStreamEnded
	}
	conn, ok := p.byID[stream.ConnectionID]
	if !ok {
		return fmt.Errorf("%w: %s", ErrUnknownConnection, stream.ConnectionID)
	}
	stream.ended = true
	delete(conn.active, stream.ID)
	return nil
}

func reuseBlocker(conn *connection, domain, zone string, resolved []address) (string, string) {
	if !isHTTP2(conn.protocol) {
		return ReasonProtocolMismatch, "negotiated " + conn.protocol
	}
	if !certificateCovers(conn.verified, domain) {
		return ReasonCertificateMismatch, "verified for " + strings.Join(conn.verified, ",")
	}
	if !addressesMatch(conn.remote, resolved) {
		return ReasonAddressMismatch, conn.remote.String() + " not in resolved set"
	}
	if conn.zone != zone {
		return ReasonZoneMismatch, conn.zone + " != " + zone
	}
	if conn.revoked[domain] {
		return ReasonRevokedPair, "421 received for this domain on this connection"
	}
	return "", ""
}

func validateDialed(conn *connection, domain, zone string, resolved []address) error {
	if !isHTTP2(conn.protocol) {
		return fmt.Errorf("%w: negotiated %q", ErrProtocolUnsupported, conn.protocol)
	}
	if !certificateCovers(conn.verified, domain) {
		return fmt.Errorf("%w: %s not covered by %s", ErrCertificateMismatch, domain, strings.Join(conn.verified, ","))
	}
	if !addressesMatch(conn.remote, resolved) {
		return fmt.Errorf("%w: %s not in %s", ErrAddressMismatch, conn.remote.String(), strings.Join(addressStrings(resolved), ","))
	}
	if conn.zone != zone {
		return fmt.Errorf("%w: %s != %s", ErrZoneMismatch, conn.zone, zone)
	}
	return nil
}

func leastLoaded(connections []*connection) *connection {
	best := connections[0]
	for _, candidate := range connections[1:] {
		if len(candidate.active) < len(best.active) ||
			(len(candidate.active) == len(best.active) && candidate.id < best.id) {
			best = candidate
		}
	}
	return best
}
