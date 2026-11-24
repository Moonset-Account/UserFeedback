// Package wipeverify implements cryptographic-erasure verification for
// decommissioned volumes in a disk-encryption service.
//
// A volume key is sealed into hardware slots on several devices (production
// host, disaster-recovery host, ...). At decommission time the service must
// prove that every copy of the key was destroyed. The input evidence is:
//
//   - the volume's last sealing manifest (which copies existed where, at
//     which slot generation), and
//   - device receipts (install / migrate / destroy) whose signatures have
//     already been verified by the caller. This package does NOT do any
//     cryptography on receipts; it only checks the bookkeeping logic.
//
// Verification rules implemented here:
//
//  1. Receipts carry per-device sequence numbers. Only the contiguous
//     prefix of each device's chain (starting at the smallest sequence
//     number seen for that device) is usable evidence. A missing
//     intermediate sequence number — or two conflicting receipts for the
//     same sequence number — leaves every later receipt of that device
//     unusable, so the affected copies stay in the pending state.
//  2. A copy is closed only by a destroy (or migrate-away) receipt that
//     matches the copy's device, slot, volume AND original generation
//     exactly. If a slot was reused for another volume at a newer
//     generation, a destroy receipt for that newer generation never closes
//     the decommissioned copy.
//  3. Receipt arrival order does not matter: migration establishes the new
//     copy before wiping the old one, but the two receipts may be delivered
//     in reverse order. The verifier evaluates the accumulated receipt set,
//     not the arrival order.
package wipeverify

import (
	"crypto/sha256"
	"errors"
	"fmt"
	"sort"
)

// Kind enumerates the device receipt types.
type Kind int

const (
	// KindInstall: a new key copy was sealed into (device, slot, generation).
	KindInstall Kind = iota
	// KindMigrate: the copy at (device, slot, generation) was migrated away
	// and wiped at the source as part of a verified migration. Recorded on
	// the source device; closes the source copy like a destroy receipt.
	KindMigrate
	// KindDestroy: the copy at (device, slot, generation) was destroyed.
	KindDestroy
)

func (k Kind) String() string {
	switch k {
	case KindInstall:
		return "install"
	case KindMigrate:
		return "migrate"
	case KindDestroy:
		return "destroy"
	default:
		return "unknown"
	}
}

// Receipt is one device-signed receipt. The signature is assumed to have
// been verified by the caller before the receipt is handed to this package.
type Receipt struct {
	Device     string // device that issued the receipt
	Seq        uint64 // per-device sequence number, starts at 1
	Kind       Kind
	Volume     string // volume the receipt refers to
	Slot       int    // hardware slot index on the device
	Generation uint64 // slot generation the receipt refers to
}

// CopyRef identifies one sealed copy of a volume key.
type CopyRef struct {
	Device     string
	Slot       int
	Generation uint64
}

// Manifest is the volume's last sealing manifest at decommission time.
type Manifest struct {
	Volume string
	Copies []CopyRef
}

// State is the verification state of one key copy.
type State int

const (
	// StatePending: destruction has not been verifiably established (待核实).
	StatePending State = iota
	// StateDestroyed: a valid original-generation destruction receipt closed
	// the copy (已销毁).
	StateDestroyed
)

func (s State) String() string {
	switch s {
	case StatePending:
		return "pending"
	case StateDestroyed:
		return "destroyed"
	default:
		return "unknown"
	}
}

// ReceiptRef points at the receipt that closed a copy.
type ReceiptRef struct {
	Device string
	Seq    uint64
	Kind   Kind
}

// CopyStatus is the verification result for one key copy.
type CopyStatus struct {
	Copy     CopyRef
	State    State
	ClosedBy *ReceiptRef // set iff State == StateDestroyed
	Note     string      // human-readable reason while pending
}

// Report is the verification outcome for a volume.
type Report struct {
	Volume   string
	Copies   []CopyStatus // sorted by (device, slot, generation)
	Pending  int          // number of copies still in StatePending
	Complete bool         // true iff every known copy is destroyed
}

// ClosedCopy records one destroyed copy inside a Certificate.
type ClosedCopy struct {
	Copy     CopyRef
	ClosedBy ReceiptRef
}

