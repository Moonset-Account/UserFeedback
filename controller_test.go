package encupload

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"sort"
	"testing"
)

// fixture builds a controller with one key generation, one bucket and one
// session, and returns the session id.
func fixture(t *testing.T, bucket string, gen int64) (*Controller, string) {
	t.Helper()
	c := NewController()
	if err := c.NewKeyGeneration(gen); err != nil {
		t.Fatalf("NewKeyGeneration(%d): %v", gen, err)
	}
	if err := c.CreateBucket(bucket); err != nil {
		t.Fatalf("CreateBucket(%q): %v", bucket, err)
	}
	id, err := c.CreateSession(bucket)
	if err != nil {
		t.Fatalf("CreateSession: %v", err)
	}
	return c, id
}

func mustUpload(t *testing.T, c *Controller, session string, number int, data string) Receipt {
	t.Helper()
	r, err := c.UploadSegment(session, number, []byte(data))
	if err != nil {
		t.Fatalf("UploadSegment(%s, %d): %v", session, number, err)
	}
	return r
}

func digestOf(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

func selection(receipts ...Receipt) Completion {
	return Completion{Selections: append([]Receipt(nil), receipts...)}
}

func wantErrIs(t *testing.T, err, target error) {
	t.Helper()
	if !errors.Is(err, target) {
		t.Fatalf("error = %v, want errors.Is(err, %v)", err, target)
	}
}

// TestCompleteHappyPath covers the accepting path: three segments, explicit
// version selection, one session, one authorized generation, no policy.
func TestCompleteHappyPath(t *testing.T) {
	c, sid := fixture(t, "media", 1)
	r0 := mustUpload(t, c, sid, 0, "alpha")
	r1 := mustUpload(t, c, sid, 1, "beta")
	r2 := mustUpload(t, c, sid, 2, "gamma")

	// Receipts must carry the key generation actually used and the digest of
	// the bytes stored.
	for _, r := range []Receipt{r0, r1, r2} {
		if r.SessionID != sid {
			t.Fatalf("receipt session = %q, want %q", r.SessionID, sid)
		}
		if r.KeyGeneration != 1 {
			t.Fatalf("receipt key generation = %d, want 1", r.KeyGeneration)
		}
	}
	if r0.Digest != digestOf("alpha") {
		t.Fatalf("receipt digest = %s, want sha256(alpha)", r0.Digest)
	}
	if r0.Version != 1 || r1.Version != 1 || r2.Version != 1 {
		t.Fatalf("first uploads should be version 1, got %d/%d/%d", r0.Version, r1.Version, r2.Version)
	}

	obj, err := c.Complete(selection(r0, r1, r2))
	if err != nil {
		t.Fatalf("Complete: %v", err)
	}
	if want := "alphabetagamma"; string(obj.Data) != want {
		t.Fatalf("object data = %q, want %q", obj.Data, want)
	}
	if obj.Segments != 3 || obj.Size != int64(len("alphabetagamma")) {
		t.Fatalf("segments/size = %d/%d", obj.Segments, obj.Size)
	}
	if obj.KeyGeneration != 1 {
		t.Fatalf("object key generation = %d, want 1", obj.KeyGeneration)
	}
	if obj.Digest == "" {
		t.Fatal("object digest must not be empty")
	}

	// A reader sees the same complete object.
	read, err := c.Object("media")
	if err != nil {
		t.Fatalf("Object: %v", err)
	}
	if !bytes.Equal(read.Data, obj.Data) || read.Digest != obj.Digest {
		t.Fatalf("reader object = %+v, want %+v", read, obj)
	}

	// A completed session refuses further uploads and a second completion.
	if _, err := c.UploadSegment(sid, 3, []byte("delta")); !errors.Is(err, ErrSessionCompleted) {
		t.Fatalf("upload after completion = %v, want ErrSessionCompleted", err)
	}
	if _, err := c.Complete(selection(r0, r1, r2)); !errors.Is(err, ErrSessionCompleted) {
		t.Fatalf("second completion = %v, want ErrSessionCompleted", err)
	}
}

// TestReuploadDoesNotReplaceListedVersion is the core version-pinning case: a
// listed version stays byte-identical even after the same segment number is
// uploaded again, and a caller cannot cite the new version with the old
// receipt.
func TestReuploadDoesNotReplaceListedVersion(t *testing.T) {
	c, sid := fixture(t, "media", 1)
	r0v1 := mustUpload(t, c, sid, 0, "original")
	r1 := mustUpload(t, c, sid, 1, "tail")

	r0v2 := mustUpload(t, c, sid, 0, "replacement")
	if r0v2.Version != 2 {
		t.Fatalf("re-upload version = %d, want 2", r0v2.Version)
	}
	if r0v2.Number != 0 || r0v1.Version != 1 {
		t.Fatalf("unexpected receipt numbers/versions: %+v %+v", r0v1, r0v2)
	}
	versions, err := c.SegmentVersions(sid, 0)
	if err != nil {
		t.Fatalf("SegmentVersions: %v", err)
	}
	if len(versions) != 2 || versions[0] != 1 || versions[1] != 2 {
		t.Fatalf("stored versions = %v, want [1 2]", versions)
	}

	// Selecting v2 while citing v1's receipt is rejected.
	tampered := r0v2
	tampered.Digest = r0v1.Digest
	_, err = c.Complete(selection(tampered, r1))
	wantErrIs(t, err, ErrReceiptMismatch)

	// A completely made-up key generation in the receipt is also rejected.
	lying := r0v1
	lying.KeyGeneration = 9
	_, err = c.Complete(selection(lying, r1))
	wantErrIs(t, err, ErrReceiptMismatch)

	// The completion lists version 1 explicitly, after version 2 exists.
	obj, err := c.Complete(selection(r0v1, r1))
	if err != nil {
		t.Fatalf("Complete selecting v1: %v", err)
	}
	if want := "originaltail"; string(obj.Data) != want {
		t.Fatalf("object data = %q, want %q (v1 must not be replaced)", obj.Data, want)
	}
}

// TestCompletionMembershipAndCoverage covers the cross-constraints that keep a
// composition inside one session and exactly the uploaded segment set.
func TestCompletionMembershipAndCoverage(t *testing.T) {
	c, sid := fixture(t, "media", 1)
	r0 := mustUpload(t, c, sid, 0, "a")
	r1 := mustUpload(t, c, sid, 1, "b")

	other, err := c.CreateSession("media")
	if err != nil {
		t.Fatalf("CreateSession: %v", err)
	}
	foreign := mustUpload(t, c, other, 1, "b")

	// Receipt from a second session is rejected as a foreign session.
	_, err = c.Complete(selection(r0, foreign))
	wantErrIs(t, err, ErrForeignSession)

	// Missing segment.
	_, err = c.Complete(selection(r0))
	wantErrIs(t, err, ErrIncompleteComposition)

	// Duplicate segment number.
	_, err = c.Complete(selection(r0, r1, r0))
	wantErrIs(t, err, ErrDuplicateSegment)

	// Version that was never issued.
	ghost := r0
	ghost.Version = 4
	_, err = c.Complete(selection(ghost, r1))
	wantErrIs(t, err, ErrUnknownVersion)

	// Unknown session.
	unknown := r0
	unknown.SessionID = "sess-does-not-exist"
	_, err = c.Complete(selection(unknown))
	wantErrIs(t, err, ErrSessionNotFound)

	// Empty selection.
	if _, err := c.Complete(Completion{}); !errors.Is(err, ErrEmptySelection) {
		t.Fatalf("empty completion = %v, want ErrEmptySelection", err)
	}

	// A segment the session never received cannot be invented by listing a
	// number that no receipt covers.
	phantom := r0
	phantom.Number = 7
	_, err = c.Complete(selection(r0, r1, phantom))
	wantErrIs(t, err, ErrUnknownVersion)

	// None of the rejections published anything, and the session is still
	// retryable.
	if _, err := c.Object("media"); !errors.Is(err, ErrObjectNotFound) {
		t.Fatalf("Object before success = %v, want ErrObjectNotFound", err)
	}
	st, err := c.Session(sid)
	if err != nil {
		t.Fatalf("Session: %v", err)
	}
	if st.Published {
		t.Fatal("session must not be published after rejected completions")
	}
	if st.FailedAttempts == 0 || st.LastError == "" {
		t.Fatalf("session should record rejected attempts, got %+v", st)
	}

	obj, err := c.Complete(selection(r0, r1))
	if err != nil {
		t.Fatalf("retry after rejections: %v", err)
	}
	if string(obj.Data) != "ab" {
		t.Fatalf("object data = %q, want ab", obj.Data)
	}
}

// TestSingleAuthorizedKeyGeneration covers the key rule: segments must share
// one generation, and that generation must still be authorized. After a
// failure the client re-encrypts every segment and completes successfully.
func TestSingleAuthorizedKeyGeneration(t *testing.T) {
	c, sid := fixture(t, "media", 1)
	r0 := mustUpload(t, c, sid, 0, "a")
	_ = mustUpload(t, c, sid, 1, "b")

	// Rotation: new uploads use generation 2, old versions stay at 1.
	if err := c.NewKeyGeneration(2); err != nil {
		t.Fatalf("NewKeyGeneration(2): %v", err)
	}
	r0v2 := mustUpload(t, c, sid, 0, "a")
	r1v2 := mustUpload(t, c, sid, 1, "b")
	if r0v2.KeyGeneration != 2 || r1v2.KeyGeneration != 2 {
		t.Fatalf("post-rotation receipts = %+v %+v, want generation 2", r0v2, r1v2)
	}

	// Mixing generation 1 and 2 is rejected.
	_, err := c.Complete(selection(r0, r1v2))
	wantErrIs(t, err, ErrMixedKeyGenerations)

	// Uniform generation 2 is accepted.
	obj, err := c.Complete(selection(r0v2, r1v2))
	if err != nil {
		t.Fatalf("uniform gen2 completion: %v", err)
	}
	if obj.KeyGeneration != 2 || string(obj.Data) != "ab" {
		t.Fatalf("object = %+v", obj)
	}
}

func TestRevokedGenerationBlocksCompletionUntilReencrypted(t *testing.T) {
	c, sid := fixture(t, "media", 1)
	r0 := mustUpload(t, c, sid, 0, "one")
	r1 := mustUpload(t, c, sid, 1, "two")

	if err := c.RevokeKeyGeneration(1); err != nil {
		t.Fatalf("RevokeKeyGeneration(1): %v", err)
	}
	if got := c.AuthorizedKeyGenerations(); len(got) != 0 {
		t.Fatalf("authorized generations = %v, want none", got)
	}

	// Completion with the now-revoked generation is refused even though every
	// selected segment uses it.
	_, err := c.Complete(selection(r0, r1))
	wantErrIs(t, err, ErrKeyRevoked)

	// No authorized key means uploads cannot proceed either.
	if _, err := c.UploadSegment(sid, 2, []byte("three")); !errors.Is(err, ErrNoAuthorizedKey) {
		t.Fatalf("upload with no authorized key = %v, want ErrNoAuthorizedKey", err)
	}
	if _, err := c.Object("media"); !errors.Is(err, ErrObjectNotFound) {
		t.Fatalf("reader must not see a partially published object, got %v", err)
	}

	// New generation, re-encrypt both segments, then completion succeeds.
	if err := c.NewKeyGeneration(2); err != nil {
		t.Fatalf("NewKeyGeneration(2): %v", err)
	}
	nr0 := mustUpload(t, c, sid, 0, "one")
	nr1 := mustUpload(t, c, sid, 1, "two")
	obj, err := c.Complete(selection(nr0, nr1))
	if err != nil {
		t.Fatalf("completion after re-encryption: %v", err)
	}
	if obj.KeyGeneration != 2 || string(obj.Data) != "onetwo" {
		t.Fatalf("object = %+v", obj)
	}

	// Revoking an unknown generation is distinct from revoking a known one.
	if err := c.RevokeKeyGeneration(77); !errors.Is(err, ErrKeyGenerationUnknown) {
		t.Fatalf("revoke unknown = %v, want ErrKeyGenerationUnknown", err)
	}
	if err := c.RevokeKeyGeneration(2); err != nil {
		t.Fatalf("revoke known generation: %v", err)
	}
	if err := c.RevokeKeyGeneration(2); err != nil {
		t.Fatalf("revoke twice should be idempotent: %v", err)
	}
	// The already-published object remains readable after revocation: bytes
	// are committed, only new compositions are blocked.
	read, err := c.Object("media")
	if err != nil {
		t.Fatalf("Object after revocation: %v", err)
	}
	if string(read.Data) != "onetwo" || read.Digest != obj.Digest {
		t.Fatalf("published object changed: %+v", read)
	}
}

// TestBucketPolicyBoundaries checks the policy gates, including the inclusive
// edges where a selection exactly at the limit is accepted.
func TestBucketPolicyBoundaries(t *testing.T) {
	c, sid := fixture(t, "media", 1)
	r0 := mustUpload(t, c, sid, 0, "12345") // 5 bytes
	r1 := mustUpload(t, c, sid, 1, "67890") // 5 bytes

	// Exactly at the byte limit: accepted.
	if err := c.SetPolicy("media", &Policy{MaxObjectBytes: 10}); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}
	if _, err := c.Complete(selection(r0, r1)); err != nil {
		t.Fatalf("completion at exact byte limit: %v", err)
	}

	// A separate bucket to test the one-byte-over boundary.
	c2, sid2 := fixture(t, "media2", 1)
	s0 := mustUpload(t, c2, sid2, 0, "12345")
	s1 := mustUpload(t, c2, sid2, 1, "67890")
	if err := c2.SetPolicy("media2", &Policy{MaxObjectBytes: 9}); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}
	_, err := c2.Complete(selection(s0, s1))
	wantErrIs(t, err, ErrPolicyRejected)

	// Segment count bounds and the key-generation pin.
	c3, sid3 := fixture(t, "media3", 1)
	t0 := mustUpload(t, c3, sid3, 0, "x")
	t1 := mustUpload(t, c3, sid3, 1, "y")
	if err := c3.SetPolicy("media3", &Policy{MinSegments: 3}); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}
	_, err = c3.Complete(selection(t0, t1))
	wantErrIs(t, err, ErrPolicyRejected)

	if err := c3.SetPolicy("media3", &Policy{MaxSegments: 1}); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}
	_, err = c3.Complete(selection(t0, t1))
	wantErrIs(t, err, ErrPolicyRejected)

	if err := c3.SetPolicy("media3", &Policy{RequiredKeyGeneration: 5}); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}
	_, err = c3.Complete(selection(t0, t1))
	wantErrIs(t, err, ErrPolicyRejected)

	// The latest installed policy wins: a permissive policy restores the
	// ability to publish, and the object records which policy stamp it was
	// captured against.
	c3.Tick()
	c3.Tick()
	if err := c3.SetPolicy("media3", &Policy{}); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}
	publishedAt := c3.Now()
	if publishedAt == 0 {
		t.Fatal("test needs a non-zero clock to distinguish the policy stamp")
	}
	obj, err := c3.Complete(selection(t0, t1))
	if err != nil {
		t.Fatalf("completion under permissive policy: %v", err)
	}
	if obj.PolicyVersion != publishedAt {
		t.Fatalf("object policy version = %d, want %d", obj.PolicyVersion, publishedAt)
	}

	// Invalid policy values are rejected at installation.
	if err := c3.SetPolicy("media3", &Policy{MinSegments: 4, MaxSegments: 2}); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("invalid policy = %v, want ErrInvalidArgument", err)
	}
	if err := c3.SetPolicy("nope", &Policy{}); !errors.Is(err, ErrBucketNotFound) {
		t.Fatalf("policy on missing bucket = %v, want ErrBucketNotFound", err)
	}
}

