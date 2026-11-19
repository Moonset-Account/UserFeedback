package encupload

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
)

// Complete publishes an object from the explicitly listed segment versions,
// evaluated against the bucket policy in force now. The whole decision runs
// under one lock, so an immediate completion and a concurrent policy
// installation are separated by the lock, not by a read-then-act gap.
func (c *Controller) Complete(req Completion) (*Object, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(req.Selections) == 0 {
		return nil, ErrEmptySelection
	}
	c.next++
	seq := c.next
	res := c.execLocked(&operation{
		ticket: Ticket(seq),
		seq:    seq,
		at:     c.clock,
		kind:   opComplete,
		comp:   req,
	})
	if res.Err != nil {
		return nil, res.Err
	}
	return res.Object, nil
}

// ScheduleCompletion submits a completion for logical time at. It returns a
// ticket once the request has been accepted for execution. A request whose
// time has already arrived executes immediately, and its rejection is
// returned as an ordinary completion error rather than a submission error.
//
// Completions cannot be scheduled in the past: the policy that governs an
// object is the one in force when the commit happens, and back-dating a
// commit would let a caller pick a policy that has already been superseded.
func (c *Controller) ScheduleCompletion(req Completion, at int64) (Ticket, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(req.Selections) == 0 {
		return 0, ErrEmptySelection
	}
	if at < 0 {
		return 0, fmt.Errorf("%w: negative completion time %d", ErrInvalidArgument, at)
	}
	if at < c.clock {
		return 0, fmt.Errorf("%w: completion time %d precedes clock %d", ErrInvalidArgument, at, c.clock)
	}
	return c.submitLocked(&operation{at: at, kind: opComplete, comp: req}), nil
}

// completeLocked is the whole publish path. It runs under the controller lock
// and mutates visible state at only one point, the final commit. Any error
// before that point leaves readers exactly as they were, which is what makes
// a failed completion invisible to readers and retryable by writers.
func (c *Controller) completeLocked(req Completion, at, seq int64) (*Object, error) {
	obj, ssn, err := c.buildObjectLocked(req, at, seq)
	if err != nil {
		if ssn != nil {
			ssn.failedAttempts++
			ssn.lastErr = err
		}
		return nil, err
	}
	ssn.published = obj
	c.buckets[obj.Bucket].object = obj
	return obj.clone(), nil
}

func (c *Controller) buildObjectLocked(req Completion, at, seq int64) (*Object, *session, error) {
	if len(req.Selections) == 0 {
		return nil, nil, ErrEmptySelection
	}

	// Exactly one session for the whole composition.
	ssn, err := c.resolveSessionLocked(req.Selections)
	if err != nil {
		return nil, nil, err
	}
	if ssn.published != nil {
		return nil, ssn, fmt.Errorf("%w: %s", ErrSessionCompleted, ssn.id)
	}

	// Verify every listed receipt against stored state. Versions are
	// append-only, so a version listed here can never be replaced by a later
	// re-upload of the same segment number.
	chosen := make(map[int]*segmentVersion, len(req.Selections))
	numbers := make([]int, 0, len(req.Selections))
	for _, r := range req.Selections {
		if r.SessionID != ssn.id {
			return nil, ssn, fmt.Errorf("%w: %q vs %q", ErrForeignSession, r.SessionID, ssn.id)
		}
		if r.Number < 0 {
			return nil, ssn, fmt.Errorf("%w: negative segment number %d", ErrInvalidArgument, r.Number)
		}
		if _, dup := chosen[r.Number]; dup {
			return nil, ssn, fmt.Errorf("%w: segment %d", ErrDuplicateSegment, r.Number)
		}
		v, err := verifyReceiptLocked(ssn, r)
		if err != nil {
			return nil, ssn, err
		}
		chosen[r.Number] = v
		numbers = append(numbers, r.Number)
	}

	// Coverage: exactly the segments the session holds, no more, no fewer.
	if len(numbers) != len(ssn.segments) {
		return nil, ssn, fmt.Errorf("%w: selected %d segments, session holds %d",
			ErrIncompleteComposition, len(numbers), len(ssn.segments))
	}
	for _, n := range numbers {
		if _, ok := ssn.segments[n]; !ok {
			return nil, ssn, fmt.Errorf("%w: segment %d absent from session %s",
				ErrIncompleteComposition, n, ssn.id)
		}
	}
	sort.Ints(numbers)

	// One key generation for the whole object, and it must still be
	// authorized. A revoked generation is refused even if it is uniform.
	gen := chosen[numbers[0]].keyGen
	for _, n := range numbers {
		if g := chosen[n].keyGen; g != gen {
			return nil, ssn, fmt.Errorf("%w: segment %d uses generation %d, segment %d uses %d",
				ErrMixedKeyGenerations, n, g, numbers[0], gen)
		}
	}
	if !c.authorized[gen] {
		return nil, ssn, fmt.Errorf("%w: generation %d", ErrKeyRevoked, gen)
	}
	key, ok := c.keys[gen]
	if !ok {
		return nil, ssn, fmt.Errorf("%w: generation %d", ErrKeyGenerationUnknown, gen)
	}

	// Bucket policy effective at this completion's logical position.
	b, err := c.bucketLocked(ssn.bucket)
	if err != nil {
		return nil, ssn, err
	}
	policy, pv, pseq := b.policyEffectiveAt(at, seq)

	var size int64
	for _, n := range numbers {
		size += int64(len(chosen[n].plaintext))
	}
	if err := policy.deny(len(numbers), size, gen); err != nil {
		return nil, ssn, err
	}

	// Assemble from ciphertext: every segment must authenticate and match its
	// acknowledged digest before a single byte becomes visible.
	data := make([]byte, 0, size)
	hashes := make([]string, 0, len(numbers))
	for _, n := range numbers {
		v := chosen[n]
		pt, err := openSegment(key, n, gen, v.ciphertext)
		if err != nil {
			return nil, ssn, fmt.Errorf("segment %d: %w", n, err)
		}
		d := sha256.Sum256(pt)
		got := hex.EncodeToString(d[:])
		if got != v.digest {
			return nil, ssn, fmt.Errorf("%w: segment %d digest %s, stored %s",
				ErrIntegrity, n, got, v.digest)
		}
		data = append(data, pt...)
		hashes = append(hashes, v.digest)
	}

	obj := &Object{
		Bucket:        ssn.bucket,
		SessionID:     ssn.id,
		KeyGeneration: gen,
		Segments:      len(numbers),
		Size:          size,
		PolicyVersion: pv,
		PolicySeq:     pseq,
		Data:          data,
	}
	obj.Digest = objectDigest(objectRecord{
		Bucket:        obj.Bucket,
		Session:       obj.SessionID,
		KeyGeneration: obj.KeyGeneration,
		Segments:      obj.Segments,
		Size:          obj.Size,
		PolicyVersion: obj.PolicyVersion,
		SegmentHashes: hashes,
	})
	return obj, ssn, nil
}

