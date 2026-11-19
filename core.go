package encupload

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"sync"
)

// Policy governs how a bucket may compose a published object.
//
// RequiredKeyGeneration pins the only key generation whose segments may be
// published into the bucket; a zero value means "no pin". MinSegments and
// MaxSegments bound the number of segments in one object, and MaxObjectBytes
// bounds the plaintext size; zero means "no bound".
//
// Policy is an immutable value. Installing a policy copies the struct, so a
// caller cannot mutate an installed policy afterwards.
type Policy struct {
	RequiredKeyGeneration int64
	MinSegments           int
	MaxSegments           int
	MaxObjectBytes        int64
}

func (p Policy) validate() error {
	if p.RequiredKeyGeneration < 0 {
		return fmt.Errorf("%w: negative required key generation %d", ErrInvalidArgument, p.RequiredKeyGeneration)
	}
	if p.MinSegments < 0 || p.MaxSegments < 0 {
		return fmt.Errorf("%w: negative segment bound", ErrInvalidArgument)
	}
	if p.MaxSegments > 0 && p.MinSegments > p.MaxSegments {
		return fmt.Errorf("%w: min segments %d exceeds max segments %d", ErrInvalidArgument, p.MinSegments, p.MaxSegments)
	}
	if p.MaxObjectBytes < 0 {
		return fmt.Errorf("%w: negative object byte bound", ErrInvalidArgument)
	}
	return nil
}

// deny reports whether the policy rejects a completed selection.
func (p Policy) deny(segments int, plaintextBytes int64, keyGeneration int64) error {
	if p.RequiredKeyGeneration != 0 && p.RequiredKeyGeneration != keyGeneration {
		return fmt.Errorf("%w: policy requires key generation %d, selection uses %d",
			ErrPolicyRejected, p.RequiredKeyGeneration, keyGeneration)
	}
	if p.MinSegments > 0 && segments < p.MinSegments {
		return fmt.Errorf("%w: policy requires at least %d segments, selection has %d",
			ErrPolicyRejected, p.MinSegments, segments)
	}
	if p.MaxSegments > 0 && segments > p.MaxSegments {
		return fmt.Errorf("%w: policy allows at most %d segments, selection has %d",
			ErrPolicyRejected, p.MaxSegments, segments)
	}
	if p.MaxObjectBytes > 0 && plaintextBytes > p.MaxObjectBytes {
		return fmt.Errorf("%w: policy allows at most %d bytes, selection has %d",
			ErrPolicyRejected, p.MaxObjectBytes, plaintextBytes)
	}
	return nil
}

// policyVersion is an installed policy. version is the logical time at which
// it takes effect; seq is the commit order of the installation, which breaks
// ties when a policy and a completion share a logical time.
type policyVersion struct {
	version int64
	seq     int64
	policy  Policy
}

// Receipt is the acknowledgement the backend returned for one upload. It
// records the session, segment number, the version that upload created, the
// plaintext digest, and the key generation the backend actually used.
type Receipt struct {
	SessionID     string `json:"session_id"`
	Number        int    `json:"number"`
	Version       int    `json:"version"`
	Digest        string `json:"digest"`
	KeyGeneration int64  `json:"key_generation"`
}

// Equal compares every field.
func (r Receipt) Equal(o Receipt) bool {
	return r.SessionID == o.SessionID &&
		r.Number == o.Number &&
		r.Version == o.Version &&
		r.Digest == o.Digest &&
		r.KeyGeneration == o.KeyGeneration
}

// Completion is the request to publish an object from an explicit list of
// segment versions. Each element is a receipt the backend actually issued, so
// the selection is pinned to a version rather than to "whatever segment N is
// now"; a later re-upload of the same segment number cannot replace a version
// listed here. Every receipt must come from the same upload session.
type Completion struct {
	Selections []Receipt
}

// Object is the published view of a completed upload. It is assembled only
// after every check passes, so readers never observe a partial object.
//
// PolicyVersion is the logical time at which the governing policy took effect
// (0 for the implicit default policy), and PolicySeq is the submission
// sequence of that policy installation (0 for the default). Together they
// identify exactly which policy the object was composed against.
type Object struct {
	Bucket        string
	SessionID     string
	KeyGeneration int64
	Segments      int
	Size          int64
	PolicyVersion int64
	PolicySeq     int64
	Digest        string
	Data          []byte
}

