package delivery

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"sync"
)

// Sentinel errors. Use errors.Is to test them.
var (
	ErrInvalidOptions   = errors.New("delivery: invalid options")
	ErrInvalidMessage   = errors.New("delivery: invalid message")
	ErrInvalidRequest   = errors.New("delivery: invalid request id")
	ErrInvalidExtension = errors.New("delivery: renewal must extend the lease")
	ErrDuplicateMessage = errors.New("delivery: duplicate message id")
	ErrMessageExpired   = errors.New("delivery: message has expired")
	ErrMessageDead      = errors.New("delivery: message exhausted its attempt limit")
	ErrMessageAcked     = errors.New("delivery: message has already been acknowledged")
	ErrNoMessage        = errors.New("delivery: no message available")
	ErrUnknownReceipt   = errors.New("delivery: unknown receipt")
	ErrStaleReceipt     = errors.New("delivery: receipt was superseded by a newer attempt")
	ErrLeaseExpired     = errors.New("delivery: lease has expired")
	ErrRequestConflict  = errors.New("delivery: request id was already used for a different operation")
	ErrCorruptJournal   = errors.New("delivery: corrupt journal")
)

// State is the externally visible state of a message.
type State string

const (
	StatePending State = "pending" // scheduled, delayed or waiting out backoff
	StateLeased  State = "leased"  // an attempt currently owns a lease
	StateAcked   State = "acked"   // terminal: acknowledged successfully
	StateExpired State = "expired" // terminal: survival deadline reached first
	StateDead    State = "dead"    // terminal: attempt limit exhausted
)

func (s State) terminal() bool {
	return s == StateAcked || s == StateExpired || s == StateDead
}

// Options controls lease and retry timing. All values are logical
// milliseconds and must be positive.
type Options struct {
	LeaseDuration int64 `json:"lease_duration"`
	BackoffBase   int64 `json:"backoff_base"`
	BackoffMax    int64 `json:"backoff_max"`
}

// DefaultOptions returns a practical starting point for tests and examples.
func DefaultOptions() Options {
	return Options{LeaseDuration: 100, BackoffBase: 10, BackoffMax: 1000}
}

func (o Options) validate() error {
	if o.LeaseDuration <= 0 {
		return fmt.Errorf("%w: lease duration must be positive", ErrInvalidOptions)
	}
	if o.BackoffBase <= 0 || o.BackoffMax < o.BackoffBase {
		return fmt.Errorf("%w: backoff must satisfy 0 < base <= max", ErrInvalidOptions)
	}
	return nil
}

// backoff returns the delay applied after the given attempt lapses.
func (o Options) backoff(attempt int) int64 {
	if attempt < 1 {
		attempt = 1
	}
	d := o.BackoffBase
	for i := 1; i < attempt; i++ {
		if d > o.BackoffMax/2 {
			return o.BackoffMax
		}
		d *= 2
	}
	if d > o.BackoffMax {
		return o.BackoffMax
	}
	return d
}

// MessageSpec is the immutable description supplied when enqueueing.
type MessageSpec struct {
	ID          string `json:"id"`
	Queue       string `json:"queue"`
	VisibleAt   int64  `json:"visible_at"`
	ExpiresAt   int64  `json:"expires_at"`
	MaxAttempts int    `json:"max_attempts"`
	Body        string `json:"body,omitempty"`
}

func (m MessageSpec) validate() error {
	if m.ID == "" {
		return fmt.Errorf("%w: empty id", ErrInvalidMessage)
	}
	if m.Queue == "" {
		return fmt.Errorf("%w: empty queue", ErrInvalidMessage)
	}
	if m.MaxAttempts < 1 {
		return fmt.Errorf("%w: max attempts must be at least 1", ErrInvalidMessage)
	}
	if m.VisibleAt >= m.ExpiresAt {
		return fmt.Errorf("%w: visibility time must precede the deadline", ErrInvalidMessage)
	}
	return nil
}

