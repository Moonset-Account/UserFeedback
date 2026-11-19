package encupload

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"errors"
	"fmt"
)

// Ciphertext layout: nonce(16) || body || tag(32).
//
// The keystream is HMAC-SHA256(key, nonce||counter||gen||number) in 32-byte
// blocks, XORed with the plaintext. The tag is HMAC-SHA256 over the header
// and body. This is a synthetic backend, so a straightforward encrypt-then-MAC
// construction with the standard library is enough; the important property for
// this exercise is that a segment cannot be silently substituted for another
// segment, session, or key generation.
const (
	nonceSize  = 16
	tagSize    = 32
	headerSize = nonceSize
)

func keystreamBlock(key []byte, nonce []byte, counter uint64, gen int64, number int) []byte {
	h := hmac.New(sha256.New, key)
	h.Write([]byte("encupload-keystream-v1"))
	h.Write(nonce)
	var c [8]byte
	for i := 0; i < 8; i++ {
		c[i] = byte(counter >> (8 * (7 - i)))
	}
	h.Write(c[:])
	var meta [16]byte
	for i := 0; i < 8; i++ {
		meta[i] = byte(uint64(gen) >> (8 * (7 - i)))
		meta[8+i] = byte(uint32(int64(number)) >> (8 * (7 - i)))
	}
	h.Write(meta[:])
	return h.Sum(nil)
}

func xorWithKeystream(key []byte, nonce []byte, gen int64, number int, in []byte) []byte {
	out := make([]byte, len(in))
	var counter uint64
	for off := 0; off < len(in); off += sha256.Size {
		block := keystreamBlock(key, nonce, counter, gen, number)
		for i := 0; i < len(block) && off+i < len(in); i++ {
			out[off+i] = in[off+i] ^ block[i]
		}
		counter++
	}
	return out
}

func sealSegment(key []byte, number int, gen int64, plaintext []byte) []byte {
	nonce := make([]byte, nonceSize)
	if _, err := rand.Read(nonce); err != nil {
		// crypto/rand failure is fatal for an encryption backend; panic
		// rather than emit unauthenticated bytes.
		panic("encupload: entropy source failed: " + err.Error())
	}
	body := xorWithKeystream(key, nonce, gen, number, plaintext)
	out := make([]byte, 0, headerSize+len(body)+tagSize)
	out = append(out, nonce...)
	out = append(out, body...)
	out = append(out, segmentTag(key, nonce, gen, number, body)...)
	return out
}

func segmentTag(key []byte, nonce []byte, gen int64, number int, body []byte) []byte {
	h := hmac.New(sha256.New, key)
	h.Write([]byte("encupload-segment-tag-v1"))
	var meta [16]byte
	for i := 0; i < 8; i++ {
		meta[i] = byte(uint64(gen) >> (8 * (7 - i)))
		meta[8+i] = byte(uint32(int64(number)) >> (8 * (7 - i)))
	}
	h.Write(meta[:])
	h.Write(nonce)
	h.Write(body)
	return h.Sum(nil)
}

func openSegment(key []byte, number int, gen int64, ciphertext []byte) ([]byte, error) {
	if len(ciphertext) < headerSize+tagSize {
		return nil, fmt.Errorf("%w: ciphertext too short", ErrIntegrity)
	}
	nonce := ciphertext[:nonceSize]
	body := ciphertext[headerSize : len(ciphertext)-tagSize]
	tag := ciphertext[len(ciphertext)-tagSize:]
	want := segmentTag(key, nonce, gen, number, body)
	if subtle.ConstantTimeCompare(tag, want) != 1 {
		return nil, errors.New("encupload: segment authentication failed")
	}
	return xorWithKeystream(key, nonce, gen, number, body), nil
}