func (o *Object) clone() *Object {
	if o == nil {
		return nil
	}
	cp := *o
	cp.Data = append([]byte(nil), o.Data...)
	return &cp
}

type segmentVersion struct {
	version    int
	plaintext  []byte // kept for size accounting and digest cross-checks
	ciphertext []byte // authoritative stored bytes; decryption reads these
	digest     string
	keyGen     int64
}

type bucket struct {
	name     string
	versions []policyVersion // installed policies, ascending effective time
	object   *Object
}

type session struct {
	id             string
	bucket         string
	segments       map[int][]*segmentVersion
	published      *Object
	failedAttempts int
	lastErr        error
}

// Ticket identifies an operation submitted to the controller. Tickets are
// assigned in submission order and are stable for the lifetime of the
// controller.
type Ticket int64

// OpResult is the outcome of a scheduled operation. Exactly one of Object and
// Err is meaningful: a successful completion yields an Object, and every
// rejected completion yields an Err.
type OpResult struct {
	Ticket        Ticket
	At            int64
	Done          bool
	Object        *Object // completions only, nil on failure
	Err           error   // nil on success
	PolicyVersion int64   // policies: logical time the policy took effect
	PolicySeq     int64   // policies: submission sequence of the installation
}

// Controller is the encrypted upload controller. It is safe for concurrent
// use. Every mutation — key installation, upload, policy installation and
// completion — is linearized by one mutex. Scheduled operations are executed
// in (logical time, submission order), which is what makes "a policy change
// and a completion at the same instant" produce exactly one outcome that
// matches the commit order and never a torn mix.
type Controller struct {
	mu sync.Mutex

	clock int64 // logical time
	next  int64 // next submission sequence

	buckets  map[string]*bucket
	sessions map[string]*session
	pending  []*operation // scheduled operations, not yet executed
	results  map[Ticket]OpResult

	keys        map[int64][]byte
	authorized  map[int64]bool
	keyVersions []int64
}

type opKind int

const (
	opPolicy opKind = iota
	opComplete
)

type operation struct {
	ticket Ticket
	seq    int64
	at     int64
	kind   opKind

	bucket string
	policy Policy
	comp   Completion
}

// NewController returns an empty controller with no buckets or keys.
func NewController() *Controller {
	return &Controller{
		buckets:    make(map[string]*bucket),
		sessions:   make(map[string]*session),
		results:    make(map[Ticket]OpResult),
		keys:       make(map[int64][]byte),
		authorized: make(map[int64]bool),
	}
}

// NewKeyGeneration installs a fresh key generation. Generations must strictly
// increase. The new generation starts authorized and becomes the generation
// used by subsequent uploads.
func (c *Controller) NewKeyGeneration(gen int64) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if gen <= 0 {
		return fmt.Errorf("%w: key generation must be positive, got %d", ErrInvalidArgument, gen)
	}
	if len(c.keyVersions) > 0 && gen <= c.keyVersions[len(c.keyVersions)-1] {
		return fmt.Errorf("%w: key generation %d is not newer than %d",
			ErrInvalidArgument, gen, c.keyVersions[len(c.keyVersions)-1])
	}
	sum := keyMaterial(gen)
	c.keys[gen] = sum
	c.authorized[gen] = true
	c.keyVersions = append(c.keyVersions, gen)
	return nil
}

// RevokeKeyGeneration withdraws a generation from future use. Bytes already
// encrypted with it remain decryptable, but composition with it is rejected.
// Revoking an unknown generation is an error; revoking twice is idempotent.
func (c *Controller) RevokeKeyGeneration(gen int64) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.keys[gen]; !ok {
		return fmt.Errorf("%w: key generation %d", ErrKeyGenerationUnknown, gen)
	}
	c.authorized[gen] = false
	return nil
}

// AuthorizedKeyGenerations lists the generations currently authorized,
// ascending.
func (c *Controller) AuthorizedKeyGenerations() []int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]int64, 0, len(c.keyVersions))
	for _, g := range c.keyVersions {
		if c.authorized[g] {
			out = append(out, g)
		}
	}
	return out
}

