package objectstore_test

import (
	"bytes"
	"errors"
	"sync"
	"testing"

	"example.com/objectstore"
)

// expectToken fails the test when got differs from the token of want.
func expectToken(t *testing.T, got objectstore.Token, want []byte) {
	t.Helper()
	if wantTok := objectstore.ContentToken(want); got != wantTok {
		t.Fatalf("token = %q, want %q", got, wantTok)
	}
}

func mustPut(t *testing.T, s *objectstore.Store, id objectstore.SessionID, number int, content string) objectstore.Token {
	t.Helper()
	tok, err := s.PutSegment(id, number, []byte(content))
	if err != nil {
		t.Fatalf("PutSegment(%d, %q): %v", number, content, err)
	}
	return tok
}

func mustGet(t *testing.T, s *objectstore.Store, key string) (string, int64) {
	t.Helper()
	data, version, ok := s.Get(key)
	if !ok {
		t.Fatalf("Get(%q): object not found", key)
	}
	return string(data), version
}

// Normal path: segments arrive out of order, the manifest lists them in
// ascending order, and the completed object concatenates them in manifest
// order.
func TestOutOfOrderUploadThenAscendingManifest(t *testing.T) {
	s := objectstore.New()
	id := s.CreateSession("bucket/key")

	tok3 := mustPut(t, s, id, 3, "ccc")
	tok1 := mustPut(t, s, id, 1, "a")
	tok2 := mustPut(t, s, id, 2, "bb")

	if _, _, ok := s.Get("bucket/key"); ok {
		t.Fatal("object readable before completion")
	}

	version, err := s.Complete(id, []objectstore.SegmentRef{
		{Number: 1, Token: tok1},
		{Number: 2, Token: tok2},
		{Number: 3, Token: tok3},
	})
	if err != nil {
		t.Fatalf("Complete: %v", err)
	}
	if version != 1 {
		t.Fatalf("version = %d, want 1", version)
	}

	got, gotVersion := mustGet(t, s, "bucket/key")
	if got != "abbccc" {
		t.Fatalf("object = %q, want %q", got, "abbccc")
	}
	if gotVersion != 1 {
		t.Fatalf("object version = %d, want 1", gotVersion)
	}

	info, ok := s.Inspect(id)
	if !ok || !info.Closed || info.Version != 1 {
		t.Fatalf("Inspect = %+v, ok=%v; want closed session at version 1", info, ok)
	}
	if len(info.SegmentNumbers) != 0 {
		t.Fatalf("closed session still holds segments %v", info.SegmentNumbers)
	}
}

// Tokens depend only on content: same bytes give the same token across
// sessions and segment numbers, different bytes give a different token.
func TestTokenIsDerivedFromContent(t *testing.T) {
	s := objectstore.New()
	a := s.CreateSession("k1")
	b := s.CreateSession("k2")

	tokA := mustPut(t, s, a, 1, "payload")
	tokB := mustPut(t, s, b, 7, "payload")
	tokC := mustPut(t, s, a, 2, "payload!")
	expectToken(t, tokA, []byte("payload"))
	if tokA != tokB {
		t.Fatalf("equal content produced different tokens: %q vs %q", tokA, tokB)
	}
	if tokA == tokC {
		t.Fatalf("different content produced the same token %q", tokA)
	}
}

// Rewriting a segment number replaces the session's content, and the returned
// token follows the new content.
func TestRewriteSegmentReplacesContent(t *testing.T) {
	s := objectstore.New()
	id := s.CreateSession("k")

	oldTok := mustPut(t, s, id, 1, "old")
	newTok := mustPut(t, s, id, 1, "new")
	if oldTok == newTok {
		t.Fatal("rewrite did not change the content token")
	}

	version, err := s.Complete(id, []objectstore.SegmentRef{{Number: 1, Token: newTok}})
	if err != nil {
		t.Fatalf("Complete after rewrite: %v", err)
	}
	got, gotVersion := mustGet(t, s, "k")
	if got != "new" || gotVersion != 1 || version != 1 {
		t.Fatalf("object = %q v%d, complete version %d; want %q v1", got, gotVersion, version, "new")
	}
}

