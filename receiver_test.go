package advisory_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"sync"
	"testing"

	"advisory"
)

// ---------------------------------------------------------------------------
// Synthetic sending region: signs manifest regions with a detached MAC; the
// verifier accepts a region only when that exact byte string was signed under
// the same key.
// ---------------------------------------------------------------------------

type signer struct {
	key []byte
	mu  sync.Mutex
	sig map[string][]byte // region digest hex -> signature
}

func newSigner(key string) *signer {
	return &signer{key: []byte(key), sig: make(map[string][]byte)}
}

func (s *signer) mac(region []byte) []byte {
	h := sha256.New()
	h.Write(s.key)
	h.Write([]byte{0})
	h.Write(region)
	return h.Sum(nil)
}

func (s *signer) sign(region []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sig[advisory.DigestOf(region).String()] = s.mac(region)
}

func (s *signer) verifier() advisory.Verifier {
	return advisory.VerifierFunc(func(region []byte) error {
		s.mu.Lock()
		defer s.mu.Unlock()
		want, ok := s.sig[advisory.DigestOf(region).String()]
		if !ok || !bytes.Equal(want, s.mac(region)) {
			return errors.New("signature does not cover this region")
		}
		return nil
	})
}

// ---------------------------------------------------------------------------
// Bulletin fixtures
// ---------------------------------------------------------------------------

type fixture struct {
	version  uint64
	declared int
	groups   [][][]byte // per group, per chunk payload
	payload  []byte     // concatenation of every chunk
	manifest advisory.Manifest
	region   []byte // canonical signed JSON
}

// makeFixture builds a bulletin with the given per-group chunk sizes. Chunk
// bytes are deterministic and group-specific. declared == 0 means "the whole
// payload"; a smaller value exercises truncation of trailing padding.
func makeFixture(t *testing.T, version uint64, declared int, sizes [][]int) *fixture {
	t.Helper()
	f := &fixture{version: version, declared: declared, groups: make([][][]byte, len(sizes))}
	for gi, gs := range sizes {
		f.groups[gi] = make([][]byte, len(gs))
		for ci, n := range gs {
			b := make([]byte, n)
			for i := range b {
				b[i] = byte(gi*31 + ci*7 + i + 1)
			}
			f.groups[gi][ci] = b
		}
	}
	f.reseal(t)
	if f.declared > len(f.payload) {
		t.Fatalf("declared length %d exceeds payload %d", f.declared, len(f.payload))
	}
	return f
}

// reseal recomputes payload, manifest and region from the current chunk bytes.
func (f *fixture) reseal(t *testing.T) {
	t.Helper()
	var all []byte
	for _, g := range f.groups {
		for _, c := range g {
			all = append(all, c...)
		}
	}
	f.payload = all
	if f.declared == 0 {
		f.declared = len(all)
	}
	f.manifest = buildManifest(t, f.version, f.declared, f.payload, f.groups)
	f.region = mustJSON(t, f.manifest)
}

// parityBlock recomputes the XOR block for a group, zero-extending every
// chunk on the right to the group's span.
func (f *fixture) parityBlock(gi int) []byte {
	span := 0
	for _, c := range f.groups[gi] {
		if len(c) > span {
			span = len(c)
		}
	}
	p := make([]byte, span)
	for _, c := range f.groups[gi] {
		for i := range c {
			p[i] ^= c[i]
		}
	}
	return p
}

// submitAll delivers every group's parity block and chunks, in reverse index
// order, to show that arrival order does not matter.
func (f *fixture) submitAll(t *testing.T, r *advisory.Receiver) {
	t.Helper()
	for gi := len(f.groups) - 1; gi >= 0; gi-- {
		if err := r.SubmitParity(gi, f.parityBlock(gi)); err != nil {
			t.Fatalf("SubmitParity(%d): %v", gi, err)
		}
		for ci := len(f.groups[gi]) - 1; ci >= 0; ci-- {
			if err := r.SubmitChunk(gi, ci, f.groups[gi][ci]); err != nil {
				t.Fatalf("SubmitChunk(%d,%d): %v", gi, ci, err)
			}
		}
	}
}

func buildManifest(t *testing.T, version uint64, declared int, payload []byte, groups [][][]byte) advisory.Manifest {
	t.Helper()
	m := advisory.Manifest{
		Version:  version,
		Length:   declared,
		Bulletin: advisory.DigestOf(payload[:declared]),
		Groups:   make([]advisory.Group, len(groups)),
	}
	for gi, gs := range groups {
		g := advisory.Group{Chunks: make([]advisory.Chunk, len(gs))}
		for ci, c := range gs {
			g.Chunks[ci] = advisory.Chunk{Length: len(c), Digest: advisory.DigestOf(c)}
		}
		span := 0
		for _, c := range gs {
			if len(c) > span {
				span = len(c)
			}
		}
		parity := make([]byte, span)
		for _, c := range gs {
			for i := range c {
				parity[i] ^= c[i]
			}
		}
		g.Parity = advisory.DigestOf(parity)
		m.Groups[gi] = g
	}
	return m
}

