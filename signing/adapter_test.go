package signing_test

import (
	"bytes"
	"errors"
	"fmt"
	"sort"
	"sync"
	"testing"

	"example.com/feedback/signingadapter/signing"
)

const (
	keyA signing.KeyID = "key-A-2026"
	keyB signing.KeyID = "key-B-2026"
	keyX signing.KeyID = "key-X-unapproved"
)

var errCanceled = errors.New("client canceled the document")

func newAdapter(t *testing.T, slots int) (*signing.Adapter, *fakeDevice) {
	t.Helper()
	dev := newFakeDevice(slots)
	return signing.New(dev, keyA, keyB), dev
}

func mustSubmit(t *testing.T, s *signing.Session, fragments ...string) {
	t.Helper()
	for _, fragment := range fragments {
		if err := s.Submit([]byte(fragment)); err != nil {
			t.Fatalf("Submit(%q) on %s: unexpected error: %v", fragment, s.Document(), err)
		}
	}
}

// TestTwoDocumentsSignConcurrentlyOnTwoSlots is the happy path: two documents
// occupy both slots at once, each signing only its own bytes under its own key.
func TestTwoDocumentsSignConcurrentlyOnTwoSlots(t *testing.T) {
	adapter, dev := newAdapter(t, 2)

	// Interleave submissions the way two clients would: alternate fragments.
	docA, err := adapter.Begin("doc-A", keyA)
	if err != nil {
		t.Fatalf("Begin doc-A: %v", err)
	}
	docB, err := adapter.Begin("doc-B", keyB)
	if err != nil {
		t.Fatalf("Begin doc-B: %v", err)
	}
	if docA.Slot() == docB.Slot() {
		t.Fatalf("both documents hold slot %d; slots must be exclusive", docA.Slot())
	}
	if got := adapter.FreeSlots(); got != 0 {
		t.Fatalf("FreeSlots = %d after two Begin calls on a 2-slot device, want 0", got)
	}

	mustSubmit(t, docA, "A-cover")
	mustSubmit(t, docB, "B-cover")
	mustSubmit(t, docA, "A-body-1")
	mustSubmit(t, docB, "B-body-1")
	mustSubmit(t, docA, "A-body-2")

	sigA, err := docA.Sign()
	if err != nil {
		t.Fatalf("sign doc-A: %v", err)
	}
	sigB, err := docB.Sign()
	if err != nil {
		t.Fatalf("sign doc-B: %v", err)
	}

	if bytes.Equal(sigA, sigB) {
		t.Fatal("documents with different keys and bytes produced identical signatures")
	}

	wantA := signatureFor(keyA, [][]byte{[]byte("A-cover"), []byte("A-body-1"), []byte("A-body-2")})
	wantB := signatureFor(keyB, [][]byte{[]byte("B-cover"), []byte("B-body-1")})
	if !bytes.Equal(sigA, wantA) {
		t.Fatalf("doc-A signature does not cover its own fragments in order:\n got %x\nwant %x", sigA, wantA)
	}
	if !bytes.Equal(sigB, wantB) {
		t.Fatalf("doc-B signature does not cover its own fragments in order:\n got %x\nwant %x", sigB, wantB)
	}

	// The device record for each finalized authorization must contain exactly
	// the fragments and key of that session, never the interleaved neighbour's.
	recA, ok := dev.signedRecordFor(docA.Auth())
	if !ok {
		t.Fatal("device has no finalized record for doc-A")
	}
	recB, ok := dev.signedRecordFor(docB.Auth())
	if !ok {
		t.Fatal("device has no finalized record for doc-B")
	}
	assertRecord(t, "doc-A", recA, keyA, docA, []string{"A-cover", "A-body-1", "A-body-2"})
	assertRecord(t, "doc-B", recB, keyB, docB, []string{"B-cover", "B-body-1"})

	if recA.Auth == recB.Auth {
		t.Fatalf("both documents finalized through the same authorization %d", recA.Auth)
	}
	if got := adapter.FreeSlots(); got != 2 {
		t.Fatalf("FreeSlots = %d after both documents signed, want 2", got)
	}
	if len(dev.liveOccupants()) != 0 {
		t.Fatalf("device still holds %v after both documents signed", dev.liveOccupants())
	}
}

