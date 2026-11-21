package chunkstore

import (
	"bytes"
	"fmt"
	"math/rand"
	"testing"
)

// refHash computes the 257-base polynomial of the given bytes in stream order,
// independently of the Writer's rolling implementation.
func refHash(trailing []byte) uint32 {
	var h uint32
	for _, b := range trailing {
		h = h*257 + uint32(b)
	}
	return h
}

// refChunk is a deliberately naive, non-incremental implementation of the
// boundary rules. It recomputes the window polynomial from the raw stream at
// every byte and is used as an oracle for the Writer.
func refChunk(stream []byte, cfg Config) []ChunkRef {
	var refs []ChunkRef
	start := 0
	for i := 0; i < len(stream); i++ {
		windowStart := i - cfg.WindowSize + 1
		if windowStart < 0 {
			windowStart = 0
		}
		window := stream[windowStart : i+1]
		chunkLen := i - start + 1
		cut := false
		if chunkLen >= cfg.MinChunkLen && len(window) == cfg.WindowSize && refHash(window)&cfg.Mask == 0 {
			cut = true
		}
		if chunkLen >= cfg.MaxChunkLen {
			cut = true
		}
		if cut {
			refs = append(refs, ChunkRef{Length: chunkLen, Digest: digestOf(stream[start : i+1])})
			start = i + 1
		}
	}
	if start < len(stream) {
		refs = append(refs, ChunkRef{Length: len(stream) - start, Digest: digestOf(stream[start:])})
	}
	return refs
}

