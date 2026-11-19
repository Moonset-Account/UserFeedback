package anonymize

import "encoding/binary"

// checksumFold folds a one's-complement sum of 32-bit words into a 16-bit
// one's-complement checksum (the complement is applied).
func checksumFold(sum uint32) uint16 {
	for sum>>16 != 0 {
		sum = (sum & 0xffff) + (sum >> 16)
	}
	return ^uint16(sum)
}

// sumWords adds the 16-bit big-endian words of b to sum. An odd trailing byte
// is padded with zero as required by RFC 1071.
func sumWords(sum uint32, b []byte) uint32 {
	for len(b) >= 2 {
		sum += uint32(b[0])<<8 | uint32(b[1])
		b = b[2:]
	}
	if len(b) == 1 {
		sum += uint32(b[0]) << 8
	}
	return sum
}

// ipHeaderChecksum computes the IPv4 header checksum over a header whose
// checksum field has already been zeroed.
func ipHeaderChecksum(header []byte) uint16 {
	return checksumFold(sumWords(0, header))
}

// ipChecksumValid reports whether the IPv4 header carries a valid checksum.
func ipChecksumValid(header []byte) bool {
	return checksumFold(sumWords(0, header)) == 0
}

// pseudoHeaderSum returns the one's-complement sum of the IPv4/UDP
// pseudo-header.
func pseudoHeaderSum(src, dst Addr, udpLen int) uint32 {
	var sum uint32
	sum = sumWords(sum, src[:])
	sum = sumWords(sum, dst[:])
	sum += uint32(protocolUDP) // zero byte + protocol
	sum += uint32(udpLen)
	return sum
}

// udpChecksumValid reports whether a fully captured UDP datagram (including
// its stored checksum field) verifies. It must not be called for truncated
// captures.
func udpChecksumValid(src, dst Addr, udpLen int, segment []byte) bool {
	return checksumFold(pseudoHeaderSum(src, dst, udpLen)+sumWords(0, segment)) == 0
}

// udpChecksum computes the checksum that would be stored for a datagram built
// from src/dst, udpLen and a UDP segment whose checksum field is zero.
func udpChecksum(src, dst Addr, udpLen int, segment []byte) uint16 {
	c := checksumFold(pseudoHeaderSum(src, dst, udpLen) + sumWords(0, segment))
	if c == 0 {
		// RFC 768: a computed checksum of zero is transmitted as all ones in
		// IPv4 so it cannot be confused with "checksum disabled".
		c = 0xffff
	}
	return c
}

// updateUDPChecksum applies the RFC 1624 incremental checksum update to a
// transmitted checksum c when the pseudo-header endpoint addresses change.
// No payload bytes are read, so the result is identical whether or not the
// payload was fully captured.
func updateUDPChecksum(c uint16, oldSrc, oldDst, newSrc, newDst Addr) uint16 {
	sum := uint32(^c) // ~HC
	for _, w := range [][4]byte{oldSrc, oldDst} {
		sum += uint32(^binary.BigEndian.Uint16(w[0:2])) & 0xffff
		sum += uint32(^binary.BigEndian.Uint16(w[2:4])) & 0xffff
	}
	for _, w := range [][4]byte{newSrc, newDst} {
		sum += uint32(binary.BigEndian.Uint16(w[0:2]))
		sum += uint32(binary.BigEndian.Uint16(w[2:4]))
	}

	updated := checksumFold(sum)
	if updated == 0 {
		updated = 0xffff
	}
	return updated
}