// TestRejectsThirdDocumentWhileBothSlotsBusy pins the capacity boundary: the
// refusal happens before any device authorization is created.
func TestRejectsThirdDocumentWhileBothSlotsBusy(t *testing.T) {
	adapter, dev := newAdapter(t, 2)

	first, err := adapter.Begin("doc-1", keyA)
	if err != nil {
		t.Fatalf("Begin doc-1: %v", err)
	}
	second, err := adapter.Begin("doc-2", keyB)
	if err != nil {
		t.Fatalf("Begin doc-2: %v", err)
	}
	beginsBefore := dev.beginCount()

	if _, err := adapter.Begin("doc-3", keyA); !errors.Is(err, signing.ErrNoFreeSlot) {
		t.Fatalf("Begin doc-3 with both slots busy = %v, want ErrNoFreeSlot", err)
	}
	if got := dev.beginCount(); got != beginsBefore {
		t.Fatalf("device Begin called %d times for a rejected request, want %d", got, beginsBefore)
	}
	if got := adapter.FreeSlots(); got != 0 {
		t.Fatalf("FreeSlots = %d after rejected Begin, want 0", got)
	}

	// Freeing exactly one slot must let exactly one more document in.
	if err := first.Close(); err != nil {
		t.Fatalf("Close doc-1: %v", err)
	}
	if got := adapter.FreeSlots(); got != 1 {
		t.Fatalf("FreeSlots = %d after closing doc-1, want 1", got)
	}
	third, err := adapter.Begin("doc-3", keyA)
	if err != nil {
		t.Fatalf("Begin doc-3 after a slot was freed: %v", err)
	}
	if third.Slot() == second.Slot() {
		t.Fatalf("doc-3 took slot %d still held by doc-2", third.Slot())
	}
	if _, err := adapter.Begin("doc-4", keyA); !errors.Is(err, signing.ErrNoFreeSlot) {
		t.Fatalf("Begin doc-4 = %v, want ErrNoFreeSlot", err)
	}
}

// TestRejectsUnapprovedKeyWithoutConsumingCapacity checks the second
// cross-cutting constraint: only allow-listed keys may sign, and a rejection
// must not burn one of the two slots.
func TestRejectsUnapprovedKeyWithoutConsumingCapacity(t *testing.T) {
	adapter, dev := newAdapter(t, 2)

	if _, err := adapter.Begin("doc-X", keyX); !errors.Is(err, signing.ErrKeyNotAllowed) {
		t.Fatalf("Begin with unapproved key = %v, want ErrKeyNotAllowed", err)
	}
	if got := dev.beginCount(); got != 0 {
		t.Fatalf("device Begin called %d times for a rejected key, want 0", got)
	}
	if got := adapter.FreeSlots(); got != 2 {
		t.Fatalf("FreeSlots = %d after rejected key, want 2 (capacity untouched)", got)
	}

	if _, err := adapter.Begin("doc-A", keyA); err != nil {
		t.Fatalf("Begin doc-A with approved key: %v", err)
	}
	if _, err := adapter.Begin("doc-B", keyB); err != nil {
		t.Fatalf("Begin doc-B with approved key: %v", err)
	}
	if _, err := adapter.Begin("", keyA); !errors.Is(err, signing.ErrEmptyDocument) {
		t.Fatalf("Begin with empty document = %v, want ErrEmptyDocument", err)
	}
}