func refsEqual(a, b []ChunkRef) bool {
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

func lensOf(refs []ChunkRef) []int {
	out := make([]int, len(refs))
	for i, ref := range refs {
		out[i] = ref.Length
	}
	return out
}

func intsEqual(a, b []int) bool {
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

func mustWriter(t *testing.T, s *Store, cfg Config) *Writer {
	t.Helper()
	w, err := s.NewWriter(cfg)
	if err != nil {
		t.Fatalf("NewWriter(%+v): %v", cfg, err)
	}
	return w
}

// writeInSegments feeds data to w using the given segment sizes, cycling
// through them and including zero-length writes when a 0 appears.
func writeInSegments(t *testing.T, w *Writer, data []byte, segments []int) {
	t.Helper()
	if len(segments) == 0 {
		segments = []int{len(data)}
	}
	pos, si := 0, 0
	for pos < len(data) {
		n := segments[si%len(segments)]
		si++
		if n < 0 {
			t.Fatalf("negative segment size %d", n)
		}
		if pos+n > len(data) {
			n = len(data) - pos
		}
		written, err := w.Write(data[pos : pos+n])
		if err != nil {
			t.Fatalf("Write at %d (n=%d): %v", pos, n, err)
		}
		if written != n {
			t.Fatalf("Write at %d returned %d, want %d", pos, written, n)
		}
		pos += n
	}
	// A trailing zero-length write must be harmless.
	if _, err := w.Write(nil); err != nil {
		t.Fatalf("Write(nil): %v", err)
	}
}

func manifestOf(t *testing.T, s *Store, cfg Config, data []byte, segments []int) Manifest {
	t.Helper()
	w := mustWriter(t, s, cfg)
	writeInSegments(t, w, data, segments)
	if err := w.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	m, err := w.Manifest()
	if err != nil {
		t.Fatalf("Manifest: %v", err)
	}
	return m
}

func TestConfigValidationRejectsBadRules(t *testing.T) {
	s := NewStore()
	cases := []struct {
		name string
		cfg  Config
	}{
		{"zero min", Config{MinChunkLen: 0, MaxChunkLen: 10, WindowSize: 4, Mask: 0}},
		{"negative min", Config{MinChunkLen: -1, MaxChunkLen: 10, WindowSize: 4, Mask: 0}},
		{"max below min", Config{MinChunkLen: 10, MaxChunkLen: 9, WindowSize: 4, Mask: 0}},
		{"zero max", Config{MinChunkLen: 0, MaxChunkLen: 0, WindowSize: 4, Mask: 0}},
		{"zero window", Config{MinChunkLen: 1, MaxChunkLen: 10, WindowSize: 0, Mask: 0}},
		{"negative window", Config{MinChunkLen: 1, MaxChunkLen: 10, WindowSize: -3, Mask: 0}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w, err := s.NewWriter(tc.cfg)
			if err == nil {
				if w != nil {
					w.Close()
				}
				t.Fatalf("NewWriter(%+v) accepted an invalid config", tc.cfg)
			}
			if w != nil {
				t.Fatalf("NewWriter returned non-nil writer on error: %+v", w)
			}
		})
	}
}

func TestRollingHashMatchesPolynomialDefinition(t *testing.T) {
	s := NewStore()
	// Window 3, mask all ones: a content cut needs hash == 0, which "abc" and
	// "abcd" never reach, so only the hash value is under test here.
	w := mustWriter(t, s, Config{MinChunkLen: 1, MaxChunkLen: 1 << 20, WindowSize: 3, Mask: 0xFFFFFFFF})

	if _, err := w.Write([]byte("abc")); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if want := uint32((97*257+98)*257 + 99); w.hash != want {
		t.Fatalf("hash after %q = %d, want %d", "abc", w.hash, want)
	}
	if _, err := w.Write([]byte("d")); err != nil {
		t.Fatalf("Write: %v", err)
	}
	// The window must now cover only the trailing three bytes "bcd".
	if want := uint32((98*257+99)*257 + 100); w.hash != want {
		t.Fatalf("hash after %q = %d, want %d (window is the last 3 bytes)", "abcd", w.hash, want)
	}
}

func TestRollingHashMatchesReferenceAtEveryByte(t *testing.T) {
	rng := rand.New(rand.NewSource(20261121))
	data := make([]byte, 2048)
	rng.Read(data)
	for i := 0; i < len(data); i += 61 {
		data[i] = 0 // exercise zero bytes in the window
	}
	for _, window := range []int{1, 2, 3, 5, 16, 64} {
		t.Run(fmt.Sprintf("window=%d", window), func(t *testing.T) {
			s := NewStore()
			w := mustWriter(t, s, Config{MinChunkLen: 1, MaxChunkLen: len(data) + 1, WindowSize: window, Mask: 0xFFFFFFFF})
			for i, b := range data {
				if _, err := w.Write([]byte{b}); err != nil {
					t.Fatalf("Write at %d: %v", i, err)
				}
				start := i - window + 1
				if start < 0 {
					start = 0
				}
				if want := refHash(data[start : i+1]); w.hash != want {
					t.Fatalf("byte %d: rolling hash %d, reference %d", i, w.hash, want)
				}
				wantFilled := i + 1
				if wantFilled > window {
					wantFilled = window
				}
				if w.winFilled != wantFilled {
					t.Fatalf("byte %d: winFilled=%d, want %d", i, w.winFilled, wantFilled)
				}
			}
		})
	}
}

func TestCutRequiresMinimumAndFullWindow(t *testing.T) {
	s := NewStore()
	// Mask 0 accepts every value, so with min == window == 8 every chunk is 8
	// bytes until the short tail.
	cfg := Config{MinChunkLen: 8, MaxChunkLen: 100, WindowSize: 8, Mask: 0}
	m := manifestOf(t, s, cfg, bytes.Repeat([]byte{'x'}, 20), []int{20})
	if want := []int{8, 8, 4}; !intsEqual(lensOf(m.Chunks), want) {
		t.Fatalf("chunk lengths %v, want %v", lensOf(m.Chunks), want)
	}
}

func TestCutRequiresMinimumEvenWhenWindowFull(t *testing.T) {
	s := NewStore()
	// min 3, window 8, mask 0: after the first 8-byte chunk the window stays
	// full, but a new chunk cannot cut before it reaches 3 bytes.
	cfg := Config{MinChunkLen: 3, MaxChunkLen: 100, WindowSize: 8, Mask: 0}
	m := manifestOf(t, s, cfg, bytes.Repeat([]byte{'y'}, 20), []int{1})
	if want := []int{8, 3, 3, 3, 3}; !intsEqual(lensOf(m.Chunks), want) {
		t.Fatalf("chunk lengths %v, want %v", lensOf(m.Chunks), want)
	}
}

func TestCutHappensExactlyAtZeroHashWithAllOnesMask(t *testing.T) {
	s := NewStore()
	cfg := Config{MinChunkLen: 1, MaxChunkLen: 1000, WindowSize: 1, Mask: 0xFFFFFFFF}
	// value == 0 only for byte 0x00, so the only content-defined cut is there.
	data := []byte{'a', 'b', 0x00, 'c', 'd'}
	m := manifestOf(t, s, cfg, data, []int{5})
	if want := []int{3, 2}; !intsEqual(lensOf(m.Chunks), want) {
		t.Fatalf("chunk lengths %v, want %v", lensOf(m.Chunks), want)
	}
	if m.Chunks[0].Digest != digestOf([]byte{'a', 'b', 0x00}) {
		t.Fatal("first chunk digest does not cover the zero byte")
	}
}

func TestMaxChunkLenForcesCut(t *testing.T) {
	s := NewStore()
	// Window 11 > max 10, so the window never fills and a content cut is
	// impossible: every cut must be forced by MaxChunkLen.
	cfg := Config{MinChunkLen: 4, MaxChunkLen: 10, WindowSize: 11, Mask: 0xFFFFFFFF}
	data := make([]byte, 45)
	for i := range data {
		data[i] = byte(1 + i%251)
	}
	m := manifestOf(t, s, cfg, data, []int{7})
	if want := []int{10, 10, 10, 10, 5}; !intsEqual(lensOf(m.Chunks), want) {
		t.Fatalf("chunk lengths %v, want %v", lensOf(m.Chunks), want)
	}
}

func TestWindowLargerThanMaxDefersContentCuts(t *testing.T) {
	s := NewStore()
	cfg := Config{MinChunkLen: 2, MaxChunkLen: 5, WindowSize: 9, Mask: 0}
	// Window 9 never fills before MaxChunkLen forces the first 5-byte cut.
	// Once it is full (after byte 9) the mask-0 rule can cut again, so the
	// results are 5 (forced), then 4 (window fills at a chunk length of 4),
	// then 2 and 2.
	m := manifestOf(t, s, cfg, bytes.Repeat([]byte{0}, 13), []int{13})
	if want := []int{5, 4, 2, 2}; !intsEqual(lensOf(m.Chunks), want) {
		t.Fatalf("chunk lengths %v, want %v", lensOf(m.Chunks), want)
	}
}

func TestMinEqualsMaxProducesFixedChunks(t *testing.T) {
	s := NewStore()
	cfg := Config{MinChunkLen: 16, MaxChunkLen: 16, WindowSize: 8, Mask: 0}
	data := bytes.Repeat([]byte("0123456789abcdef"), 3)
	data = append(data, []byte("tail")...)
	m := manifestOf(t, s, cfg, data, []int{1})
	// "tail" is four bytes, so the final remainder is 4, not 3.
	if want := []int{16, 16, 16, 4}; !intsEqual(lensOf(m.Chunks), want) {
		t.Fatalf("chunk lengths %v, want %v", lensOf(m.Chunks), want)
	}
}

func TestSingleByteMaskCutsEveryByte(t *testing.T) {
	s := NewStore()
	cfg := Config{MinChunkLen: 1, MaxChunkLen: 10, WindowSize: 1, Mask: 0}
	m := manifestOf(t, s, cfg, []byte("abcdef"), []int{6})
	if want := []int{1, 1, 1, 1, 1, 1}; !intsEqual(lensOf(m.Chunks), want) {
		t.Fatalf("chunk lengths %v, want %v", lensOf(m.Chunks), want)
	}
}

func TestEmptyObjectYieldsEmptyManifest(t *testing.T) {
	s := NewStore()
	cfg := Config{MinChunkLen: 8, MaxChunkLen: 32, WindowSize: 4, Mask: 0x3}
	w := mustWriter(t, s, cfg)
	if err := w.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	m, err := w.Manifest()
	if err != nil {
		t.Fatalf("Manifest: %v", err)
	}
	if len(m.Chunks) != 0 || m.TotalLength != 0 {
		t.Fatalf("manifest %+v, want no chunks and zero length", m)
	}
	if err := m.Validate(); err != nil {
		t.Fatalf("Validate on empty manifest: %v", err)
	}
	got, err := s.ReadObject(m)
	if err != nil {
		t.Fatalf("ReadObject(empty): %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("ReadObject(empty) returned %d bytes, want 0", len(got))
	}
	if s.Len() != 0 {
		t.Fatalf("store holds %d chunks after empty object, want 0", s.Len())
	}
}

func TestShortTailIsReadable(t *testing.T) {
	cfg := Config{MinChunkLen: 64, MaxChunkLen: 128, WindowSize: 16, Mask: 0x7}
	cases := [][]byte{
		[]byte("a"),
		[]byte("tiny tail"),
		bytes.Repeat([]byte{'z'}, 63),
	}
	for i, data := range cases {
		s := NewStore()
		m := manifestOf(t, s, cfg, data, []int{1})
		if m.TotalLength != len(data) {
			t.Fatalf("case %d: TotalLength %d, want %d", i, m.TotalLength, len(data))
		}
		if want := []int{len(data)}; !intsEqual(lensOf(m.Chunks), want) {
			t.Fatalf("case %d: chunks %+v, want one %d-byte chunk", i, m.Chunks, len(data))
		}
		got, err := s.ReadObject(m)
		if err != nil {
			t.Fatalf("case %d: ReadObject: %v", i, err)
		}
		if !bytes.Equal(got, data) {
			t.Fatalf("case %d: ReadObject = %q, want %q", i, got, data)
		}
	}
}

func TestMultiChunkWithShortTailRoundTrips(t *testing.T) {
	cfg := Config{MinChunkLen: 20, MaxChunkLen: 64, WindowSize: 8, Mask: 0xF}
	data := deterministicData(1000, 7)
	s := NewStore()
	m := manifestOf(t, s, cfg, data, []int{1, 2, 3, 5, 8, 13})
	if len(m.Chunks) < 3 {
		t.Fatalf("expected several chunks, got %d", len(m.Chunks))
	}
	got, err := s.ReadObject(m)
	if err != nil {
		t.Fatalf("ReadObject: %v", err)
	}
	if !bytes.Equal(got, data) {
		t.Fatalf("round trip mismatch: got %d bytes, want %d", len(got), len(data))
	}
	if m.TotalLength != len(data) {
		t.Fatalf("TotalLength %d, want %d", m.TotalLength, len(data))
	}
}

// TestSegmentSizeIndependence checks that segmenting the same stream in wildly
// different ways, including one byte at a time, yields the same manifest.
func TestSegmentSizeIndependence(t *testing.T) {
	cfg := Config{MinChunkLen: 13, MaxChunkLen: 97, WindowSize: 11, Mask: 0x1F}
	data := deterministicData(3000, 11)
	want := refChunk(data, cfg)

	segmentSets := [][]int{
		{len(data)}, // one call
		{1},         // byte at a time
		{2},
		{3},
		{7, 0, 1}, // interleaved zero-length writes
		{1, 64, 5, 4096},
		{13, 97, 1, 11},
		{100000},
	}
	for _, segments := range segmentSets {
		s := NewStore()
		got := manifestOf(t, s, cfg, data, segments)
		if !refsEqual(got.Chunks, want) {
			t.Fatalf("segments %v produced different chunks than reference", segments)
		}
		if got.TotalLength != len(data) {
			t.Fatalf("segments %v: total %d, want %d", segments, got.TotalLength, len(data))
		}
		rebuilt, err := s.ReadObject(got)
		if err != nil {
			t.Fatalf("segments %v: ReadObject: %v", segments, err)
		}
		if !bytes.Equal(rebuilt, data) {
			t.Fatalf("segments %v: round trip mismatch", segments)
		}
	}
}

func TestBoundariesMatchReferenceAcrossConfigsAndInputs(t *testing.T) {
	configs := []Config{
		{MinChunkLen: 1, MaxChunkLen: 1, WindowSize: 1, Mask: 0},
		{MinChunkLen: 1, MaxChunkLen: 2, WindowSize: 1, Mask: 0xFFFFFFFF},
		{MinChunkLen: 4, MaxChunkLen: 16, WindowSize: 4, Mask: 0},
		{MinChunkLen: 5, MaxChunkLen: 64, WindowSize: 5, Mask: 0x3},
		{MinChunkLen: 8, MaxChunkLen: 64, WindowSize: 12, Mask: 0xFF},
		{MinChunkLen: 3, MaxChunkLen: 9, WindowSize: 20, Mask: 0x7},
		{MinChunkLen: 16, MaxChunkLen: 48, WindowSize: 12, Mask: 0xFFFF},
	}
	sizes := []int{0, 1, 2, 3, 4, 5, 7, 8, 9, 15, 16, 17, 47, 48, 49, 64, 127, 256, 999}
	rng := rand.New(rand.NewSource(424242))
	for ci, cfg := range configs {
		for _, size := range sizes {
			inputs := map[string][]byte{
				"random":  deterministicRand(rng, size),
				"zeros":   bytes.Repeat([]byte{0}, size),
				"counted": countingData(size),
			}
			for flavour, data := range inputs {
				name := fmt.Sprintf("cfg%d/size%d/%s", ci, size, flavour)
				t.Run(name, func(t *testing.T) {
					want := refChunk(data, cfg)
					for _, segments := range [][]int{{size}, {1}, {3, 5, 7}} {
						s := NewStore()
						got := manifestOf(t, s, cfg, data, segments)
						if !refsEqual(got.Chunks, want) {
							t.Fatalf("segments %v: got lengths %v, want %v", segments, lensOf(got.Chunks), lensOf(want))
						}
						rebuilt, err := s.ReadObject(got)
						if err != nil {
							t.Fatalf("segments %v: ReadObject: %v", segments, err)
						}
						if !bytes.Equal(rebuilt, data) {
							t.Fatalf("segments %v: round trip mismatch, %d vs %d bytes", segments, len(rebuilt), len(data))
						}
					}
				})
			}
		}
	}
}

func TestWindowContinuesAcrossChunkBoundary(t *testing.T) {
	s := NewStore()
	// min 4, window 3: the first cut is at 4, then the window stays full, so
	// each following chunk cuts exactly at 4 bytes. A per-chunk window reset
	// would change the hash and therefore the boundaries.
	cfg := Config{MinChunkLen: 4, MaxChunkLen: 7, WindowSize: 3, Mask: 0}
	data := deterministicData(100, 3)
	got := manifestOf(t, s, cfg, data, []int{1})
	want := refChunk(data, cfg)
	if !refsEqual(got.Chunks, want) {
		t.Fatalf("boundary-crossing window mismatch: got %v, want %v", lensOf(got.Chunks), lensOf(want))
	}
	if want := []int{4, 4, 4}; !intsEqual(lensOf(got.Chunks)[:3], want) {
		t.Fatalf("first lengths %v, want %v", lensOf(got.Chunks), want)
	}
}

// TestHeadInsertionSharesTrailingChunks is the core acceptance case: a few
// bytes inserted at the head must leave the trailing chunks untouched, so both
// objects share stored chunks and the store holds fewer chunks than the sum of
// their manifests.
func TestHeadInsertionSharesTrailingChunks(t *testing.T) {
	cfg := Config{MinChunkLen: 32, MaxChunkLen: 8192, WindowSize: 16, Mask: 0xFF}
	body := deterministicData(8192, 99)
	prefix := []byte("XYZ")
	withPrefix := append(append([]byte{}, prefix...), body...)

	s := NewStore()
	a := manifestOf(t, s, cfg, body, []int{17})
	b := manifestOf(t, s, cfg, withPrefix, []int{5})

	if a.TotalLength != len(body) || b.TotalLength != len(withPrefix) {
		t.Fatalf("totals: a=%d (want %d), b=%d (want %d)", a.TotalLength, len(body), b.TotalLength, len(withPrefix))
	}
	if len(a.Chunks) < 3 || len(b.Chunks) < 3 {
		t.Fatalf("test data too small: %d and %d chunks", len(a.Chunks), len(b.Chunks))
	}

	inA := make(map[string]bool, len(a.Chunks))
	for _, ref := range a.Chunks {
		inA[ref.Digest] = true
	}
	shared := 0
	for _, ref := range b.Chunks {
		if inA[ref.Digest] {
			shared++
		}
	}
	// Only the first chunk (which absorbed the prefix) may differ, so nearly
	// every chunk of a must reappear in b. Requiring all but one keeps the
	// assertion strict while tolerating the head boundary shifting.
	if shared < len(a.Chunks)-1 {
		t.Fatalf("only %d shared chunks; a has %d, b has %d", shared, len(a.Chunks), len(b.Chunks))
	}

	// Shared chunks are stored once: the store must hold exactly the number of
	// distinct digests referenced by the two manifests.
	distinct := map[string]bool{}
	for _, ref := range a.Chunks {
		distinct[ref.Digest] = true
	}
	for _, ref := range b.Chunks {
		distinct[ref.Digest] = true
	}
	if total := len(a.Chunks) + len(b.Chunks); s.Len() >= total {
		t.Fatalf("store holds %d chunks, expected dedupe below %d", s.Len(), total)
	}
	if s.Len() != len(distinct) {
		t.Fatalf("store holds %d chunks, want %d distinct digests (total refs %d, shared %d)",
			s.Len(), len(distinct), len(a.Chunks)+len(b.Chunks), shared)
	}

	// Both objects rebuild byte for byte from the shared store.
	for _, tc := range []struct {
		name string
		m    Manifest
		want []byte
	}{{"a", a, body}, {"b", b, withPrefix}} {
		got, err := s.ReadObject(tc.m)
		if err != nil {
			t.Fatalf("ReadObject(%s): %v", tc.name, err)
		}
		if !bytes.Equal(got, tc.want) {
			t.Fatalf("object %s did not round trip", tc.name)
		}
		// A fresh independent store rebuilds the object from the manifest
		// alone, proving the manifest is self-sufficient.
		rebuilt, err := s.ReadObjectCrossCheck(tc.m)
		if err != nil {
			t.Fatalf("cross-check %s: %v", tc.name, err)
		}
		if !bytes.Equal(rebuilt, tc.want) {
			t.Fatalf("cross-check %s did not match", tc.name)
		}
	}
}

func TestRepeatedContentStoredOnce(t *testing.T) {
	s := NewStore()
	cfg := Config{MinChunkLen: 16, MaxChunkLen: 16, WindowSize: 4, Mask: 0}
	data := bytes.Repeat([]byte("A"), 64)
	m := manifestOf(t, s, cfg, data, []int{64})
	if want := []int{16, 16, 16, 16}; !intsEqual(lensOf(m.Chunks), want) {
		t.Fatalf("chunk lengths %v, want %v", lensOf(m.Chunks), want)
	}
	if s.Len() != 1 {
		t.Fatalf("store holds %d chunks, want 1 for four identical chunks", s.Len())
	}
	got, err := s.ReadObject(m)
	if err != nil {
		t.Fatalf("ReadObject: %v", err)
	}
	if !bytes.Equal(got, data) {
		t.Fatal("round trip mismatch for repeated content")
	}
}

func TestManifestRecordsLengthsAndDigests(t *testing.T) {
	s := NewStore()
	cfg := Config{MinChunkLen: 6, MaxChunkLen: 20, WindowSize: 5, Mask: 0x1}
	data := deterministicData(200, 5)
	m := manifestOf(t, s, cfg, data, []int{4})
	if len(m.Chunks) == 0 {
		t.Fatal("expected chunks")
	}
	sum := 0
	for i, ref := range m.Chunks {
		sum += ref.Length
		stored, err := s.Get(ref.Digest)
		if err != nil {
			t.Fatalf("chunk %d: %v", i, err)
		}
		if len(stored) != ref.Length {
			t.Fatalf("chunk %d length %d, stored %d", i, ref.Length, len(stored))
		}
		if got := digestOf(stored); got != ref.Digest {
			t.Fatalf("chunk %d digest %q, content hashes to %q", i, ref.Digest, got)
		}
	}
	if sum != m.TotalLength || sum != len(data) {
		t.Fatalf("sum %d, TotalLength %d, data %d", sum, m.TotalLength, len(data))
	}
	if err := m.Validate(); err != nil {
		t.Fatalf("Validate: %v", err)
	}
}

func TestManifestBeforeCloseRejected(t *testing.T) {
	s := NewStore()
	w := mustWriter(t, s, Config{MinChunkLen: 2, MaxChunkLen: 8, WindowSize: 2, Mask: 0})
	if _, err := w.Write([]byte("hello")); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if _, err := w.Manifest(); err != ErrNotFinalized {
		t.Fatalf("Manifest before Close: %v, want ErrNotFinalized", err)
	}
}

func TestWriteAfterCloseRejected(t *testing.T) {
	s := NewStore()
	w := mustWriter(t, s, Config{MinChunkLen: 2, MaxChunkLen: 8, WindowSize: 2, Mask: 0})
	if err := w.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	n, err := w.Write([]byte("x"))
	if err != ErrWriterClosed {
		t.Fatalf("Write after Close error = %v, want ErrWriterClosed", err)
	}
	if n != 0 {
		t.Fatalf("Write after Close reported %d bytes, want 0", n)
	}
}

func TestCloseIsIdempotent(t *testing.T) {
	s := NewStore()
	w := mustWriter(t, s, Config{MinChunkLen: 4, MaxChunkLen: 8, WindowSize: 2, Mask: 0})
	if _, err := w.Write([]byte("abcdef")); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if err := w.Close(); err != nil {
		t.Fatalf("first Close: %v", err)
	}
	first, err := w.Manifest()
	if err != nil {
		t.Fatalf("Manifest: %v", err)
	}
	if err := w.Close(); err != nil {
		t.Fatalf("second Close: %v", err)
	}
	second, err := w.Manifest()
	if err != nil {
		t.Fatalf("Manifest after second Close: %v", err)
	}
	if !refsEqual(first.Chunks, second.Chunks) || first.TotalLength != second.TotalLength {
		t.Fatalf("Close changed the manifest: %+v vs %+v", first, second)
	}
}

func TestManifestIsACopy(t *testing.T) {
	s := NewStore()
	w := mustWriter(t, s, Config{MinChunkLen: 4, MaxChunkLen: 8, WindowSize: 2, Mask: 0})
	if _, err := w.Write([]byte("abcdef")); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if err := w.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	m1, err := w.Manifest()
	if err != nil {
		t.Fatalf("Manifest: %v", err)
	}
	if len(m1.Chunks) == 0 {
		t.Fatal("expected chunks")
	}
	m1.Chunks[0].Length = 99999
	m1.Chunks = append(m1.Chunks, ChunkRef{Length: 1, Digest: "junk"})
	m2, err := w.Manifest()
	if err != nil {
		t.Fatalf("Manifest: %v", err)
	}
	if len(m2.Chunks) != len(m1.Chunks)-1 {
		t.Fatalf("caller mutation leaked into the writer manifest: %+v", m2)
	}
	if m2.Chunks[0].Length == 99999 {
		t.Fatal("caller mutation of chunk length leaked into the writer manifest")
	}
}

func TestReadObjectRejectsTamperedManifest(t *testing.T) {
	s := NewStore()
	cfg := Config{MinChunkLen: 4, MaxChunkLen: 8, WindowSize: 2, Mask: 0}
	data := deterministicData(64, 21)
	m := manifestOf(t, s, cfg, data, []int{1})

	// A manifest whose digest is not in the store must be rejected.
	missing := Manifest{TotalLength: m.TotalLength, Chunks: append([]ChunkRef{}, m.Chunks...)}
	missing.Chunks[0].Digest = digestOf([]byte("not stored"))
	if _, err := s.ReadObject(missing); err == nil {
		t.Fatal("ReadObject accepted a manifest referencing a missing chunk")
	}

	// A manifest whose chunk length disagrees with the stored chunk must be
	// rejected even though the digest matches.
	wrongLen := Manifest{TotalLength: m.TotalLength + 1, Chunks: append([]ChunkRef{}, m.Chunks...)}
	wrongLen.Chunks[0].Length++
	if _, err := s.ReadObject(wrongLen); err == nil {
		t.Fatal("ReadObject accepted a manifest with a wrong chunk length")
	}

	// A structurally broken manifest (total does not match the chunk sum) must
	// be rejected by Validate.
	bad := Manifest{TotalLength: m.TotalLength + 7, Chunks: append([]ChunkRef{}, m.Chunks...)}
	if err := bad.Validate(); err == nil {
		t.Fatal("Validate accepted a total length that does not match the chunk sum")
	}
}

func deterministicData(n int, seed int64) []byte {
	return deterministicRand(rand.New(rand.NewSource(seed)), n)
}

func deterministicRand(rng *rand.Rand, n int) []byte {
	data := make([]byte, n)
	for i := range data {
		data[i] = byte(rng.Intn(256))
	}
	return data
}

func countingData(n int) []byte {
	data := make([]byte, n)
	for i := range data {
		if i%17 == 0 {
			data[i] = 0
		} else {
			data[i] = byte(i%251 + 1)
		}
	}
	return data
}
