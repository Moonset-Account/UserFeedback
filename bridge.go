package reqreplybridge

import "sync"

// Direction names one of the two forwarding directions of a bridge.
type Direction string

// The two fixed directions.
const (
	LeftToRight Direction = "left->right"
	RightToLeft Direction = "right->left"
)

// ForwardEvent reports one request hop: an ingress correlation became an
// egress correlation on a specific session incarnation.
type ForwardEvent struct {
	Direction Direction
	Ingress   ReplyAddr
	Egress    ReplyAddr
}

// ReplyEvent reports one reply hop back through a bridge, including any branch
// or parent completion caused by the fragment.
type ReplyEvent struct {
	Direction Direction
	// Received is the fragment as it arrived on the egress bus.
	Received ReplyMsg
	// Delivered is the translated fragment sent back on the ingress bus.
	Delivered ReplyMsg

	// BranchEnded is true when this fragment completed one branch.
	BranchEnded bool
	// BranchNumber is the caller-local (ingress) branch number when BranchEnded.
	BranchNumber uint64
	// ParentEnded is true when every branch of the parent request has ended.
	ParentEnded bool
	// TotalBranches is valid when ParentEnded.
	TotalBranches uint64

	// Error is non-nil when the fragment could not be mapped or proved a gap.
	Error error
}

// LegStats is a diagnostic snapshot of one bridge direction.
type LegStats struct {
	EgressEpoch      int
	NextEgressCorr   uint64
	ActiveParents    int
	BranchesSeen     uint64
	FinishedBranches uint64
	FinishedParents  uint64
}

// branchState tracks one responder-local branch while its fragments arrive.
type branchState struct {
	egressNumber uint64
	ingressNum   uint64
	tailDeclared bool
	tail         uint64
	seqs         map[uint64]struct{}
	ended        bool
	gapReported  bool
}

// parentState tracks one forwarded request between two session incarnations.
type parentState struct {
	egressEpoch    int
	egressCorr     uint64
	ingressAddr    ReplyAddr
	branchCount    uint64
	branchesDecl   bool
	branches       map[uint64]*branchState
	finishedBranch uint64
	ended          bool
}

// parentKey identifies a forwarded request by egress session incarnation AND
// egress correlation. Because short numbers restart at 1 after a reconnect,
// the epoch is required to keep an old incarnation's late packets separate.
type parentKey struct {
	epoch int
	corr  uint64
}

// bridgeLeg owns one direction: one ingress endpoint and one egress endpoint.
type bridgeLeg struct {
	direction     Direction
	ingressBus    *Bus
	egressBus     *Bus
	ingress       *Endpoint
	egress        *Endpoint
	forwardTarget string // endpoint id on the egress bus that receives forwards

	mu      sync.Mutex
	parents map[parentKey]*parentState // keyed by (egress epoch, correlation)
	seen    uint64
	doneB   uint64
	doneP   uint64

	onForward func(ForwardEvent)
	onReply   func(ReplyEvent)
}

func (l *bridgeLeg) egressSession() *Session {
	return l.egress.Session()
}

// replyHandler adapts handleReply to the bus's ReplyHandler signature.
func (l *bridgeLeg) replyHandler(m ReplyMsg) { l.handleReply(m) }

// handleRequest allocates an egress short number and forwards the request.
func (l *bridgeLeg) handleRequest(m RequestMsg) error {
	es := l.egressSession()
	if es == nil {
		return ErrEgressDown
	}
	egressCorr, err := es.AllocateCorrelation()
	if err != nil {
		return ErrEgressDown
	}

	parent := &parentState{
		egressEpoch: es.epoch,
		egressCorr:  egressCorr,
		ingressAddr: m.ReplyTo,
		branches:    map[uint64]*branchState{},
	}
	l.mu.Lock()
	l.parents[parentKey{epoch: es.epoch, corr: egressCorr}] = parent
	l.mu.Unlock()

	out := RequestMsg{
		Target:      l.forwardTarget,
		Correlation: egressCorr,
		Branch:      m.Branch,
		Seq:         m.Seq,
		Payload:     m.Payload,
	}
	if err := l.egressBus.SendRequest(es, out); err != nil {
		return err
	}
	if l.onForward != nil {
		l.onForward(ForwardEvent{
			Direction: l.direction,
			Ingress:   m.ReplyTo,
			Egress: ReplyAddr{
				Endpoint:    l.egress.id,
				Epoch:       es.epoch,
				Correlation: egressCorr,
			},
		})
	}
	return nil
}

