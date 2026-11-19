package anonymize

import (
	"errors"
	"fmt"
)

// Sentinel errors returned (wrapped) by Translate. Match them with errors.Is.
var (
	// ErrTruncatedHeader reports that the sample or the requested capture
	// length does not include the full link, IPv4 and UDP headers needed for
	// a lossless rewrite.
	ErrTruncatedHeader = errors.New("anonymize: truncated header")
	// ErrNotIPv4 reports a non-IPv4 frame or a wrong IP version.
	ErrNotIPv4 = errors.New("anonymize: not IPv4")
	// ErrNotUDP reports a non-UDP IPv4 packet.
	ErrNotUDP = errors.New("anonymize: not UDP")
	// ErrFragmented reports a fragmented IPv4 packet. Only unfragmented
	// datagrams can be rewritten without touching the payload.
	ErrFragmented = errors.New("anonymize: fragmented packet")
	// ErrInvalidLengths reports inconsistent IPv4 total length and UDP length.
	ErrInvalidLengths = errors.New("anonymize: inconsistent lengths")
	// ErrInvalidHeader reports a malformed IPv4/UDP header.
	ErrInvalidHeader = errors.New("anonymize: invalid header")
	// ErrBadChecksum reports a packet whose original checksum does not verify.
	ErrBadChecksum = errors.New("anonymize: bad checksum")
	// ErrMissingMapping reports an endpoint address with no replacement in
	// the table.
	ErrMissingMapping = errors.New("anonymize: missing address mapping")
	// ErrInvalidTable reports an unusable replacement table.
	ErrInvalidTable = errors.New("anonymize: invalid address table")
	// ErrInvalidCaptureLen reports a capture length outside [0, len(sample)].
	ErrInvalidCaptureLen = errors.New("anonymize: invalid capture length")
	// ErrUnsupportedLink reports an unknown link-layer selector.
	ErrUnsupportedLink = errors.New("anonymize: unsupported link layer")
)

// Table maps original endpoint addresses to replacement addresses. Both the
// source and the destination address of every translated packet must be
// present in the table; the table is never extended implicitly.
type Table map[Addr]Addr

// ValidateTable reports whether t is a usable one-to-one replacement table.
//
// The table must be non-empty and injective: no two original addresses may be
// mapped onto the same replacement, since that would make the two endpoints
// indistinguishable in the shared output.
func ValidateTable(t Table) error {
	if len(t) == 0 {
		return fmt.Errorf("%w: no mappings", ErrInvalidTable)
	}

	seen := make(map[Addr]Addr, len(t))
	for from, to := range t {
		if prev, dup := seen[to]; dup {
			return fmt.Errorf("%w: %s and %s both map to %s", ErrInvalidTable, prev, from, to)
		}
		seen[to] = from
	}
	return nil
}
