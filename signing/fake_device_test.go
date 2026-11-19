package signing_test

import (
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"sync"

	"example.com/feedback/signingadapter/signing"
)

// Device-side errors used by the fake. They model failures a real signing
// device reports and are intentionally distinct from the adapter's own errors.
var (
	errSlotBusy       = errors.New("fake: slot already authorized")
	errSlotOutOfRange = errors.New("fake: slot out of range")
	errStaleEpoch     = errors.New("fake: stale device epoch")
	errUnknownAuth    = errors.New("fake: unknown or already cleaned authorization")
	errAuthExpired    = errors.New("fake: authorization expired by reset")
	errFinalized      = errors.New("fake: authorization already finalized")
	errBadSeq         = errors.New("fake: fragment sequence out of order")
	errNotOwner       = errors.New("fake: authorization does not own its slot")
	errNoFragments    = errors.New("fake: cannot finalize a document without fragments")
)

// fakeDevice is an in-memory signing device that records, per authorization,
// the slot, the key and every fragment in submission order. It enforces the
// device contract the adapter is supposed to respect:
//
//   - a slot is authorized for at most one live authorization at a time;
//   - authorizations from an older epoch are dead after Reset;
//   - fragments must arrive with strictly increasing sequence numbers;
//   - Release only frees the slot owned by exactly that authorization.
//
// Failures can be injected through the hook fields to model a device that
// reports errors. The adapter serializes all calls, so the fake needs no
// internal locking for correctness; a mutex is still used so tests can inspect
// recordings while a goroutine is running.
type fakeDevice struct {
	mu sync.Mutex

	slotCount int
	epoch     uint64
	nextAuth  signing.AuthID

	auths    map[signing.AuthID]*fakeAuth
	occupant map[int]signing.AuthID
	signed   []SignedRecord
	ops      []string

	beginCalls int
	releaseOK  int

	// Hooks; nil means "succeed". Each may be called multiple times.
	beginErr   func(slot int, key signing.KeyID, epoch uint64) error
	appendErr  func(auth signing.AuthID, seq int) error
	signErr    func(auth signing.AuthID) error
	releaseErr func(auth signing.AuthID) error
}

type fakeAuth struct {
	id        signing.AuthID
	slot      int
	key       signing.KeyID
	epoch     uint64
	nextSeq   int
	fragments [][]byte
	expired   bool
	signed    bool
	released  bool
}

// SignedRecord is what the device produced for one finalized authorization.
type SignedRecord struct {
	Auth      signing.AuthID
	Slot      int
	Key       signing.KeyID
	Fragments [][]byte
	Signature []byte
}

func newFakeDevice(slots int) *fakeDevice {
	return &fakeDevice{
		slotCount: slots,
		epoch:     1,
		auths:     make(map[signing.AuthID]*fakeAuth),
		occupant:  make(map[int]signing.AuthID),
	}
}

func (d *fakeDevice) Slots() int { return d.slotCount }

func (d *fakeDevice) Epoch() uint64 {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.epoch
}

func (d *fakeDevice) Reset() (uint64, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.epoch++
	for _, a := range d.auths {
		a.expired = true
	}
	d.occupant = make(map[int]signing.AuthID)
	d.ops = append(d.ops, fmt.Sprintf("reset epoch=%d", d.epoch))
	return d.epoch, nil
}

func (d *fakeDevice) Begin(slot int, key signing.KeyID, epoch uint64) (signing.AuthID, error) {
	d.mu.Lock()
	defer d.mu.Unlock()

	d.beginCalls++
	d.ops = append(d.ops, fmt.Sprintf("begin slot=%d key=%s epoch=%d", slot, key, epoch))

	if d.beginErr != nil {
		if err := d.beginErr(slot, key, epoch); err != nil {
			return 0, err
		}
	}
	if slot < 0 || slot >= d.slotCount {
		return 0, errSlotOutOfRange
	}
	if epoch != d.epoch {
		return 0, errStaleEpoch
	}
	if _, busy := d.occupant[slot]; busy {
		return 0, errSlotBusy
	}

	d.nextAuth++
	auth := &fakeAuth{id: d.nextAuth, slot: slot, key: key, epoch: epoch}
	d.auths[auth.id] = auth
	d.occupant[slot] = auth.id
	return auth.id, nil
}

func (d *fakeDevice) Append(authID signing.AuthID, seq int, fragment []byte) error {
	d.mu.Lock()
	defer d.mu.Unlock()

	d.ops = append(d.ops, fmt.Sprintf("append auth=%d seq=%d", authID, seq))

	a, err := d.liveAuthLocked(authID)
	if err != nil {
		return err
	}
	if d.appendErr != nil {
		if err := d.appendErr(authID, seq); err != nil {
			return err
		}
	}
	if seq != a.nextSeq {
		return fmt.Errorf("%w: got %d want %d", errBadSeq, seq, a.nextSeq)
	}
	a.fragments = append(a.fragments, append([]byte(nil), fragment...))
	a.nextSeq++
	return nil
}

