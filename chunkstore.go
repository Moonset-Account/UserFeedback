// Package chunkstore implements an incremental content-defined chunking (CDC)
// writer for a local, in-memory object store.
//
// A Writer accepts byte fragments of any size and splits the byte stream into
// content-defined chunks. Every chunk is stored exactly once, keyed by its
// SHA-256 content digest, and the Writer records an ordered manifest of chunk
// lengths and digests. Because boundaries are derived from the bytes around
// them, inserting a few bytes at the head of an object only disturbs the first
// chunks: later chunks keep their old digests and are shared by the store.
//
// Boundary rules are supplied by the caller through Config:
//
//   - MinChunkLen: a chunk is never cut before it reaches this length.
//   - MaxChunkLen: a chunk is always cut once it reaches this length.
//   - WindowSize:  number of trailing bytes in the rolling window.
//   - Mask:        a candidate cut is accepted when the rolling value ANDed
//     with Mask is zero.
//
// The rolling value is the unsigned 32-bit polynomial of the most recent
// WindowSize bytes in stream order, with base 257:
//
//	value = (((b0*257 + b1)*257 + b2)...)*257 + b_{n-1}   (mod 2^32)
//
// A cut is made after appending a byte when the current chunk has reached
// MinChunkLen, the window has filled (WindowSize bytes have been seen in the
// stream), and value&Mask == 0. If MaxChunkLen is reached first the chunk is
// cut regardless of the rolling value. The window keeps sliding across chunk
// boundaries; only the current chunk length restarts at zero after a cut.
// Consequently feeding the same bytes one at a time or all at once produces
// the same manifest.
package chunkstore

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
)

var (
	// ErrInvalidConfig reports a Config that cannot describe cuts.
	ErrInvalidConfig = errors.New("chunkstore: invalid config")
	// ErrWriterClosed reports a Write after Close.
	ErrWriterClosed = errors.New("chunkstore: writer is closed")
	// ErrNotFinalized reports Manifest before Close.
	ErrNotFinalized = errors.New("chunkstore: writer is not finalized; call Close first")
	// ErrChunkNotFound reports a manifest referencing a digest absent from the store.
	ErrChunkNotFound = errors.New("chunkstore: chunk not found in store")
	// ErrChunkLengthMismatch reports stored chunk length differing from the manifest.
	ErrChunkLengthMismatch = errors.New("chunkstore: chunk length mismatch")
	// ErrChunkDigestMismatch reports stored chunk bytes differing from their digest.
	ErrChunkDigestMismatch = errors.New("chunkstore: chunk digest mismatch")
	// ErrManifestInvalid reports a structurally invalid manifest.
	ErrManifestInvalid = errors.New("chunkstore: invalid manifest")
	// ErrManifestLengthMismatch reports a manifest total differing from its chunk sum.
	ErrManifestLengthMismatch = errors.New("chunkstore: manifest total length mismatch")
	// ErrObjectLengthMismatch reports a reconstructed object whose size differs
	// from the manifest total.
	ErrObjectLengthMismatch = errors.New("chunkstore: object length mismatch")
)

// Config describes the content-defined cut rules. All lengths are in bytes.
type Config struct {
	// MinChunkLen is the smallest allowed chunk length (>= 1).
	MinChunkLen int
	// MaxChunkLen is the length at which a chunk is cut unconditionally
	// (>= MinChunkLen).
	MaxChunkLen int
	// WindowSize is the rolling window length (>= 1).
	WindowSize int
	// Mask selects which rolling values trigger a cut: value&Mask == 0.
	Mask uint32
}

func (c Config) validate() error {
	switch {
	case c.MinChunkLen < 1:
		return fmt.Errorf("%w: MinChunkLen must be >= 1, got %d", ErrInvalidConfig, c.MinChunkLen)
	case c.MaxChunkLen < c.MinChunkLen:
		return fmt.Errorf("%w: MaxChunkLen (%d) must be >= MinChunkLen (%d)", ErrInvalidConfig, c.MaxChunkLen, c.MinChunkLen)
	case c.WindowSize < 1:
		return fmt.Errorf("%w: WindowSize must be >= 1, got %d", ErrInvalidConfig, c.WindowSize)
	}
	return nil
}

