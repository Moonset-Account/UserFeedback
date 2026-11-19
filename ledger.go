// Package seatledger implements an authorization ledger for commercial
// software seats shared between offline branches.
//
// HQ pre-allocates the global concurrency cap to branches. A branch may
// check out seats only from its locally available share. Seats move between
// branches through a three-phase transfer:
//
//  1. The source branch freezes the seats (InitiateTransfer) and issues a
//     unique, signed TransferRecord.
//  2. The receiving branch verifies the record (ReceiveTransfer) and credits
//     the seats, returning a signed Ack. Duplicate records are credited once;
//     the Ack can be re-sent if the confirmation is lost.
//  3. The source branch confirms the Ack (ConfirmTransfer), permanently
//     moving the seats out of its quota.
//
// A source branch that stops waiting (timeout) must never unilaterally
// reclaim frozen seats: only a signed Rejection from the receiver unfreezes
// them. This guarantees the two ends can never own the same seat at once.
//
// Every state change is appended to a per-branch event log. A branch can be
// recovered from any persisted prefix of its log and then resume exchanging
// duplicate or out-of-order records idempotently.
package seatledger

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strconv"
	"strings"
)

var (
	// ErrInsufficientSeats is returned when a checkout or transfer would
	// exceed the locally available share.
	ErrInsufficientSeats = errors.New("seatledger: insufficient available seats")
	// ErrInvalidSignature is returned when a record, ack or rejection fails
	// authentication against the issuer's key.
	ErrInvalidSignature = errors.New("seatledger: invalid signature")
	// ErrUnknownTransfer is returned when a confirmation or rejection refers
	// to a transfer this branch never froze.
	ErrUnknownTransfer = errors.New("seatledger: unknown transfer")
)

// KeyRegistry maps branch names to their HMAC keys. Receivers use it to
// verify records issued by a source branch, and sources use it to verify
// acks and rejections issued by a receiving branch.
type KeyRegistry map[string][]byte

func signRecord(key []byte, parts ...string) string {
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(strings.Join(parts, "|")))
	return hex.EncodeToString(mac.Sum(nil))
}

// TransferRecord is the unique, source-signed record authorizing a seat
// transfer. It is safe to deliver it more than once or out of order.
type TransferRecord struct {
	ID    string `json:"id"`
	From  string `json:"from"`
	To    string `json:"to"`
	Count int    `json:"count"`
	Sig   string `json:"sig"`
}

func (r TransferRecord) parts() []string {
	return []string{"transfer", r.ID, r.From, r.To, strconv.Itoa(r.Count)}
}

// Ack is the receiver-signed confirmation that a transfer was credited.
type Ack struct {
	TransferID string `json:"transfer_id"`
	From       string `json:"from"` // original source branch
	To         string `json:"to"`   // receiving branch (signer)
	Count      int    `json:"count"`
	Sig        string `json:"sig"`
}

func (a Ack) parts() []string {
	return []string{"ack", a.TransferID, a.From, a.To, strconv.Itoa(a.Count)}
}

// Rejection is the receiver-signed refusal of a transfer. It is the only
// message that unfreezes seats at the source before confirmation.
type Rejection struct {
	TransferID string `json:"transfer_id"`
	From       string `json:"from"` // original source branch
	To         string `json:"to"`   // receiving branch (signer)
	Count      int    `json:"count"`
	Reason     string `json:"reason"`
	Sig        string `json:"sig"`
}

func (r Rejection) parts() []string {
	return []string{"reject", r.TransferID, r.From, r.To, strconv.Itoa(r.Count)}
}

type outgoing struct {
	To    string
	Count int
}

type incoming struct {
	From  string
	Count int
}

// Snapshot is a consistent view of a branch's local seat states.
type Snapshot struct {
	Branch    string
	Issued    int            // seats allocated by HQ so far
	Available int            // locally usable for offline checkout
	InUse     int            // checked out
	FrozenOut map[string]int // transfer ID -> seats frozen for an outgoing transfer
}