// TestPolicyAndCompletionAtSameInstant checks the deterministic tie: a policy
// change and a completion submitted for the same logical time resolve in
// submission order, and each ordering yields exactly one outcome.
func TestPolicyAndCompletionAtSameInstant(t *testing.T) {
	// Order 1: policy first, completion second. The policy is in force for
	// the completion at the same instant, so the completion is rejected.
	t.Run("policy first rejects", func(t *testing.T) {
		c, sid := fixture(t, "media", 1)
		r0 := mustUpload(t, c, sid, 0, "a")
		r1 := mustUpload(t, c, sid, 1, "b")

		at := c.Now() + 5
		pt, err := c.SchedulePolicy("media", &Policy{MaxSegments: 1}, at)
		if err != nil {
			t.Fatalf("SchedulePolicy: %v", err)
		}
		ct, err := c.ScheduleCompletion(selection(r0, r1), at)
		if err != nil {
			t.Fatalf("ScheduleCompletion: %v", err)
		}

		results, err := c.RunUntil(at)
		if err != nil {
			t.Fatalf("RunUntil: %v", err)
		}
		if len(results) != 2 {
			t.Fatalf("results = %d, want 2", len(results))
		}
		if results[0].Ticket != pt || results[1].Ticket != ct {
			t.Fatalf("execution order = %v, want [%v %v]", []Ticket{results[0].Ticket, results[1].Ticket}, pt, ct)
		}
		wantErrIs(t, results[1].Err, ErrPolicyRejected)
		if results[1].Object != nil {
			t.Fatal("rejected completion must not yield an object")
		}
		if _, err := c.Object("media"); !errors.Is(err, ErrObjectNotFound) {
			t.Fatalf("reader saw an object despite rejection: %v", err)
		}
	})

	// Order 2: completion first, policy second. The completion is evaluated
	// against the previously installed (permissive) policy and commits; the
	// later policy does not retroactively reject it.
	t.Run("completion first commits", func(t *testing.T) {
		c, sid := fixture(t, "media", 1)
		r0 := mustUpload(t, c, sid, 0, "a")
		r1 := mustUpload(t, c, sid, 1, "b")

		at := c.Now() + 5
		ct, err := c.ScheduleCompletion(selection(r0, r1), at)
		if err != nil {
			t.Fatalf("ScheduleCompletion: %v", err)
		}
		pt, err := c.SchedulePolicy("media", &Policy{MaxSegments: 1}, at)
		if err != nil {
			t.Fatalf("SchedulePolicy: %v", err)
		}

		results, err := c.RunUntil(at)
		if err != nil {
			t.Fatalf("RunUntil: %v", err)
		}
		if len(results) != 2 {
			t.Fatalf("results = %d, want 2", len(results))
		}
		if results[0].Ticket != ct || results[1].Ticket != pt {
			t.Fatalf("execution order = %v, want [%v %v]", []Ticket{results[0].Ticket, results[1].Ticket}, ct, pt)
		}
		if results[0].Err != nil {
			t.Fatalf("first completion at instant: %v", results[0].Err)
		}
		if results[0].Object == nil || string(results[0].Object.Data) != "ab" {
			t.Fatalf("object = %+v, want data ab", results[0].Object)
		}
		// The object was captured against the pre-change policy (stamp 0).
		if results[0].Object.PolicyVersion != 0 {
			t.Fatalf("object policy version = %d, want 0", results[0].Object.PolicyVersion)
		}
		read, err := c.Object("media")
		if err != nil {
			t.Fatalf("Object: %v", err)
		}
		if string(read.Data) != "ab" {
			t.Fatalf("reader data = %q, want ab", read.Data)
		}
	})

	// The same instant produces a single, stable result regardless of clock
	// granularity: a policy scheduled strictly before the completion rejects
	// it, and one scheduled strictly after does not.
	t.Run("strict ordering", func(t *testing.T) {
		c, sid := fixture(t, "media", 1)
		r0 := mustUpload(t, c, sid, 0, "a")
		r1 := mustUpload(t, c, sid, 1, "b")
		if _, err := c.SchedulePolicy("media", &Policy{MaxSegments: 1}, 3); err != nil {
			t.Fatalf("SchedulePolicy: %v", err)
		}
		if _, err := c.ScheduleCompletion(selection(r0, r1), 4); err != nil {
			t.Fatalf("ScheduleCompletion: %v", err)
		}
		results, err := c.RunUntil(4)
		if err != nil {
			t.Fatalf("RunUntil: %v", err)
		}
		if len(results) != 2 {
			t.Fatalf("results = %d, want 2", len(results))
		}
		wantErrIs(t, results[1].Err, ErrPolicyRejected)
	})
}