// A manifest token that no longer matches the stored segment fails with a
// detailed mismatch error, and the session's bound manifest means the stale
// token cannot be replaced by a different manifest afterwards.
func TestStaleTokenMismatch(t *testing.T) {
	s := objectstore.New()
	id := s.CreateSession("k")

	oldTok := mustPut(t, s, id, 1, "old")
	newTok := mustPut(t, s, id, 1, "new")

	_, err := s.Complete(id, []objectstore.SegmentRef{{Number: 1, Token: oldTok}})
	if !errors.Is(err, objectstore.ErrSegmentMismatch) {
		t.Fatalf("Complete with stale token: err = %v, want ErrSegmentMismatch", err)
	}
	var mismatch *objectstore.SegmentMismatchError
	if !errors.As(err, &mismatch) {
		t.Fatalf("error %v is not *SegmentMismatchError", err)
	}
	if mismatch.Number != 1 || mismatch.Want != oldTok || mismatch.Got != newTok || mismatch.Missing {
		t.Fatalf("mismatch detail = %+v", mismatch)
	}

	if _, err := s.Complete(id, []objectstore.SegmentRef{{Number: 1, Token: newTok}}); !errors.Is(err, objectstore.ErrManifestConflict) {
		t.Fatalf("different manifest after bound attempt: err = %v, want ErrManifestConflict", err)
	}
	if _, _, ok := s.Get("k"); ok {
		t.Fatal("failed completion published an object")
	}
}

// Only segments named in the manifest are concatenated; uploaded segments
// that the manifest omits are left out.
func TestOnlyListedSegmentsArePublished(t *testing.T) {
	s := objectstore.New()
	id := s.CreateSession("k")

	tok1 := mustPut(t, s, id, 1, "A")
	mustPut(t, s, id, 2, "IGNORED")
	tok3 := mustPut(t, s, id, 3, "C")

	if _, err := s.Complete(id, []objectstore.SegmentRef{
		{Number: 1, Token: tok1},
		{Number: 3, Token: tok3},
	}); err != nil {
		t.Fatalf("Complete: %v", err)
	}

	got, _ := mustGet(t, s, "k")
	if got != "AC" {
		t.Fatalf("object = %q, want %q", got, "AC")
	}
}

// A manifest must be non-empty and strictly increasing, and a rejected
// manifest neither publishes nor binds the session.
func TestManifestValidation(t *testing.T) {
	cases := []struct {
		name     string
		manifest []objectstore.SegmentRef
	}{
		{"empty", nil},
		{"descending", []objectstore.SegmentRef{{Number: 2}, {Number: 1}}},
		{"duplicate", []objectstore.SegmentRef{{Number: 1}, {Number: 1}}},
		{"zero", []objectstore.SegmentRef{{Number: 0}}},
		{"negative", []objectstore.SegmentRef{{Number: -1}}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := objectstore.New()
			id := s.CreateSession("k")
			mustPut(t, s, id, 1, "a")

			_, err := s.Complete(id, tc.manifest)
			if !errors.Is(err, objectstore.ErrManifestInvalid) {
				t.Fatalf("Complete: err = %v, want ErrManifestInvalid", err)
			}
			if _, _, ok := s.Get("k"); ok {
				t.Fatal("invalid manifest published an object")
			}
			info, _ := s.Inspect(id)
			if info.Closed || len(info.Manifest) != 0 {
				t.Fatalf("invalid manifest changed session: %+v", info)
			}
			// The session is still usable with a valid manifest.
			tok := mustPut(t, s, id, 1, "a")
			if _, err := s.Complete(id, []objectstore.SegmentRef{{Number: 1, Token: tok}}); err != nil {
				t.Fatalf("Complete after invalid manifest: %v", err)
			}
		})
	}
}

