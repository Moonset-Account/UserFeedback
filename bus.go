// Package reqreplybridge implements an in-process request/reply message bus
// and bridges that forward requests between two buses while renumbering
// correlation identifiers on every hop.
//
// The model intentionally mirrors the problem statement:
//
//   - A client session numbers its own requests starting from correlation 1.
//   - A bridge allocates a fresh per-egress-session correlation number for
//     every forwarded request and swaps it back to the original session's
//     correlation (and branch) numbers when replies return.
//   - An egress session may disconnect and reconnect, after which its short
//     correlation numbers are reused from 1. Every session incarnation has an
//     epoch, and late packets from an old incarnation are delivered to that
//     old incarnation rather than to the new one.
//   - A request may be answered on several branches. A branch ends only after
//     fragment sequence numbers 0..TailSeq are all present continuously.
//     The parent request can finish only after every branch has ended.
package reqreplybridge

import (
	"errors"
	"sync"
)

// ReplyAddr addresses the session incarnation that must receive a reply.
//
// Endpoint identifies the client/bridge endpoint; Epoch selects one
// incarnation of that endpoint's egress/ingress connection. Correlation is
// the caller-local short number of the outstanding request.
type ReplyAddr struct {
	Endpoint    EndpointID
	Epoch       int
	Correlation uint64
}

// RequestMsg is a request fragment carried on a bus.
type RequestMsg struct {
	Target      string
	Correlation uint64
	Branch      uint64
	Seq         uint64
	Payload     string
	// ReplyTo is stamped by the bus when the request is sent and must be
	// copied verbatim onto every reply fragment.
	ReplyTo ReplyAddr
}

// ReplyMsg is a reply fragment carried on a bus.
type ReplyMsg struct {
	Addr ReplyAddr

	// Branch is the responder-local branch number; the receiving bridge maps
	// it back to the caller-local branch number.
	Branch uint64
	Seq    uint64

	// DeclareBranches optionally declares the total number of branches of the
	// parent request. It may arrive on any reply fragment, even late.
	DeclareBranches *uint64
	// DeclareTail optionally declares the final sequence number of this
	// branch. It may arrive on any fragment of the branch, even late.
	DeclareTail *uint64

	Payload string
}

// Error sentinels returned by the bus and the bridge.
var (
	// ErrEndpointClosed is returned when an endpoint has been closed.
	ErrEndpointClosed = errors.New("endpoint is closed")
	// ErrNotConnected is returned when an endpoint acts without a live session.
	ErrNotConnected = errors.New("endpoint is not connected")
	// ErrUnknownEndpoint is returned when a reply names an endpoint that the
	// bus does not host.
	ErrUnknownEndpoint = errors.New("unknown endpoint")
	// ErrNoHandler is returned when a request targets an endpoint that has no
	// request handler registered.
	ErrNoHandler = errors.New("no request handler registered for endpoint")
	// ErrBadCorrelation is returned when a reply names a correlation that the
	// addressed session incarnation never allocated.
	ErrBadCorrelation = errors.New("correlation number unknown to session")
	// ErrEgressDown is returned when a bridge cannot forward because its
	// egress endpoint is disconnected or closed.
	ErrEgressDown = errors.New("bridge egress is down")
	// ErrStaleReply is returned when a reply cannot be mapped because the
	// bridge already discarded the corresponding forwarded request.
	ErrStaleReply = errors.New("reply belongs to a finalized or unknown request")
	// ErrGap is returned when a reply would finish a branch while one or more
	// sequence numbers are still missing.
	ErrGap = errors.New("branch fragments are not continuous from zero")
)

// EndpointID names an endpoint attached to a bus.
type EndpointID string

// RequestHandler receives every request targeted at an endpoint.
type RequestHandler func(RequestMsg) error

// ReplyHandler receives every reply addressed to one session incarnation.
type ReplyHandler func(ReplyMsg)

// Session is one incarnation of an endpoint connection.
//
// Allocations start at 1 and are only valid for this incarnation. Even after
// the endpoint reconnects (creating a new session), a detached session keeps
// accepting late replies addressed to its epoch.
type Session struct {
	endpoint EndpointID
	epoch    int

	mu          sync.Mutex
	nextCorr    uint64
	allocated   map[uint64]struct{}
	reply       ReplyHandler
	detached    bool
	closed      bool
	lateDropped int
}

// Epoch returns the incarnation number of this session.
func (s *Session) Epoch() int { return s.epoch }

// Endpoint returns the endpoint this session belongs to.
func (s *Session) Endpoint() EndpointID { return s.endpoint }

// AllocateCorrelation reserves the next caller-local short number. The first
// allocation on every incarnation returns 1.
func (s *Session) AllocateCorrelation() (uint64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return 0, ErrEndpointClosed
	}
	s.nextCorr++
	s.allocated[s.nextCorr] = struct{}{}
	return s.nextCorr, nil
}

// deliver is invoked by the bus; detached sessions still accept late replies.
func (s *Session) deliver(m ReplyMsg) error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return ErrEndpointClosed
	}
	if _, ok := s.allocated[m.Addr.Correlation]; !ok {
		s.lateDropped++
		s.mu.Unlock()
		return ErrBadCorrelation
	}
	h := s.reply
	s.mu.Unlock()
	if h != nil {
		h(m)
	}
	return nil
}