func (c *Controller) resolveSessionLocked(sels []Receipt) (*session, error) {
	id := sels[0].SessionID
	if id == "" {
		return nil, fmt.Errorf("%w: empty session id", ErrInvalidArgument)
	}
	for _, sel := range sels[1:] {
		if sel.SessionID != id {
			return nil, fmt.Errorf("%w: %q and %q", ErrForeignSession, id, sel.SessionID)
		}
	}
	return c.sessionLocked(id)
}

// verifyReceiptLocked confirms that a cited receipt is exactly the receipt
// the backend issued for that stored version.
func verifyReceiptLocked(ssn *session, r Receipt) (*segmentVersion, error) {
	versions, ok := ssn.segments[r.Number]
	if !ok {
		return nil, fmt.Errorf("%w: session %s has no segment %d", ErrUnknownVersion, ssn.id, r.Number)
	}
	if r.Version < 1 || r.Version > len(versions) {
		return nil, fmt.Errorf("%w: segment %d version %d, session has %d versions",
			ErrUnknownVersion, r.Number, r.Version, len(versions))
	}
	v := versions[r.Version-1]
	if !r.Equal(receiptFor(ssn.id, r.Number, v)) {
		return nil, fmt.Errorf("%w: segment %d version %d", ErrReceiptMismatch, r.Number, r.Version)
	}
	return v, nil
}

// policyEffectiveAt returns the policy in force for an operation committed at
// logical time at with submission sequence seq, together with two identifiers:
// the logical time the policy took effect and a 1-based installation ordinal
// that is unique within the bucket. A policy installed at the same logical
// time is effective only if it was submitted earlier, which makes the tie
// between "policy change" and "completion" resolve by commit order and gives
// exactly one observable outcome.
func (b *bucket) policyEffectiveAt(at, seq int64) (Policy, int64, int64) {
	var chosen Policy
	var version, ordinal int64
	found := false
	for _, pv := range b.versions {
		if pv.version > at || (pv.version == at && pv.seq > seq) {
			continue
		}
		if !found || pv.version > version || (pv.version == version && pv.seq > ordinal) {
			chosen, version, ordinal, found = pv.policy, pv.version, pv.seq, true
		}
	}
	return chosen, version, ordinal
}

// Object reads the published object of a bucket. Until a completion commits
// it returns ErrObjectNotFound. The result is a deep copy.
func (c *Controller) Object(bucketName string) (*Object, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	b, err := c.bucketLocked(bucketName)
	if err != nil {
		return nil, err
	}
	if b.object == nil {
		return nil, fmt.Errorf("%w: bucket %q", ErrObjectNotFound, bucketName)
	}
	return b.object.clone(), nil
}

// CompleteWithReceipts is an alias for Complete, kept so intent reads clearly
// at call sites that retry with freshly issued receipts after a re-encryption.
func (c *Controller) CompleteWithReceipts(receipts []Receipt) (*Object, error) {
	return c.Complete(Completion{Selections: receipts})
}