type wireRef struct {
	Version  uint64    `json:"version"`
	Length   int       `json:"length"`
	Bulletin string    `json:"bulletin"`
	Groups   []wireGrp `json:"groups"`
}

type wireGrp struct {
	Chunks []wireChunk `json:"chunks"`
	Parity string      `json:"parity"`
}

type wireChunk struct {
	Length int    `json:"length"`
	Digest string `json:"digest"`
}

func mustJSON(t *testing.T, m advisory.Manifest) []byte {
	return mustJSONCase(t, m, false)
}

func mustJSONCase(t *testing.T, m advisory.Manifest, upper bool) []byte {
	t.Helper()
	digest := func(d advisory.Digest) string {
		s := d.String()
		if upper {
			return strings.ToUpper(s)
		}
		return s
	}
	w := wireRef{
		Version:  m.Version,
		Length:   m.Length,
		Bulletin: digest(m.Bulletin),
		Groups:   make([]wireGrp, len(m.Groups)),
	}
	for gi, g := range m.Groups {
		wg := wireGrp{Parity: digest(g.Parity), Chunks: make([]wireChunk, len(g.Chunks))}
		for ci, c := range g.Chunks {
			wg.Chunks[ci] = wireChunk{Length: c.Length, Digest: digest(c.Digest)}
		}
		w.Groups[gi] = wg
	}
	b, err := json.Marshal(w)
	if err != nil {
		t.Fatalf("marshal manifest: %v", err)
	}
	return b
}

func newReceiver(t *testing.T, s *signer) *advisory.Receiver {
	t.Helper()
	r, err := advisory.New(s.verifier())
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return r
}

func offer(t *testing.T, r *advisory.Receiver, s *signer, f *fixture) (advisory.Manifest, error) {
	t.Helper()
	s.sign(f.region)
	return r.OfferManifest(f.region)
}

func missingGroups(err error) []int {
	var ie *advisory.IncompleteError
	if errors.As(err, &ie) {
		return ie.Missing
	}
	return nil
}

// ---------------------------------------------------------------------------
// Accept path
// ---------------------------------------------------------------------------

// TestPublishOutOfOrderWithParityRepair is the main accept path: chunks arrive
// in any order, one chunk of a group with uneven chunk lengths is rebuilt from
// the XOR block using right zero extension, and the result is cut to the
// declared length.
func TestPublishOutOfOrderWithParityRepair(t *testing.T) {
	s := newSigner("region-key")
	r := newReceiver(t, s)

	// Group 0: span 9 with uneven chunks. Group 1: span 12, missing chunk 0 is
	// the shorter one, so its rebuild must truncate. Group 2: the declared
	// length 40 ends inside its second chunk.
	f := makeFixture(t, 7, 40, [][]int{
		{5, 9, 3},
		{4, 12},
		{6, 6, 6},
	})
	if _, err := offer(t, r, s, f); err != nil {
		t.Fatalf("OfferManifest: %v", err)
	}

	// Group 1 has exactly one missing chunk and its parity block is present.
	if err := r.SubmitParity(1, f.parityBlock(1)); err != nil {
		t.Fatalf("SubmitParity(1): %v", err)
	}
	if err := r.SubmitChunk(1, 1, f.groups[1][1]); err != nil {
		t.Fatalf("SubmitChunk(1,1): %v", err)
	}
	st := r.Status()
	if !st.Groups[1].Repairable || st.Groups[1].Complete {
		t.Fatalf("group 1 = %+v, want repairable", st.Groups[1])
	}
	if !reflect.DeepEqual(st.MissingGroups, []int{0, 2}) {
		t.Fatalf("MissingGroups = %v, want [0 2]", st.MissingGroups)
	}
	if st.Ready {
		t.Fatalf("status reported ready while groups 0 and 2 are missing")
	}
	_, err := r.Publish()
	if !errors.Is(err, advisory.ErrIncomplete) {
		t.Fatalf("Publish with missing groups: err = %v, want ErrIncomplete", err)
	}
	if got := missingGroups(err); !reflect.DeepEqual(got, []int{0, 2}) {
		t.Fatalf("IncompleteError.Missing = %v, want [0 2]", got)
	}

	// Deliver the rest in reverse order, leaving group 1 chunk 0 out so that
	// only the parity block can complete the bulletin.
	for gi := 2; gi >= 0; gi-- {
		if err := r.SubmitParity(gi, f.parityBlock(gi)); err != nil {
			t.Fatalf("SubmitParity(%d): %v", gi, err)
		}
		for ci := len(f.groups[gi]) - 1; ci >= 0; ci-- {
			if gi == 1 && ci == 0 {
				continue
			}
			if err := r.SubmitChunk(gi, ci, f.groups[gi][ci]); err != nil {
				t.Fatalf("SubmitChunk(%d,%d): %v", gi, ci, err)
			}
		}
	}
	st = r.Status()
	if !st.Ready || len(st.MissingGroups) != 0 {
		t.Fatalf("status = %+v, want ready", st)
	}
	if !st.Groups[1].Repairable || st.Groups[1].Complete {
		t.Fatalf("group 1 = %+v, want repairable (not yet assembled)", st.Groups[1])
	}

	b, err := r.Publish()
	if err != nil {
		t.Fatalf("Publish: %v", err)
	}
	if b.Version != 7 || b.Length != 40 {
		t.Fatalf("bulletin = version %d length %d, want 7/40", b.Version, b.Length)
	}
	if !bytes.Equal(b.Data, f.payload[:40]) {
		t.Fatalf("bulletin data = %x, want %x", b.Data, f.payload[:40])
	}
	if b.Digest != advisory.DigestOf(f.payload[:40]) {
		t.Fatalf("bulletin digest does not match the declared one")
	}
	cur, ok := r.Current()
	if !ok || cur.Version != 7 || !bytes.Equal(cur.Data, b.Data) {
		t.Fatalf("Current() = %+v, %v", cur, ok)
	}
	if st := r.Status(); st.HasManifest {
		t.Fatalf("pending session must be cleared after publish: %+v", st)
	}
}