// Certificate is the erasure certificate issued once every copy of the
// volume key is verifiably destroyed. Digest is a deterministic SHA-256
// over the certificate content and can be used as the certificate ID.
type Certificate struct {
	Volume string
	Copies []ClosedCopy
	Digest [32]byte
}

// Verifier accumulates receipts for one decommissioned volume and produces
// erasure reports. It is not safe for concurrent use.
type Verifier struct {
	volume    string
	manifest  []CopyRef
	byDevice  map[string]map[uint64]Receipt // device -> seq -> receipt
	conflicts map[string]map[uint64]bool    // device -> seq poisoned by conflicting receipts
}

// NewVerifier creates a Verifier for the given last sealing manifest.
func NewVerifier(m Manifest) *Verifier {
	copies := make([]CopyRef, len(m.Copies))
	copy(copies, m.Copies)
	return &Verifier{
		volume:    m.Volume,
		manifest:  copies,
		byDevice:  make(map[string]map[uint64]Receipt),
		conflicts: make(map[string]map[uint64]bool),
	}
}

// AddReceipt records one receipt whose device signature the caller has
// already verified. Receipts may arrive in any order, including the
// reversed (destroy-before-install) order produced by migrations.
//
// Re-delivering an identical receipt is a no-op. Two different receipts
// carrying the same (device, seq) poison the device chain at that point:
// the conflicting sequence number and everything after it become unusable
// evidence, exactly like a gap.
func (v *Verifier) AddReceipt(r Receipt) error {
	switch {
	case r.Device == "":
		return errors.New("wipeverify: receipt has empty device")
	case r.Seq == 0:
		return errors.New("wipeverify: receipt sequence numbers start at 1")
	case r.Volume == "":
		return errors.New("wipeverify: receipt has empty volume")
	case r.Slot < 0:
		return errors.New("wipeverify: receipt has negative slot")
	}
	switch r.Kind {
	case KindInstall, KindMigrate, KindDestroy:
	default:
		return fmt.Errorf("wipeverify: unknown receipt kind %d", int(r.Kind))
	}
	dev := v.byDevice[r.Device]
	if dev == nil {
		dev = make(map[uint64]Receipt)
		v.byDevice[r.Device] = dev
	}
	if prev, ok := dev[r.Seq]; ok {
		if prev == r {
			return nil // idempotent redelivery
		}
		if v.conflicts[r.Device] == nil {
			v.conflicts[r.Device] = make(map[uint64]bool)
		}
		v.conflicts[r.Device][r.Seq] = true
		return nil
	}
	dev[r.Seq] = r
	return nil
}

// usableReceipts returns the contiguous, conflict-free prefix of the
// device's receipt chain, starting at the smallest sequence number seen.
func (v *Verifier) usableReceipts(device string) []Receipt {
	bySeq := v.byDevice[device]
	if len(bySeq) == 0 {
		return nil
	}
	seqs := make([]uint64, 0, len(bySeq))
	for s := range bySeq {
		seqs = append(seqs, s)
	}
	sort.Slice(seqs, func(i, j int) bool { return seqs[i] < seqs[j] })
	base := seqs[0]
	out := make([]Receipt, 0, len(seqs))
	for i, s := range seqs {
		if s != base+uint64(i) {
			break // gap: this and all later receipts are unusable
		}
		if v.conflicts[device][s] {
			break // conflicting receipt: chain broken here
		}
		out = append(out, bySeq[s])
	}
	return out
}

