package encupload

import "errors"

// Sentinel errors returned by the controller. Callers should test them with
// errors.Is, because most failures carry additional context via fmt.Errorf.
var (
	// ErrInvalidArgument reports a malformed request (empty names, negative
	// segment numbers, invalid policy values).
	ErrInvalidArgument = errors.New("encupload: invalid argument")

	// ErrBucketNotFound reports a bucket that has not been created.
	ErrBucketNotFound = errors.New("encupload: bucket not found")

	// ErrBucketExists reports a duplicate bucket creation.
	ErrBucketExists = errors.New("encupload: bucket already exists")

	// ErrSessionNotFound reports an unknown upload session.
	ErrSessionNotFound = errors.New("encupload: upload session not found")

	// ErrSessionCompleted reports use of a session whose object has already
	// been published successfully.
	ErrSessionCompleted = errors.New("encupload: upload session already completed")

	// ErrNoAuthorizedKey reports that every key generation has been revoked,
	// so the backend cannot encrypt a new segment.
	ErrNoAuthorizedKey = errors.New("encupload: no authorized key generation")

	// ErrKeyGenerationUnknown reports a key generation the backend never had.
	ErrKeyGenerationUnknown = errors.New("encupload: unknown key generation")

	// ErrKeyRevoked reports composition with a key generation that is no
	// longer authorized.
	ErrKeyRevoked = errors.New("encupload: key generation is revoked")

	// ErrForeignSession reports a completion selection that references a
	// receipt issued by a different upload session.
	ErrForeignSession = errors.New("encupload: segment receipt belongs to another session")

	// ErrDuplicateSegment reports the same segment number listed twice in one
	// completion request.
	ErrDuplicateSegment = errors.New("encupload: duplicate segment selection")

	// ErrIncompleteComposition reports a selection that does not cover every
	// segment uploaded in the session exactly once.
	ErrIncompleteComposition = errors.New("encupload: selection does not cover segments 0..n-1")

	// ErrUnknownVersion reports a selected segment version that does not
	// exist in the session.
	ErrUnknownVersion = errors.New("encupload: segment version not found")

	// ErrReceiptMismatch reports a receipt whose digest or key generation
	// does not match the stored segment version.
	ErrReceiptMismatch = errors.New("encupload: receipt does not match stored segment version")

	// ErrMixedKeyGenerations reports a composition whose selected segments
	// were encrypted with different key generations.
	ErrMixedKeyGenerations = errors.New("encupload: selected segments use different key generations")

	// ErrPolicyRejected reports a composition that the current target bucket
	// policy refuses.
	ErrPolicyRejected = errors.New("encupload: composition rejected by bucket policy")

	// ErrEmptySelection reports a completion request with no segments.
	ErrEmptySelection = errors.New("encupload: empty segment selection")

	// ErrObjectNotFound reports a read of an object that has not been
	// completely published.
	ErrObjectNotFound = errors.New("encupload: object not found")

	// ErrIntegrity reports stored bytes that no longer match their recorded
	// digest.
	ErrIntegrity = errors.New("encupload: stored object failed integrity check")
)
