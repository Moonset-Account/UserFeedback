package advisory

import (
	"bytes"
	"crypto/subtle"
	"errors"
	"fmt"
	"sync"
)

// Verifier authenticates the manifest region with the sending region's
// signature. The receiver calls it before any other processing, so a manifest
// that fails verification cannot change receiver state or trigger publication.
type Verifier interface {
	Verify(region []byte) error
}

// VerifierFunc adapts a plain function to Verifier.
type VerifierFunc func(region []byte) error

// Verify calls f(region).
func (f VerifierFunc) Verify(region []byte) error { return f(region) }

// Bulletin is a fully assembled and verified security bulletin.
type Bulletin struct {
	Version uint64
	Length  int
	Digest  Digest
	Data    []byte // exactly Length bytes

	// fingerprint records which manifest produced the bulletin so a manifest
	// with the same version but different content can be recognised later.
	fingerprint Digest
}

// GroupStatus reports the assembly state of one group.
type GroupStatus struct {
	// Group is the group's index in the manifest.
	Group int
	// MissingChunks lists the chunk indices not currently held, i.e. never
	// received or rejected for a digest mismatch. A group with exactly one
	// entry here is still repairable when a parity block is held.
	MissingChunks []int
	// ParityReceived reports whether a parity block matching the manifest
	// digest is held for this group.
	ParityReceived bool
	// Repairable reports whether the group is absent at most one chunk and a
	// usable parity block is held, i.e. the missing chunk can be rebuilt.
	Repairable bool
	// Complete reports whether every chunk of the group is held.
	Complete bool
}

// Status is a snapshot of the receiver.
type Status struct {
	// HasManifest reports whether a manifest has been admitted.
	HasManifest bool
	// Version is the admitted manifest's version (zero when none).
	Version uint64
	// Ready reports whether every group is complete or repairable, i.e. the
	// bulletin can be assembled. It is equivalent to MissingGroups being
	// empty; the whole-bulletin digest check still runs during Publish.
	Ready bool
	// Groups reports every group in manifest order.
	Groups []GroupStatus
	// MissingGroups lists the indices of groups that are neither complete nor
	// repairable. It is empty when the bulletin is ready to assemble.
	MissingGroups []int
	// Current is the most recently published complete bulletin, if any.
	Current *Bulletin
}

// IncompleteError reports a provably incomplete attempt to publish. It names
// the groups that are still missing data.
type IncompleteError struct {
	Version uint64
	Missing []int // group indices
}

func (e *IncompleteError) Error() string {
	return fmt.Sprintf("%v: version %d still needs groups %v", ErrIncomplete, e.Version, e.Missing)
}

// Unwrap makes errors.Is(err, ErrIncomplete) succeed.
func (e *IncompleteError) Unwrap() error { return ErrIncomplete }

// ErrIncomplete is the sentinel for an attempt to publish an incomplete
// bulletin.
var ErrIncomplete = errors.New("advisory: bulletin incomplete")

type session struct {
	manifest    Manifest
	fingerprint Digest
	chunks      map[int]map[int][]byte
	parity      map[int][]byte
}

func newSession(m Manifest) *session {
	return &session{
		manifest:    m,
		fingerprint: m.fingerprint(),
		chunks:      make(map[int]map[int][]byte),
		parity:      make(map[int][]byte),
	}
}

// Receiver assembles one signed bulletin at a time and publishes a version
// only after the whole bulletin checks out and it is newer than the published
// one. A Receiver is safe for concurrent use.
type Receiver struct {
	mu      sync.Mutex
	verify  Verifier
	pending *session
	current *Bulletin
}

// New returns a Receiver that admits manifests only when verify accepts them.
// A nil verifier is rejected: without source verification nothing may be
// published.
func New(verify Verifier) (*Receiver, error) {
	if verify == nil {
		return nil, ErrNoVerifier
	}
	return &Receiver{verify: verify}, nil
}