// ChunkRef is one entry of a Manifest: the chunk length in bytes and the
// lowercase hex SHA-256 digest of its content.
type ChunkRef struct {
	Length int
	Digest string
}

// Manifest is the ordered description of an object: the chunks that make up
// the object and the total object length.
type Manifest struct {
	Chunks      []ChunkRef
	TotalLength int
}

// digestOf returns the lowercase hex SHA-256 digest of data.
func digestOf(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// Writer is an io.Writer that splits its input into content-defined chunks,
// stores each chunk once in its Store, and accumulates a Manifest.
//
// A Writer must be finalized with Close before Manifest is called.
type Writer struct {
	store *Store
	cfg   Config

	// pow is 257^WindowSize modulo 2^32, used to evict the oldest window byte.
	pow uint32
	// hash is the polynomial over the bytes currently in the window.
	hash uint32
	// window is a ring buffer holding the most recent WindowSize bytes.
	window []byte
	// winPos is the index of the oldest byte (next overwrite position).
	winPos int
	// winFilled counts bytes seen, capped at WindowSize.
	winFilled int

	// chunk buffers the bytes of the chunk currently being assembled.
	chunk    []byte
	manifest Manifest

	closed bool
}

// NewWriter validates cfg and returns a Writer that stores chunks in s.
func (s *Store) NewWriter(cfg Config) (*Writer, error) {
	if err := cfg.validate(); err != nil {
		return nil, err
	}
	w := &Writer{
		store:  s,
		cfg:    cfg,
		window: make([]byte, cfg.WindowSize),
	}
	w.pow = 1
	for i := 0; i < cfg.WindowSize; i++ {
		w.pow *= 257
	}
	return w, nil
}

// Write feeds p to the chunker. It accepts fragments of any size, including
// zero bytes, and always reports len(p) on success. The boundary decisions
// depend only on the byte sequence, never on how the sequence was split into
// Write calls.
func (w *Writer) Write(p []byte) (int, error) {
	if w.closed {
		return 0, ErrWriterClosed
	}
	for _, b := range p {
		w.push(b)
	}
	return len(p), nil
}

// push appends one byte to the stream, updates the rolling window and cuts the
// current chunk when a boundary rule fires.
func (w *Writer) push(b byte) {
	if w.winFilled == w.cfg.WindowSize {
		// Evict the oldest byte before rolling the polynomial forward.
		oldest := w.window[w.winPos]
		w.hash = w.hash*257 + uint32(b) - uint32(oldest)*w.pow
	} else {
		w.hash = w.hash*257 + uint32(b)
		w.winFilled++
	}
	w.window[w.winPos] = b
	w.winPos++
	if w.winPos == w.cfg.WindowSize {
		w.winPos = 0
	}

	w.chunk = append(w.chunk, b)
	length := len(w.chunk)

	cut := false
	if length >= w.cfg.MinChunkLen && w.winFilled == w.cfg.WindowSize && (w.hash&w.cfg.Mask) == 0 {
		cut = true
	} else if length >= w.cfg.MaxChunkLen {
		cut = true
	}
	if cut {
		w.flushChunk()
	}
}

// flushChunk hands the buffered chunk to the store and resets the chunk length.
// The rolling window is deliberately left untouched so that it continues
// across chunk boundaries.
func (w *Writer) flushChunk() {
	if len(w.chunk) == 0 {
		return
	}
	ref := w.store.put(w.chunk)
	w.manifest.Chunks = append(w.manifest.Chunks, ref)
	w.manifest.TotalLength += len(w.chunk)
	w.chunk = w.chunk[:0]
}

// Close finalizes the object. A non-empty remainder shorter than MinChunkLen
// is emitted as the final chunk. Close is idempotent.
func (w *Writer) Close() error {
	if w.closed {
		return nil
	}
	w.closed = true
	w.flushChunk()
	return nil
}

// Manifest returns a copy of the finalized object manifest. It fails with
// ErrNotFinalized if Close has not been called.
func (w *Writer) Manifest() (Manifest, error) {
	if !w.closed {
		return Manifest{}, ErrNotFinalized
	}
	out := Manifest{
		Chunks:      make([]ChunkRef, len(w.manifest.Chunks)),
		TotalLength: w.manifest.TotalLength,
	}
	copy(out.Chunks, w.manifest.Chunks)
	return out, nil
}
