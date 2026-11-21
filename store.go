package chunkstore

import (
	"encoding/hex"
	"fmt"
	"sync"
)

// Store is an in-memory content-addressed chunk store. Chunks are keyed by the
// lowercase hex SHA-256 digest of their content, so a chunk shared by several
// objects (or by several versions of one object) is stored exactly once.
//
// A Store is safe for concurrent use.
type Store struct {
	mu     sync.RWMutex
	chunks map[string][]byte
}

// NewStore returns an empty chunk store.
func NewStore() *Store {
	return &Store{chunks: make(map[string][]byte)}
}

// put stores data under its digest and returns the manifest reference. An
// identical chunk already present is not written again; its existing entry is
// kept.
func (s *Store) put(data []byte) ChunkRef {
	digest := digestOf(data)
	s.mu.Lock()
	if _, ok := s.chunks[digest]; !ok {
		stored := make([]byte, len(data))
		copy(stored, data)
		s.chunks[digest] = stored
	}
	s.mu.Unlock()
	return ChunkRef{Length: len(data), Digest: digest}
}

// PutChunk stores data in the store and returns its digest and length. It is
// the public manual counterpart of the Writer's internal chunk insertion.
func (s *Store) PutChunk(data []byte) ChunkRef {
	return s.put(data)
}

// Get returns a copy of the chunk stored under digest, or ErrChunkNotFound.
func (s *Store) Get(digest string) ([]byte, error) {
	s.mu.RLock()
	data, ok := s.chunks[digest]
	s.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("%w: %s", ErrChunkNotFound, digest)
	}
	out := make([]byte, len(data))
	copy(out, data)
	return out, nil
}

// Has reports whether digest is present in the store.
func (s *Store) Has(digest string) bool {
	s.mu.RLock()
	_, ok := s.chunks[digest]
	s.mu.RUnlock()
	return ok
}

// Len returns the number of distinct chunks held by the store. Objects that
// share chunks therefore add fewer entries than their total chunk count.
func (s *Store) Len() int {
	s.mu.RLock()
	n := len(s.chunks)
	s.mu.RUnlock()
	return n
}

// Validate checks that m is structurally sound: every chunk has a positive
// length and a lowercase hex SHA-256 digest, and the total length equals the
// sum of the chunk lengths. It does not consult the store.
func (m Manifest) Validate() error {
	sum := 0
	for i, ref := range m.Chunks {
		if ref.Length < 1 {
			return fmt.Errorf("%w: chunk %d has non-positive length %d", ErrManifestInvalid, i, ref.Length)
		}
		if len(ref.Digest) != sha256HexLen {
			return fmt.Errorf("%w: chunk %d digest %q is not %d hex characters", ErrManifestInvalid, i, ref.Digest, sha256HexLen)
		}
		if _, err := hex.DecodeString(ref.Digest); err != nil {
			return fmt.Errorf("%w: chunk %d digest %q is not hex: %v", ErrManifestInvalid, i, ref.Digest, err)
		}
		sum += ref.Length
	}
	if sum != m.TotalLength {
		return fmt.Errorf("%w: total %d, chunk sum %d", ErrManifestLengthMismatch, m.TotalLength, sum)
	}
	return nil
}

const sha256HexLen = 64

// ReadObject reconstructs the object described by m from the store. Every
// chunk must be present, must have the length recorded in the manifest, and
// must hash to its digest. The rebuilt object is verified against the manifest
// total length.
func (s *Store) ReadObject(m Manifest) ([]byte, error) {
	if err := m.Validate(); err != nil {
		return nil, err
	}
	out := make([]byte, 0, m.TotalLength)
	for i, ref := range m.Chunks {
		data, err := s.Get(ref.Digest)
		if err != nil {
			return nil, fmt.Errorf("chunk %d: %w", i, err)
		}
		if len(data) != ref.Length {
			return nil, fmt.Errorf("%w: chunk %d digest %s: manifest length %d, stored length %d",
				ErrChunkLengthMismatch, i, ref.Digest, ref.Length, len(data))
		}
		if got := digestOf(data); got != ref.Digest {
			return nil, fmt.Errorf("%w: chunk %d digest %s, content hashes to %s",
				ErrChunkDigestMismatch, i, ref.Digest, got)
		}
		out = append(out, data...)
	}
	if len(out) != m.TotalLength {
		return nil, fmt.Errorf("%w: rebuilt %d bytes, manifest says %d",
			ErrObjectLengthMismatch, len(out), m.TotalLength)
	}
	return out, nil
}

// ReadObjectCrossCheck reconstructs the object into a fresh independent store.
// Each chunk is copied over by digest and re-hashed in the new store, so a
// successful cross-check means the manifest alone is sufficient to rebuild the
// object from chunks whose content matches their digests and lengths.
func (s *Store) ReadObjectCrossCheck(m Manifest) ([]byte, error) {
	if err := m.Validate(); err != nil {
		return nil, err
	}
	other := NewStore()
	for i, ref := range m.Chunks {
		data, err := s.Get(ref.Digest)
		if err != nil {
			return nil, fmt.Errorf("chunk %d: %w", i, err)
		}
		if len(data) != ref.Length {
			return nil, fmt.Errorf("%w: chunk %d digest %s: manifest length %d, stored length %d",
				ErrChunkLengthMismatch, i, ref.Digest, ref.Length, len(data))
		}
		if got := digestOf(data); got != ref.Digest {
			return nil, fmt.Errorf("%w: chunk %d digest %s, content hashes to %s",
				ErrChunkDigestMismatch, i, ref.Digest, got)
		}
		other.PutChunk(data)
	}
	return other.ReadObject(m)
}
