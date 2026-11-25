package recordlayer

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"errors"
)

// Cipher is the replaceable cryptographic boundary of the record layer.
//
// Seal protects plaintext for the given key generation and record sequence and
// returns the wire payload. Open performs the inverse operation. The
// coordinator guarantees that generation/sequence on Open match the values the
// sender passed to Seal; implementations must additionally authenticate them as
// associated data so that tampering with the plaintext header is detected.
type Cipher interface {
	Seal(generation, sequence uint64, recordType RecordType, plaintext []byte) (ciphertext []byte, err error)
	Open(generation, sequence uint64, recordType RecordType, ciphertext []byte) (plaintext []byte, err error)
}

// DemoCipher is a self-contained, dependency-free demonstration Cipher.
//
// It is NOT a production cipher. Per-generation keys are derived with
// SHA-256, payloads are encrypted with an XOR keystream built from HMAC-SHA256
// counters, and an HMAC tag over (generation, sequence, record type,
// ciphertext) authenticates every record. Both key generations share the base
// secret, which models a key hierarchy whose per-generation secrets are
// derived locally without a key exchange.
type DemoCipher struct {
	key []byte
}

// NewDemoCipher creates a demonstration cipher from a non-empty base secret.
func NewDemoCipher(baseKey []byte) (*DemoCipher, error) {
	if len(baseKey) == 0 {
		return nil, errors.New("recordlayer: empty base key")
	}
	cp := make([]byte, len(baseKey))
	copy(cp, baseKey)
	return &DemoCipher{key: cp}, nil
}

const (
	demoTagLen    = sha256.Size
	demoBlockSize = 32
)

func (c *DemoCipher) derivedKey(generation uint64) []byte {
	var g [8]byte
	for i := 0; i < 8; i++ {
		g[7-i] = byte(generation >> (8 * i))
	}
	h := sha256.New()
	h.Write([]byte("recordlayer-key-v1"))
	h.Write(g[:])
	h.Write(c.key)
	return h.Sum(nil)
}

// associatedData binds the non-secret header to the authentication tag.
// Layout: record type (1 byte), generation (8, big endian), sequence (8).
func associatedData(generation, sequence uint64, recordType RecordType) []byte {
	var b [17]byte
	b[0] = byte(recordType)
	for i := 0; i < 8; i++ {
		b[8-i] = byte(generation >> (8 * i))
		b[16-i] = byte(sequence >> (8 * i))
	}
	return b[:]
}

func xorKeystream(key, generationKey, seq []byte, n int) []byte {
	out := make([]byte, n)
	var counter uint64
	for off := 0; off < n; off += demoBlockSize {
		mac := hmac.New(sha256.New, key)
		mac.Write(generationKey)
		var c [8]byte
		for i := 0; i < 8; i++ {
			c[7-i] = byte(counter >> (8 * i))
		}
		mac.Write(c[:])
		mac.Write(seq)
		block := mac.Sum(nil)
		copy(out[off:], block)
		counter++
	}
	return out[:n]
}

func (c *DemoCipher) tag(generation, sequence uint64, recordType RecordType, body []byte) []byte {
	mac := hmac.New(sha256.New, c.derivedKey(generation))
	mac.Write(associatedData(generation, sequence, recordType))
	mac.Write(body)
	return mac.Sum(nil)
}

// Seal encrypts plaintext and appends an authentication tag.
func (c *DemoCipher) Seal(generation, sequence uint64, recordType RecordType, plaintext []byte) ([]byte, error) {
	gk := c.derivedKey(generation)
	var seqBytes [8]byte
	for i := 0; i < 8; i++ {
		seqBytes[7-i] = byte(sequence >> (8 * i))
	}
	stream := xorKeystream(c.key, gk, seqBytes[:], len(plaintext))
	body := make([]byte, len(plaintext))
	for i := range plaintext {
		body[i] = plaintext[i] ^ stream[i]
	}
	return append(body, c.tag(generation, sequence, recordType, body)...), nil
}

// Open verifies the authentication tag and decrypts the payload.
func (c *DemoCipher) Open(generation, sequence uint64, recordType RecordType, ciphertext []byte) ([]byte, error) {
	if len(ciphertext) < demoTagLen {
		return nil, errors.New("recordlayer: ciphertext shorter than authentication tag")
	}
	body := ciphertext[:len(ciphertext)-demoTagLen]
	want := c.tag(generation, sequence, recordType, body)
	got := ciphertext[len(ciphertext)-demoTagLen:]
	if subtle.ConstantTimeCompare(want, got) != 1 {
		return nil, errors.New("recordlayer: authentication tag mismatch")
	}
	gk := c.derivedKey(generation)
	var seqBytes [8]byte
	for i := 0; i < 8; i++ {
		seqBytes[7-i] = byte(sequence >> (8 * i))
	}
	stream := xorKeystream(c.key, gk, seqBytes[:], len(body))
	plaintext := make([]byte, len(body))
	for i := range body {
		plaintext[i] = body[i] ^ stream[i]
	}
	return plaintext, nil
}

// CountingCipher wraps a Cipher and counts Seal/Open invocations per key
// generation. It is useful for asserting that application data is opened
// exactly once during overlapping key rotations.
type CountingCipher struct {
	inner     Cipher
	sealCount map[uint64]int
	openCount map[uint64]int
}

// NewCountingCipher wraps inner with invocation counters.
func NewCountingCipher(inner Cipher) *CountingCipher {
	return &CountingCipher{
		inner:     inner,
		sealCount: make(map[uint64]int),
		openCount: make(map[uint64]int),
	}
}

// Seal delegates to the wrapped cipher after recording the invocation.
func (c *CountingCipher) Seal(generation, sequence uint64, recordType RecordType, plaintext []byte) ([]byte, error) {
	out, err := c.inner.Seal(generation, sequence, recordType, plaintext)
	if err == nil {
		c.sealCount[generation]++
	}
	return out, err
}

// Open delegates to the wrapped cipher after recording the invocation.
func (c *CountingCipher) Open(generation, sequence uint64, recordType RecordType, ciphertext []byte) ([]byte, error) {
	out, err := c.inner.Open(generation, sequence, recordType, ciphertext)
	if err == nil {
		c.openCount[generation]++
	}
	return out, err
}

// SealCount returns the number of successful Seal calls for a generation.
func (c *CountingCipher) SealCount(generation uint64) int { return c.sealCount[generation] }

// OpenCount returns the number of successful Open calls for a generation.
func (c *CountingCipher) OpenCount(generation uint64) int { return c.openCount[generation] }
