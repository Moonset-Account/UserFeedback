// Package anonymize rewrites the endpoint addresses of IPv4/UDP packet
// samples while keeping every other captured byte intact.
//
// It is intended for sharing synthetic packet-capture samples in which the
// payload is already address-free. Only the IPv4 source/destination addresses
// are replaced; the IPv4 header checksum and, when enabled, the UDP checksum
// are updated. The original IP total length and the captured payload are
// preserved, so samples with incomplete payload captures remain usable.
package anonymize

import (
	"encoding/binary"
	"fmt"
)

const (
	etherTypeIPv4  = 0x0800
	etherTypeVLAN  = 0x8100
	etherTypeQinQ  = 0x88a8
	etherTypeVLAN2 = 0x9100
	protocolUDP    = 17
)

// LinkLayer selects the framing of the input sample.
type LinkLayer uint8

const (
	// LinkEthernet expects an Ethernet II frame, including any number of
	// stacked 802.1Q/802.1ad VLAN tags.
	LinkEthernet LinkLayer = iota
	// LinkRawIPv4 expects the sample to start at the IPv4 header.
	LinkRawIPv4
)

// headerInfo describes the parsed offsets of a sample. All offsets are
// relative to the start of the sample byte slice.
type headerInfo struct {
	ipStart     int // first byte of the IPv4 header
	ipHeaderEnd int // first byte after the IPv4 header (incl. options)
	udpStart    int // first byte of the UDP header
	udpEnd      int // first byte after the UDP datagram (ipStart + IP total length)
	udpLen      int // value of the UDP length field
	udpChecksum uint16
	src, dst    Addr
}

func (h headerInfo) headerEnd() int { return h.udpStart + 8 }

// Translate copies the captured sample and applies the address table to its
// IPv4/UDP endpoints. captureLen is the number of captured bytes, counted
// from the first byte of pkt; it must include the complete link, IPv4 and UDP
// headers. A value of zero means "the whole slice was captured". Bytes beyond
// captureLen are ignored entirely and never inspected.
//
// The returned slice is always newly allocated, has exactly captureLen bytes
// and never aliases pkt. On error the returned slice is nil; in particular,
// samples with incomplete headers, missing endpoint mappings or invalid
// original checksums are rejected before any bytes are returned, so a caller
// can never accidentally publish a fragment that still holds an original
// address.
func Translate(pkt []byte, captureLen int, table Table, link LinkLayer) ([]byte, error) {
	if err := ValidateTable(table); err != nil {
		return nil, err
	}
	if captureLen < 0 {
		return nil, fmt.Errorf("%w: %d", ErrInvalidCaptureLen, captureLen)
	}
	if captureLen == 0 {
		captureLen = len(pkt)
	}
	if captureLen > len(pkt) {
		return nil, fmt.Errorf("%w: %d exceeds sample size %d", ErrInvalidCaptureLen, captureLen, len(pkt))
	}

	// Only the captured prefix is a valid input; anything after it was never
	// captured and must not influence parsing or checksum verification.
	sample := pkt[:captureLen]

	h, err := parseHeaders(sample, link)
	if err != nil {
		return nil, err
	}

	// The IPv4 header is always fully present, so its checksum can always be
	// verified. The UDP checksum can only be verified when the whole datagram
	// was captured; for partial captures the stored value is trusted, and the
	// incremental update below keeps it valid for the full datagram.
	if !ipChecksumValid(sample[h.ipStart:h.ipHeaderEnd]) {
		return nil, fmt.Errorf("%w: IPv4 header", ErrBadChecksum)
	}
	if h.udpChecksum != 0 && len(sample) >= h.udpEnd {
		if !udpChecksumValid(h.src, h.dst, h.udpLen, sample[h.udpStart:h.udpEnd]) {
			return nil, fmt.Errorf("%w: UDP datagram", ErrBadChecksum)
		}
	}

	newSrc, ok := table[h.src]
	if !ok {
		return nil, fmt.Errorf("%w: source address %s", ErrMissingMapping, h.src)
	}
	newDst, ok := table[h.dst]
	if !ok {
		return nil, fmt.Errorf("%w: destination address %s", ErrMissingMapping, h.dst)
	}

	out := make([]byte, captureLen)
	copy(out, sample)

	// Replace both endpoints. The capture is guaranteed to contain the full
	// IPv4 and UDP headers, so these slices are in range.
	copy(out[h.ipStart+12:h.ipStart+16], newSrc[:])
	copy(out[h.ipStart+16:h.ipStart+20], newDst[:])

	binary.BigEndian.PutUint16(out[h.ipStart+10:h.ipStart+12], 0)
	binary.BigEndian.PutUint16(out[h.ipStart+10:h.ipStart+12], ipHeaderChecksum(out[h.ipStart:h.ipHeaderEnd]))

	if h.udpChecksum != 0 {
		// RFC 1624 incremental update: only the two pseudo-header addresses
		// changed, so the result is identical to recomputing the checksum over
		// a fully captured, already translated datagram and then truncating.
		updated := updateUDPChecksum(h.udpChecksum, h.src, h.dst, newSrc, newDst)
		binary.BigEndian.PutUint16(out[h.udpStart+6:h.udpStart+8], updated)
	}
	// A zero UDP checksum means "checksum disabled" in IPv4; the all-zero
	// field is intentionally left untouched.

	return out, nil
}