// Receipt proves that one specific attempt owns a lease.
type Receipt struct {
	ReceiptID  string `json:"receipt_id"`
	MessageID  string `json:"message_id"`
	Queue      string `json:"queue"`
	Attempt    int    `json:"attempt"`
	IssuedAt   int64  `json:"issued_at"`
	LeaseUntil int64  `json:"lease_until"`
}

func (r Receipt) clone() Receipt { return r }

// AckResult is the durable outcome of a successful acknowledgement.
type AckResult struct {
	MessageID string `json:"message_id"`
	ReceiptID string `json:"receipt_id"`
	Attempt   int    `json:"attempt"`
	AckedAt   int64  `json:"acked_at"`
}

// ReceiptStatus is the state of a receipt as observed at a given time.
type ReceiptStatus string

const (
	ReceiptActive  ReceiptStatus = "active"
	ReceiptAcked   ReceiptStatus = "acked"
	ReceiptExpired ReceiptStatus = "expired"
)

// ReceiptView is a receipt together with its effective status at Now.
type ReceiptView struct {
	Receipt
	Status     ReceiptStatus `json:"status"`
	Expired    bool          `json:"expired"`
	Superseded bool          `json:"superseded"`
	Now        int64         `json:"now"`
}

// MessageView is the effective state of a message at Now.
type MessageView struct {
	ID                string `json:"id"`
	Queue             string `json:"queue"`
	Body              string `json:"body,omitempty"`
	State             State  `json:"state"`
	Attempts          int    `json:"attempts"`           // claims issued
	CompletedAttempts int    `json:"completed_attempts"` // attempts that reached a conclusion
	AckedAttempt      int    `json:"acked_attempt,omitempty"`
	EnqueueOrder      uint64 `json:"enqueue_order"`
	VisibleAt         int64  `json:"visible_at"`
	LeaseUntil        int64  `json:"lease_until,omitempty"`
	ExpiresAt         int64  `json:"expires_at"`
	MaxAttempts       int    `json:"max_attempts"`
	CurrentReceiptID  string `json:"current_receipt_id,omitempty"`
}

type message struct {
	spec              MessageSpec
	order             uint64
	state             State
	attempts          int
	completedAttempts int
	ackedAttempt      int
	ackedAt           int64
	ackReceiptID      string
	visibleAt         int64
	leaseUntil        int64
	currentReceiptID  string
}

type receiptRecord struct {
	Receipt
	requestID string
}

type claimEntry struct {
	queue   string
	receipt Receipt
}

// Engine is a single-process delivery engine. It is safe for concurrent use.
type Engine struct {
	mu sync.Mutex

	opts      Options
	messages  map[string]*message
	order     uint64
	receipts  map[string]*receiptRecord
	receiptNo uint64
	claims    map[string]claimEntry // claim request id -> result
	acks      map[string]AckResult  // ack request id -> result
	journal   []Record
}

func newRawEngine(opts Options) *Engine {
	return &Engine{
		opts:     opts,
		messages: make(map[string]*message),
		receipts: make(map[string]*receiptRecord),
		claims:   make(map[string]claimEntry),
		acks:     make(map[string]AckResult),
	}
}

// NewEngine creates an empty engine and starts its replayable journal with a
// configuration record.
func NewEngine(opts Options) (*Engine, error) {
	if err := opts.validate(); err != nil {
		return nil, err
	}
	e := newRawEngine(opts)
	cfg := opts
	e.append(Record{Kind: RecordConfig, At: 0, Options: &cfg})
	return e, nil
}