// OfferManifest authenticates region and, if accepted, replaces the pending
// session with the manifest it carries. Collected data is kept only when the
// offered manifest describes the same bulletin as the pending one (same
// version and same canonical content); otherwise the pending data is discarded
// because it belongs to another version or another manifest.
//
// Errors:
//   - the verifier rejects, or fails: ErrUnverified (no state change)
//   - region is not a valid manifest: ErrInvalidManifest (no state change)
//   - same version as the pending or published manifest but different content:
//     ErrManifestConflict (no state change)
//   - version not newer than the pending or published version: ErrStaleManifest
//     (no state change)
func (r *Receiver) OfferManifest(region []byte) (Manifest, error) {
	if err := r.verify.Verify(region); err != nil {
		return Manifest{}, fmt.Errorf("%w: %v", ErrUnverified, err)
	}
	m, err := ParseManifest(region)
	if err != nil {
		return Manifest{}, err
	}
	fp := m.fingerprint()

	r.mu.Lock()
	defer r.mu.Unlock()

	if r.current != nil {
		switch {
		case m.Version == r.current.Version && fp != r.current.fingerprint:
			return Manifest{}, fmt.Errorf("%w: version %d already published with different content",
				ErrManifestConflict, m.Version)
		case m.Version <= r.current.Version:
			return Manifest{}, fmt.Errorf("%w: version %d is not newer than published version %d",
				ErrStaleManifest, m.Version, r.current.Version)
		}
	}
	if r.pending != nil {
		switch {
		case m.Version == r.pending.manifest.Version && fp != r.pending.fingerprint:
			return Manifest{}, fmt.Errorf("%w: version %d differs from the admitted manifest",
				ErrManifestConflict, m.Version)
		case m.Version < r.pending.manifest.Version:
			return Manifest{}, fmt.Errorf("%w: version %d is older than pending version %d",
				ErrStaleManifest, m.Version, r.pending.manifest.Version)
		case m.Version == r.pending.manifest.Version:
			// Same version and same content: a retransmission. Keep the data
			// already collected.
			return r.pending.manifest, nil
		}
	}
	r.pending = newSession(m)
	return m, nil
}

// SubmitChunk offers data for chunk index of group gi, in any order. Chunk
// data whose length or SHA-256 digest disagrees with the manifest is not
// stored and is reported as ErrChunkDigestMismatch; the chunk then counts as
// missing for repair purposes. Previously accepted data is never replaced by
// a rejected submission.
func (r *Receiver) SubmitChunk(gi, ci int, data []byte) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	s, err := r.session()
	if err != nil {
		return err
	}
	if gi < 0 || gi >= len(s.manifest.Groups) {
		return fmt.Errorf("%w: group %d", ErrUnknownGroup, gi)
	}
	g := s.manifest.Groups[gi]
	if ci < 0 || ci >= len(g.Chunks) {
		return fmt.Errorf("%w: chunk %d in group %d", ErrUnknownChunk, ci, gi)
	}
	want := g.Chunks[ci]
	if len(data) != want.Length {
		return fmt.Errorf("%w: group %d chunk %d has length %d, want %d",
			ErrChunkDigestMismatch, gi, ci, len(data), want.Length)
	}
	if DigestOf(data) != want.Digest {
		return fmt.Errorf("%w: group %d chunk %d", ErrChunkDigestMismatch, gi, ci)
	}
	if s.chunks[gi] == nil {
		s.chunks[gi] = make(map[int][]byte, len(g.Chunks))
	}
	s.chunks[gi][ci] = append([]byte(nil), data...)
	return nil
}