// TestFullDelivery covers direct delivery of every chunk with parity present.
func TestFullDelivery(t *testing.T) {
	s := newSigner("k")
	r := newReceiver(t, s)
	f := makeFixture(t, 1, 0, [][]int{{3, 3}, {4}})
	if _, err := offer(t, r, s, f); err != nil {
		t.Fatalf("OfferManifest: %v", err)
	}
	f.submitAll(t, r)
	b, err := r.Publish()
	if err != nil {
		t.Fatalf("Publish: %v", err)
	}
	if !bytes.Equal(b.Data, f.payload) {
		t.Fatalf("data = %x, want %x", b.Data, f.payload)
	}
}

// TestTruncatesToDeclaredLength proves trailing padding never reaches the
// published bulletin.
func TestTruncatesToDeclaredLength(t *testing.T) {
	s := newSigner("k")
	r := newReceiver(t, s)
	f := makeFixture(t, 2, 7, [][]int{{4}, {8}})
	if _, err := offer(t, r, s, f); err != nil {
		t.Fatalf("OfferManifest: %v", err)
	}
	f.submitAll(t, r)
	b, err := r.Publish()
	if err != nil {
		t.Fatalf("Publish: %v", err)
	}
	if len(b.Data) != 7 || !bytes.Equal(b.Data, f.payload[:7]) {
		t.Fatalf("data = %x (len %d), want %x", b.Data, len(b.Data), f.payload[:7])
	}
}

// ---------------------------------------------------------------------------
// Reject paths
// ---------------------------------------------------------------------------

// TestUnverifiedManifestCannotPublish proves an unverifiable manifest changes
// no state and can never publish.
func TestUnverifiedManifestCannotPublish(t *testing.T) {
	s := newSigner("k")
	r := newReceiver(t, s)
	f := makeFixture(t, 9, 0, [][]int{{4}, {4}})

	// Never signed.
	if _, err := r.OfferManifest(f.region); !errors.Is(err, advisory.ErrUnverified) {
		t.Fatalf("OfferManifest(unsigned) err = %v, want ErrUnverified", err)
	}
	if st := r.Status(); st.HasManifest {
		t.Fatalf("unsigned manifest changed receiver state: %+v", st)
	}
	if _, err := r.Publish(); !errors.Is(err, advisory.ErrNoSession) {
		t.Fatalf("Publish: err = %v, want ErrNoSession", err)
	}

	// Signed region then tampered: the signature no longer covers the bytes.
	s.sign(f.region)
	tampered := append([]byte(nil), f.region...)
	tampered[len(tampered)-3] ^= 0x01
	if _, err := r.OfferManifest(tampered); !errors.Is(err, advisory.ErrUnverified) {
		t.Fatalf("OfferManifest(tampered) err = %v, want ErrUnverified", err)
	}

	// A region signed by a different sender must be refused, even though its
	// bytes are a valid manifest.
	foreign := makeFixture(t, 33, 0, [][]int{{4}, {4}, {1}})
	newSigner("other-key").sign(foreign.region)
	if _, err := r.OfferManifest(foreign.region); !errors.Is(err, advisory.ErrUnverified) {
		t.Fatalf("OfferManifest(foreign signature) err = %v, want ErrUnverified", err)
	}
	if st := r.Status(); st.HasManifest {
		t.Fatalf("rejected manifest changed receiver state: %+v", st)
	}
}