// handleReply maps an egress reply back to the ingress session and evaluates
// branch/parent completion.
func (l *bridgeLeg) handleReply(m ReplyMsg) ReplyEvent {
	ev := ReplyEvent{Direction: l.direction, Received: m}

	l.mu.Lock()
	key := parentKey{epoch: m.Addr.Epoch, corr: m.Addr.Correlation}
	parent, ok := l.parents[key]
	if !ok {
		l.mu.Unlock()
		ev.Error = ErrStaleReply
		if l.onReply != nil {
			l.onReply(ev)
		}
		return ev
	}

	br, ok := parent.branches[m.Branch]
	if !ok {
		br = &branchState{
			egressNumber: m.Branch,
			ingressNum:   m.Branch,
			seqs:         map[uint64]struct{}{},
		}
		parent.branches[m.Branch] = br
		l.seen++
	}
	if m.DeclareBranches != nil {
		parent.branchCount = *m.DeclareBranches
		parent.branchesDecl = true
	}
	if m.DeclareTail != nil {
		br.tail = *m.DeclareTail
		br.tailDeclared = true
	}
	br.seqs[m.Seq] = struct{}{}

	branchEnded := false
	gap := false
	if !br.ended && br.tailDeclared {
		if continuous(br.seqs, br.tail) {
			br.ended = true
			parent.finishedBranch++
			l.doneB++
			branchEnded = true
		} else if !br.gapReported {
			// Report the first failed continuity check once; later fragments
			// that still do not complete the branch are ordinary fragments.
			br.gapReported = true
			gap = true
		}
	}

	parentEnded := false
	total := uint64(0)
	if !parent.ended && parent.branchesDecl && parent.finishedBranch == parent.branchCount {
		allEnded := len(parent.branches) == int(parent.branchCount)
		for _, b := range parent.branches {
			if !b.ended {
				allEnded = false
			}
		}
		if allEnded {
			parent.ended = true
			parentEnded = true
			total = parent.branchCount
			l.doneP++
			delete(l.parents, key)
		}
	}

	delivered := ReplyMsg{
		Addr:            parent.ingressAddr,
		Branch:          br.ingressNum,
		Seq:             m.Seq,
		DeclareBranches: m.DeclareBranches,
		DeclareTail:     m.DeclareTail,
		Payload:         m.Payload,
	}
	l.mu.Unlock()

	// Deliver outside the leg lock: ingress handlers may run synchronously.
	if err := l.ingressBus.SendReply(delivered); err != nil {
		ev.Error = err
	} else {
		ev.Delivered = delivered
	}
	if gap {
		ev.Error = ErrGap
	}
	ev.BranchEnded = branchEnded
	ev.BranchNumber = br.ingressNum
	ev.ParentEnded = parentEnded
	ev.TotalBranches = total

	if l.onReply != nil {
		l.onReply(ev)
	}
	return ev
}

// continuous reports whether every sequence number 0..tail is present.
func continuous(seqs map[uint64]struct{}, tail uint64) bool {
	for i := uint64(0); i <= tail; i++ {
		if _, ok := seqs[i]; !ok {
			return false
		}
	}
	return true
}

// Bridge forwards requests in both directions between two buses.
//
// Each direction owns an ingress endpoint (where caller requests arrive) and
// an egress endpoint (the identity used on the far bus). Reconnecting an
// egress endpoint starts a new epoch and reuses short numbers from 1.
type Bridge struct {
	name     string
	leftBus  *Bus
	rightBus *Bus
	leftIn   *Endpoint // ingress for LeftToRight, attached to leftBus
	rightOut *Endpoint // egress for LeftToRight, attached to rightBus
	rightIn  *Endpoint // ingress for RightToLeft, attached to rightBus
	leftOut  *Endpoint // egress for RightToLeft, attached to leftBus
	lr       *bridgeLeg
	rl       *bridgeLeg
}

// BridgeConfig names the four endpoint identifiers used by a bridge and the
// two forwarding targets (endpoint ids on the opposite bus).
type BridgeConfig struct {
	Name string

	LeftIngress  EndpointID
	LeftEgress   EndpointID
	RightIngress EndpointID
	RightEgress  EndpointID

	// LRForwardTarget receives forwarded left->right requests on the right bus.
	LRForwardTarget string
	// RLForwardTarget receives forwarded right->left requests on the left bus.
	RLForwardTarget string
}

// NewBridge attaches the four endpoints and builds the bridge. Call Start to
// register the handlers.
func NewBridge(left, right *Bus, cfg BridgeConfig) (*Bridge, error) {
	leftIn, err := left.Attach(cfg.LeftIngress)
	if err != nil {
		return nil, err
	}
	leftOut, err := left.Attach(cfg.LeftEgress)
	if err != nil {
		return nil, err
	}
	rightIn, err := right.Attach(cfg.RightIngress)
	if err != nil {
		return nil, err
	}
	rightOut, err := right.Attach(cfg.RightEgress)
	if err != nil {
		return nil, err
	}

	lr := &bridgeLeg{
		direction:     LeftToRight,
		ingressBus:    left,
		egressBus:     right,
		ingress:       leftIn,
		egress:        rightOut,
		forwardTarget: cfg.LRForwardTarget,
		parents:       map[parentKey]*parentState{},
	}
	rl := &bridgeLeg{
		direction:     RightToLeft,
		ingressBus:    right,
		egressBus:     left,
		ingress:       rightIn,
		egress:        leftOut,
		forwardTarget: cfg.RLForwardTarget,
		parents:       map[parentKey]*parentState{},
	}
	return &Bridge{
		name:     cfg.Name,
		leftBus:  left,
		rightBus: right,
		leftIn:   leftIn,
		rightOut: rightOut,
		rightIn:  rightIn,
		leftOut:  leftOut,
		lr:       lr,
		rl:       rl,
	}, nil
}