func (c *Controller) currentAuthorizedGenLocked() (int64, bool) {
	for i := len(c.keyVersions) - 1; i >= 0; i-- {
		g := c.keyVersions[i]
		if c.authorized[g] {
			return g, true
		}
	}
	return 0, false
}

// CreateBucket creates a bucket with the default (unrestricted) policy.
func (c *Controller) CreateBucket(name string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if name == "" {
		return fmt.Errorf("%w: empty bucket name", ErrInvalidArgument)
	}
	if _, ok := c.buckets[name]; ok {
		return fmt.Errorf("%w: %q", ErrBucketExists, name)
	}
	c.buckets[name] = &bucket{name: name}
	return nil
}

func (c *Controller) bucketLocked(name string) (*bucket, error) {
	b, ok := c.buckets[name]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrBucketNotFound, name)
	}
	return b, nil
}

// Now reports the current logical time.
func (c *Controller) Now() int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.clock
}

// Tick advances the logical clock by one and executes every operation now due,
// returning the new time.
func (c *Controller) Tick() int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.advanceLocked(c.clock + 1)
	return c.clock
}

// RunUntil advances the clock to at (which must not be in the past) and
// executes every scheduled operation whose logical time is at or before at,
// in (time, submission order). It returns the results of the operations that
// executed, in execution order.
func (c *Controller) RunUntil(at int64) ([]OpResult, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if at < c.clock {
		return nil, fmt.Errorf("%w: run time %d precedes clock %d", ErrInvalidArgument, at, c.clock)
	}
	return c.advanceLocked(at), nil
}

func (c *Controller) advanceLocked(at int64) []OpResult {
	var out []OpResult
	for {
		// Find the earliest due operation, breaking ties by submission order.
		var idx = -1
		for i, op := range c.pending {
			if op.at > at {
				continue
			}
			if idx == -1 || op.at < c.pending[idx].at ||
				(op.at == c.pending[idx].at && op.seq < c.pending[idx].seq) {
				idx = i
			}
		}
		if idx == -1 {
			break
		}
		op := c.pending[idx]
		c.pending = append(c.pending[:idx], c.pending[idx+1:]...)
		if op.at > c.clock {
			c.clock = op.at
		}
		res := c.execLocked(op)
		c.results[op.ticket] = res
		out = append(out, res)
	}
	if at > c.clock {
		c.clock = at
	}
	return out
}

// TicketResult reports the outcome of a previously submitted operation.
func (c *Controller) TicketResult(t Ticket) (OpResult, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, op := range c.pending {
		if op.ticket == t {
			return OpResult{Ticket: t, At: op.at}, false
		}
	}
	if r, ok := c.results[t]; ok {
		return r, true
	}
	return OpResult{}, false
}

// submitLocked stamps an operation with the next submission sequence and
// either queues it or executes it immediately when its time has arrived.
// Sequences are the tie-breaker for operations sharing a logical time, so they
// must be allocated at submission, not at execution.
func (c *Controller) submitLocked(op *operation) Ticket {
	c.next++
	op.seq = c.next
	op.ticket = Ticket(c.next)
	if op.at <= c.clock {
		c.results[op.ticket] = c.execLocked(op)
		return op.ticket
	}
	c.pending = append(c.pending, op)
	return op.ticket
}

func (c *Controller) execLocked(op *operation) OpResult {
	res := OpResult{Ticket: op.ticket, At: op.at, Done: true}
	switch op.kind {
	case opPolicy:
		b, err := c.bucketLocked(op.bucket)
		if err != nil {
			res.Err = err
			return res
		}
		b.versions = append(b.versions, policyVersion{version: op.at, seq: op.seq, policy: op.policy})
		res.PolicyVersion = op.at
		res.PolicySeq = op.seq
	case opComplete:
		obj, err := c.completeLocked(op.comp, op.at, op.seq)
		if err != nil {
			res.Err = err
			return res
		}
		res.Object = obj
	}
	return res
}