// TestPendingTicketIsNotDone checks ticket bookkeeping for scheduled work.
func TestPendingTicketIsNotDone(t *testing.T) {
	c, sid := fixture(t, "media", 1)
	r0 := mustUpload(t, c, sid, 0, "a")

	at := c.Now() + 10
	ticket, err := c.ScheduleCompletion(selection(r0), at)
	if err != nil {
		t.Fatalf("ScheduleCompletion: %v", err)
	}
	if _, done := c.TicketResult(ticket); done {
		t.Fatal("scheduled completion must not be done before its time")
	}
	if _, err := c.Object("media"); !errors.Is(err, ErrObjectNotFound) {
		t.Fatalf("Object before scheduled completion = %v, want ErrObjectNotFound", err)
	}
	if _, err := c.ScheduleCompletion(selection(r0), -1); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("negative completion time = %v, want ErrInvalidArgument", err)
	}

	if _, err := c.RunUntil(at); err != nil {
		t.Fatalf("RunUntil: %v", err)
	}
	res, done := c.TicketResult(ticket)
	if !done {
		t.Fatal("ticket should be done after RunUntil")
	}
	if res.Err != nil || res.Object == nil {
		t.Fatalf("result = %+v", res)
	}
	if _, err := c.RunUntil(at - 1); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("RunUntil into the past = %v, want ErrInvalidArgument", err)
	}
}