// Start connects the two egress endpoints (epochs start at 1, correlations at
// 1) and registers request handlers on the two ingress endpoints.
func (br *Bridge) Start() error {
	if _, err := br.rightOut.Connect(br.lr.replyHandler); err != nil {
		return err
	}
	if _, err := br.leftOut.Connect(br.rl.replyHandler); err != nil {
		return err
	}
	br.leftIn.OnRequest(br.lr.handleRequest)
	br.rightIn.OnRequest(br.rl.handleRequest)
	return nil
}

// ReconnectEgress disconnects and reconnects one egress endpoint. The new
// incarnation gets a fresh epoch and correlations are again allocated from 1.
// Old incarnations remain reachable for late replies.
func (br *Bridge) ReconnectEgress(d Direction) error {
	switch d {
	case LeftToRight:
		br.rightOut.Disconnect()
		_, err := br.rightOut.Connect(br.lr.replyHandler)
		return err
	case RightToLeft:
		br.leftOut.Disconnect()
		_, err := br.leftOut.Connect(br.rl.replyHandler)
		return err
	default:
		return ErrUnknownEndpoint
	}
}

// EgressSession exposes the live egress session of one direction.
func (br *Bridge) EgressSession(d Direction) *Session {
	if d == LeftToRight {
		return br.rightOut.Session()
	}
	return br.leftOut.Session()
}

// EgressEndpoint exposes an egress endpoint, mainly for tests.
func (br *Bridge) EgressEndpoint(d Direction) *Endpoint {
	if d == LeftToRight {
		return br.rightOut
	}
	return br.leftOut
}

// IngressEndpoint exposes an ingress endpoint, mainly for tests.
func (br *Bridge) IngressEndpoint(d Direction) *Endpoint {
	if d == LeftToRight {
		return br.leftIn
	}
	return br.rightIn
}

// HandleEgressReply feeds one reply directly into a bridge leg as if it had
// been delivered on the egress bus. It is intended for tests that need to
// exercise bridge-level mapping (for example stale replies) without the bus
// session validation rejecting the message first.
func (br *Bridge) HandleEgressReply(d Direction, m ReplyMsg) ReplyEvent {
	if d == LeftToRight {
		return br.lr.handleReply(m)
	}
	return br.rl.handleReply(m)
}

// Stats returns the diagnostic snapshot of one direction.
func (br *Bridge) Stats(d Direction) LegStats {
	if d == LeftToRight {
		return br.lr.stats()
	}
	return br.rl.stats()
}

// OnForward observes every successful forwarded request on both directions.
func (br *Bridge) OnForward(fn func(ForwardEvent)) {
	br.lr.onForward = fn
	br.rl.onForward = fn
}

// OnForwardLeg observes forwarded requests on a single direction.
func (br *Bridge) OnForwardLeg(d Direction, fn func(ForwardEvent)) {
	if d == LeftToRight {
		br.lr.onForward = fn
	} else {
		br.rl.onForward = fn
	}
}

// OnBridgedReply observes every reply hop (including unmappable/gap ones).
func (br *Bridge) OnBridgedReply(fn func(ReplyEvent)) {
	br.lr.onReply = fn
	br.rl.onReply = fn
}

// OnBridgedReplyLeg observes reply hops on a single direction.
func (br *Bridge) OnBridgedReplyLeg(d Direction, fn func(ReplyEvent)) {
	if d == LeftToRight {
		br.lr.onReply = fn
	} else {
		br.rl.onReply = fn
	}
}

// Stop closes every endpoint owned by the bridge.
func (br *Bridge) Stop() {
	br.leftIn.Close()
	br.leftOut.Close()
	br.rightIn.Close()
	br.rightOut.Close()
}

func (l *bridgeLeg) stats() LegStats {
	l.mu.Lock()
	defer l.mu.Unlock()
	s := LegStats{
		ActiveParents:    len(l.parents),
		BranchesSeen:     l.seen,
		FinishedBranches: l.doneB,
		FinishedParents:  l.doneP,
	}
	if es := l.egressSession(); es != nil {
		es.mu.Lock()
		s.EgressEpoch = es.epoch
		s.NextEgressCorr = es.nextCorr
		es.mu.Unlock()
	}
	return s
}