// SetPolicy installs p immediately, at the current logical time. The read of
// the clock and the installation happen under one lock, so a policy can never
// be stamped with a time that has already been observed by some other
// operation.
func (c *Controller) SetPolicy(bucketName string, p *Policy) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if p == nil {
		return fmt.Errorf("%w: nil policy", ErrInvalidArgument)
	}
	if err := c.precheckPolicyLocked(bucketName, *p, c.clock); err != nil {
		return err
	}
	c.submitLocked(&operation{at: c.clock, kind: opPolicy, bucket: bucketName, policy: *p})
	return nil
}

// SchedulePolicy installs p so that it takes effect at logical time at. A
// policy may only be scheduled for the current time or later: back-dating a
// policy would retroactively change which rules an already-committed object
// was captured under.
//
// A policy and a completion scheduled for the same logical time resolve in
// submission order; a policy submitted before the completion wins the tie and
// can reject it.
func (c *Controller) SchedulePolicy(bucketName string, p *Policy, at int64) (Ticket, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if p == nil {
		return 0, fmt.Errorf("%w: nil policy", ErrInvalidArgument)
	}
	if err := c.precheckPolicyLocked(bucketName, *p, at); err != nil {
		return 0, err
	}
	if at < c.clock {
		return 0, fmt.Errorf("%w: policy time %d precedes clock %d", ErrInvalidArgument, at, c.clock)
	}
	t := c.submitLocked(&operation{at: at, kind: opPolicy, bucket: bucketName, policy: *p})
	if r, ok := c.results[t]; ok && r.Err != nil {
		return 0, r.Err
	}
	return t, nil
}

func (c *Controller) precheckPolicyLocked(bucketName string, p Policy, at int64) error {
	if _, err := c.bucketLocked(bucketName); err != nil {
		return err
	}
	if err := p.validate(); err != nil {
		return err
	}
	if at < 0 {
		return fmt.Errorf("%w: negative policy time %d", ErrInvalidArgument, at)
	}
	return nil
}

// CreateSession opens an upload session targeting bucket.
func (c *Controller) CreateSession(bucketName string) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, err := c.bucketLocked(bucketName); err != nil {
		return "", err
	}
	id := fmt.Sprintf("sess-%d", len(c.sessions)+1)
	for c.sessions[id] != nil {
		id += "x"
	}
	c.sessions[id] = &session{
		id:       id,
		bucket:   bucketName,
		segments: make(map[int][]*segmentVersion),
	}
	return id, nil
}

func (c *Controller) sessionLocked(id string) (*session, error) {
	ssn, ok := c.sessions[id]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrSessionNotFound, id)
	}
	return ssn, nil
}

// UploadSegment appends data as a new version of segment number and returns
// the receipt describing that version, including the key generation the
// backend actually used. Existing versions of the same segment number are
// never overwritten.
func (c *Controller) UploadSegment(sessionID string, number int, data []byte) (Receipt, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	ssn, err := c.sessionLocked(sessionID)
	if err != nil {
		return Receipt{}, err
	}
	v, err := c.uploadLocked(ssn, number, data)
	if err != nil {
		return Receipt{}, err
	}
	return receiptFor(ssn.id, number, v), nil
}

// UploadBatch uploads several segments atomically: either every segment gets a
// new version or none does. A partially applied batch would leave a reader
// able to compose bytes the client never acknowledged, so the batch is
// validated before any version is appended.
func (c *Controller) UploadBatch(sessionID string, segments map[int][]byte) ([]Receipt, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	ssn, err := c.sessionLocked(sessionID)
	if err != nil {
		return nil, err
	}
	if len(segments) == 0 {
		return nil, fmt.Errorf("%w: empty batch", ErrInvalidArgument)
	}
	order := make([]int, 0, len(segments))
	for n := range segments {
		if n < 0 {
			return nil, fmt.Errorf("%w: negative segment number %d", ErrInvalidArgument, n)
		}
		order = append(order, n)
	}
	if err := c.uploadPrecheckLocked(ssn); err != nil {
		return nil, err
	}
	sort.Ints(order)
	receipts := make([]Receipt, 0, len(order))
	for _, n := range order {
		v, err := c.uploadLocked(ssn, n, segments[n])
		if err != nil {
			return nil, err
		}
		receipts = append(receipts, receiptFor(ssn.id, n, v))
	}
	return receipts, nil
}