// SubmitParity offers the XOR parity block of group gi. The block must be
// exactly as long as the group's span (its longest chunk) and match the
// manifest digest; otherwise it is rejected with ErrChunkDigestMismatch and
// counts as not received.
func (r *Receiver) SubmitParity(gi int, data []byte) error {
	r.mu.Lock()
	defer r.mu.Unlock()

	s, err := r.session()
	if err != nil {
		return err
	}
	if gi < 0 || gi >= len(s.manifest.Groups) {
		return fmt.Errorf("%w: group %d", ErrUnknownGroup, gi)
	}
	g := s.manifest.Groups[gi]
	if len(data) != g.Span() {
		return fmt.Errorf("%w: group %d parity has length %d, want %d",
			ErrChunkDigestMismatch, gi, len(data), g.Span())
	}
	if DigestOf(data) != g.Parity {
		return fmt.Errorf("%w: group %d parity", ErrChunkDigestMismatch, gi)
	}
	s.parity[gi] = append([]byte(nil), data...)
	return nil
}

// Status snapshots the receiver. MissingGroups names the groups that are
// provably short of data; the previously published bulletin is reported in
// Current regardless of the pending session's state.
func (r *Receiver) Status() Status {
	r.mu.Lock()
	defer r.mu.Unlock()

	st := Status{Current: r.copyCurrent()}
	if r.pending == nil {
		return st
	}
	s := r.pending
	st.HasManifest = true
	st.Version = s.manifest.Version
	st.Groups = make([]GroupStatus, len(s.manifest.Groups))
	for gi := range s.manifest.Groups {
		gs, _ := s.resolveGroup(gi)
		st.Groups[gi] = gs
		if !gs.Complete && !gs.Repairable {
			st.MissingGroups = append(st.MissingGroups, gi)
		}
	}
	st.Ready = len(st.MissingGroups) == 0
	return st
}

// Publish assembles the pending bulletin and publishes it when every group is
// complete or repairable, the assembled bytes hash to the declared digest,
// and the version is newer than the published one.
//
// Failures:
//   - no admitted manifest: ErrNoSession
//   - provably missing data: *IncompleteError wrapping ErrIncomplete; state is
//     kept so the caller can fill the named groups
//   - assembled bytes do not match the declared digest: the session's data is
//     discarded and ErrBulletinDigestMismatch is returned, because the bad
//     input cannot be attributed to a group
//   - version is not newer than the published one: ErrStaleManifest
//
// On success the published bulletin is stored as Current and the pending
// session is cleared.
func (r *Receiver) Publish() (Bulletin, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	s, err := r.session()
	if err != nil {
		return Bulletin{}, err
	}
	if r.current != nil && s.manifest.Version <= r.current.Version {
		return Bulletin{}, fmt.Errorf("%w: version %d is not newer than published version %d",
			ErrStaleManifest, s.manifest.Version, r.current.Version)
	}

	data, missing := s.assemble()
	if len(missing) > 0 {
		return Bulletin{}, &IncompleteError{Version: s.manifest.Version, Missing: missing}
	}
	m := s.manifest
	data = data[:m.Length]
	if DigestOf(data) != m.Bulletin {
		// No group can be singled out; require the whole bulletin again.
		s.chunks = make(map[int]map[int][]byte)
		s.parity = make(map[int][]byte)
		return Bulletin{}, fmt.Errorf("%w: version %d", ErrBulletinDigestMismatch, m.Version)
	}
	b := &Bulletin{
		Version:     m.Version,
		Length:      m.Length,
		Digest:      m.Bulletin,
		Data:        append([]byte(nil), data...),
		fingerprint: s.fingerprint,
	}
	r.current = b
	r.pending = nil
	return cloneBulletin(b), nil
}

// Current returns the most recently published complete bulletin.
func (r *Receiver) Current() (Bulletin, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.current == nil {
		return Bulletin{}, false
	}
	return cloneBulletin(r.current), true
}

// Reset discards the pending session but keeps the published bulletin.
func (r *Receiver) Reset() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.pending = nil
}

func (r *Receiver) session() (*session, error) {
	if r.pending == nil {
		return nil, ErrNoSession
	}
	return r.pending, nil
}