// A mismatched completion leaves the previously published object untouched,
// and the uploader can repair the segment and retry with the same manifest.
func TestMismatchKeepsOldObjectAndRetrySucceeds(t *testing.T) {
	s := objectstore.New()

	first := s.CreateSession("k")
	tok1 := mustPut(t, s, first, 1, "hello ")
	tok2 := mustPut(t, s, first, 2, "world")
	if _, err := s.Complete(first, []objectstore.SegmentRef{
		{Number: 1, Token: tok1},
		{Number: 2, Token: tok2},
	}); err != nil {
		t.Fatalf("first Complete: %v", err)
	}

	second := s.CreateSession("k")
	goodTok1 := mustPut(t, s, second, 1, "HELLO ")
	// The manifest records the token of the intended content, but segment 2
	// currently holds different bytes.
	wantTok2 := objectstore.ContentToken([]byte("there!"))
	if _, err := s.PutSegment(second, 2, []byte("there")); err != nil {
		t.Fatalf("PutSegment: %v", err)
	}
	manifest := []objectstore.SegmentRef{
		{Number: 1, Token: goodTok1},
		{Number: 2, Token: wantTok2},
	}
	_, err := s.Complete(second, manifest)
	if !errors.Is(err, objectstore.ErrSegmentMismatch) {
		t.Fatalf("Complete with wrong token: err = %v, want ErrSegmentMismatch", err)
	}

	old, version := mustGet(t, s, "k")
	if old != "hello world" || version != 1 {
		t.Fatalf("after failed completion object = %q v%d, want %q v1", old, version, "hello world")
	}
	info, _ := s.Inspect(second)
	if info.Closed {
		t.Fatal("failed completion closed the session")
	}

	// Repair the segment so it matches the bound manifest and retry.
	if _, err := s.PutSegment(second, 2, []byte("there!")); err != nil {
		t.Fatalf("repair PutSegment: %v", err)
	}
	v2, err := s.Complete(second, manifest)
	if err != nil {
		t.Fatalf("retry Complete: %v", err)
	}
	got, gotVersion := mustGet(t, s, "k")
	if got != "HELLO there!" || gotVersion != 2 || v2 != 2 {
		t.Fatalf("after retry object = %q v%d complete=%d", got, gotVersion, v2)
	}
}

// The first valid-shaped manifest is bound to the session: after a failed
// settlement the same manifest may be retried, a different one is a conflict.
func TestSessionBindsFirstManifest(t *testing.T) {
	s := objectstore.New()
	id := s.CreateSession("k")

	tok1 := mustPut(t, s, id, 1, "a")
	missingTok := objectstore.ContentToken([]byte("b"))
	first := []objectstore.SegmentRef{
		{Number: 1, Token: tok1},
		{Number: 2, Token: missingTok},
	}
	if _, err := s.Complete(id, first); !errors.Is(err, objectstore.ErrSegmentMismatch) {
		t.Fatalf("first Complete: err = %v, want ErrSegmentMismatch", err)
	}

	// A repaired retry with the same manifest succeeds.
	mustPut(t, s, id, 2, "b")
	if _, err := s.Complete(id, first); err != nil {
		t.Fatalf("retry with bound manifest: %v", err)
	}

	// Any different manifest is rejected, even though the session is closed.
	other := []objectstore.SegmentRef{{Number: 1, Token: tok1}}
	if _, err := s.Complete(id, other); !errors.Is(err, objectstore.ErrManifestConflict) {
		t.Fatalf("different manifest on closed session: err = %v, want ErrManifestConflict", err)
	}

	// A session that failed before binding (invalid manifest) has no conflict.
	s2 := objectstore.New()
	id2 := s2.CreateSession("k2")
	t2 := mustPut(t, s2, id2, 1, "x")
	if _, err := s2.Complete(id2, nil); !errors.Is(err, objectstore.ErrManifestInvalid) {
		t.Fatalf("empty manifest: err = %v, want ErrManifestInvalid", err)
	}
	if _, err := s2.Complete(id2, []objectstore.SegmentRef{{Number: 1, Token: t2}}); err != nil {
		t.Fatalf("Complete after invalid manifest: %v", err)
	}
}

// Resending an old session's manifest after a newer version was published
// returns that session's own version and leaves the object pointing at the
// newer version.
func TestReplayAfterNewerVersionReturnsOwnVersion(t *testing.T) {
	s := objectstore.New()
	key := "k"

	older := s.CreateSession(key)
	ot := mustPut(t, s, older, 1, "old")
	oldManifest := []objectstore.SegmentRef{{Number: 1, Token: ot}}
	v1, err := s.Complete(older, oldManifest)
	if err != nil || v1 != 1 {
		t.Fatalf("older Complete = %d, %v; want 1", v1, err)
	}

	newer := s.CreateSession(key)
	nt := mustPut(t, s, newer, 1, "new")
	newManifest := []objectstore.SegmentRef{{Number: 1, Token: nt}}
	v2, err := s.Complete(newer, newManifest)
	if err != nil || v2 != 2 {
		t.Fatalf("newer Complete = %d, %v; want 2", v2, err)
	}

	got, _ := mustGet(t, s, key)
	if got != "new" {
		t.Fatalf("object = %q, want %q", got, "new")
	}

	replayed, err := s.Complete(older, oldManifest)
	if err != nil {
		t.Fatalf("replay older Complete: %v", err)
	}
	if replayed != 1 {
		t.Fatalf("replay returned version %d, want 1", replayed)
	}
	got, version := mustGet(t, s, key)
	if got != "new" || version != 2 {
		t.Fatalf("replay moved object to %q v%d, want %q v2", got, version, "new")
	}

	// A third version does not change the answer for the older session.
	third := s.CreateSession(key)
	tt := mustPut(t, s, third, 1, "newest")
	if v3, err := s.Complete(third, []objectstore.SegmentRef{{Number: 1, Token: tt}}); err != nil || v3 != 3 {
		t.Fatalf("third Complete = %d, %v; want 3", v3, err)
	}
	if replayed, err = s.Complete(older, oldManifest); err != nil || replayed != 1 {
		t.Fatalf("second replay = %d, %v; want 1", replayed, err)
	}
	got, version = mustGet(t, s, key)
	if got != "newest" || version != 3 {
		t.Fatalf("object after third = %q v%d, want %q v3", got, version, "newest")
	}
}

