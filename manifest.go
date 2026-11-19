// Package advisory implements the receive side of a one-way security-bulletin
// distribution channel.
//
// A bulletin is described by a sender-signed manifest (the "sending region")
// that carries the bulletin version, its declared length and digest, the
// digest and length of every chunk, and the digest of one XOR parity block per
// group. Chunks may arrive in any order. Within a group at most one chunk may
// be missing for the parity block to repair it, using the equal-length rule
// (all chunks of a group are zero-extended on the right to the longest chunk
// in that group before XOR). The assembled bulletin is finally cut to the
// declared length.
//
// A manifest is admitted only after the caller-supplied Verifier accepts the
// signature over the exact region bytes; otherwise it cannot trigger
// publication. Data whose digest does not match its manifest entry is treated
// as missing. Two distinct manifests carrying the same version can never be
// mixed. A bulletin is published only when every group is complete, the
// assembled bytes match the declared digest, and the version is newer than the
// currently displayed one.
package advisory

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

// Hard limits applied to decoded manifests. They bound the memory a hostile or
// malformed sender can make the receiver allocate before any verification.
const (
	MaxBulletinLength = 1 << 30 // 1 GiB
	MaxChunkLength    = 1 << 26 // 64 MiB
	MaxGroups         = 4096
	MaxChunksPerGroup = 4096
)

// Digest is a SHA-256 digest.
type Digest [sha256.Size]byte

// String returns the lower-case hexadecimal form of d.
func (d Digest) String() string { return hex.EncodeToString(d[:]) }

// DigestOf returns the SHA-256 digest of data.
func DigestOf(data []byte) Digest { return Digest(sha256.Sum256(data)) }

// ParseDigest parses a 64-character hexadecimal SHA-256 digest. Upper-case
// hex is accepted and normalised to lower case (see Manifest fingerprinting).
func ParseDigest(s string) (Digest, error) {
	var d Digest
	if len(s) != hex.EncodedLen(len(d)) {
		return d, fmt.Errorf("%w: digest must be %d hex characters, got %d",
			ErrInvalidManifest, hex.EncodedLen(len(d)), len(s))
	}
	b, err := hex.DecodeString(s)
	if err != nil {
		return d, fmt.Errorf("%w: digest is not hexadecimal: %v", ErrInvalidManifest, err)
	}
	copy(d[:], b)
	return d, nil
}

// Chunk describes one chunk as fixed by the signed manifest.
type Chunk struct {
	// Length is the exact number of bytes the chunk must carry.
	Length int
	// Digest is the SHA-256 digest of the chunk bytes.
	Digest Digest
}

// Group is a set of chunks protected by one XOR parity block. The parity
// block length is the length of the longest chunk in the group; shorter
// chunks are zero-extended on the right before being XORed.
type Group struct {
	Chunks []Chunk
	Parity Digest
}

// Span returns the byte length of the group's parity block, i.e. the length
// of its longest chunk.
func (g Group) Span() int {
	span := 0
	for _, c := range g.Chunks {
		if c.Length > span {
			span = c.Length
		}
	}
	return span
}

// Manifest is the decoded, sender-signed description of one bulletin.
type Manifest struct {
	Version  uint64
	Length   int
	Bulletin Digest
	Groups   []Group
}

// manifestDTO is the on-the-wire JSON shape. Field order is fixed and all
// digests are lower-case hex, so re-encoding a decoded manifest is canonical.
type manifestDTO struct {
	Version  uint64     `json:"version"`
	Length   int        `json:"length"`
	Bulletin string     `json:"bulletin"`
	Groups   []groupDTO `json:"groups"`
}

type groupDTO struct {
	Chunks []chunkDTO `json:"chunks"`
	Parity string     `json:"parity"`
}

type chunkDTO struct {
	Length int    `json:"length"`
	Digest string `json:"digest"`
}

func (m Manifest) dto() manifestDTO {
	dto := manifestDTO{
		Version:  m.Version,
		Length:   m.Length,
		Bulletin: m.Bulletin.String(),
		Groups:   make([]groupDTO, len(m.Groups)),
	}
	for gi, g := range m.Groups {
		gd := groupDTO{
			Chunks: make([]chunkDTO, len(g.Chunks)),
			Parity: g.Parity.String(),
		}
		for ci, c := range g.Chunks {
			gd.Chunks[ci] = chunkDTO{Length: c.Length, Digest: c.Digest.String()}
		}
		dto.Groups[gi] = gd
	}
	return dto
}

