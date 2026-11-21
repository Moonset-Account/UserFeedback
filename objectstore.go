// Package objectstore implements an in-memory object service that supports
// resumable, out-of-order segment uploads followed by an atomic,
// manifest-based completion.
//
// The service has three building blocks:
//
//   - An upload session is created for a target object key and receives
//     segments in any order. Writing a segment number again replaces the
//     content stored in that session. PutSegment returns a token that is
//     derived only from the segment content, so equal content always yields
//     an equal token.
//   - Complete settles a session against an ascending manifest of
//     (number, token) pairs. Only the listed segments are concatenated, in
//     manifest order. Every token must equal the token of the segment
//     currently stored for that number.
//   - A successful Complete publishes a new immutable version of the target
//     object and closes the session. Later PutSegment calls on that session
//     fail with ErrSessionClosed.
//
// Completion is idempotent and linearizable:
//
//   - The first structurally valid manifest submitted for a session is bound
//     to that session, even if settlement then fails. Re-submitting the same
//     manifest after repairing segments succeeds; submitting a different
//     manifest fails with ErrManifestConflict.
//   - A failed settlement never changes the published object bytes or
//     version, so the previous object stays readable while the uploader
//     repairs the session and retries.
//   - Resubmitting the manifest of an already completed session returns the
//     version that session published, even if a newer version has since been
//     published by another session. It never moves the object backwards.
//   - Complete and PutSegment for the same session are serialized by the
//     store lock. A concurrent pair therefore behaves exactly as if one of
//     the two operations had run first.
//
// The store is intended for a single process. It keeps all state in memory.
package objectstore

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"sync"
)

// Sentinel errors returned by the store. Concrete failures may wrap extra
// detail; use errors.Is to test for these values.
var (
	// ErrSessionNotFound is returned for an unknown session identifier.
	ErrSessionNotFound = errors.New("objectstore: upload session not found")
	// ErrSessionClosed is returned when a segment is written to a session
	// that has already completed successfully.
	ErrSessionClosed = errors.New("objectstore: upload session is already completed")
	// ErrInvalidSegment is returned when a segment number is not positive.
	ErrInvalidSegment = errors.New("objectstore: segment number must be positive")
	// ErrManifestInvalid is returned when a completion manifest is empty or
	// does not list strictly increasing positive segment numbers.
	ErrManifestInvalid = errors.New("objectstore: manifest must list strictly increasing positive segment numbers")
	// ErrSegmentMismatch is returned when a manifest references a segment
	// that was never uploaded, or whose content-derived token differs from
	// the token recorded in the manifest.
	ErrSegmentMismatch = errors.New("objectstore: manifest segment does not match the uploaded content")
	// ErrManifestConflict is returned when a session that already accepted a
	// completion manifest is given a different one.
	ErrManifestConflict = errors.New("objectstore: session is bound to a different completion manifest")
)

// Token is the content-derived identity of a segment. Tokens are computed as
// the hexadecimal SHA-256 digest of the segment bytes, so two segments have
// the same token if and only if their contents are identical.
type Token string

// ContentToken returns the token that PutSegment would return for content.
// It lets clients build or verify manifests without uploading.
func ContentToken(content []byte) Token {
	sum := sha256.Sum256(content)
	return Token(hex.EncodeToString(sum[:]))
}

// SessionID identifies an upload session.
type SessionID string

// SegmentRef is one entry of a completion manifest: a segment number and the
// token that was returned when that segment content was last uploaded.
type SegmentRef struct {
	Number int
	Token  Token
}

// SegmentMismatchError describes the first manifest entry that failed to
// settle. It matches ErrSegmentMismatch under errors.Is.
type SegmentMismatchError struct {
	Number  int
	Want    Token
	Got     Token
	Missing bool
}

func (e *SegmentMismatchError) Error() string {
	if e.Missing {
		return fmt.Sprintf("objectstore: segment %d has not been uploaded", e.Number)
	}
	return fmt.Sprintf("objectstore: segment %d token mismatch: want %s, got %s", e.Number, e.Want, e.Got)
}

// Is makes errors.Is(err, ErrSegmentMismatch) report true.
func (e *SegmentMismatchError) Is(target error) bool {
	return target == ErrSegmentMismatch
}

// SessionInfo is a read-only snapshot of a session, mainly useful for
// diagnostics and tests.
type SessionInfo struct {
	ID SessionID
	// Key is the object key the session publishes to.
	Key string
	// Closed reports whether the session completed successfully.
	Closed bool
	// Manifest is the bound completion manifest, if one was accepted.
	Manifest []SegmentRef
	// Version is the object version published by this session; only
	// meaningful when Closed is true.
	Version int64
	// SegmentNumbers lists, in ascending order, the segment numbers currently
	// stored in the session. It is empty after the session is closed, because
	// completed sessions release their segment buffers.
	SegmentNumbers []int
}

// Store is the in-memory object service. The zero value is ready to use.
// A Store is safe for concurrent use by multiple goroutines.
type Store struct {
	mu            sync.Mutex
	nextSessionID uint64
	sessions      map[SessionID]*session
	objects       map[string]object
}