// Ledger is the authorization book of one branch.
type Ledger struct {
	name  string
	key   []byte
	store *Store

	issued     int
	available  int
	inUse      int
	frozenOut  map[string]outgoing // frozen, awaiting ack or rejection
	doneOut    map[string]bool     // confirmed or rejection-applied (idempotence)
	acceptedIn map[string]incoming // credited incoming transfers (dedupe + ack resend)
	rejectedIn map[string]incoming // rejected incoming transfers (dedupe + resend)
	seq        uint64
}

// Open creates or recovers a branch ledger. If store is non-nil, any
// persisted event prefix is replayed first and new events are appended.
// A nil store keeps the ledger in memory only.
func Open(name string, key []byte, store *Store) (*Ledger, error) {
	if name == "" {
		return nil, errors.New("seatledger: branch name required")
	}
	l := &Ledger{
		name:       name,
		key:        key,
		store:      store,
		frozenOut:  map[string]outgoing{},
		doneOut:    map[string]bool{},
		acceptedIn: map[string]incoming{},
		rejectedIn: map[string]incoming{},
	}
	if store != nil {
		events, err := ReadPrefix(store.Path())
		if err != nil {
			return nil, err
		}
		for _, e := range events {
			l.apply(e)
		}
	}
	return l, nil
}

// Name returns the branch name.
func (l *Ledger) Name() string { return l.name }

// Allocate records seats issued by HQ to this branch.
func (l *Ledger) Allocate(count int) error {
	if count <= 0 {
		return fmt.Errorf("seatledger: allocation must be positive, got %d", count)
	}
	return l.commit(Event{Type: evAllocate, Count: count})
}

// Checkout takes seats from the locally available share (offline use).
func (l *Ledger) Checkout(count int) error {
	if count <= 0 {
		return fmt.Errorf("seatledger: checkout must be positive, got %d", count)
	}
	if l.available < count {
		return ErrInsufficientSeats
	}
	return l.commit(Event{Type: evCheckout, Count: count})
}

// Checkin returns previously checked-out seats to the available share.
func (l *Ledger) Checkin(count int) error {
	if count <= 0 {
		return fmt.Errorf("seatledger: checkin must be positive, got %d", count)
	}
	if l.inUse < count {
		return fmt.Errorf("seatledger: cannot check in %d, only %d in use", count, l.inUse)
	}
	return l.commit(Event{Type: evCheckin, Count: count})
}

// InitiateTransfer freezes count seats and issues the unique signed record
// the receiver must present. The seats stay frozen until the source confirms
// an Ack or applies a Rejection; a timeout never unfreezes them.
func (l *Ledger) InitiateTransfer(to string, count int) (TransferRecord, error) {
	if to == "" || to == l.name {
		return TransferRecord{}, fmt.Errorf("seatledger: invalid transfer destination %q", to)
	}
	if count <= 0 {
		return TransferRecord{}, fmt.Errorf("seatledger: transfer count must be positive, got %d", count)
	}
	if l.available < count {
		return TransferRecord{}, ErrInsufficientSeats
	}
	l.seq++
	id := fmt.Sprintf("%s-%d", l.name, l.seq)
	if err := l.commit(Event{Type: evFreeze, Seq: l.seq, ID: id, Peer: to, Count: count}); err != nil {
		return TransferRecord{}, err
	}
	rec := TransferRecord{ID: id, From: l.name, To: to, Count: count}
	rec.Sig = signRecord(l.key, rec.parts()...)
	return rec, nil
}

