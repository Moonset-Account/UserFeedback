package trustlist

import (
	"strings"
	"time"
)

// PublicKey is a key in its wire form: an algorithm name plus the encoded blob
// exactly as it was exchanged. The library never decodes or cryptographically
// verifies the blob; it compares the pair byte for byte, so callers must hand
// in the same encoding the peer presented.
type PublicKey struct {
	Type string
	Blob string
}

// NewPublicKey normalises a key type and trims both fields.
func NewPublicKey(keyType, blob string) PublicKey {
	return PublicKey{Type: normalizeKeyType(keyType), Blob: strings.TrimSpace(blob)}
}

func normalizeKeyType(s string) string {
	return strings.ToLower(strings.TrimSpace(s))
}

// String renders the key back into "<type>:<blob>" form.
func (k PublicKey) String() string {
	if normalizeKeyType(k.Type) == "" && strings.TrimSpace(k.Blob) == "" {
		return "<empty key>"
	}
	return normalizeKeyType(k.Type) + ":" + strings.TrimSpace(k.Blob)
}

// valid reports whether the key carries both a type and a blob.
func (k PublicKey) valid() bool {
	return normalizeKeyType(k.Type) != "" && strings.TrimSpace(k.Blob) != ""
}

// keyEqual compares two keys, treating the type as case-insensitive and the
// blob as an exact byte sequence.
func keyEqual(a, b PublicKey) bool {
	return normalizeKeyType(a.Type) == normalizeKeyType(b.Type) &&
		strings.TrimSpace(a.Blob) == strings.TrimSpace(b.Blob)
}

// HostCertificate is a host certificate presented by the peer.
//
// SignatureVerified is the caller's assertion that the certificate signature
// was checked against Issuer by the transport. The library does not repeat the
// cryptographic check, but it refuses a certificate whose flag is false, and it
// always enforces the validity window and the principal coverage itself because
// those depend on the connection name and on the evaluation time, which only
// the caller has.
type HostCertificate struct {
	// Subject is the key the peer proved possession of.
	Subject PublicKey
	// Issuer is the certificate authority key that signed the certificate.
	Issuer PublicKey
	// Principals are the names the certificate is valid for; globs allowed.
	Principals []string
	// NotBefore and NotAfter bound the validity window inclusively. A zero
	// NotAfter is treated as expired.
	NotBefore time.Time
	NotAfter  time.Time
	// SignatureVerified must be true for the certificate to be usable.
	SignatureVerified bool
}

// coversName reports whether any principal matches host.
func (c *HostCertificate) coversName(host string) bool {
	for _, principal := range c.Principals {
		if matchName(principal, host) {
			return true
		}
	}
	return false
}