// TestCorruptDataCountsAsMissing shows that data failing its digest is not
// stored, is reported missing, and does not displace a later valid copy.
func TestCorruptDataCountsAsMissing(t *testing.T) {
	s := newSigner("k")
	r := newReceiver(t, s)
	f := makeFixture(t, 3, 0, [][]int{{5, 5}, {5, 5}})
	if _, err := offer(t, r, s, f); err != nil {
		t.Fatalf("OfferManifest: %v", err)
	}
	good := f.groups[0][0]

	bad := append([]byte(nil), good...)
	bad[1] ^= 0xFF
	if err := r.SubmitChunk(0, 0, bad); !errors.Is(err, advisory.ErrChunkDigestMismatch) {
		t.Fatalf("SubmitChunk(corrupt) err = %v, want ErrChunkDigestMismatch", err)
	}
	if err := r.SubmitChunk(0, 0, good[:len(good)-1]); !errors.Is(err, advisory.ErrChunkDigestMismatch) {
		t.Fatalf("SubmitChunk(short) err = %v, want ErrChunkDigestMismatch", err)
	}
	st := r.Status()
	if !reflect.DeepEqual(st.Groups[0].MissingChunks, []int{0, 1}) {
		t.Fatalf("group 0 missing = %v, want [0 1] (corrupt chunk must not be stored)", st.Groups[0].MissingChunks)
	}
	// Group 1 was never offered anything, so it is missing as well.
	if !reflect.DeepEqual(st.MissingGroups, []int{0, 1}) {
		t.Fatalf("MissingGroups = %v, want [0 1]", st.MissingGroups)
	}
	if st.Groups[0].Repairable {
		t.Fatalf("group 0 repairable without a parity block")
	}
	if _, err := r.Publish(); !errors.Is(err, advisory.ErrIncomplete) {
		t.Fatalf("Publish err = %v, want ErrIncomplete", err)
	}
	if got := missingGroups(func() error { _, e := r.Publish(); return e }()); !reflect.DeepEqual(got, []int{0, 1}) {
		t.Fatalf("IncompleteError.Missing = %v, want [0 1]", got)
	}
	// A valid retransmission is accepted, and the corrupt copy never replaced
	// anything.
	if err := r.SubmitChunk(0, 0, good); err != nil {
		t.Fatalf("SubmitChunk(valid retransmission) err = %v", err)
	}
	if got := r.Status().Groups[0].MissingChunks; !reflect.DeepEqual(got, []int{1}) {
		t.Fatalf("group 0 missing = %v, want [1] after valid chunk 0", got)
	}

	// A corrupt parity block counts as no parity at all.
	p := f.parityBlock(1)
	badParity := append([]byte(nil), p...)
	badParity[0] ^= 0xFF
	if err := r.SubmitParity(1, badParity); !errors.Is(err, advisory.ErrChunkDigestMismatch) {
		t.Fatalf("SubmitParity(corrupt) err = %v, want ErrChunkDigestMismatch", err)
	}
	if r.Status().Groups[1].ParityReceived {
		t.Fatalf("corrupt parity was stored")
	}

	f.submitAll(t, r)
	if _, err := r.Publish(); err != nil {
		t.Fatalf("Publish after repair: %v", err)
	}
}

// TestParityRepairsCorruptChunk covers the cross-cutting rule that a chunk
// failing its digest is treated exactly like a missing chunk, so the parity
// block can still recover it. It also covers the case where the missing chunk
// is the group's longest, i.e. no right truncation is needed.
func TestParityRepairsCorruptChunk(t *testing.T) {
	s := newSigner("k")
	r := newReceiver(t, s)
	// Group 0: span 6 belongs to chunk 1. Group 1: span 8 belongs to chunk 0.
	f := makeFixture(t, 15, 0, [][]int{{4, 6}, {8, 2}})
	if _, err := offer(t, r, s, f); err != nil {
		t.Fatalf("OfferManifest: %v", err)
	}
	for gi := range f.groups {
		if err := r.SubmitParity(gi, f.parityBlock(gi)); err != nil {
			t.Fatalf("SubmitParity(%d): %v", gi, err)
		}
	}
	// Group 0: chunk 0 arrives corrupt; chunk 1 (the longest) is fine.
	bad := append([]byte(nil), f.groups[0][0]...)
	bad[0] ^= 0x80
	if err := r.SubmitChunk(0, 0, bad); !errors.Is(err, advisory.ErrChunkDigestMismatch) {
		t.Fatalf("SubmitChunk(corrupt) err = %v, want ErrChunkDigestMismatch", err)
	}
	if err := r.SubmitChunk(0, 1, f.groups[0][1]); err != nil {
		t.Fatalf("SubmitChunk: %v", err)
	}
	// Group 1: chunk 1 arrives corrupt; chunk 0 (the longest) is fine, so the
	// rebuilt chunk is the short one again.
	bad2 := append([]byte(nil), f.groups[1][1]...)
	bad2[0] ^= 0x80
	if err := r.SubmitChunk(1, 1, bad2); !errors.Is(err, advisory.ErrChunkDigestMismatch) {
		t.Fatalf("SubmitChunk(corrupt) err = %v, want ErrChunkDigestMismatch", err)
	}
	if err := r.SubmitChunk(1, 0, f.groups[1][0]); err != nil {
		t.Fatalf("SubmitChunk: %v", err)
	}
	st := r.Status()
	for gi := range f.groups {
		if !st.Groups[gi].Repairable {
			t.Fatalf("group %d = %+v, want repairable via parity", gi, st.Groups[gi])
		}
	}
	if st.Ready != true {
		t.Fatalf("status not ready: %+v", st)
	}
	b, err := r.Publish()
	if err != nil {
		t.Fatalf("Publish: %v", err)
	}
	if !bytes.Equal(b.Data, f.payload) {
		t.Fatalf("repaired bulletin = %x, want %x", b.Data, f.payload)
	}
}