// TestBatchUploadAtomicity checks that a batch either fully applies or leaves
// the session untouched.
func TestBatchUploadAtomicity(t *testing.T) {
	c, sid := fixture(t, "media", 1)
	receipts, err := c.UploadBatch(sid, map[int][]byte{0: []byte("a"), 1: []byte("b"), 2: []byte("c")})
	if err != nil {
		t.Fatalf("UploadBatch: %v", err)
	}
	if len(receipts) != 3 {
		t.Fatalf("receipts = %d, want 3", len(receipts))
	}
	// Receipts come back in segment order.
	nums := make([]int, 0, len(receipts))
	for _, r := range receipts {
		nums = append(nums, r.Number)
	}
	if !sort.IntsAreSorted(nums) {
		t.Fatalf("receipts not ordered by segment: %v", nums)
	}

	if _, err := c.UploadBatch(sid, nil); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("empty batch = %v, want ErrInvalidArgument", err)
	}
	if _, err := c.UploadBatch(sid, map[int][]byte{-1: []byte("x")}); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("negative segment batch = %v, want ErrInvalidArgument", err)
	}
	// Rejected batches must not have added versions.
	for n := 0; n < 3; n++ {
		vs, err := c.SegmentVersions(sid, n)
		if err != nil {
			t.Fatalf("SegmentVersions: %v", err)
		}
		if len(vs) != 1 {
			t.Fatalf("segment %d versions = %v, want exactly 1", n, vs)
		}
	}

	if _, err := c.UploadSegment(sid, -1, []byte("x")); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("negative segment = %v, want ErrInvalidArgument", err)
	}
}