// TestSlotFreedOnlyAfterSuccessfulCleanup covers the cancellation rule: a
// failed Release keeps the slot quarantined, a retried Close frees it, and only
// then may the next document get that slot.
func TestSlotFreedOnlyAfterSuccessfulCleanup(t *testing.T) {
	adapter, dev := newAdapter(t, 1)

	doc, err := adapter.Begin("doc-1", keyA)
	if err != nil {
		t.Fatalf("Begin doc-1: %v", err)
	}
	mustSubmit(t, doc, "partial")

	releaseShouldFail := true
	dev.releaseErr = func(signing.AuthID) error {
		if releaseShouldFail {
			return errors.New("fake: mechanism still ejecting")
		}
		return nil
	}

	if err := doc.Abort(errCanceled); err == nil {
		t.Fatal("Abort with a failing device Release returned nil")
	}
	if got := adapter.FreeSlots(); got != 0 {
		t.Fatalf("FreeSlots = %d after failed cleanup, want 0 (slot must stay quarantined)", got)
	}
	if _, err := adapter.Begin("doc-2", keyB); !errors.Is(err, signing.ErrNoFreeSlot) {
		t.Fatalf("Begin doc-2 during pending cleanup = %v, want ErrNoFreeSlot", err)
	}
	if _, err := doc.Sign(); !errors.Is(err, signing.ErrCleanupPending) {
		t.Fatalf("Sign on a session with pending cleanup = %v, want ErrCleanupPending", err)
	}

	// The client keeps retrying the same close; once the device confirms, the
	// slot is ours to hand out again.
	releaseShouldFail = false
	if err := doc.Close(); err != nil {
		t.Fatalf("retried Close: %v", err)
	}
	if got := adapter.FreeSlots(); got != 1 {
		t.Fatalf("FreeSlots = %d after successful cleanup retry, want 1", got)
	}
	if got := dev.successfulReleases(); got != 1 {
		t.Fatalf("successful device releases = %d, want 1", got)
	}

	next, err := adapter.Begin("doc-2", keyB)
	if err != nil {
		t.Fatalf("Begin doc-2 after cleanup: %v", err)
	}
	if next.Slot() != doc.Slot() {
		t.Fatalf("doc-2 got slot %d, want the reclaimed slot %d", next.Slot(), doc.Slot())
	}
}

// TestDeviceResetExpiresSessionsAndLateCloseCannotStealSlot is the reset /
// late-notification scenario. A reset kills the old session's authorization;
// the old session's delayed close must be a harmless no-op even though the
// same physical slot now belongs to a new document with a different key.
func TestDeviceResetExpiresSessionsAndLateCloseCannotStealSlot(t *testing.T) {
	adapter, dev := newAdapter(t, 2)

	old, err := adapter.Begin("doc-old", keyA)
	if err != nil {
		t.Fatalf("Begin doc-old: %v", err)
	}
	mustSubmit(t, old, "old-cover", "old-body")
	oldSlot := old.Slot()

	if err := adapter.Reset(); err != nil {
		t.Fatalf("Reset: %v", err)
	}
	if got := adapter.FreeSlots(); got != 2 {
		t.Fatalf("FreeSlots = %d right after reset, want 2", got)
	}
	if !errors.Is(old.Cause(), signing.ErrSessionExpired) {
		t.Fatalf("old session cause = %v, want ErrSessionExpired", old.Cause())
	}
	if err := old.Submit([]byte("late")); !errors.Is(err, signing.ErrSessionExpired) {
		t.Fatalf("Submit on reset session = %v, want ErrSessionExpired", err)
	}
	if _, err := old.Sign(); !errors.Is(err, signing.ErrSessionExpired) {
		t.Fatalf("Sign on reset session = %v, want ErrSessionExpired", err)
	}
	if got := dev.successfulReleases(); got != 0 {
		t.Fatalf("device releases after reset = %d, want 0", got)
	}

	fresh, err := adapter.Begin("doc-new", keyB)
	if err != nil {
		t.Fatalf("Begin doc-new after reset: %v", err)
	}
	if fresh.Slot() != oldSlot {
		t.Fatalf("doc-new got slot %d, want the recycled slot %d", fresh.Slot(), oldSlot)
	}
	mustSubmit(t, fresh, "new-cover")
	occupantsBefore := dev.liveOccupants()

	// The stale cleanup notification arrives late.
	if err := old.Close(); err != nil {
		t.Fatalf("late Close of reset session returned error: %v", err)
	}
	if got := adapter.FreeSlots(); got != 1 {
		t.Fatalf("FreeSlots = %d after stale close, want 1 (new document still holds its slot)", got)
	}
	if got, want := dev.liveOccupants(), occupantsBefore; !sameOccupants(got, want) {
		t.Fatalf("device occupants changed by stale close: got %v want %v", got, want)
	}
	if !sameOccupants(dev.liveOccupants(), map[int]signing.AuthID{fresh.Slot(): fresh.Auth()}) {
		t.Fatalf("device occupants = %v, want only doc-new", dev.liveOccupants())
	}

	// And doc-new must still sign purely with key B and its own bytes.
	sig, err := fresh.Sign()
	if err != nil {
		t.Fatalf("doc-new Sign after stale close: %v", err)
	}
	want := signatureFor(keyB, [][]byte{[]byte("new-cover")})
	if !bytes.Equal(sig, want) {
		t.Fatalf("doc-new signature polluted after stale close:\n got %x\nwant %x", sig, want)
	}
	if got := dev.signedRecords(); len(got) != 1 || got[0].Key != keyB {
		t.Fatalf("device signed records = %+v, want one record under %s", got, keyB)
	}

	// Closing it again must remain a no-op.
	if err := old.Close(); err != nil {
		t.Fatalf("second late Close: %v", err)
	}
}