func (r *Receiver) copyCurrent() *Bulletin {
	if r.current == nil {
		return nil
	}
	b := cloneBulletin(r.current)
	return &b
}

func cloneBulletin(b *Bulletin) Bulletin {
	return Bulletin{
		Version:     b.Version,
		Length:      b.Length,
		Digest:      b.Digest,
		Data:        append([]byte(nil), b.Data...),
		fingerprint: b.fingerprint,
	}
}

// resolveGroup reports the group's state and returns, for complete or
// repairable groups, every chunk's bytes in index order.
func (s *session) resolveGroup(gi int) (GroupStatus, [][]byte) {
	g := s.manifest.Groups[gi]
	gs := GroupStatus{Group: gi, ParityReceived: s.parity[gi] != nil}
	held := s.chunks[gi]
	var missing []int
	for ci := range g.Chunks {
		if held[ci] == nil {
			missing = append(missing, ci)
		}
	}
	if len(missing) == 0 {
		gs.Complete = true
		return gs, s.chunksInOrder(gi)
	}
	gs.MissingChunks = missing
	if len(missing) == 1 && gs.ParityReceived {
		if rebuilt, ok := s.rebuild(gi, missing[0]); ok {
			gs.Repairable = true
			out := s.chunksInOrder(gi)
			out[missing[0]] = rebuilt
			return gs, out
		}
	}
	return gs, nil
}

// chunksInOrder copies out the held chunks; missing indices are nil.
func (s *session) chunksInOrder(gi int) [][]byte {
	g := s.manifest.Groups[gi]
	out := make([][]byte, len(g.Chunks))
	for ci := range g.Chunks {
		if d := s.chunks[gi][ci]; d != nil {
			out[ci] = d
		}
	}
	return out
}

// rebuild reconstructs a single missing chunk from the group's parity block
// and the other chunks. Every chunk is zero-extended on the right to the
// group's span before the XOR. The result is accepted only when its length and
// digest match the manifest entry.
func (s *session) rebuild(gi, missing int) ([]byte, bool) {
	g := s.manifest.Groups[gi]
	parity := s.parity[gi]
	if parity == nil {
		return nil, false
	}
	buf := append([]byte(nil), parity...)
	for ci, c := range g.Chunks {
		if ci == missing {
			continue
		}
		d := s.chunks[gi][ci]
		if d == nil || len(d) != c.Length {
			return nil, false
		}
		for i := 0; i < len(d); i++ {
			buf[i] ^= d[i]
		}
		// Bytes past the chunk's declared length stay untouched: the chunk is
		// zero-extended, and XOR with zero is a no-op.
	}
	want := g.Chunks[missing]
	if len(buf) < want.Length {
		return nil, false
	}
	out := append([]byte(nil), buf[:want.Length]...)
	if subtle.ConstantTimeCompare(digestSlice(DigestOf(out)), digestSlice(want.Digest)) != 1 {
		return nil, false
	}
	return out, true
}

// assemble concatenates every group's chunks in index order, groups in
// manifest order. A group that is neither complete nor repairable contributes
// no bytes at all; its index is reported instead. Nothing partial is ever
// returned.
func (s *session) assemble() ([]byte, []int) {
	var buf bytes.Buffer
	var missing []int
	for gi := range s.manifest.Groups {
		gs, chunks := s.resolveGroup(gi)
		if !gs.Complete && !gs.Repairable {
			missing = append(missing, gi)
			continue
		}
		// resolveGroup returns one entry per chunk of a complete or repairable
		// group, each of exactly the declared length.
		for ci, c := range s.manifest.Groups[gi].Chunks {
			if len(chunks[ci]) != c.Length {
				return nil, append(missing, gi)
			}
			buf.Write(chunks[ci])
		}
	}
	if len(missing) > 0 {
		return nil, missing
	}
	return buf.Bytes(), nil
}

func digestSlice(d Digest) []byte { return d[:] }
