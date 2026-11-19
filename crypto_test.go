package encupload

import (
	"bytes"
	"errors"
	"testing"
)

// TestSegmentEncryptionRoundTrip checks the synthetic backend's cipher:
// ciphertext hides the plaintext, round-trips correctly, and binds both the
// segment number and the key generation.
func TestSegmentEncryptionRoundTrip(t *testing.T) {
	plaintext := []byte("the quick brown fox jumps over the lazy dog")
	ct := SealSegment(1, 3, plaintext)

	if bytes.Contains(ct, plaintext) {
		t.Fatal("ciphertext contains the plaintext")
	}
	if len(ct) != len(plaintext)+nonceSize+tagSize {
		t.Fatalf("ciphertext length = %d, want %d", len(ct), len(plaintext)+nonceSize+tagSize)
	}
	pt, err := OpenSegment(1, 3, ct)
	if err != nil {
		t.Fatalf("OpenSegment: %v", err)
	}
	if !bytes.Equal(pt, plaintext) {
		t.Fatalf("round trip = %q, want %q", pt, plaintext)
	}

	// Wrong segment number: the tag binds position, so this must fail.
	if _, err := OpenSegment(1, 4, ct); err == nil {
		t.Fatal("opening under the wrong segment number succeeded")
	}
	// Wrong key generation: the tag binds the generation.
	if _, err := OpenSegment(2, 3, ct); err == nil {
		t.Fatal("opening under the wrong key generation succeeded")
	}

	// A single flipped bit anywhere in the body or tag is detected.
	for _, idx := range []int{nonceSize, nonceSize + 4, len(ct) - 1} {
		corrupt := append([]byte(nil), ct...)
		corrupt[idx] ^= 0x80
		if _, err := OpenSegment(1, 3, corrupt); err == nil {
			t.Fatalf("corruption at byte %d was not detected", idx)
		}
	}
	if _, err := OpenSegment(1, 3, ct[:headerSize+tagSize-1]); err == nil {
		t.Fatal("truncated ciphertext was accepted")
	}

	// Two seals of identical input differ (fresh nonce), so identical
	// segments are not byte-identical on the backend.
	if bytes.Equal(ct, SealSegment(1, 3, plaintext)) {
		t.Fatal("two seals of the same plaintext produced identical bytes")
	}
}

// TestTamperedStorageIsRejected is a white-box check: if the backend's stored
// ciphertext is altered after upload, completion refuses to publish rather
// than surfacing corrupt bytes.
func TestTamperedStorageIsRejected(t *testing.T) {
	c, sid := fixture(t, "media", 1)
	r0 := mustUpload(t, c, sid, 0, "payload")
	r1 := mustUpload(t, c, sid, 1, "tail")

	stored := c.sessions[sid].segments[0][0]
	stored.ciphertext[len(stored.ciphertext)/2] ^= 0x01

	_, err := c.Complete(selection(r0, r1))
	if err == nil {
		t.Fatal("completion published a tampered segment")
	}
	if _, objErr := c.Object("media"); !errors.Is(objErr, ErrObjectNotFound) {
		t.Fatalf("reader saw an object after tampering: %v", objErr)
	}
}

// TestDigestMismatchIsRejected is a white-box check: if the recorded digest no
// longer matches the authenticated bytes, completion fails with ErrIntegrity
// rather than publishing bytes the backend cannot vouch for. The cited receipt
// is adjusted to the forged digest so the earlier receipt check passes and the
// integrity check is the one that fires.
func TestDigestMismatchIsRejected(t *testing.T) {
	c, sid := fixture(t, "media", 1)
	r0 := mustUpload(t, c, sid, 0, "payload")

	forged := "0000000000000000000000000000000000000000000000000000000000000000"
	c.sessions[sid].segments[0][0].digest = forged
	r0.Digest = forged

	_, err := c.Complete(selection(r0))
	if !errors.Is(err, ErrIntegrity) {
		t.Fatalf("completion with a wrong digest = %v, want ErrIntegrity", err)
	}
}

// TestKeyFingerprintIsGenerationBound keeps the observation helper honest: a
// receipt's generation must map to one fingerprint, distinct per generation.
func TestKeyFingerprintIsGenerationBound(t *testing.T) {
	a, b := KeyFingerprint(1), KeyFingerprint(2)
	if a == b || a == "" || b == "" {
		t.Fatalf("fingerprints must be non-empty and distinct, got %q and %q", a, b)
	}
	if KeyFingerprint(1) != a {
		t.Fatal("fingerprint is not stable")
	}
}