// After successful completion the session is sealed: new writes are rejected.
func TestCompletedSessionRejectsWrites(t *testing.T) {
	s := objectstore.New()
	id := s.CreateSession("k")
	tok := mustPut(t, s, id, 1, "a")
	manifest := []objectstore.SegmentRef{{Number: 1, Token: tok}}
	if _, err := s.Complete(id, manifest); err != nil {
		t.Fatalf("Complete: %v", err)
	}

	if _, err := s.PutSegment(id, 2, []byte("late")); !errors.Is(err, objectstore.ErrSessionClosed) {
		t.Fatalf("PutSegment on closed session: err = %v, want ErrSessionClosed", err)
	}
	if _, err := s.PutSegment(id, 1, []byte("a")); !errors.Is(err, objectstore.ErrSessionClosed) {
		t.Fatalf("identical rewrite on closed session: err = %v, want ErrSessionClosed", err)
	}
	got, version := mustGet(t, s, "k")
	if got != "a" || version != 1 {
		t.Fatalf("rejected writes changed object: %q v%d", got, version)
	}
}

func TestUnknownSessionAndInvalidSegment(t *testing.T) {
	s := objectstore.New()

	if _, err := s.PutSegment("missing", 1, []byte("x")); !errors.Is(err, objectstore.ErrSessionNotFound) {
		t.Fatalf("PutSegment unknown: err = %v, want ErrSessionNotFound", err)
	}
	if _, err := s.Complete("missing", []objectstore.SegmentRef{{Number: 1}}); !errors.Is(err, objectstore.ErrSessionNotFound) {
		t.Fatalf("Complete unknown: err = %v, want ErrSessionNotFound", err)
	}

	id := s.CreateSession("k")
	for _, number := range []int{0, -3} {
		if _, err := s.PutSegment(id, number, []byte("x")); !errors.Is(err, objectstore.ErrInvalidSegment) {
			t.Fatalf("PutSegment(%d): err = %v, want ErrInvalidSegment", number, err)
		}
	}
	info, _ := s.Inspect(id)
	if len(info.SegmentNumbers) != 0 {
		t.Fatalf("invalid segment numbers were stored: %v", info.SegmentNumbers)
	}
}