// TestParityCannotFakeChunk proves the XOR result is checked against the
// manifest: a parity block that does not correspond to the group is refused
// outright, so it can never fabricate chunk bytes.
func TestParityCannotFakeChunk(t *testing.T) {
	s := newSigner("k")
	r := newReceiver(t, s)
	f := makeFixture(t, 16, 0, [][]int{{4, 4}})
	if _, err := offer(t, r, s, f); err != nil {
		t.Fatalf("OfferManifest: %v", err)
	}
	wrong := make([]byte, 4)
	if err := r.SubmitParity(0, wrong); !errors.Is(err, advisory.ErrChunkDigestMismatch) {
		t.Fatalf("SubmitParity(wrong) err = %v, want ErrChunkDigestMismatch", err)
	}
	if err := r.SubmitChunk(0, 0, f.groups[0][0]); err != nil {
		t.Fatalf("SubmitChunk: %v", err)
	}
	if st := r.Status(); st.Groups[0].Repairable || st.Groups[0].ParityReceived {
		t.Fatalf("rejected parity surfaced as received: %+v", st.Groups[0])
	}
	if _, err := r.Publish(); !errors.Is(err, advisory.ErrIncomplete) {
		t.Fatalf("Publish err = %v, want ErrIncomplete", err)
	}
}

// TestTwoMissingChunksAreNotRepairable is the boundary of the XOR rule: one
// missing chunk is recoverable, two are not.
func TestTwoMissingChunksAreNotRepairable(t *testing.T) {
	s := newSigner("k")
	r := newReceiver(t, s)
	f := makeFixture(t, 4, 0, [][]int{{4, 4, 4}})
	if _, err := offer(t, r, s, f); err != nil {
		t.Fatalf("OfferManifest: %v", err)
	}
	if err := r.SubmitParity(0, f.parityBlock(0)); err != nil {
		t.Fatalf("SubmitParity: %v", err)
	}
	if err := r.SubmitChunk(0, 2, f.groups[0][2]); err != nil {
		t.Fatalf("SubmitChunk: %v", err)
	}
	st := r.Status()
	if st.Groups[0].Repairable {
		t.Fatalf("group with two missing chunks reported repairable")
	}
	if !reflect.DeepEqual(st.MissingGroups, []int{0}) {
		t.Fatalf("MissingGroups = %v, want [0]", st.MissingGroups)
	}
	_, err := r.Publish()
	if !errors.Is(err, advisory.ErrIncomplete) {
		t.Fatalf("Publish err = %v, want ErrIncomplete", err)
	}
	if !reflect.DeepEqual(missingGroups(err), []int{0}) {
		t.Fatalf("IncompleteError.Missing = %v, want [0]", missingGroups(err))
	}

	// Supplying one of the two makes the group repairable again.
	if err := r.SubmitChunk(0, 0, f.groups[0][0]); err != nil {
		t.Fatalf("SubmitChunk: %v", err)
	}
	if !r.Status().Groups[0].Repairable {
		t.Fatalf("group 0 not repairable after supplying one of two missing")
	}
	b, err := r.Publish()
	if err != nil {
		t.Fatalf("Publish: %v", err)
	}
	if !bytes.Equal(b.Data, f.payload) {
		t.Fatalf("data = %x, want %x", b.Data, f.payload)
	}
}

// TestSameVersionDifferentManifestConflicts proves two distinct manifests with
// one version are never mixed, and the admitted one keeps its data.
func TestSameVersionDifferentManifestConflicts(t *testing.T) {
	s := newSigner("k")
	r := newReceiver(t, s)
	f := makeFixture(t, 5, 0, [][]int{{4, 4}})
	if _, err := offer(t, r, s, f); err != nil {
		t.Fatalf("OfferManifest: %v", err)
	}
	if err := r.SubmitChunk(0, 0, f.groups[0][0]); err != nil {
		t.Fatalf("SubmitChunk: %v", err)
	}

	// Same version, different chunk content -> different manifest.
	alt := makeFixture(t, 5, 0, [][]int{{4, 4}})
	alt.groups[0][1] = bytes.Repeat([]byte{'Z'}, 4)
	alt.reseal(t)
	if _, err := offer(t, r, s, alt); !errors.Is(err, advisory.ErrManifestConflict) {
		t.Fatalf("OfferManifest(conflict) err = %v, want ErrManifestConflict", err)
	}
	st := r.Status()
	if st.Version != 5 || !reflect.DeepEqual(st.Groups[0].MissingChunks, []int{1}) {
		t.Fatalf("conflicting manifest disturbed the admitted session: %+v", st.Groups)
	}
	if err := r.SubmitChunk(0, 1, f.groups[0][1]); err != nil {
		t.Fatalf("SubmitChunk: %v", err)
	}
	b, err := r.Publish()
	if err != nil {
		t.Fatalf("Publish: %v", err)
	}
	if !bytes.Equal(b.Data, f.payload) {
		t.Fatalf("published data came from the conflicting manifest")
	}

	// An identical retransmission of the published version is stale.
	if _, err := offer(t, r, s, f); !errors.Is(err, advisory.ErrStaleManifest) {
		t.Fatalf("OfferManifest(published version) err = %v, want ErrStaleManifest", err)
	}
	// Same version, different content after publication is a conflict.
	alt2 := makeFixture(t, 5, 0, [][]int{{8}})
	if _, err := offer(t, r, s, alt2); !errors.Is(err, advisory.ErrManifestConflict) {
		t.Fatalf("OfferManifest(published conflict) err = %v, want ErrManifestConflict", err)
	}
	cur, ok := r.Current()
	if !ok || !bytes.Equal(cur.Data, f.payload) {
		t.Fatalf("conflict attempt changed the published bulletin")
	}
}