func (c *Controller) uploadPrecheckLocked(ssn *session) error {
	if ssn.published != nil {
		return fmt.Errorf("%w: %s", ErrSessionCompleted, ssn.id)
	}
	if _, ok := c.currentAuthorizedGenLocked(); !ok {
		return ErrNoAuthorizedKey
	}
	return nil
}

func (c *Controller) uploadLocked(ssn *session, number int, data []byte) (*segmentVersion, error) {
	if number < 0 {
		return nil, fmt.Errorf("%w: negative segment number %d", ErrInvalidArgument, number)
	}
	if err := c.uploadPrecheckLocked(ssn); err != nil {
		return nil, err
	}
	gen, _ := c.currentAuthorizedGenLocked()
	pt := append([]byte(nil), data...)
	d := sha256.Sum256(pt)
	v := &segmentVersion{
		version:    len(ssn.segments[number]) + 1,
		plaintext:  pt,
		ciphertext: sealSegment(c.keys[gen], number, gen, pt),
		digest:     hex.EncodeToString(d[:]),
		keyGen:     gen,
	}
	ssn.segments[number] = append(ssn.segments[number], v)
	return v, nil
}

func receiptFor(sessionID string, number int, v *segmentVersion) Receipt {
	return Receipt{
		SessionID:     sessionID,
		Number:        number,
		Version:       v.version,
		Digest:        v.digest,
		KeyGeneration: v.keyGen,
	}
}

// SegmentVersions lists the version numbers stored for a segment number.
func (c *Controller) SegmentVersions(sessionID string, number int) ([]int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	ssn, err := c.sessionLocked(sessionID)
	if err != nil {
		return nil, err
	}
	vs := ssn.segments[number]
	out := make([]int, len(vs))
	for i, v := range vs {
		out[i] = v.version
	}
	return out, nil
}

// SessionStatus is a read-only view of an upload session.
type SessionStatus struct {
	ID             string
	Bucket         string
	SegmentNumbers []int
	Published      bool
	FailedAttempts int
	LastError      string
}

// Session reports the current state of an upload session.
func (c *Controller) Session(id string) (SessionStatus, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	ssn, err := c.sessionLocked(id)
	if err != nil {
		return SessionStatus{}, err
	}
	nums := make([]int, 0, len(ssn.segments))
	for n := range ssn.segments {
		nums = append(nums, n)
	}
	sort.Ints(nums)
	st := SessionStatus{
		ID:             ssn.id,
		Bucket:         ssn.bucket,
		SegmentNumbers: nums,
		Published:      ssn.published != nil,
		FailedAttempts: ssn.failedAttempts,
	}
	if ssn.lastErr != nil {
		st.LastError = ssn.lastErr.Error()
	}
	return st, nil
}

// SealSegment encrypts plaintext with key generation gen, so fixtures can
// build a segment exactly the way the controller does.
func SealSegment(keyGen int64, number int, plaintext []byte) []byte {
	return sealSegment(keyMaterial(keyGen), number, keyGen, plaintext)
}

// OpenSegment decrypts a segment sealed by SealSegment and verifies its tag.
func OpenSegment(keyGen int64, number int, ciphertext []byte) ([]byte, error) {
	return openSegment(keyMaterial(keyGen), number, keyGen, ciphertext)
}

// KeyFingerprint returns a non-secret, stable identifier for a key
// generation, so tests can observe which generation encrypted a payload
// without ever handling key bytes.
func KeyFingerprint(gen int64) string {
	return hex.EncodeToString(keyMaterial(gen))
}

func keyMaterial(gen int64) []byte {
	sum := sha256.Sum256([]byte(fmt.Sprintf("encupload-key-gen-%d", gen)))
	return sum[:]
}

// objectRecord is the canonical, data-free form of a published object used to
// derive its overall digest. Two objects with the same layout but different
// contents or keys hash differently.
type objectRecord struct {
	Bucket        string
	Session       string
	KeyGeneration int64
	Segments      int
	Size          int64
	PolicyVersion int64
	SegmentHashes []string
}

func objectDigest(rec objectRecord) string {
	raw, _ := json.Marshal(rec)
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