// TestReaderNeverSeesPartialObject interrupts a failing completion and a
// successful retry with concurrent readers. Readers may see "not found" or a
// fully assembled object, never an object missing segments.
func TestReaderNeverSeesPartialObject(t *testing.T) {
	c, sid := fixture(t, "media", 1)
	var receipts []Receipt
	for n := 0; n < 4; n++ {
		receipts = append(receipts, mustUpload(t, c, sid, n, string(rune('a'+n))))
	}
	want := "abcd"

	// Force a failure first by tightening the policy, then restore it.
	if err := c.SetPolicy("media", &Policy{MaxObjectBytes: 1}); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}
	done := make(chan struct{})
	var readerErr error
	var readerWG = make(chan struct{})
	go func() {
		defer close(readerWG)
		for {
			select {
			case <-done:
				return
			default:
			}
			obj, err := c.Object("media")
			if err != nil {
				if !errors.Is(err, ErrObjectNotFound) {
					readerErr = err
					return
				}
				continue
			}
			if string(obj.Data) != want || obj.Segments != 4 {
				readerErr = errors.New("reader observed an incomplete object")
				return
			}
		}
	}()

	if _, err := c.Complete(selection(receipts...)); !errors.Is(err, ErrPolicyRejected) {
		t.Fatalf("expected policy rejection, got %v", err)
	}
	if err := c.SetPolicy("media", &Policy{}); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}
	if _, err := c.Complete(selection(receipts...)); err != nil {
		t.Fatalf("Complete after policy restore: %v", err)
	}
	close(done)
	<-readerWG
	if readerErr != nil {
		t.Fatalf("reader saw a torn object: %v", readerErr)
	}
}