// TestManyClientInterleavingsKeepSignaturesIsolated runs several rounds of two
// clients submitting in opposite orders on the two slots; each round the
// finalized device record must match its own session exactly.
func TestManyClientInterleavingsKeepSignaturesIsolated(t *testing.T) {
	adapter, dev := newAdapter(t, 2)

	type expectation struct {
		session *signing.Session
		key     signing.KeyID
		frags   []string
	}
	var all []expectation

	for round := 0; round < 5; round++ {
		docA, err := adapter.Begin(fmt.Sprintf("A-%d", round), keyA)
		if err != nil {
			t.Fatalf("round %d Begin A: %v", round, err)
		}
		docB, err := adapter.Begin(fmt.Sprintf("B-%d", round), keyB)
		if err != nil {
			t.Fatalf("round %d Begin B: %v", round, err)
		}

		fragsA := []string{
			fmt.Sprintf("A-%d/0", round),
			fmt.Sprintf("A-%d/1", round),
			fmt.Sprintf("A-%d/2", round),
		}
		fragsB := []string{
			fmt.Sprintf("B-%d/0", round),
			fmt.Sprintf("B-%d/1", round),
		}

		// Round-robin submission, A first on even rounds and B first on odd.
		for i := 0; i < 3; i++ {
			order := []*signing.Session{docA, docB}
			fragOrder := map[*signing.Session][]string{docA: fragsA, docB: fragsB}
			if round%2 == 1 {
				order = []*signing.Session{docB, docA}
			}
			for _, session := range order {
				pending := fragOrder[session]
				if i < len(pending) {
					if err := session.Submit([]byte(pending[i])); err != nil {
						t.Fatalf("round %d submit %q: %v", round, pending[i], err)
					}
				}
			}
		}

		// Finalize in the opposite order from submission start.
		finalOrder := []*signing.Session{docB, docA}
		if round%2 == 1 {
			finalOrder = []*signing.Session{docA, docB}
		}
		for _, session := range finalOrder {
			if _, err := session.Sign(); err != nil {
				t.Fatalf("round %d Sign %s: %v", round, session.Document(), err)
			}
		}

		all = append(all,
			expectation{session: docA, key: keyA, frags: fragsA},
			expectation{session: docB, key: keyB, frags: fragsB},
		)
	}

	records := dev.signedRecords()
	if len(records) != len(all) {
		t.Fatalf("device finalized %d documents, want %d", len(records), len(all))
	}
	byAuth := make(map[signing.AuthID]SignedRecord, len(records))
	for _, rec := range records {
		byAuth[rec.Auth] = rec
	}
	for _, exp := range all {
		rec, ok := byAuth[exp.session.Auth()]
		if !ok {
			t.Fatalf("no device record for %s", exp.session.Document())
		}
		assertRecord(t, exp.session.Document(), rec, exp.key, exp.session, exp.frags)
	}
	if got := adapter.FreeSlots(); got != 2 {
		t.Fatalf("FreeSlots = %d after all rounds, want 2", got)
	}
}