// TestSameVersionRetransmissionKeepsData shows a byte-identical manifest
// retransmission does not discard collected chunks.
func TestSameVersionRetransmissionKeepsData(t *testing.T) {
	s := newSigner("k")
	r := newReceiver(t, s)
	f := makeFixture(t, 6, 0, [][]int{{4, 4}})
	if _, err := offer(t, r, s, f); err != nil {
		t.Fatalf("OfferManifest: %v", err)
	}
	if err := r.SubmitChunk(0, 0, f.groups[0][0]); err != nil {
		t.Fatalf("SubmitChunk: %v", err)
	}
	if _, err := offer(t, r, s, f); err != nil {
		t.Fatalf("OfferManifest(retransmission): %v", err)
	}
	if got := r.Status().Groups[0].MissingChunks; !reflect.DeepEqual(got, []int{1}) {
		t.Fatalf("retransmission lost collected data: missing = %v", got)
	}
	if err := r.SubmitChunk(0, 1, f.groups[0][1]); err != nil {
		t.Fatalf("SubmitChunk: %v", err)
	}
	if _, err := r.Publish(); err != nil {
		t.Fatalf("Publish: %v", err)
	}
}

// TestOlderVersionRejected keeps the displayed bulletin when an older version
// arrives, and gives it no pending session.
func TestOlderVersionRejected(t *testing.T) {
	s := newSigner("k")
	r := newReceiver(t, s)

	v2 := makeFixture(t, 2, 0, [][]int{{4}})
	if _, err := offer(t, r, s, v2); err != nil {
		t.Fatalf("OfferManifest(v2): %v", err)
	}
	v2.submitAll(t, r)
	if _, err := r.Publish(); err != nil {
		t.Fatalf("Publish(v2): %v", err)
	}

	v1 := makeFixture(t, 1, 0, [][]int{{4}})
	if _, err := offer(t, r, s, v1); !errors.Is(err, advisory.ErrStaleManifest) {
		t.Fatalf("OfferManifest(v1) err = %v, want ErrStaleManifest", err)
	}
	cur, ok := r.Current()
	if !ok || cur.Version != 2 {
		t.Fatalf("Current = %+v, want v2", cur)
	}
	if st := r.Status(); st.HasManifest {
		t.Fatalf("stale manifest created a pending session: %+v", st)
	}

	// A newer version replaces the pending session and publishes.
	v3 := makeFixture(t, 3, 0, [][]int{{4}})
	if _, err := offer(t, r, s, v3); err != nil {
		t.Fatalf("OfferManifest(v3): %v", err)
	}
	v3.submitAll(t, r)
	b, err := r.Publish()
	if err != nil {
		t.Fatalf("Publish(v3): %v", err)
	}
	if b.Version != 3 {
		t.Fatalf("published version %d, want 3", b.Version)
	}
}

// TestBulletinDigestMismatchBlocksPublish covers the whole-bulletin check: if
// the assembled bytes do not hash to the declared digest nothing is published,
// the previous bulletin stays, and a version bump alone is not enough.
func TestBulletinDigestMismatchBlocksPublish(t *testing.T) {
	s := newSigner("k")
	r := newReceiver(t, s)

	base := makeFixture(t, 10, 0, [][]int{{4}, {4}})
	if _, err := offer(t, r, s, base); err != nil {
		t.Fatalf("OfferManifest(base): %v", err)
	}
	base.submitAll(t, r)
	good, err := r.Publish()
	if err != nil {
		t.Fatalf("Publish(base): %v", err)
	}

	forged := makeFixture(t, 11, 0, [][]int{{4}, {4}})
	forged.manifest.Bulletin = advisory.DigestOf([]byte("something else"))
	forged.region = mustJSON(t, forged.manifest)
	if _, err := offer(t, r, s, forged); err != nil {
		t.Fatalf("OfferManifest(forged): %v", err)
	}
	forged.submitAll(t, r)

	if _, err := r.Publish(); !errors.Is(err, advisory.ErrBulletinDigestMismatch) {
		t.Fatalf("Publish(forged) err = %v, want ErrBulletinDigestMismatch", err)
	}
	cur, ok := r.Current()
	if !ok || cur.Version != 10 || !bytes.Equal(cur.Data, good.Data) {
		t.Fatalf("Current changed after a failed publish: %+v", cur)
	}
	// The rejected data was discarded, so the session is provably short of
	// every group rather than silently re-published.
	if st := r.Status(); !reflect.DeepEqual(st.MissingGroups, []int{0, 1}) {
		t.Fatalf("after digest mismatch MissingGroups = %v, want [0 1]", st.MissingGroups)
	}
}