// TestObjectAndReceiptCopiesAreIsolated checks that callers cannot mutate
// published state or receipts through returned values.
func TestObjectAndReceiptCopiesAreIsolated(t *testing.T) {
	c, sid := fixture(t, "media", 1)
	r := mustUpload(t, c, sid, 0, "abc")
	obj, err := c.Complete(selection(r))
	if err != nil {
		t.Fatalf("Complete: %v", err)
	}
	obj.Data[0] = 'z'
	read, err := c.Object("media")
	if err != nil {
		t.Fatalf("Object: %v", err)
	}
	if string(read.Data) != "abc" {
		t.Fatalf("mutation leaked into published object: %q", read.Data)
	}
}

// TestArgumentValidation covers the cheap rejections that must happen before
// any state is touched: bad bucket names, duplicate buckets, bad key
// generations, and back-dated scheduling.
func TestArgumentValidation(t *testing.T) {
	c := NewController()

	if err := c.CreateBucket(""); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("empty bucket name = %v, want ErrInvalidArgument", err)
	}
	if err := c.CreateBucket("media"); err != nil {
		t.Fatalf("CreateBucket: %v", err)
	}
	if err := c.CreateBucket("media"); !errors.Is(err, ErrBucketExists) {
		t.Fatalf("duplicate bucket = %v, want ErrBucketExists", err)
	}
	if _, err := c.CreateSession("missing"); !errors.Is(err, ErrBucketNotFound) {
		t.Fatalf("session on missing bucket = %v, want ErrBucketNotFound", err)
	}

	if err := c.NewKeyGeneration(0); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("generation 0 = %v, want ErrInvalidArgument", err)
	}
	if err := c.NewKeyGeneration(2); err != nil {
		t.Fatalf("NewKeyGeneration(2): %v", err)
	}
	if err := c.NewKeyGeneration(2); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("duplicate generation = %v, want ErrInvalidArgument", err)
	}
	if err := c.NewKeyGeneration(1); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("regressing generation = %v, want ErrInvalidArgument", err)
	}

	c.Tick()
	// Back-dating a policy or a completion would let a caller select a policy
	// that has already been superseded, so both are refused.
	if _, err := c.SchedulePolicy("media", &Policy{}, 0); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("back-dated policy = %v, want ErrInvalidArgument", err)
	}
	if _, err := c.ScheduleCompletion(Completion{Selections: []Receipt{{
		SessionID: "sess-1",
	}}}, 0); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("back-dated completion = %v, want ErrInvalidArgument", err)
	}
	if _, err := c.ScheduleCompletion(Completion{}, c.Now()); !errors.Is(err, ErrEmptySelection) {
		t.Fatalf("empty scheduled completion = %v, want ErrEmptySelection", err)
	}
	if _, err := c.SchedulePolicy("missing", &Policy{}, c.Now()); !errors.Is(err, ErrBucketNotFound) {
		t.Fatalf("policy on missing bucket = %v, want ErrBucketNotFound", err)
	}
	if _, err := c.SchedulePolicy("media", nil, c.Now()); !errors.Is(err, ErrInvalidArgument) {
		t.Fatalf("nil policy = %v, want ErrInvalidArgument", err)
	}
}