// TestConcurrentClients checks the adapter's serialization under real
// goroutines: whichever document wins a slot, its signature is still computed
// from its own fragments and key only.
func TestConcurrentClients(t *testing.T) {
	adapter, dev := newAdapter(t, 2)

	const clients = 8
	var wg sync.WaitGroup
	var mu sync.Mutex
	type result struct {
		document string
		key      signing.KeyID
		frags    []string
		sig      []byte
	}
	var results []result

	for i := 0; i < clients; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			key := keyA
			if i%2 == 1 {
				key = keyB
			}
			document := fmt.Sprintf("doc-%d", i)

			// Poll for a slot; the two-slot device forces contention.
			var session *signing.Session
			for {
				s, err := adapter.Begin(document, key)
				if err == nil {
					session = s
					break
				}
				if !errors.Is(err, signing.ErrNoFreeSlot) {
					t.Errorf("%s Begin: %v", document, err)
					return
				}
			}

			frags := []string{document + "/0", document + "/1", document + "/2"}
			for _, fragment := range frags {
				if err := session.Submit([]byte(fragment)); err != nil {
					t.Errorf("%s Submit: %v", document, err)
					return
				}
			}
			sig, err := session.Sign()
			if err != nil {
				t.Errorf("%s Sign: %v", document, err)
				return
			}
			mu.Lock()
			results = append(results, result{document: document, key: key, frags: frags, sig: sig})
			mu.Unlock()
		}(i)
	}
	wg.Wait()

	if t.Failed() {
		return
	}
	if len(results) != clients {
		t.Fatalf("completed %d clients, want %d", len(results), clients)
	}
	for _, res := range results {
		var raws [][]byte
		for _, fragment := range res.frags {
			raws = append(raws, []byte(fragment))
		}
		want := signatureFor(res.key, raws)
		if !bytes.Equal(res.sig, want) {
			t.Fatalf("%s signature does not match its own key/fragments", res.document)
		}
	}
	if got := adapter.FreeSlots(); got != 2 {
		t.Fatalf("FreeSlots = %d after all concurrent clients, want 2", got)
	}

	// Every device record must be internally consistent: the key and bytes
	// belong to one and the same document.
	for _, rec := range dev.signedRecords() {
		if len(rec.Fragments) != 3 {
			t.Fatalf("device record %d has %d fragments, want 3", rec.Auth, len(rec.Fragments))
		}
		prefix := string(rec.Fragments[0])
		if len(prefix) < 2 || prefix[len(prefix)-2:] != "/0" {
			t.Fatalf("device record %d first fragment %q is malformed", rec.Auth, rec.Fragments[0])
		}
		document := prefix[:len(prefix)-2]
		for i, fragment := range rec.Fragments {
			wantFragment := fmt.Sprintf("%s/%d", document, i)
			if string(fragment) != wantFragment {
				t.Fatalf("device record %d fragment %d = %q, want %q (cross-session bleed)",
					rec.Auth, i, fragment, wantFragment)
			}
		}
		wantKey := keyA
		var n int
		if _, err := fmt.Sscanf(document, "doc-%d", &n); err == nil && n%2 == 1 {
			wantKey = keyB
		}
		if rec.Key != wantKey {
			t.Fatalf("device record %d for %s signed under %s, want %s", rec.Auth, document, rec.Key, wantKey)
		}
	}
}