// TestIncompleteKeepsPreviousBulletin covers the display rule: an unproven
// bulletin never replaces the previous complete one, and the missing groups are
// named.
func TestIncompleteKeepsPreviousBulletin(t *testing.T) {
	s := newSigner("k")
	r := newReceiver(t, s)

	v1 := makeFixture(t, 1, 0, [][]int{{3}, {3}})
	if _, err := offer(t, r, s, v1); err != nil {
		t.Fatalf("OfferManifest(v1): %v", err)
	}
	v1.submitAll(t, r)
	if _, err := r.Publish(); err != nil {
		t.Fatalf("Publish(v1): %v", err)
	}

	// v2 has a partially filled group 0 plus two untouched groups.
	v2 := makeFixture(t, 2, 0, [][]int{{3, 3}, {3}, {3}})
	if _, err := offer(t, r, s, v2); err != nil {
		t.Fatalf("OfferManifest(v2): %v", err)
	}
	if err := r.SubmitChunk(0, 0, v2.groups[0][0]); err != nil {
		t.Fatalf("SubmitChunk: %v", err)
	}
	_, err := r.Publish()
	if !errors.Is(err, advisory.ErrIncomplete) {
		t.Fatalf("Publish(v2 partial) err = %v, want ErrIncomplete", err)
	}
	if got := missingGroups(err); !reflect.DeepEqual(got, []int{0, 1, 2}) {
		t.Fatalf("IncompleteError.Missing = %v, want [0 1 2]", got)
	}
	st := r.Status()
	if st.Current == nil || st.Current.Version != 1 {
		t.Fatalf("Status.Current = %+v, want v1 still displayed", st.Current)
	}
	if st.Ready {
		t.Fatalf("status reported ready while groups are missing")
	}
	if !reflect.DeepEqual(st.MissingGroups, []int{0, 1, 2}) {
		t.Fatalf("Status.MissingGroups = %v, want [0 1 2]", st.MissingGroups)
	}
	if !reflect.DeepEqual(st.Groups[0].MissingChunks, []int{1}) {
		t.Fatalf("group 0 missing chunks = %v, want [1]", st.Groups[0].MissingChunks)
	}

	// Completing v2 publishes it and retires v1.
	v2.submitAll(t, r)
	if _, err := r.Publish(); err != nil {
		t.Fatalf("Publish(v2): %v", err)
	}
	cur, ok := r.Current()
	if !ok || cur.Version != 2 || !bytes.Equal(cur.Data, v2.payload) {
		t.Fatalf("Current = %+v, want v2", cur)
	}
}

// ---------------------------------------------------------------------------
// Malformed input and strict parsing
// ---------------------------------------------------------------------------

// TestMalformedManifestsRejected exercises the strict parser on signed but
// structurally bad regions.
func TestMalformedManifestsRejected(t *testing.T) {
	f := makeFixture(t, 8, 4, [][]int{{4}})
	d := f.manifest.Bulletin.String()

	cases := map[string][]byte{
		"empty":         nil,
		"not json":      []byte("{"),
		"trailing data": append(append([]byte(nil), f.region...), []byte("{}")...),
		"unknown field": bytes.Replace(f.region, []byte(`"version":8`),
			[]byte(`"version":8,"extra":1`), 1),
		"negative version": bytes.Replace(f.region, []byte(`"version":8`),
			[]byte(`"version":-1`), 1),
		"bad hex":      bytes.Replace(f.region, []byte(d), []byte("zz"+d[2:]), 1),
		"short digest": bytes.Replace(f.region, []byte(d), []byte(d[2:]), 1),
		"declared length too large": bytes.Replace(f.region, []byte(`"length":4`),
			[]byte(`"length":5`), 1),
		"chunk length zero": bytes.Replace(f.region, []byte(`"length":4,"digest"`),
			[]byte(`"length":0,"digest"`), 1),
		"no groups": func() []byte {
			b, err := json.Marshal(wireRef{
				Version:  8,
				Length:   1,
				Bulletin: advisory.DigestOf([]byte("a")).String(),
				Groups:   []wireGrp{},
			})
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			return b
		}(),
	}

	for name, region := range cases {
		region := region
		t.Run(name, func(t *testing.T) {
			s := newSigner("k")
			s.sign(region)
			r := newReceiver(t, s)
			if _, err := r.OfferManifest(region); !errors.Is(err, advisory.ErrInvalidManifest) {
				t.Fatalf("OfferManifest(%s) err = %v, want ErrInvalidManifest", name, err)
			}
			if r.Status().HasManifest {
				t.Fatalf("invalid manifest was admitted")
			}
		})
	}
}