// TestInterleavedReuploadAndPolicy covers two interleavings that decide what a
// scheduled completion actually composes:
//
//   - a re-upload that lands after the completion was submitted must not
//     change the version the request listed, and
//   - a policy installed before the completion's logical time governs it,
//     because the object must match the policy in force when it is composed.
func TestInterleavedReuploadAndPolicy(t *testing.T) {
	t.Run("reupload after submit does not change selection", func(t *testing.T) {
		c, sid := fixture(t, "media", 1)
		v1 := mustUpload(t, c, sid, 0, "listed")
		at := c.Now() + 3
		ticket, err := c.ScheduleCompletion(selection(v1), at)
		if err != nil {
			t.Fatalf("ScheduleCompletion: %v", err)
		}

		// The client re-uploads the same segment number before the scheduled
		// completion runs. That creates version 2 but must not touch v1.
		v2 := mustUpload(t, c, sid, 0, "intruder")
		if v2.Version != 2 {
			t.Fatalf("re-upload version = %d, want 2", v2.Version)
		}

		results, err := c.RunUntil(at)
		if err != nil {
			t.Fatalf("RunUntil: %v", err)
		}
		if len(results) != 1 || results[0].Ticket != ticket {
			t.Fatalf("results = %+v", results)
		}
		if results[0].Err != nil {
			t.Fatalf("scheduled completion: %v", results[0].Err)
		}
		if got := string(results[0].Object.Data); got != "listed" {
			t.Fatalf("composed %q, want %q (the listed version)", got, "listed")
		}
	})

	t.Run("policy installed before the completion governs it", func(t *testing.T) {
		c, sid := fixture(t, "media", 1)
		r0 := mustUpload(t, c, sid, 0, "ab")
		r1 := mustUpload(t, c, sid, 1, "cd")

		// Submit the completion first, then tighten policy before its time.
		at := c.Now() + 4
		if _, err := c.ScheduleCompletion(selection(r0, r1), at); err != nil {
			t.Fatalf("ScheduleCompletion: %v", err)
		}
		c.Tick()
		if err := c.SetPolicy("media", &Policy{MaxObjectBytes: 3}); err != nil {
			t.Fatalf("SetPolicy: %v", err)
		}

		results, err := c.RunUntil(at)
		if err != nil {
			t.Fatalf("RunUntil: %v", err)
		}
		if len(results) != 1 {
			t.Fatalf("results = %d, want 1", len(results))
		}
		wantErrIs(t, results[0].Err, ErrPolicyRejected)
		if _, err := c.Object("media"); !errors.Is(err, ErrObjectNotFound) {
			t.Fatalf("reader saw an object rejected by the policy in force: %v", err)
		}
	})
}