// TestAppendFailureQuarantinesSlot makes a device-level append failure dirty
// the session so it can neither sign nor be recycled before cleanup.
func TestAppendFailureQuarantinesSlot(t *testing.T) {
	adapter, dev := newAdapter(t, 1)

	doc, err := adapter.Begin("doc-1", keyA)
	if err != nil {
		t.Fatalf("Begin doc-1: %v", err)
	}
	mustSubmit(t, doc, "first")

	appendFails := true
	appendCause := errors.New("fake: transport stalled")
	dev.appendErr = func(signing.AuthID, int) error {
		if appendFails {
			return appendCause
		}
		return nil
	}

	if err := doc.Submit([]byte("second")); err == nil {
		t.Fatal("Submit with a failing device returned nil")
	}
	if !errors.Is(doc.Cause(), appendCause) {
		t.Fatalf("session cause does not carry the device error: %v", doc.Cause())
	}
	if _, err := doc.Sign(); !errors.Is(err, signing.ErrCleanupPending) {
		t.Fatalf("Sign after append failure = %v, want ErrCleanupPending", err)
	}
	if _, err := adapter.Begin("doc-2", keyB); !errors.Is(err, signing.ErrNoFreeSlot) {
		t.Fatalf("Begin during append-failure quarantine = %v, want ErrNoFreeSlot", err)
	}
	if got := dev.successfulReleases(); got != 0 {
		t.Fatalf("device releases before cleanup = %d, want 0", got)
	}

	if err := doc.Close(); err != nil {
		t.Fatalf("Close after append failure: %v", err)
	}
	if got := adapter.FreeSlots(); got != 1 {
		t.Fatalf("FreeSlots = %d after cleanup, want 1", got)
	}
	next, err := adapter.Begin("doc-2", keyB)
	if err != nil {
		t.Fatalf("Begin doc-2 after cleanup: %v", err)
	}
	if next.Slot() != doc.Slot() {
		t.Fatalf("doc-2 got slot %d, want %d", next.Slot(), doc.Slot())
	}
}

// TestSignFailureQuarantinesSlotUntilCleanup covers a failure at the final
// step: the document never gets a signature, the slot must not be recycled
// until the device confirms cleanup, and the next document then signs with its
// own key and bytes only.
func TestSignFailureQuarantinesSlotUntilCleanup(t *testing.T) {
	adapter, dev := newAdapter(t, 1)

	doc, err := adapter.Begin("doc-1", keyA)
	if err != nil {
		t.Fatalf("Begin doc-1: %v", err)
	}
	mustSubmit(t, doc, "A-cover", "A-body")

	signFails := true
	signCause := errors.New("fake: signature unit busy")
	dev.signErr = func(signing.AuthID) error {
		if signFails {
			return signCause
		}
		return nil
	}

	if _, err := doc.Sign(); !errors.Is(err, signCause) {
		t.Fatalf("Sign with failing device = %v, want the device error", err)
	}
	if !errors.Is(doc.Cause(), signCause) {
		t.Fatalf("session cause = %v, want the device sign error", doc.Cause())
	}
	if got := dev.signedRecords(); len(got) != 0 {
		t.Fatalf("device produced %d signatures despite the failure, want 0", len(got))
	}
	if _, err := adapter.Begin("doc-2", keyB); !errors.Is(err, signing.ErrNoFreeSlot) {
		t.Fatalf("Begin during sign-failure quarantine = %v, want ErrNoFreeSlot", err)
	}

	if err := doc.Close(); err != nil {
		t.Fatalf("cleanup after sign failure: %v", err)
	}
	signFails = false
	if got := adapter.FreeSlots(); got != 1 {
		t.Fatalf("FreeSlots = %d after cleanup, want 1", got)
	}

	next, err := adapter.Begin("doc-2", keyB)
	if err != nil {
		t.Fatalf("Begin doc-2: %v", err)
	}
	if next.Slot() != doc.Slot() {
		t.Fatalf("doc-2 got slot %d, want the reclaimed slot %d", next.Slot(), doc.Slot())
	}
	mustSubmit(t, next, "B-cover")
	sig, err := next.Sign()
	if err != nil {
		t.Fatalf("doc-2 Sign: %v", err)
	}
	want := signatureFor(keyB, [][]byte{[]byte("B-cover")})
	if !bytes.Equal(sig, want) {
		t.Fatalf("doc-2 signature does not match key B and its own bytes:\n got %x\nwant %x", sig, want)
	}
	recs := dev.signedRecords()
	if len(recs) != 1 || recs[0].Key != keyB {
		t.Fatalf("device records = %+v, want exactly one record under %s", recs, keyB)
	}
	if recs[0].Fragments[0] != nil && string(recs[0].Fragments[0]) != "B-cover" {
		t.Fatalf("doc-2 device record carries foreign bytes: %q", recs[0].Fragments[0])
	}
}