// A concurrent Complete and rewrite of the same segment must behave as if one
// of them ran first: either completion wins and the late write is rejected,
// or the write wins and completion fails without touching the object.
func TestCompleteAndPutSegmentConcurrent(t *testing.T) {
	for i := 0; i < 200; i++ {
		s := objectstore.New()
		id := s.CreateSession("k")
		tokA := mustPut(t, s, id, 1, "A")
		tokB := mustPut(t, s, id, 2, "B")
		manifest := []objectstore.SegmentRef{
			{Number: 1, Token: tokA},
			{Number: 2, Token: tokB},
		}

		var wg sync.WaitGroup
		wg.Add(2)
		start := make(chan struct{})
		var (
			putErr  error
			doneVer int64
			doneErr error
		)
		go func() {
			defer wg.Done()
			<-start
			_, putErr = s.PutSegment(id, 2, []byte("X"))
		}()
		go func() {
			defer wg.Done()
			<-start
			doneVer, doneErr = s.Complete(id, manifest)
		}()
		close(start)
		wg.Wait()

		switch {
		case doneErr == nil:
			// Completion ran first; the rewrite must have been rejected.
			if doneVer != 1 {
				t.Fatalf("iter %d: complete version = %d, want 1", i, doneVer)
			}
			if !errors.Is(putErr, objectstore.ErrSessionClosed) {
				t.Fatalf("iter %d: completion won but rewrite err = %v, want ErrSessionClosed", i, putErr)
			}
			got, version := mustGet(t, s, "k")
			if got != "AB" || version != 1 {
				t.Fatalf("iter %d: object = %q v%d, want AB v1", i, got, version)
			}
		case errors.Is(doneErr, objectstore.ErrSegmentMismatch):
			// The rewrite ran first; nothing was published.
			if putErr != nil {
				t.Fatalf("iter %d: rewrite err = %v after winning race", i, putErr)
			}
			if _, _, ok := s.Get("k"); ok {
				t.Fatalf("iter %d: failed completion published an object", i)
			}
			// The uploader can repair and retry with the same manifest.
			if _, err := s.PutSegment(id, 2, []byte("B")); err != nil {
				t.Fatalf("iter %d: repair PutSegment: %v", i, err)
			}
			v, err := s.Complete(id, manifest)
			if err != nil || v != 1 {
				t.Fatalf("iter %d: retry Complete = %d, %v; want 1", i, v, err)
			}
			got, version := mustGet(t, s, "k")
			if got != "AB" || version != 1 {
				t.Fatalf("iter %d: object = %q v%d, want AB v1", i, got, version)
			}
		default:
			t.Fatalf("iter %d: Complete err = %v, want nil or ErrSegmentMismatch", i, doneErr)
		}
	}
}

// Two sessions targeting the same key publish distinct, increasing versions.
func TestConcurrentSessionsPublishSequentialVersions(t *testing.T) {
	s := objectstore.New()
	a := s.CreateSession("k")
	b := s.CreateSession("k")
	ta := mustPut(t, s, a, 1, "from-a")
	tb := mustPut(t, s, b, 1, "from-b")
	ma := []objectstore.SegmentRef{{Number: 1, Token: ta}}
	mb := []objectstore.SegmentRef{{Number: 1, Token: tb}}

	var (
		wg     sync.WaitGroup
		va, vb int64
		ea, eb error
	)
	wg.Add(2)
	start := make(chan struct{})
	go func() {
		defer wg.Done()
		<-start
		va, ea = s.Complete(a, ma)
	}()
	go func() {
		defer wg.Done()
		<-start
		vb, eb = s.Complete(b, mb)
	}()
	close(start)
	wg.Wait()

	if ea != nil || eb != nil {
		t.Fatalf("complete errors: %v, %v", ea, eb)
	}
	if va == vb || va > 2 || vb > 2 || va < 1 || vb < 1 {
		t.Fatalf("versions = %d, %d; want distinct values from {1,2}", va, vb)
	}
	data, version, ok := s.Get("k")
	if !ok || version != 2 {
		t.Fatalf("final object version = %d ok=%v, want 2", version, ok)
	}
	want := "from-a"
	if vb == 2 {
		want = "from-b"
	}
	if string(data) != want {
		t.Fatalf("final object = %q, want %q", data, want)
	}

	// Replaying each manifest returns that session's version and never
	// regresses the object.
	if v, err := s.Complete(a, ma); err != nil || v != va {
		t.Fatalf("replay a = %d, %v; want %d", v, err, va)
	}
	if v, err := s.Complete(b, mb); err != nil || v != vb {
		t.Fatalf("replay b = %d, %v; want %d", v, err, vb)
	}
	if data, version, _ := s.Get("k"); version != 2 || string(data) != want {
		t.Fatalf("replay changed object: %q v%d", data, version)
	}
}

// Get returns a defensive copy so callers cannot mutate stored bytes.
func TestGetReturnsCopy(t *testing.T) {
	s := objectstore.New()
	id := s.CreateSession("k")
	tok := mustPut(t, s, id, 1, "abc")
	if _, err := s.Complete(id, []objectstore.SegmentRef{{Number: 1, Token: tok}}); err != nil {
		t.Fatalf("Complete: %v", err)
	}
	first, _, _ := s.Get("k")
	first[0] = 'X'
	second, _, _ := s.Get("k")
	if !bytes.Equal(second, []byte("abc")) {
		t.Fatalf("stored bytes mutated through Get: %q", second)
	}
}