// LateDropped reports how many replies failed validation against this
// incarnation (for example correlations it never allocated).
func (s *Session) LateDropped() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.lateDropped
}

// Endpoint is a named client/bridge attachment point on a bus.
type Endpoint struct {
	id EndpointID

	mu       sync.Mutex
	sessions map[int]*Session
	current  *Session
	closed   bool

	requestHandlers []RequestHandler
}

// ID returns the endpoint identifier.
func (e *Endpoint) ID() EndpointID { return e.id }

// OnRequest registers a request handler. Multiple handlers each receive the
// request; the first handler error aborts fan-out.
func (e *Endpoint) OnRequest(h RequestHandler) {
	e.mu.Lock()
	e.requestHandlers = append(e.requestHandlers, h)
	e.mu.Unlock()
}

// Connect creates a new session incarnation. Its correlation allocator starts
// again from 1 and its epoch is one greater than the previous incarnation.
// Detached previous sessions remain reachable for late replies.
func (e *Endpoint) Connect(replyHandler ReplyHandler) (*Session, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.closed {
		return nil, ErrEndpointClosed
	}
	epoch := 1
	if e.current != nil {
		e.current.mu.Lock()
		e.current.detached = true
		e.current.mu.Unlock()
	}
	// A disconnected endpoint has no current session but keeps the old
	// incarnations; derive the next epoch from every retained incarnation so
	// epochs stay strictly monotonic across disconnect/reconnect cycles.
	for existing := range e.sessions {
		if existing >= epoch {
			epoch = existing + 1
		}
	}
	s := &Session{
		endpoint:  e.id,
		epoch:     epoch,
		nextCorr:  0,
		allocated: map[uint64]struct{}{},
		reply:     replyHandler,
	}
	e.sessions[epoch] = s
	e.current = s
	return s, nil
}

// Disconnect detaches the current session without discarding it. Late replies
// addressed to its epoch continue to be delivered until Close.
func (e *Endpoint) Disconnect() {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.current != nil {
		e.current.mu.Lock()
		e.current.detached = true
		e.current.mu.Unlock()
		e.current = nil
	}
}

// Session returns the live session incarnation, or nil while disconnected.
func (e *Endpoint) Session() *Session {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.current
}

// SessionByEpoch returns an incarnation (including detached ones) by epoch.
func (e *Endpoint) SessionByEpoch(epoch int) (*Session, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	s, ok := e.sessions[epoch]
	return s, ok
}

// Close permanently shuts the endpoint and all of its incarnations.
func (e *Endpoint) Close() {
	e.mu.Lock()
	sessions := e.sessions
	e.sessions = map[int]*Session{}
	e.current = nil
	e.closed = true
	e.mu.Unlock()
	for _, s := range sessions {
		s.mu.Lock()
		s.closed = true
		s.mu.Unlock()
	}
}

// Bus is an in-process request/reply message bus.
type Bus struct {
	name string

	mu        sync.Mutex
	endpoints map[EndpointID]*Endpoint
}

// NewBus creates an empty bus with a diagnostic name.
func NewBus(name string) *Bus {
	return &Bus{name: name, endpoints: map[EndpointID]*Endpoint{}}
}

// Name returns the diagnostic bus name.
func (b *Bus) Name() string { return b.name }

// Attach creates an endpoint on the bus. Endpoint identifiers must be unique
// within one bus.
func (b *Bus) Attach(id EndpointID) (*Endpoint, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if _, exists := b.endpoints[id]; exists {
		return nil, errors.New("duplicate endpoint on bus: " + string(id))
	}
	e := &Endpoint{id: id, sessions: map[int]*Session{}}
	b.endpoints[id] = e
	return e, nil
}

// endpoint looks up an endpoint without taking the bus lock twice.
func (b *Bus) endpoint(id EndpointID) (*Endpoint, error) {
	b.mu.Lock()
	e, ok := b.endpoints[id]
	b.mu.Unlock()
	if !ok {
		return nil, ErrUnknownEndpoint
	}
	return e, nil
}

// SendRequest routes a request to the endpoint named by Target. The bus stamps
// ReplyTo from the sender's live session so the responder can address replies
// to exactly this incarnation.
func (b *Bus) SendRequest(sender *Session, m RequestMsg) error {
	if sender == nil {
		return ErrNotConnected
	}
	target, err := b.endpoint(EndpointID(m.Target))
	if err != nil {
		return err
	}
	m.ReplyTo = ReplyAddr{
		Endpoint:    sender.endpoint,
		Epoch:       sender.epoch,
		Correlation: m.Correlation,
	}
	target.mu.Lock()
	handlers := append([]RequestHandler(nil), target.requestHandlers...)
	closed := target.closed
	target.mu.Unlock()
	if closed {
		return ErrEndpointClosed
	}
	if len(handlers) == 0 {
		return ErrNoHandler
	}
	for _, h := range handlers {
		if err := h(m); err != nil {
			return err
		}
	}
	return nil
}

// SendReply delivers a reply to the incarnation named by m.Addr. A reply to an
// old, disconnected epoch is still delivered to that old session.
func (b *Bus) SendReply(m ReplyMsg) error {
	e, err := b.endpoint(m.Addr.Endpoint)
	if err != nil {
		return err
	}
	s, ok := e.SessionByEpoch(m.Addr.Epoch)
	if !ok {
		return ErrUnknownEndpoint
	}
	return s.deliver(m)
}