// ReceiveTransfer verifies the source-signed record and credits the seats.
// It is idempotent: a duplicate delivery returns the Ack again without
// crediting twice. A record that fails verification changes nothing.
func (l *Ledger) ReceiveTransfer(rec TransferRecord, keys KeyRegistry) (Ack, error) {
	if rec.To != l.name {
		return Ack{}, fmt.Errorf("seatledger: record %s addressed to %q, not %q", rec.ID, rec.To, l.name)
	}
	if err := verify(keys, rec.From, rec.Sig, rec.parts()); err != nil {
		return Ack{}, err
	}
	if _, ok := l.rejectedIn[rec.ID]; ok {
		return Ack{}, fmt.Errorf("seatledger: transfer %s already rejected", rec.ID)
	}
	if inc, ok := l.acceptedIn[rec.ID]; ok {
		// Duplicate delivery: re-issue the ack, do not credit again.
		return l.signAck(rec.ID, inc), nil
	}
	inc := incoming{From: rec.From, Count: rec.Count}
	if err := l.commit(Event{Type: evAccept, ID: rec.ID, Peer: rec.From, Count: rec.Count}); err != nil {
		return Ack{}, err
	}
	return l.signAck(rec.ID, inc), nil
}

// RejectTransfer refuses a record before acceptance. The signed Rejection is
// the only message that lets the source unfreeze the seats. Idempotent.
func (l *Ledger) RejectTransfer(rec TransferRecord, keys KeyRegistry, reason string) (Rejection, error) {
	if rec.To != l.name {
		return Rejection{}, fmt.Errorf("seatledger: record %s addressed to %q, not %q", rec.ID, rec.To, l.name)
	}
	if err := verify(keys, rec.From, rec.Sig, rec.parts()); err != nil {
		return Rejection{}, err
	}
	if _, ok := l.acceptedIn[rec.ID]; ok {
		return Rejection{}, fmt.Errorf("seatledger: transfer %s already accepted", rec.ID)
	}
	if _, ok := l.rejectedIn[rec.ID]; ok {
		return l.signRejection(rec, reason), nil
	}
	if err := l.commit(Event{Type: evRejectIn, ID: rec.ID, Peer: rec.From, Count: rec.Count}); err != nil {
		return Rejection{}, err
	}
	return l.signRejection(rec, reason), nil
}

// ConfirmTransfer applies a receiver's Ack, permanently moving the frozen
// seats out of this branch's quota. Duplicate acks are no-ops.
func (l *Ledger) ConfirmTransfer(ack Ack, keys KeyRegistry) error {
	if ack.From != l.name {
		return fmt.Errorf("seatledger: ack for %s not addressed to %q", ack.TransferID, l.name)
	}
	if err := verify(keys, ack.To, ack.Sig, ack.parts()); err != nil {
		return err
	}
	if l.doneOut[ack.TransferID] {
		return nil
	}
	o, ok := l.frozenOut[ack.TransferID]
	if !ok {
		return ErrUnknownTransfer
	}
	if o.Count != ack.Count || o.To != ack.To {
		return fmt.Errorf("seatledger: ack for %s does not match frozen transfer", ack.TransferID)
	}
	return l.commit(Event{Type: evConfirm, ID: ack.TransferID, Peer: ack.To, Count: ack.Count})
}

// ApplyRejection unfreezes the seats of a transfer the receiver refused.
// Duplicate rejections are no-ops.
func (l *Ledger) ApplyRejection(rej Rejection, keys KeyRegistry) error {
	if rej.From != l.name {
		return fmt.Errorf("seatledger: rejection for %s not addressed to %q", rej.TransferID, l.name)
	}
	if err := verify(keys, rej.To, rej.Sig, rej.parts()); err != nil {
		return err
	}
	if l.doneOut[rej.TransferID] {
		return nil
	}
	o, ok := l.frozenOut[rej.TransferID]
	if !ok {
		return ErrUnknownTransfer
	}
	if o.Count != rej.Count || o.To != rej.To {
		return fmt.Errorf("seatledger: rejection for %s does not match frozen transfer", rej.TransferID)
	}
	return l.commit(Event{Type: evUnfreeze, ID: rej.TransferID, Peer: rej.To, Count: rej.Count})
}