// fingerprint identifies the manifest content independently of the exact JSON
// byte layout that was signed (key order, hex case, whitespace). Two manifests
// with the same version but different fingerprints are different bulletins and
// must never be combined.
func (m Manifest) fingerprint() Digest {
	canonical, err := json.Marshal(m.dto())
	if err != nil {
		// The DTO only contains strings, integers and slices; Marshal cannot
		// fail for a validated manifest. Guard anyway.
		return Digest(sha256.Sum256([]byte(fmt.Sprintf("%#v", m))))
	}
	return DigestOf(canonical)
}

// ParseManifest strictly decodes a signed manifest region. Unknown fields,
// trailing data, malformed digests and internally inconsistent lengths are
// rejected with an error wrapping ErrInvalidManifest.
func ParseManifest(region []byte) (Manifest, error) {
	if len(region) == 0 {
		return Manifest{}, fmt.Errorf("%w: empty region", ErrInvalidManifest)
	}
	dec := json.NewDecoder(bytes.NewReader(region))
	dec.DisallowUnknownFields()
	var dto manifestDTO
	if err := dec.Decode(&dto); err != nil {
		return Manifest{}, fmt.Errorf("%w: %v", ErrInvalidManifest, err)
	}
	var extra json.RawMessage
	if err := dec.Decode(&extra); err != io.EOF {
		return Manifest{}, fmt.Errorf("%w: trailing data after manifest", ErrInvalidManifest)
	}

	m := Manifest{
		Version: dto.Version,
		Length:  dto.Length,
		Groups:  make([]Group, len(dto.Groups)),
	}
	var err error
	if m.Bulletin, err = ParseDigest(dto.Bulletin); err != nil {
		return Manifest{}, err
	}
	for gi, gd := range dto.Groups {
		g := Group{
			Chunks: make([]Chunk, len(gd.Chunks)),
		}
		if g.Parity, err = ParseDigest(gd.Parity); err != nil {
			return Manifest{}, err
		}
		for ci, cd := range gd.Chunks {
			c := Chunk{Length: cd.Length}
			if c.Digest, err = ParseDigest(cd.Digest); err != nil {
				return Manifest{}, err
			}
			g.Chunks[ci] = c
		}
		m.Groups[gi] = g
	}
	if err := validateManifest(m); err != nil {
		return Manifest{}, err
	}
	return m, nil
}

func validateManifest(m Manifest) error {
	if m.Length < 1 || m.Length > MaxBulletinLength {
		return fmt.Errorf("%w: bulletin length %d outside [1,%d]",
			ErrInvalidManifest, m.Length, MaxBulletinLength)
	}
	if len(m.Groups) == 0 || len(m.Groups) > MaxGroups {
		return fmt.Errorf("%w: group count %d outside [1,%d]",
			ErrInvalidManifest, len(m.Groups), MaxGroups)
	}
	total := int64(0)
	for gi, g := range m.Groups {
		if len(g.Chunks) == 0 || len(g.Chunks) > MaxChunksPerGroup {
			return fmt.Errorf("%w: group %d has %d chunks, outside [1,%d]",
				ErrInvalidManifest, gi, len(g.Chunks), MaxChunksPerGroup)
		}
		for ci, c := range g.Chunks {
			if c.Length < 1 || c.Length > MaxChunkLength {
				return fmt.Errorf("%w: chunk %d/%d length %d outside [1,%d]",
					ErrInvalidManifest, gi, ci, c.Length, MaxChunkLength)
			}
			total += int64(c.Length)
		}
	}
	if total < int64(m.Length) {
		return fmt.Errorf("%w: chunks carry %d bytes, less than declared length %d",
			ErrInvalidManifest, total, m.Length)
	}
	// Trailing bytes beyond the declared length are permitted (the bulletin is
	// cut to its declared length), but keep the padding bounded.
	if total > int64(MaxBulletinLength)+int64(MaxChunkLength) {
		return fmt.Errorf("%w: chunk bytes %d exceed the supported maximum", ErrInvalidManifest, total)
	}
	return nil
}

// Sentinel errors returned (possibly wrapped) by the receiver.
var (
	ErrNoVerifier             = errors.New("advisory: no source verifier configured")
	ErrUnverified             = errors.New("advisory: manifest source verification failed")
	ErrInvalidManifest        = errors.New("advisory: invalid manifest")
	ErrManifestConflict       = errors.New("advisory: conflicting manifest for version")
	ErrStaleManifest          = errors.New("advisory: manifest version is not newer than pending or current")
	ErrNoSession              = errors.New("advisory: no admitted manifest")
	ErrUnknownGroup           = errors.New("advisory: unknown group")
	ErrUnknownChunk           = errors.New("advisory: unknown chunk")
	ErrChunkDigestMismatch    = errors.New("advisory: data does not match the manifest digest")
	ErrBulletinDigestMismatch = errors.New("advisory: assembled bulletin digest mismatch")
)