func (d *fakeDevice) Sign(authID signing.AuthID) ([]byte, error) {
	d.mu.Lock()
	defer d.mu.Unlock()

	d.ops = append(d.ops, fmt.Sprintf("sign auth=%d", authID))

	a, err := d.liveAuthLocked(authID)
	if err != nil {
		return nil, err
	}
	if d.signErr != nil {
		if err := d.signErr(authID); err != nil {
			return nil, err
		}
	}
	if len(a.fragments) == 0 {
		return nil, errNoFragments
	}
	sig := signatureFor(a.key, a.fragments)
	a.signed = true
	delete(d.occupant, a.slot)
	d.signed = append(d.signed, SignedRecord{
		Auth:      a.id,
		Slot:      a.slot,
		Key:       a.key,
		Fragments: cloneFragments(a.fragments),
		Signature: append([]byte(nil), sig...),
	})
	return sig, nil
}

func (d *fakeDevice) Release(authID signing.AuthID) error {
	d.mu.Lock()
	defer d.mu.Unlock()

	d.ops = append(d.ops, fmt.Sprintf("release auth=%d", authID))

	a, ok := d.auths[authID]
	if !ok || a.released || a.signed {
		return fmt.Errorf("%w: %d", errUnknownAuth, authID)
	}
	if a.expired {
		return fmt.Errorf("%w: %d", errAuthExpired, authID)
	}
	if d.releaseErr != nil {
		if err := d.releaseErr(authID); err != nil {
			return err
		}
	}
	// Only the authorization that still owns the slot may clear it. A stale
	// notification aimed at a slot that has since been rebound must not free
	// the new occupant.
	if owner, held := d.occupant[a.slot]; !held || owner != authID {
		return fmt.Errorf("%w: auth %d slot %d", errNotOwner, authID, a.slot)
	}
	a.released = true
	delete(d.occupant, a.slot)
	d.releaseOK++
	return nil
}

func (d *fakeDevice) liveAuthLocked(authID signing.AuthID) (*fakeAuth, error) {
	a, ok := d.auths[authID]
	if !ok {
		return nil, fmt.Errorf("%w: %d", errUnknownAuth, authID)
	}
	if a.expired {
		return nil, fmt.Errorf("%w: %d", errAuthExpired, authID)
	}
	if a.released {
		return nil, fmt.Errorf("%w: %d", errUnknownAuth, authID)
	}
	if a.signed {
		return nil, fmt.Errorf("%w: %d", errFinalized, authID)
	}
	if a.epoch != d.epoch {
		return nil, fmt.Errorf("%w: %d", errAuthExpired, authID)
	}
	return a, nil
}

// Snapshot accessors, used by tests after the adapter has finished calling in.

func (d *fakeDevice) signedRecords() []SignedRecord {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]SignedRecord, len(d.signed))
	for i, rec := range d.signed {
		out[i] = SignedRecord{
			Auth:      rec.Auth,
			Slot:      rec.Slot,
			Key:       rec.Key,
			Fragments: cloneFragments(rec.Fragments),
			Signature: append([]byte(nil), rec.Signature...),
		}
	}
	return out
}

func (d *fakeDevice) signedRecordFor(auth signing.AuthID) (SignedRecord, bool) {
	for _, rec := range d.signedRecords() {
		if rec.Auth == auth {
			return rec, true
		}
	}
	return SignedRecord{}, false
}

func (d *fakeDevice) liveOccupants() map[int]signing.AuthID {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make(map[int]signing.AuthID, len(d.occupant))
	for slot, auth := range d.occupant {
		out[slot] = auth
	}
	return out
}

func (d *fakeDevice) beginCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.beginCalls
}

func (d *fakeDevice) successfulReleases() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.releaseOK
}

// signatureFor is the fake device's deterministic signature over exactly the
// authorized key and the fragments it accepted, in order.
func signatureFor(key signing.KeyID, fragments [][]byte) []byte {
	h := sha256.New()
	h.Write([]byte("fake-sign-v1\x00"))
	var n [4]byte
	binary.BigEndian.PutUint32(n[:], uint32(len(key)))
	h.Write(n[:])
	h.Write([]byte(key))
	for _, fragment := range fragments {
		binary.BigEndian.PutUint32(n[:], uint32(len(fragment)))
		h.Write(n[:])
		h.Write(fragment)
	}
	return h.Sum(nil)
}

func cloneFragments(in [][]byte) [][]byte {
	out := make([][]byte, len(in))
	for i, fragment := range in {
		out[i] = append([]byte(nil), fragment...)
	}
	return out
}