type session struct {
	key         string
	segments    map[int][]byte
	hasManifest bool
	manifest    []SegmentRef
	closed      bool
	version     int64
}

type object struct {
	data    []byte
	version int64
}

// New returns an empty Store.
func New() *Store {
	s := &Store{}
	s.init()
	return s
}

// init lazily creates internal maps so that the zero Store also works.
func (s *Store) init() {
	if s.sessions == nil {
		s.sessions = make(map[SessionID]*session)
	}
	if s.objects == nil {
		s.objects = make(map[string]object)
	}
}

// CreateSession starts an upload session that will publish to key. Multiple
// sessions may target the same key concurrently; each successful completion
// appears as a new version of the object.
func (s *Store) CreateSession(key string) SessionID {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.init()

	s.nextSessionID++
	id := SessionID(fmt.Sprintf("s-%d", s.nextSessionID))
	s.sessions[id] = &session{
		key:      key,
		segments: make(map[int][]byte),
	}
	return id
}

// PutSegment stores content for segment number in the session, replacing any
// content previously stored under that number in this session. Segments may
// be written in any order. It returns the segment's content-derived token.
//
// A completed session rejects every write with ErrSessionClosed, including a
// rewrite of a segment whose content has not changed.
func (s *Store) PutSegment(id SessionID, number int, content []byte) (Token, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	sess, ok := s.sessions[id]
	if !ok {
		return "", ErrSessionNotFound
	}
	if sess.closed {
		return "", ErrSessionClosed
	}
	if number <= 0 {
		return "", ErrInvalidSegment
	}

	data := append([]byte(nil), content...)
	sess.segments[number] = data
	return ContentToken(data), nil
}

// Complete settles the session against manifest and, on success, publishes
// the concatenation of the listed segments as a new object version. It
// returns the version this session published.
//
// The manifest must be non-empty and list positive segment numbers in
// strictly increasing order. Only the listed segments are concatenated;
// uploaded segments omitted from the manifest are ignored.
//
// If a segment is missing or its token differs from the manifest, the method
// returns a *SegmentMismatchError wrapping ErrSegmentMismatch. The published
// object is left untouched and the session stays open so the uploader can
// write the missing content and retry with the same manifest. The first
// valid-shaped manifest is bound to the session; any later call with a
// different manifest fails with ErrManifestConflict.
//
// Repeating a successful completion with the same manifest returns the
// version that was published by that session and does not republish or move
// the object backwards, even if a newer version exists.
func (s *Store) Complete(id SessionID, manifest []SegmentRef) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	sess, ok := s.sessions[id]
	if !ok {
		return 0, ErrSessionNotFound
	}
	if err := validateManifest(manifest); err != nil {
		return 0, err
	}

	if sess.hasManifest {
		if !sameManifest(sess.manifest, manifest) {
			return 0, ErrManifestConflict
		}
	} else {
		sess.hasManifest = true
		sess.manifest = append([]SegmentRef(nil), manifest...)
	}

	// A completed session only ever answers with its own published version.
	if sess.closed {
		return sess.version, nil
	}

	contents := make([][]byte, len(manifest))
	total := 0
	for i, ref := range manifest {
		data, ok := sess.segments[ref.Number]
		if !ok {
			return 0, &SegmentMismatchError{Number: ref.Number, Want: ref.Token, Missing: true}
		}
		got := ContentToken(data)
		if got != ref.Token {
			return 0, &SegmentMismatchError{Number: ref.Number, Want: ref.Token, Got: got}
		}
		contents[i] = data
		total += len(data)
	}

	data := make([]byte, 0, total)
	for _, part := range contents {
		data = append(data, part...)
	}

	version := s.objects[sess.key].version + 1
	s.objects[sess.key] = object{data: data, version: version}

	sess.closed = true
	sess.version = version
	sess.segments = nil

	return version, nil
}

// Get returns a copy of the current bytes and version of the object stored at
// key. ok is false when no version has been published for the key.
func (s *Store) Get(key string) (content []byte, version int64, ok bool) {
	s.mu.Lock()
	defer s.mu.Unlock()

	obj, ok := s.objects[key]
	if !ok {
		return nil, 0, false
	}
	return append([]byte(nil), obj.data...), obj.version, true
}

// Inspect returns a snapshot of the session with the given identifier. ok is
// false when the session does not exist.
func (s *Store) Inspect(id SessionID) (SessionInfo, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()

	sess, ok := s.sessions[id]
	if !ok {
		return SessionInfo{}, false
	}

	numbers := make([]int, 0, len(sess.segments))
	for number := range sess.segments {
		numbers = append(numbers, number)
	}
	sort.Ints(numbers)

	return SessionInfo{
		ID:             id,
		Key:            sess.key,
		Closed:         sess.closed,
		Manifest:       append([]SegmentRef(nil), sess.manifest...),
		Version:        sess.version,
		SegmentNumbers: numbers,
	}, true
}

func validateManifest(manifest []SegmentRef) error {
	if len(manifest) == 0 {
		return ErrManifestInvalid
	}
	prev := 0
	for _, ref := range manifest {
		if ref.Number <= prev {
			return ErrManifestInvalid
		}
		prev = ref.Number
	}
	return nil
}

func sameManifest(a, b []SegmentRef) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