// AbortTransfer reports why a frozen transfer cannot be unilaterally
// reclaimed. A waiting timeout never frees the seats at the source: only a
// receiver-signed Rejection (ApplyRejection) or Ack (ConfirmTransfer) may
// resolve the transfer, so the two ends can never own the same seats.
func (l *Ledger) AbortTransfer(id string) error {
	if _, ok := l.frozenOut[id]; ok {
		return fmt.Errorf("seatledger: transfer %s is frozen; unilateral abort refused, require receiver ack or rejection", id)
	}
	if l.doneOut[id] {
		return fmt.Errorf("seatledger: transfer %s already resolved", id)
	}
	return ErrUnknownTransfer
}

// AckFor re-issues the Ack of an accepted transfer, e.g. when the original
// confirmation was lost in transit and the receiver resends it.
func (l *Ledger) AckFor(transferID string) (Ack, bool) {
	inc, ok := l.acceptedIn[transferID]
	if !ok {
		return Ack{}, false
	}
	return l.signAck(transferID, inc), true
}

// RejectionFor re-issues the Rejection of a rejected transfer.
func (l *Ledger) RejectionFor(transferID, reason string) (Rejection, bool) {
	inc, ok := l.rejectedIn[transferID]
	if !ok {
		return Rejection{}, false
	}
	rej := Rejection{TransferID: transferID, From: inc.From, To: l.name, Count: inc.Count, Reason: reason}
	rej.Sig = signRecord(l.key, rej.parts()...)
	return rej, true
}

// Snapshot returns the branch's current local seat states.
func (l *Ledger) Snapshot() Snapshot {
	frozen := make(map[string]int, len(l.frozenOut))
	for id, o := range l.frozenOut {
		frozen[id] = o.Count
	}
	return Snapshot{
		Branch:    l.name,
		Issued:    l.issued,
		Available: l.available,
		InUse:     l.inUse,
		FrozenOut: frozen,
	}
}

func (l *Ledger) signAck(id string, inc incoming) Ack {
	ack := Ack{TransferID: id, From: inc.From, To: l.name, Count: inc.Count}
	ack.Sig = signRecord(l.key, ack.parts()...)
	return ack
}

func (l *Ledger) signRejection(rec TransferRecord, reason string) Rejection {
	rej := Rejection{TransferID: rec.ID, From: rec.From, To: l.name, Count: rec.Count, Reason: reason}
	rej.Sig = signRecord(l.key, rej.parts()...)
	return rej
}

func verify(keys KeyRegistry, signer, sig string, parts []string) error {
	key, ok := keys[signer]
	if !ok {
		return fmt.Errorf("seatledger: no key registered for branch %q", signer)
	}
	if signRecord(key, parts...) != sig {
		return ErrInvalidSignature
	}
	return nil
}

func (l *Ledger) commit(e Event) error {
	if l.store != nil {
		if err := l.store.Append(e); err != nil {
			return err
		}
	}
	l.apply(e)
	return nil
}

func (l *Ledger) apply(e Event) {
	switch e.Type {
	case evAllocate:
		l.issued += e.Count
		l.available += e.Count
	case evCheckout:
		l.available -= e.Count
		l.inUse += e.Count
	case evCheckin:
		l.inUse -= e.Count
		l.available += e.Count
	case evFreeze:
		l.available -= e.Count
		l.frozenOut[e.ID] = outgoing{To: e.Peer, Count: e.Count}
		if e.Seq > l.seq {
			l.seq = e.Seq
		}
	case evAccept:
		l.available += e.Count
		l.acceptedIn[e.ID] = incoming{From: e.Peer, Count: e.Count}
	case evRejectIn:
		l.rejectedIn[e.ID] = incoming{From: e.Peer, Count: e.Count}
	case evConfirm:
		delete(l.frozenOut, e.ID)
		l.doneOut[e.ID] = true
	case evUnfreeze:
		if o, ok := l.frozenOut[e.ID]; ok {
			l.available += o.Count
			delete(l.frozenOut, e.ID)
		}
		l.doneOut[e.ID] = true
	}
}