// Enqueue adds a message. A message whose deadline has already passed at now
// is rejected rather than silently stored.
func (e *Engine) Enqueue(spec MessageSpec, now int64) error {
	if err := spec.validate(); err != nil {
		return err
	}
	e.mu.Lock()
	defer e.mu.Unlock()

	if _, exists := e.messages[spec.ID]; exists {
		return fmt.Errorf("%w: %s", ErrDuplicateMessage, spec.ID)
	}
	if now >= spec.ExpiresAt {
		return fmt.Errorf("%w: enqueued after its deadline", ErrMessageExpired)
	}

	e.order++
	m := &message{
		spec:      spec,
		order:     e.order,
		state:     StatePending,
		visibleAt: spec.VisibleAt,
	}
	e.messages[spec.ID] = m
	s := spec
	e.append(Record{
		Kind:      RecordEnqueue,
		At:        now,
		Queue:     spec.Queue,
		MessageID: spec.ID,
		Spec:      &s,
	})
	return nil
}

// Claim leases the next eligible message of queue, or returns the original
// receipt when requestID was already used. Eligibility is ordered by the
// message's current visible time and then by first-enqueue order.
func (e *Engine) Claim(queue, requestID string, now int64) (Receipt, error) {
	if queue == "" {
		return Receipt{}, fmt.Errorf("%w: empty queue", ErrInvalidRequest)
	}
	if requestID == "" {
		return Receipt{}, fmt.Errorf("%w: claim requires a request id", ErrInvalidRequest)
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	if entry, ok := e.claims[requestID]; ok {
		if entry.queue != queue {
			return Receipt{}, fmt.Errorf("%w: request %q belongs to queue %q", ErrRequestConflict, requestID, entry.queue)
		}
		return entry.receipt.clone(), nil
	}

	e.sweepLocked(now)

	var best *message
	for _, m := range e.messages {
		if m.spec.Queue != queue || m.state != StatePending {
			continue
		}
		if now < m.visibleAt || now >= m.spec.ExpiresAt || m.attempts >= m.spec.MaxAttempts {
			continue
		}
		if best == nil || m.visibleAt < best.visibleAt ||
			(m.visibleAt == best.visibleAt && m.order < best.order) {
			best = m
		}
	}
	if best == nil {
		return Receipt{}, ErrNoMessage
	}

	rid := e.nextReceiptIDLocked()
	leaseUntil := now + e.opts.LeaseDuration
	if leaseUntil < now || leaseUntil > best.spec.ExpiresAt {
		leaseUntil = best.spec.ExpiresAt
	}
	best.attempts++
	best.state = StateLeased
	best.leaseUntil = leaseUntil
	best.currentReceiptID = rid

	r := Receipt{
		ReceiptID:  rid,
		MessageID:  best.spec.ID,
		Queue:      best.spec.Queue,
		Attempt:    best.attempts,
		IssuedAt:   now,
		LeaseUntil: leaseUntil,
	}
	e.receipts[rid] = &receiptRecord{Receipt: r, requestID: requestID}
	e.claims[requestID] = claimEntry{queue: queue, receipt: r}
	e.append(Record{
		Kind:       RecordClaim,
		At:         now,
		Queue:      queue,
		MessageID:  best.spec.ID,
		RequestID:  requestID,
		ReceiptID:  rid,
		Attempt:    r.Attempt,
		LeaseUntil: leaseUntil,
	})
	return r.clone(), nil
}

func (e *Engine) nextReceiptIDLocked() string {
	for {
		e.receiptNo++
		id := fmt.Sprintf("rcpt-%d", e.receiptNo)
		if _, exists := e.receipts[id]; !exists {
			return id
		}
	}
}

// Renew extends the current lease by extend milliseconds. The result is capped
// at the message deadline; a renewal never shortens a lease and a lapsed lease
// cannot be revived.
func (e *Engine) Renew(receiptID string, extend, now int64) (Receipt, error) {
	if extend <= 0 {
		return Receipt{}, fmt.Errorf("%w: extension must be positive", ErrInvalidExtension)
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	rr, ok := e.receipts[receiptID]
	if !ok {
		return Receipt{}, fmt.Errorf("%w: %s", ErrUnknownReceipt, receiptID)
	}
	e.sweepLocked(now)
	m := e.messages[rr.MessageID]

	switch m.state {
	case StateExpired:
		return Receipt{}, ErrMessageExpired
	case StateDead:
		return Receipt{}, ErrMessageDead
	case StateAcked:
		if m.ackReceiptID == receiptID {
			return Receipt{}, ErrMessageAcked
		}
		return Receipt{}, fmt.Errorf("%w: attempt %d is not current", ErrStaleReceipt, rr.Attempt)
	}

	if rr.Attempt != m.attempts {
		return Receipt{}, fmt.Errorf("%w: attempt %d, current %d", ErrStaleReceipt, rr.Attempt, m.attempts)
	}
	if now >= rr.LeaseUntil {
		return Receipt{}, ErrLeaseExpired
	}
	if m.state != StateLeased || m.currentReceiptID != receiptID {
		return Receipt{}, fmt.Errorf("%w: receipt is no longer active", ErrStaleReceipt)
	}

	lease := rr.LeaseUntil
	if extend >= m.spec.ExpiresAt-lease {
		lease = m.spec.ExpiresAt
	} else {
		lease += extend
	}
	if lease == rr.LeaseUntil {
		return rr.Receipt.clone(), nil
	}

	rr.LeaseUntil = lease
	m.leaseUntil = lease
	e.append(Record{
		Kind:       RecordRenew,
		At:         now,
		Queue:      m.spec.Queue,
		MessageID:  m.spec.ID,
		ReceiptID:  receiptID,
		Attempt:    rr.Attempt,
		LeaseUntil: lease,
	})
	return rr.Receipt.clone(), nil
}

// Ack acknowledges a receipt. Acknowledgements are fenced by attempt number:
// once a newer attempt exists, an old receipt can no longer acknowledge it.
// Retrying Ack with the same requestID is idempotent, including after replay.
func (e *Engine) Ack(receiptID, requestID string, now int64) (AckResult, error) {
	if requestID == "" {
		return AckResult{}, fmt.Errorf("%w: ack requires a request id", ErrInvalidRequest)
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	if prev, ok := e.acks[requestID]; ok {
		if prev.ReceiptID != receiptID {
			return AckResult{}, fmt.Errorf("%w: request %q acknowledged %q", ErrRequestConflict, requestID, prev.ReceiptID)
		}
		return prev, nil
	}

	rr, ok := e.receipts[receiptID]
	if !ok {
		return AckResult{}, fmt.Errorf("%w: %s", ErrUnknownReceipt, receiptID)
	}
	e.sweepLocked(now)
	m := e.messages[rr.MessageID]

	switch m.state {
	case StateExpired:
		return AckResult{}, ErrMessageExpired
	case StateDead:
		return AckResult{}, ErrMessageDead
	case StateAcked:
		if m.ackedAttempt == rr.Attempt && m.ackReceiptID == receiptID {
			return AckResult{
				MessageID: m.spec.ID,
				ReceiptID: receiptID,
				Attempt:   rr.Attempt,
				AckedAt:   m.ackedAt,
			}, nil
		}
		return AckResult{}, fmt.Errorf("%w: attempt %d is not the acknowledged attempt", ErrStaleReceipt, rr.Attempt)
	}

	if rr.Attempt != m.attempts {
		return AckResult{}, fmt.Errorf("%w: attempt %d, current %d", ErrStaleReceipt, rr.Attempt, m.attempts)
	}
	if now >= rr.LeaseUntil {
		return AckResult{}, ErrLeaseExpired
	}
	if m.state != StateLeased || m.currentReceiptID != receiptID {
		return AckResult{}, fmt.Errorf("%w: receipt is no longer active", ErrStaleReceipt)
	}
	if now >= m.spec.ExpiresAt {
		return AckResult{}, ErrMessageExpired
	}

	m.completedAttempts++
	m.state = StateAcked
	m.ackedAttempt = rr.Attempt
	m.ackReceiptID = receiptID
	m.ackedAt = now
	m.leaseUntil = 0
	m.currentReceiptID = ""

	res := AckResult{MessageID: m.spec.ID, ReceiptID: receiptID, Attempt: rr.Attempt, AckedAt: now}
	e.acks[requestID] = res
	e.append(Record{
		Kind:      RecordAck,
		At:        now,
		Queue:     m.spec.Queue,
		MessageID: m.spec.ID,
		RequestID: requestID,
		ReceiptID: receiptID,
		Attempt:   rr.Attempt,
	})
	return res, nil
}

// Sweep advances all messages to their effective state at now and records any
// terminal transition exactly once. Claim, Ack, Renew, Message and Receipt also
// sweep internally; calling Sweep explicitly makes terminal transitions easy to
// observe. When the survival deadline and a lease end at the same instant, the
// deadline wins.
func (e *Engine) Sweep(now int64) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.sweepLocked(now)
}

func (e *Engine) sweepLocked(now int64) {
	pending := make([]*message, 0, len(e.messages))
	for _, m := range e.messages {
		if !m.state.terminal() {
			pending = append(pending, m)
		}
	}
	sort.Slice(pending, func(i, j int) bool { return pending[i].order < pending[j].order })
	for _, m := range pending {
		if now >= m.spec.ExpiresAt {
			e.expireLocked(m, now)
			continue
		}
		if m.state == StateLeased && now >= m.leaseUntil {
			e.lapseLocked(m, now)
		}
	}
}

// expireLocked moves a message to the expired terminal state. The deadline wins
// over a lease ending at the same instant because sweep checks it first.
func (e *Engine) expireLocked(m *message, now int64) {
	if m.state == StateLeased {
		m.completedAttempts++ // the in-flight attempt ended without success
	}
	m.state = StateExpired
	m.leaseUntil = 0
	m.currentReceiptID = ""
	e.append(Record{
		Kind:      RecordExpire,
		At:        now,
		Queue:     m.spec.Queue,
		MessageID: m.spec.ID,
		Attempt:   m.attempts,
	})
}

// lapseLocked concludes the current attempt and either makes the message
// visible again after backoff or marks it dead when attempts are exhausted.
func (e *Engine) lapseLocked(m *message, now int64) {
	oldLease := m.leaseUntil
	m.completedAttempts++
	m.currentReceiptID = ""
	m.leaseUntil = 0

	if m.attempts >= m.spec.MaxAttempts {
		m.state = StateDead
		e.append(Record{
			Kind:      RecordDead,
			At:        now,
			Queue:     m.spec.Queue,
			MessageID: m.spec.ID,
			Attempt:   m.attempts,
		})
		return
	}

	m.visibleAt = oldLease + e.opts.backoff(m.attempts)
	m.state = StatePending
	e.append(Record{
		Kind:      RecordLapse,
		At:        now,
		Queue:     m.spec.Queue,
		MessageID: m.spec.ID,
		Attempt:   m.attempts,
		VisibleAt: m.visibleAt,
	})
}

// Message returns the effective view of a message at now. It advances the
// message through sweep before reporting.
func (e *Engine) Message(messageID string, now int64) (MessageView, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()

	m, ok := e.messages[messageID]
	if !ok {
		return MessageView{}, false
	}
	e.sweepLocked(now)
	return e.viewLocked(m), true
}

func (e *Engine) viewLocked(m *message) MessageView {
	return MessageView{
		ID:                m.spec.ID,
		Queue:             m.spec.Queue,
		Body:              m.spec.Body,
		State:             m.state,
		Attempts:          m.attempts,
		CompletedAttempts: m.completedAttempts,
		AckedAttempt:      m.ackedAttempt,
		EnqueueOrder:      m.order,
		VisibleAt:         m.visibleAt,
		LeaseUntil:        m.leaseUntil,
		ExpiresAt:         m.spec.ExpiresAt,
		MaxAttempts:       m.spec.MaxAttempts,
		CurrentReceiptID:  m.currentReceiptID,
	}
}

// Receipt returns a receipt and its effective status at now. Expired receipts
// remain queryable even after the message has been reclaimed or terminated.
func (e *Engine) Receipt(receiptID string, now int64) (ReceiptView, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()

	rr, ok := e.receipts[receiptID]
	if !ok {
		return ReceiptView{}, false
	}
	e.sweepLocked(now)
	m := e.messages[rr.MessageID]

	v := ReceiptView{Receipt: rr.Receipt.clone(), Now: now}
	switch {
	case m.state == StateAcked && m.ackReceiptID == receiptID:
		v.Status = ReceiptAcked
	case rr.Attempt < m.attempts || m.state == StateAcked ||
		m.state == StateExpired || m.state == StateDead || now >= rr.LeaseUntil:
		v.Status = ReceiptExpired
		v.Expired = true
		v.Superseded = rr.Attempt < m.attempts
	default:
		v.Status = ReceiptActive
	}
	return v, true
}

// LookupClaim returns the result of a previous successful claim by request id.
// It is a pure lookup and does not advance time, so it remains stable across
// restarts.
func (e *Engine) LookupClaim(requestID string) (Receipt, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	entry, ok := e.claims[requestID]
	if !ok {
		return Receipt{}, false
	}
	return entry.receipt.clone(), true
}

// LookupAck returns the recorded outcome of a previous successful
// acknowledgement by request id, without advancing time.
func (e *Engine) LookupAck(requestID string) (AckResult, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	res, ok := e.acks[requestID]
	return res, ok
}

func (e *Engine) append(rec Record) {
	rec.Seq = int64(len(e.journal) + 1)
	e.journal = append(e.journal, rec)
}

// Journal returns a deep copy of the append-only replayable record stream.
func (e *Engine) Journal() []Record {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := make([]Record, len(e.journal))
	for i, rec := range e.journal {
		out[i] = rec.clone()
	}
	return out
}

// Record is the persistent representation of one state transition.
// Newline-delimited JSON is produced by EncodeJournal.
type Record struct {
	Seq        int64        `json:"seq"`
	Kind       string       `json:"kind"`
	At         int64        `json:"at"`
	Queue      string       `json:"queue,omitempty"`
	MessageID  string       `json:"message_id,omitempty"`
	Spec       *MessageSpec `json:"spec,omitempty"`
	RequestID  string       `json:"request_id,omitempty"`
	ReceiptID  string       `json:"receipt_id,omitempty"`
	Attempt    int          `json:"attempt,omitempty"`
	LeaseUntil int64        `json:"lease_until,omitempty"`
	VisibleAt  int64        `json:"visible_at,omitempty"`
	Options    *Options     `json:"options,omitempty"`
}

// Journal record kinds.
const (
	RecordConfig  = "config"
	RecordEnqueue = "enqueue"
	RecordClaim   = "claim"
	RecordRenew   = "renew"
	RecordAck     = "ack"
	RecordLapse   = "lapse"
	RecordExpire  = "expire"
	RecordDead    = "dead"
)

func (r Record) clone() Record {
	if r.Spec != nil {
		s := *r.Spec
		r.Spec = &s
	}
	if r.Options != nil {
		o := *r.Options
		r.Options = &o
	}
	return r
}

// EncodeJournal serialises records as newline-delimited JSON.
func EncodeJournal(records []Record) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	for i, rec := range records {
		if err := enc.Encode(rec); err != nil {
			return nil, fmt.Errorf("%w: encode record %d: %v", ErrCorruptJournal, i, err)
		}
	}
	return buf.Bytes(), nil
}

// DecodeJournal parses newline-delimited JSON records.
func DecodeJournal(data []byte) ([]Record, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	var out []Record
	for {
		var rec Record
		err := dec.Decode(&rec)
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("%w: %v", ErrCorruptJournal, err)
		}
		out = append(out, rec)
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("%w: empty journal", ErrCorruptJournal)
	}
	return out, nil
}