// TestSignWithoutFragmentsIsRejected ensures an empty document never reaches
// the device as a signature request and keeps its slot for a retry.
func TestSignWithoutFragmentsIsRejected(t *testing.T) {
	adapter, dev := newAdapter(t, 1)

	doc, err := adapter.Begin("doc-1", keyA)
	if err != nil {
		t.Fatalf("Begin doc-1: %v", err)
	}
	if _, err := doc.Sign(); !errors.Is(err, signing.ErrNoFragments) {
		t.Fatalf("Sign without fragments = %v, want ErrNoFragments", err)
	}
	if len(dev.signedRecords()) != 0 {
		t.Fatal("device produced a signature for an empty document")
	}
	if got := adapter.FreeSlots(); got != 0 {
		t.Fatalf("FreeSlots = %d, want 0 (session may still submit and retry)", got)
	}

	mustSubmit(t, doc, "late-cover")
	if _, err := doc.Sign(); err != nil {
		t.Fatalf("Sign after submitting a fragment: %v", err)
	}
	if _, err := doc.Sign(); !errors.Is(err, signing.ErrSessionEnded) {
		t.Fatalf("second Sign = %v, want ErrSessionEnded", err)
	}
}

func assertRecord(t *testing.T, label string, rec SignedRecord, key signing.KeyID, session *signing.Session, frags []string) {
	t.Helper()
	if rec.Key != key {
		t.Fatalf("%s finalized under key %q, want %q", label, rec.Key, key)
	}
	if rec.Slot != session.Slot() {
		t.Fatalf("%s finalized on slot %d, want %d", label, rec.Slot, session.Slot())
	}
	if len(rec.Fragments) != len(frags) {
		t.Fatalf("%s device recorded %d fragments, want %d", label, len(rec.Fragments), len(frags))
	}
	for i, want := range frags {
		if string(rec.Fragments[i]) != want {
			t.Fatalf("%s device fragment %d = %q, want %q", label, i, rec.Fragments[i], want)
		}
	}
	wantSig := signatureFor(key, rec.Fragments)
	if !bytes.Equal(rec.Signature, wantSig) {
		t.Fatalf("%s signature does not cover its recorded fragments", label)
	}
}

func sameOccupants(a, b map[int]signing.AuthID) bool {
	if len(a) != len(b) {
		return false
	}
	var aKeys, bKeys []int
	for k := range a {
		aKeys = append(aKeys, k)
	}
	for k := range b {
		bKeys = append(bKeys, k)
	}
	sort.Ints(aKeys)
	sort.Ints(bKeys)
	for i := range aKeys {
		if aKeys[i] != bKeys[i] || a[aKeys[i]] != b[bKeys[i]] {
			return false
		}
	}
	return true
}