// TestUnknownIndicesRejected covers out-of-range group and chunk indices.
func TestUnknownIndicesRejected(t *testing.T) {
	s := newSigner("k")
	r := newReceiver(t, s)
	f := makeFixture(t, 12, 0, [][]int{{2}})
	if _, err := offer(t, r, s, f); err != nil {
		t.Fatalf("OfferManifest: %v", err)
	}
	if err := r.SubmitChunk(1, 0, []byte("xy")); !errors.Is(err, advisory.ErrUnknownGroup) {
		t.Fatalf("SubmitChunk(bad group) err = %v, want ErrUnknownGroup", err)
	}
	if err := r.SubmitChunk(0, 1, []byte("xy")); !errors.Is(err, advisory.ErrUnknownChunk) {
		t.Fatalf("SubmitChunk(bad chunk) err = %v, want ErrUnknownChunk", err)
	}
	if err := r.SubmitParity(-1, []byte("xy")); !errors.Is(err, advisory.ErrUnknownGroup) {
		t.Fatalf("SubmitParity(bad group) err = %v, want ErrUnknownGroup", err)
	}
}

// TestNilVerifierRefused proves the receiver cannot exist without a source
// checker, because nothing could then be published safely.
func TestNilVerifierRefused(t *testing.T) {
	if _, err := advisory.New(nil); !errors.Is(err, advisory.ErrNoVerifier) {
		t.Fatalf("New(nil) err = %v, want ErrNoVerifier", err)
	}
}

// TestDigestCaseInsensitive covers hex normalisation and its effect on
// manifest identity: a re-signed region that differs only in hex case is the
// same bulletin, not a conflicting one.
func TestDigestCaseInsensitive(t *testing.T) {
	d := advisory.DigestOf([]byte("abc"))
	got, err := advisory.ParseDigest(strings.ToUpper(d.String()))
	if err != nil {
		t.Fatalf("ParseDigest(upper): %v", err)
	}
	if got != d {
		t.Fatalf("ParseDigest(upper) = %x, want %x", got, d)
	}
	if _, err := advisory.ParseDigest(d.String()[:10]); !errors.Is(err, advisory.ErrInvalidManifest) {
		t.Fatalf("ParseDigest(short) err = %v, want ErrInvalidManifest", err)
	}
	if _, err := advisory.ParseDigest("zz" + d.String()[2:]); !errors.Is(err, advisory.ErrInvalidManifest) {
		t.Fatalf("ParseDigest(non-hex) err = %v, want ErrInvalidManifest", err)
	}

	s := newSigner("k")
	r := newReceiver(t, s)
	f := makeFixture(t, 13, 0, [][]int{{4, 4}})
	if _, err := offer(t, r, s, f); err != nil {
		t.Fatalf("OfferManifest: %v", err)
	}
	if err := r.SubmitChunk(0, 0, f.groups[0][0]); err != nil {
		t.Fatalf("SubmitChunk: %v", err)
	}

	// Same manifest, digests re-encoded in upper-case hex, signed afresh.
	upper := mustJSONCase(t, f.manifest, true)
	if bytes.Equal(upper, f.region) {
		t.Fatalf("fixture unexpectedly already upper-case")
	}
	s.sign(upper)
	if _, err := r.OfferManifest(upper); err != nil {
		t.Fatalf("OfferManifest(upper-case) err = %v, want nil (same manifest)", err)
	}
	if got := r.Status().Groups[0].MissingChunks; !reflect.DeepEqual(got, []int{1}) {
		t.Fatalf("upper-case retransmission lost data: missing = %v", got)
	}
}

// TestConcurrentSubmissions exercises the receiver's locking with out-of-order
// concurrent delivery; the result must still assemble exactly.
func TestConcurrentSubmissions(t *testing.T) {
	s := newSigner("k")
	r := newReceiver(t, s)
	f := makeFixture(t, 14, 0, [][]int{{4, 4, 4}, {4, 4}, {4}})
	if _, err := offer(t, r, s, f); err != nil {
		t.Fatalf("OfferManifest: %v", err)
	}

	var wg sync.WaitGroup
	errs := make(chan error, 32)
	for gi := range f.groups {
		gi := gi
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := r.SubmitParity(gi, f.parityBlock(gi)); err != nil {
				errs <- err
				return
			}
			for ci := range f.groups[gi] {
				if err := r.SubmitChunk(gi, ci, f.groups[gi][ci]); err != nil {
					errs <- err
				}
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent submit: %v", err)
	}
	b, err := r.Publish()
	if err != nil {
		t.Fatalf("Publish: %v", err)
	}
	if !bytes.Equal(b.Data, f.payload) {
		t.Fatalf("data mismatch after concurrent delivery")
	}
}