// NewEngineFromJournal rebuilds an engine from a complete record stream. The
// first record must be the configuration record emitted by NewEngine.
func NewEngineFromJournal(records []Record) (*Engine, error) {
	if len(records) == 0 {
		return nil, fmt.Errorf("%w: empty journal", ErrCorruptJournal)
	}
	first := records[0]
	if first.Kind != RecordConfig || first.Options == nil {
		return nil, fmt.Errorf("%w: journal must start with a config record", ErrCorruptJournal)
	}
	if err := first.Options.validate(); err != nil {
		return nil, fmt.Errorf("%w: bad config: %v", ErrCorruptJournal, err)
	}
	e := newRawEngine(*first.Options)
	e.journal = make([]Record, 0, len(records))

	for i, rec := range records {
		if rec.Seq != int64(i+1) {
			return nil, fmt.Errorf("%w: record %d has seq %d", ErrCorruptJournal, i, rec.Seq)
		}
		if err := e.applyLocked(rec); err != nil {
			return nil, fmt.Errorf("%w: record %d (%s): %v", ErrCorruptJournal, i+1, rec.Kind, err)
		}
		e.journal = append(e.journal, rec.clone())
	}
	return e, nil
}

func (e *Engine) applyLocked(rec Record) error {
	switch rec.Kind {
	case RecordConfig:
		if len(e.journal) != 0 {
			return errors.New("config record must be first")
		}
		return nil
	case RecordEnqueue:
		if rec.Spec == nil {
			return errors.New("missing message spec")
		}
		if err := rec.Spec.validate(); err != nil {
			return err
		}
		if _, exists := e.messages[rec.Spec.ID]; exists {
			return errors.New("duplicate message")
		}
		if rec.At >= rec.Spec.ExpiresAt {
			return errors.New("message enqueued after its deadline")
		}
		e.order++
		e.messages[rec.Spec.ID] = &message{
			spec:      *rec.Spec,
			order:     e.order,
			state:     StatePending,
			visibleAt: rec.Spec.VisibleAt,
		}
		return nil
	case RecordClaim:
		m, ok := e.messages[rec.MessageID]
		if !ok {
			return errors.New("claim for unknown message")
		}
		if m.state != StatePending || rec.Attempt != m.attempts+1 {
			return fmt.Errorf("invalid claim (state %s, attempt %d)", m.state, rec.Attempt)
		}
		if _, exists := e.receipts[rec.ReceiptID]; exists {
			return errors.New("duplicate receipt")
		}
		if _, exists := e.claims[rec.RequestID]; exists {
			return errors.New("duplicate claim request")
		}
		if rec.At >= m.spec.ExpiresAt || rec.LeaseUntil <= rec.At {
			return errors.New("invalid claim timing")
		}
		m.attempts = rec.Attempt
		m.state = StateLeased
		m.leaseUntil = rec.LeaseUntil
		m.currentReceiptID = rec.ReceiptID
		r := Receipt{
			ReceiptID:  rec.ReceiptID,
			MessageID:  m.spec.ID,
			Queue:      m.spec.Queue,
			Attempt:    rec.Attempt,
			IssuedAt:   rec.At,
			LeaseUntil: rec.LeaseUntil,
		}
		e.receipts[rec.ReceiptID] = &receiptRecord{Receipt: r, requestID: rec.RequestID}
		e.claims[rec.RequestID] = claimEntry{queue: m.spec.Queue, receipt: r}
		return nil
	case RecordRenew:
		m, err := e.leasedMessageLocked(rec)
		if err != nil {
			return err
		}
		if rec.At >= m.leaseUntil {
			return errors.New("renewal after lease expiry")
		}
		if rec.LeaseUntil <= m.leaseUntil || rec.LeaseUntil > m.spec.ExpiresAt {
			return errors.New("renewal must extend but not exceed the deadline")
		}
		m.leaseUntil = rec.LeaseUntil
		e.receipts[rec.ReceiptID].LeaseUntil = rec.LeaseUntil
		return nil
	case RecordAck:
		m, err := e.leasedMessageLocked(rec)
		if err != nil {
			return err
		}
		if rec.At >= m.spec.ExpiresAt {
			return errors.New("ack after the deadline")
		}
		if rec.At >= m.leaseUntil {
			return errors.New("ack after lease expiry")
		}
		if _, exists := e.acks[rec.RequestID]; exists {
			return errors.New("duplicate ack request")
		}
		m.completedAttempts++
		m.state = StateAcked
		m.ackedAttempt = rec.Attempt
		m.ackReceiptID = rec.ReceiptID
		m.ackedAt = rec.At
		m.leaseUntil = 0
		m.currentReceiptID = ""
		e.acks[rec.RequestID] = AckResult{
			MessageID: m.spec.ID,
			ReceiptID: rec.ReceiptID,
			Attempt:   rec.Attempt,
			AckedAt:   rec.At,
		}
		return nil
	case RecordLapse:
		m, ok := e.messages[rec.MessageID]
		if !ok {
			return errors.New("lapse for unknown message")
		}
		if m.state != StateLeased || rec.Attempt != m.attempts || rec.At < m.leaseUntil {
			return fmt.Errorf("invalid lapse (state %s, attempt %d)", m.state, rec.Attempt)
		}
		if want := m.leaseUntil + e.opts.backoff(rec.Attempt); rec.VisibleAt != want {
			return fmt.Errorf("lapse visibility %d does not match backoff rule (%d)", rec.VisibleAt, want)
		}
		m.completedAttempts++
		m.state = StatePending
		m.visibleAt = rec.VisibleAt
		m.leaseUntil = 0
		m.currentReceiptID = ""
		return nil
	case RecordDead:
		m, ok := e.messages[rec.MessageID]
		if !ok {
			return errors.New("dead for unknown message")
		}
		if m.state != StateLeased || rec.Attempt != m.attempts || rec.At < m.leaseUntil {
			return fmt.Errorf("invalid dead transition (state %s)", m.state)
		}
		if m.attempts < m.spec.MaxAttempts {
			return errors.New("dead before attempt limit")
		}
		m.completedAttempts++
		m.state = StateDead
		m.leaseUntil = 0
		m.currentReceiptID = ""
		return nil
	case RecordExpire:
		m, ok := e.messages[rec.MessageID]
		if !ok {
			return errors.New("expire for unknown message")
		}
		if m.state.terminal() {
			return fmt.Errorf("message already terminal (%s)", m.state)
		}
		if rec.At < m.spec.ExpiresAt {
			return errors.New("expire before the deadline")
		}
		if m.state == StateLeased {
			m.completedAttempts++
		}
		m.state = StateExpired
		m.leaseUntil = 0
		m.currentReceiptID = ""
		return nil
	default:
		return fmt.Errorf("unknown record kind %q", rec.Kind)
	}
}

func (e *Engine) leasedMessageLocked(rec Record) (*message, error) {
	m, ok := e.messages[rec.MessageID]
	if !ok {
		return nil, errors.New("record for unknown message")
	}
	if m.state != StateLeased || m.currentReceiptID != rec.ReceiptID || rec.Attempt != m.attempts {
		return nil, fmt.Errorf("receipt is not the current lease (state %s)", m.state)
	}
	if _, ok := e.receipts[rec.ReceiptID]; !ok {
		return nil, errors.New("unknown receipt")
	}
	return m, nil
}

// SaveJournal writes the encoded record stream to path.
func (e *Engine) SaveJournal(path string) error {
	data, err := EncodeJournal(e.Journal())
	if err != nil {
		return err
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return fmt.Errorf("delivery: save journal: %w", err)
	}
	return nil
}

// LoadJournal reads a journal file and rebuilds the engine.
func LoadJournal(path string) (*Engine, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("delivery: load journal: %w", err)
	}
	records, err := DecodeJournal(data)
	if err != nil {
		return nil, err
	}
	return NewEngineFromJournal(records)
}