// TranslateEthernet translates an Ethernet-framed sample. See Translate.
func TranslateEthernet(frame []byte, captureLen int, table Table) ([]byte, error) {
	return Translate(frame, captureLen, table, LinkEthernet)
}

// TranslateRawIPv4 translates a sample that starts directly at the IPv4 header.
func TranslateRawIPv4(packet []byte, captureLen int, table Table) ([]byte, error) {
	return Translate(packet, captureLen, table, LinkRawIPv4)
}

func parseHeaders(pkt []byte, link LinkLayer) (headerInfo, error) {
	var h headerInfo

	ipStart, err := parseLink(pkt, link)
	if err != nil {
		return h, err
	}
	if len(pkt) < ipStart+20 {
		return h, fmt.Errorf("%w: sample has %d bytes, need 20 for IPv4 header",
			ErrTruncatedHeader, len(pkt)-ipStart)
	}
	if v := pkt[ipStart] >> 4; v != 4 {
		return h, fmt.Errorf("%w: version %d", ErrNotIPv4, v)
	}
	ihl := int(pkt[ipStart]&0x0f) * 4
	if ihl < 20 {
		return h, fmt.Errorf("%w: IPv4 header length %d", ErrInvalidHeader, ihl)
	}
	if len(pkt) < ipStart+ihl {
		return h, fmt.Errorf("%w: sample has %d bytes, need %d for IPv4 header",
			ErrTruncatedHeader, len(pkt)-ipStart, ihl)
	}

	total := int(binary.BigEndian.Uint16(pkt[ipStart+2 : ipStart+4]))
	if total < ihl+8 {
		return h, fmt.Errorf("%w: IPv4 total length %d cannot hold an UDP datagram", ErrInvalidLengths, total)
	}
	flagsFrag := binary.BigEndian.Uint16(pkt[ipStart+6 : ipStart+8])
	if flagsFrag&0x2000 != 0 || flagsFrag&0x1fff != 0 {
		return h, fmt.Errorf("%w: flags/fragment offset 0x%04x", ErrFragmented, flagsFrag)
	}
	if proto := pkt[ipStart+9]; proto != protocolUDP {
		return h, fmt.Errorf("%w: IPv4 protocol %d", ErrNotUDP, proto)
	}

	h.ipStart = ipStart
	h.ipHeaderEnd = ipStart + ihl
	h.udpStart = h.ipHeaderEnd
	h.udpEnd = ipStart + total

	if len(pkt) < h.udpStart+8 {
		return headerInfo{}, fmt.Errorf("%w: sample has %d bytes, need %d for UDP header",
			ErrTruncatedHeader, len(pkt), h.udpStart+8)
	}
	h.udpLen = int(binary.BigEndian.Uint16(pkt[h.udpStart+4 : h.udpStart+6]))
	if h.udpLen < 8 {
		return headerInfo{}, fmt.Errorf("%w: UDP length %d", ErrInvalidLengths, h.udpLen)
	}
	if h.udpLen != total-ihl {
		return headerInfo{}, fmt.Errorf("%w: UDP length %d does not match IPv4 payload %d",
			ErrInvalidLengths, h.udpLen, total-ihl)
	}
	h.udpChecksum = binary.BigEndian.Uint16(pkt[h.udpStart+6 : h.udpStart+8])
	copy(h.src[:], pkt[ipStart+12:ipStart+16])
	copy(h.dst[:], pkt[ipStart+16:ipStart+20])
	return h, nil
}

func parseLink(pkt []byte, link LinkLayer) (int, error) {
	switch link {
	case LinkRawIPv4:
		return 0, nil
	case LinkEthernet:
		if len(pkt) < 14 {
			return 0, fmt.Errorf("%w: sample has %d bytes, need 14 for Ethernet header",
				ErrTruncatedHeader, len(pkt))
		}
		et := binary.BigEndian.Uint16(pkt[12:14])
		off := 14
		for et == etherTypeVLAN || et == etherTypeQinQ || et == etherTypeVLAN2 {
			if len(pkt) < off+4 {
				return 0, fmt.Errorf("%w: VLAN tag is truncated", ErrTruncatedHeader)
			}
			et = binary.BigEndian.Uint16(pkt[off+2 : off+4])
			off += 4
		}
		if et != etherTypeIPv4 {
			return 0, fmt.Errorf("%w: EtherType 0x%04x", ErrNotIPv4, et)
		}
		return off, nil
	default:
		return 0, fmt.Errorf("%w: %d", ErrUnsupportedLink, link)
	}
}