// Report evaluates all receipts accumulated so far and returns the
// per-copy verification state. The result depends only on the receipt
// set, never on the order in which receipts were added.
func (v *Verifier) Report() Report {
	obligations := make(map[CopyRef]struct{})
	for _, c := range v.manifest {
		obligations[c] = struct{}{}
	}

	devices := make([]string, 0, len(v.byDevice))
	for d := range v.byDevice {
		devices = append(devices, d)
	}
	sort.Strings(devices)

	usable := make(map[string][]Receipt, len(devices))
	for _, d := range devices {
		usable[d] = v.usableReceipts(d)
		// Install receipts for this volume open additional copy
		// obligations (e.g. historical copies later replaced by a
		// migration): every copy that ever existed must be destroyed.
		for _, r := range usable[d] {
			if r.Kind == KindInstall && r.Volume == v.volume {
				obligations[CopyRef{Device: r.Device, Slot: r.Slot, Generation: r.Generation}] = struct{}{}
			}
		}
	}

	// A copy is closed by the earliest usable destroy/migrate receipt that
	// matches device, slot, volume and the ORIGINAL generation exactly.
	closedBy := make(map[CopyRef]ReceiptRef)
	for _, d := range devices {
		for _, r := range usable[d] {
			if r.Volume != v.volume {
				continue
			}
			if r.Kind != KindDestroy && r.Kind != KindMigrate {
				continue
			}
			cr := CopyRef{Device: r.Device, Slot: r.Slot, Generation: r.Generation}
			if _, known := obligations[cr]; !known {
				continue
			}
			if _, done := closedBy[cr]; !done {
				closedBy[cr] = ReceiptRef{Device: r.Device, Seq: r.Seq, Kind: r.Kind}
			}
		}
	}

	refs := make([]CopyRef, 0, len(obligations))
	for c := range obligations {
		refs = append(refs, c)
	}
	sort.Slice(refs, func(i, j int) bool {
		a, b := refs[i], refs[j]
		if a.Device != b.Device {
			return a.Device < b.Device
		}
		if a.Slot != b.Slot {
			return a.Slot < b.Slot
		}
		return a.Generation < b.Generation
	})

	rep := Report{Volume: v.volume, Copies: make([]CopyStatus, 0, len(refs))}
	for _, c := range refs {
		cs := CopyStatus{Copy: c, State: StatePending}
		if cl, ok := closedBy[c]; ok {
			cs.State = StateDestroyed
			ref := cl
			cs.ClosedBy = &ref
		} else {
			rep.Pending++
			cs.Note = v.pendingNote(c, usable)
		}
		rep.Copies = append(rep.Copies, cs)
	}
	rep.Complete = rep.Pending == 0
	return rep
}

// pendingNote explains why a copy is still pending.
func (v *Verifier) pendingNote(c CopyRef, usable map[string][]Receipt) string {
	// A matching destruction credential exists but sits beyond a gap or a
	// conflict in the device chain.
	for _, r := range v.byDevice[c.Device] {
		if r.Volume == v.volume && r.Slot == c.Slot && r.Generation == c.Generation &&
			(r.Kind == KindDestroy || r.Kind == KindMigrate) &&
			!containsReceipt(usable[c.Device], r) {
			return "destruction receipt for the original generation exists but the device sequence chain is incomplete (missing or conflicting intermediate receipts)"
		}
	}
	// The slot was reused at a newer generation (possibly by another
	// volume); only the original generation's destroy receipt can close.
	for _, r := range usable[c.Device] {
		if r.Kind == KindInstall && r.Slot == c.Slot && r.Generation > c.Generation {
			return fmt.Sprintf("slot reused at generation %d (volume %q); only a destroy receipt for the original generation %d can close this copy",
				r.Generation, r.Volume, c.Generation)
		}
	}
	return "no destroy receipt for the original generation yet"
}

func containsReceipt(list []Receipt, r Receipt) bool {
	for _, x := range list {
		if x == r {
			return true
		}
	}
	return false
}

// Certificate issues the erasure certificate. It returns an error while
// any copy is still pending verification.
func (v *Verifier) Certificate() (Certificate, error) {
	rep := v.Report()
	if !rep.Complete {
		return Certificate{}, fmt.Errorf("wipeverify: cannot issue erasure certificate for volume %q: %d of %d copies still pending",
			rep.Volume, rep.Pending, len(rep.Copies))
	}
	cert := Certificate{Volume: rep.Volume, Copies: make([]ClosedCopy, 0, len(rep.Copies))}
	h := sha256.New()
	fmt.Fprintf(h, "wipeverify-v1\nvolume:%s\n", rep.Volume)
	for _, cs := range rep.Copies {
		cert.Copies = append(cert.Copies, ClosedCopy{Copy: cs.Copy, ClosedBy: *cs.ClosedBy})
		fmt.Fprintf(h, "%s|%d|%d|%s|%d\n",
			cs.Copy.Device, cs.Copy.Slot, cs.Copy.Generation, cs.ClosedBy.Kind, cs.ClosedBy.Seq)
	}
	copy(cert.Digest[:], h.Sum(nil))
	return cert, nil
}
