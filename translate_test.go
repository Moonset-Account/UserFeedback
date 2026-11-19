package anonymize

import (
	"bytes"
	"encoding/binary"
	"errors"
	"testing"
)

// --- synthetic fixture builders (no external captures) ----------------------

func mustAddr(t *testing.T, s string) Addr {
	t.Helper()
	a, err := ParseAddr(s)
	if err != nil {
		t.Fatalf("ParseAddr(%q): %v", s, err)
	}
	return a
}

type udpFixture struct {
	src, dst    Addr
	payload     []byte
	udpChecksum bool
}

// buildIPv4UDP assembles an unfragmented IPv4/UDP packet with correct
// checksums, starting at the IPv4 header.
func buildIPv4UDP(t *testing.T, f udpFixture) []byte {
	t.Helper()
	const ipHL = 20
	udpLen := 8 + len(f.payload)
	total := ipHL + udpLen

	pkt := make([]byte, total)
	pkt[0] = 0x45
	binary.BigEndian.PutUint16(pkt[2:4], uint16(total))
	binary.BigEndian.PutUint16(pkt[4:6], 0x1234) // identification
	binary.BigEndian.PutUint16(pkt[6:8], 0x4000) // DF, fragment offset 0
	pkt[8] = 64                                  // TTL
	pkt[9] = protocolUDP
	copy(pkt[12:16], f.src[:])
	copy(pkt[16:20], f.dst[:])
	binary.BigEndian.PutUint16(pkt[10:12], ipHeaderChecksum(pkt[:ipHL]))

	udp := pkt[ipHL:]
	binary.BigEndian.PutUint16(udp[0:2], 40000) // source port
	binary.BigEndian.PutUint16(udp[2:4], 40001) // destination port
	binary.BigEndian.PutUint16(udp[4:6], uint16(udpLen))
	copy(udp[8:], f.payload)
	if f.udpChecksum {
		binary.BigEndian.PutUint16(udp[6:8], 0)
		binary.BigEndian.PutUint16(udp[6:8], udpChecksum(f.src, f.dst, udpLen, udp))
	}
	return pkt
}

// buildEthernet wraps an IPv4 packet in an Ethernet II frame, optionally with
// the given number of stacked VLAN tags.
func buildEthernet(t *testing.T, ip []byte, vlanTags int) []byte {
	t.Helper()
	frame := []byte{
		0x02, 0x00, 0x00, 0x00, 0x00, 0x01, // dst MAC
		0x02, 0x00, 0x00, 0x00, 0x00, 0x02, // src MAC
	}
	for i := 0; i < vlanTags; i++ {
		et := uint16(etherTypeVLAN)
		if i == 0 {
			et = etherTypeQinQ
		}
		frame = binary.BigEndian.AppendUint16(frame, et)
		frame = binary.BigEndian.AppendUint16(frame, uint16(100+i)) // TCID
	}
	frame = binary.BigEndian.AppendUint16(frame, etherTypeIPv4)
	return append(frame, ip...)
}

func headerEndFor(t *testing.T, sample []byte, link LinkLayer) int {
	t.Helper()
	h, err := parseHeaders(sample, link)
	if err != nil {
		t.Fatalf("parseHeaders: %v", err)
	}
	return h.headerEnd()
}

func udpOffsetFor(t *testing.T, sample []byte, link LinkLayer) int {
	t.Helper()
	h, err := parseHeaders(sample, link)
	if err != nil {
		t.Fatalf("parseHeaders: %v", err)
	}
	return h.udpStart
}

// --- core accept path -------------------------------------------------------

func TestTranslateFullCaptureReplacesOnlyEndpointsAndChecksums(t *testing.T) {
	oldSrc := mustAddr(t, "192.168.10.7")
	oldDst := mustAddr(t, "10.20.30.40")
	newSrc := mustAddr(t, "203.0.113.5")
	newDst := mustAddr(t, "198.51.100.9")
	payload := []byte("synthetic application payload without addresses")
	fixture := buildIPv4UDP(t, udpFixture{src: oldSrc, dst: oldDst, payload: payload, udpChecksum: true})
	frame := buildEthernet(t, fixture, 0)

	table := Table{oldSrc: newSrc, oldDst: newDst}
	out, err := TranslateEthernet(frame, 0, table)
	if err != nil {
		t.Fatalf("TranslateEthernet: %v", err)
	}
	if len(out) != len(frame) {
		t.Fatalf("length changed: got %d want %d", len(out), len(frame))
	}
	if &out[0] == &frame[0] {
		t.Fatal("output aliases input sample")
	}

	h, err := parseHeaders(out, LinkEthernet)
	if err != nil {
		t.Fatalf("re-parse output: %v", err)
	}
	if h.src != newSrc || h.dst != newDst {
		t.Fatalf("endpoints = %s -> %s, want %s -> %s", h.src, h.dst, newSrc, newDst)
	}
	if !ipChecksumValid(out[h.ipStart:h.ipHeaderEnd]) {
		t.Fatal("IPv4 header checksum invalid after translation")
	}
	if !udpChecksumValid(newSrc, newDst, h.udpLen, out[h.udpStart:h.udpEnd]) {
		t.Fatal("UDP checksum invalid after translation")
	}

	// Everything except the two addresses and the two checksum fields is
	// byte-identical, including the IP total length and the payload.
	want := append([]byte(nil), frame...)
	copy(want[h.ipStart+12:h.ipStart+16], newSrc[:])
	copy(want[h.ipStart+16:h.ipStart+20], newDst[:])
	binary.BigEndian.PutUint16(want[h.ipStart+10:h.ipStart+12], 0)
	binary.BigEndian.PutUint16(want[h.ipStart+10:h.ipStart+12], ipHeaderChecksum(want[h.ipStart:h.ipHeaderEnd]))
	binary.BigEndian.PutUint16(want[h.udpStart+6:h.udpStart+8], binary.BigEndian.Uint16(out[h.udpStart+6:h.udpStart+8]))
	if !bytes.Equal(out, want) {
		t.Fatal("output differs from expected targeted rewrite")
	}
	if got := binary.BigEndian.Uint16(out[h.ipStart+2 : h.ipStart+4]); int(got) != len(out)-h.ipStart {
		t.Fatalf("IPv4 total length changed: %d", got)
	}
	if !bytes.Contains(out[h.udpStart+8:h.udpEnd], payload) {
		t.Fatal("captured payload was modified")
	}
	for _, old := range []Addr{oldSrc, oldDst} {
		if bytes.Contains(out, old[:]) {
			t.Fatalf("output still contains original address %s", old)
		}
	}
}

// --- truncation / cross constraint -----------------------------------------

func TestTranslateTruncatedHeaderMatchesFullThenTruncate(t *testing.T) {
	oldSrc := mustAddr(t, "172.16.0.10")
	oldDst := mustAddr(t, "172.16.0.20")
	newSrc := mustAddr(t, "192.0.2.1")
	newDst := mustAddr(t, "192.0.2.2")
	payload := []byte("0123456789abcdefghijklmnopqrstuvwxyz") // 36-byte payload
	if len(payload)%2 != 0 {
		t.Fatal("fixture payload should be even to exercise the stated case")
	}
	fixture := buildIPv4UDP(t, udpFixture{src: oldSrc, dst: oldDst, payload: payload, udpChecksum: true})
	frame := buildEthernet(t, fixture, 0)
	table := Table{oldSrc: newSrc, oldDst: newDst}

	full, err := TranslateEthernet(frame, 0, table)
	if err != nil {
		t.Fatalf("full translation: %v", err)
	}
	headerEnd := headerEndFor(t, frame, LinkEthernet)
	fullUDPChecksum := binary.BigEndian.Uint16(full[udpOffsetFor(t, frame, LinkEthernet)+6:])

	truncations := map[string]int{
		"header only":        headerEnd,
		"one payload byte":   headerEnd + 1,
		"half payload":       headerEnd + len(payload)/2,
		"one byte short":     len(frame) - 1,
		"full capture again": len(frame),
	}
	for name, captureLen := range truncations {
		t.Run(name, func(t *testing.T) {
			out, err := TranslateEthernet(frame, captureLen, table)
			if err != nil {
				t.Fatalf("TranslateEthernet(captureLen=%d): %v", captureLen, err)
			}
			if len(out) != captureLen {
				t.Fatalf("output length = %d, want captureLen %d", len(out), captureLen)
			}
			if !bytes.Equal(out, full[:captureLen]) {
				t.Fatal("truncated output differs from full translation truncated to captureLen")
			}
			got := binary.BigEndian.Uint16(out[udpOffsetFor(t, frame, LinkEthernet)+6:])
			if got != fullUDPChecksum {
				t.Fatalf("UDP checksum = 0x%04x, want 0x%04x from full translation", got, fullUDPChecksum)
			}
			if bytes.Contains(out, oldSrc[:]) || bytes.Contains(out, oldDst[:]) {
				t.Fatal("truncated output leaks an original address")
			}
		})
	}
}

func TestTranslateTruncatedSampleSlice(t *testing.T) {
	// The capture itself may end mid-payload: the slice is shorter than the
	// IP total length advertises.
	oldSrc := mustAddr(t, "10.1.1.1")
	oldDst := mustAddr(t, "10.2.2.2")
	newSrc := mustAddr(t, "203.0.113.77")
	newDst := mustAddr(t, "203.0.113.88")
	fixture := buildIPv4UDP(t, udpFixture{
		src: oldSrc, dst: oldDst,
		payload:     bytes.Repeat([]byte{0xaa, 0xbb, 0xcc}, 40),
		udpChecksum: true,
	})
	sample := buildEthernet(t, fixture, 0)
	headerEnd := headerEndFor(t, sample, LinkEthernet)

	for _, cut := range []int{headerEnd, headerEnd + 7, len(sample) / 2} {
		partial := sample[:cut]
		out, err := TranslateEthernet(partial, 0, Table{oldSrc: newSrc, oldDst: newDst})
		if err != nil {
			t.Fatalf("TranslateEthernet(partial len %d): %v", cut, err)
		}
		if len(out) != cut {
			t.Fatalf("output length = %d, want %d", len(out), cut)
		}
		h, err := parseHeaders(out, LinkEthernet)
		if err != nil {
			t.Fatalf("parse output: %v", err)
		}
		if !ipChecksumValid(out[h.ipStart:h.ipHeaderEnd]) {
			t.Fatal("IPv4 checksum invalid in partial capture")
		}
		if h.src != newSrc || h.dst != newDst {
			t.Fatalf("endpoints = %s -> %s", h.src, h.dst)
		}
		// Stored UDP checksum must equal the one a full translation yields,
		// because the rewrite is independent of how much payload was captured.
		full, err := TranslateEthernet(sample, 0, Table{oldSrc: newSrc, oldDst: newDst})
		if err != nil {
			t.Fatalf("full translation: %v", err)
		}
		off := udpOffsetFor(t, sample, LinkEthernet)
		if got, want := binary.BigEndian.Uint16(out[off+6:off+8]), binary.BigEndian.Uint16(full[off+6:off+8]); got != want {
			t.Fatalf("UDP checksum = 0x%04x, want 0x%04x", got, want)
		}
	}
}

// TestBytesBeyondCaptureLengthAreIgnored pins down that captureLen, not
// len(pkt), decides what is inspected: garbage appended after the capture
// must not affect validation or the produced prefix.
func TestBytesBeyondCaptureLengthAreIgnored(t *testing.T) {
	src := mustAddr(t, "10.3.3.3")
	dst := mustAddr(t, "10.4.4.4")
	table := Table{src: mustAddr(t, "203.0.113.33"), dst: mustAddr(t, "203.0.113.44")}
	frame := buildEthernet(t, buildIPv4UDP(t, udpFixture{
		src: src, dst: dst, payload: bytes.Repeat([]byte{0x11}, 16), udpChecksum: true,
	}), 0)
	headerEnd := headerEndFor(t, frame, LinkEthernet)
	cut := headerEnd + 4

	want, err := TranslateEthernet(frame, cut, table)
	if err != nil {
		t.Fatalf("baseline: %v", err)
	}

	dirty := append(append([]byte(nil), frame...), 0xde, 0xad, 0xbe, 0xef)
	got, err := TranslateEthernet(dirty, cut, table)
	if err != nil {
		t.Fatalf("with trailing garbage: %v", err)
	}
	if !bytes.Equal(got, want) {
		t.Fatal("bytes past captureLen influenced the translated capture")
	}
	if len(got) != cut {
		t.Fatalf("output length = %d, want %d", len(got), cut)
	}
}

// TestEthernetPaddingPreserved covers the common capture shape where the frame
// is longer than the IP total length because of Ethernet minimum-frame
// padding. The padding must survive verbatim, and the UDP checksum must cover
// only the bytes inside the IP total length.
func TestEthernetPaddingPreserved(t *testing.T) {
	src := mustAddr(t, "10.80.0.1")
	dst := mustAddr(t, "10.80.0.2")
	newSrc := mustAddr(t, "203.0.113.81")
	newDst := mustAddr(t, "203.0.113.82")
	table := Table{src: newSrc, dst: newDst}

	ip := buildIPv4UDP(t, udpFixture{src: src, dst: dst, payload: []byte("tiny"), udpChecksum: true})
	frame := buildEthernet(t, ip, 0)
	padding := bytes.Repeat([]byte{0x00}, 18) // pad the frame to the 60-byte minimum
	frame = append(frame, padding...)
	ipEnd := 14 + len(ip)

	out, err := TranslateEthernet(frame, 0, table)
	if err != nil {
		t.Fatalf("TranslateEthernet: %v", err)
	}
	if !bytes.Equal(out[ipEnd:], frame[ipEnd:]) {
		t.Fatal("Ethernet padding was modified")
	}
	if len(out) != len(frame) {
		t.Fatalf("frame length changed: %d -> %d", len(frame), len(out))
	}
	h, err := parseHeaders(out, LinkEthernet)
	if err != nil {
		t.Fatalf("parse output: %v", err)
	}
	if h.udpEnd != ipEnd {
		t.Fatalf("UDP datagram end = %d, want %d (must stop at IP total length)", h.udpEnd, ipEnd)
	}
	if !udpChecksumValid(newSrc, newDst, h.udpLen, out[h.udpStart:h.udpEnd]) {
		t.Fatal("UDP checksum is not computed over exactly the IP total length")
	}
	if !ipChecksumValid(out[h.ipStart:h.ipHeaderEnd]) {
		t.Fatal("IPv4 header checksum invalid")
	}
}

// --- UDP checksum disabled / zero-result rule ------------------------------

func TestTranslateDisabledUDPChecksumStaysZero(t *testing.T) {
	oldSrc := mustAddr(t, "192.168.0.1")
	oldDst := mustAddr(t, "192.168.0.2")
	table := Table{oldSrc: mustAddr(t, "198.18.0.1"), oldDst: mustAddr(t, "198.18.0.2")}
	fixture := buildIPv4UDP(t, udpFixture{src: oldSrc, dst: oldDst, payload: []byte("no checksum here"), udpChecksum: false})
	sample := buildEthernet(t, fixture, 0)
	off := udpOffsetFor(t, sample, LinkEthernet)

	for name, captureLen := range map[string]int{
		"full":      0,
		"truncated": headerEndFor(t, sample, LinkEthernet) + 4,
	} {
		t.Run(name, func(t *testing.T) {
			out, err := TranslateEthernet(sample, captureLen, table)
			if err != nil {
				t.Fatalf("TranslateEthernet: %v", err)
			}
			if got := binary.BigEndian.Uint16(out[off+6 : off+8]); got != 0 {
				t.Fatalf("disabled UDP checksum changed to 0x%04x, want 0x0000", got)
			}
		})
	}
}

func TestTranslateZeroComputedUDPChecksumIsAllOnes(t *testing.T) {
	oldSrc := mustAddr(t, "10.9.9.9")
	oldDst := mustAddr(t, "10.8.8.8")
	newDst := mustAddr(t, "203.0.113.200")
	payload := []byte("zero checksum collision fixture")
	fixture := buildIPv4UDP(t, udpFixture{src: oldSrc, dst: oldDst, payload: payload, udpChecksum: true})

	// Locate a replacement source address whose translated datagram checksum
	// computes to zero. Deterministic search over synthetic addresses.
	zeroed := append([]byte(nil), fixture[20:]...)
	binary.BigEndian.PutUint16(zeroed[6:8], 0)
	udpLen := len(zeroed)

	var newSrc Addr
	found := false
	for i := uint32(0); i < 1<<20; i++ {
		cand := Addr{10, byte(i >> 16), byte(i >> 8), byte(i)}
		raw := checksumFold(pseudoHeaderSum(cand, newDst, udpLen) + sumWords(0, zeroed))
		if raw == 0 {
			newSrc, found = cand, true
			break
		}
	}
	if !found {
		t.Fatal("could not find a replacement address producing a zero checksum")
	}

	sample := buildEthernet(t, fixture, 0)
	out, err := TranslateEthernet(sample, 0, Table{oldSrc: newSrc, oldDst: newDst})
	if err != nil {
		t.Fatalf("TranslateEthernet: %v", err)
	}
	h, err := parseHeaders(out, LinkEthernet)
	if err != nil {
		t.Fatalf("parse output: %v", err)
	}
	if got := binary.BigEndian.Uint16(out[h.udpStart+6 : h.udpStart+8]); got != 0xffff {
		t.Fatalf("zero computed checksum stored as 0x%04x, want 0xffff", got)
	}
	if !udpChecksumValid(newSrc, newDst, h.udpLen, out[h.udpStart:h.udpEnd]) {
		t.Fatal("all-ones checksum does not verify")
	}

	// The same rule must hold for a payload-truncated capture, where the
	// incremental update path is the only one available.
	cut := headerEndFor(t, sample, LinkEthernet) + 5
	truncOut, err := TranslateEthernet(sample, cut, Table{oldSrc: newSrc, oldDst: newDst})
	if err != nil {
		t.Fatalf("TranslateEthernet truncated: %v", err)
	}
	if got := binary.BigEndian.Uint16(truncOut[h.udpStart+6 : h.udpStart+8]); got != 0xffff {
		t.Fatalf("truncated zero computed checksum stored as 0x%04x, want 0xffff", got)
	}
}

// TestTransmittedAllOnesChecksumRoundTrips starts from a datagram whose stored
// UDP checksum is already 0xffff — the one value that is both "negative zero"
// and a legitimate all-ones sum — and checks that it still verifies after the
// address rewrite. A stored 0xffff that does not verify must be rejected.
func TestTransmittedAllOnesChecksumRoundTrips(t *testing.T) {
	src := mustAddr(t, "10.11.12.13")
	dst := mustAddr(t, "10.11.12.14")
	newSrc := mustAddr(t, "203.0.113.61")
	newDst := mustAddr(t, "203.0.113.62")

	// Find a payload whose checksum (field zeroed) folds to zero, so the
	// stored checksum is 0xffff.
	udpLen := 8 + 2
	var payload [2]byte
	found := false
	var zeroSum uint32
	for i := 0; i < 1<<16; i++ {
		payload[0], payload[1] = byte(i>>8), byte(i)
		ip := buildIPv4UDP(t, udpFixture{src: src, dst: dst, payload: payload[:], udpChecksum: false})
		udp := ip[20:]
		zeroSum = pseudoHeaderSum(src, dst, udpLen) + sumWords(0, udp)
		if checksumFold(zeroSum) == 0 {
			found = true
			break
		}
	}
	if !found {
		t.Fatal("could not construct a zero-sum UDP datagram")
	}

	ip := buildIPv4UDP(t, udpFixture{src: src, dst: dst, payload: payload[:], udpChecksum: false})
	udp := ip[20:]
	binary.BigEndian.PutUint16(udp[6:8], 0xffff) // transmitted all ones
	if !udpChecksumValid(src, dst, udpLen, udp) {
		t.Fatal("stored 0xffff does not verify on a zero-sum datagram")
	}
	binary.BigEndian.PutUint16(ip[10:12], 0)
	binary.BigEndian.PutUint16(ip[10:12], ipHeaderChecksum(ip[:20]))

	out, err := TranslateEthernet(buildEthernet(t, ip, 0), 0, Table{src: newSrc, dst: newDst})
	if err != nil {
		t.Fatalf("TranslateEthernet: %v", err)
	}
	h, err := parseHeaders(out, LinkEthernet)
	if err != nil {
		t.Fatalf("parse output: %v", err)
	}
	// The stored 0xffff was a negative-zero sum, so after changing the
	// addresses the result must equal a full recomputation rather than the
	// literal value 0xffff.
	expected := append([]byte(nil), out[h.udpStart:h.udpEnd]...)
	binary.BigEndian.PutUint16(expected[6:8], 0)
	want := checksumFold(pseudoHeaderSum(newSrc, newDst, h.udpLen) + sumWords(0, expected))
	if want == 0 {
		want = 0xffff
	}
	if got := binary.BigEndian.Uint16(out[h.udpStart+6 : h.udpStart+8]); got != want {
		t.Fatalf("checksum = 0x%04x, want 0x%04x from full recomputation", got, want)
	}
	if !udpChecksumValid(newSrc, newDst, h.udpLen, out[h.udpStart:h.udpEnd]) {
		t.Fatal("all-ones checksum does not verify after translation")
	}

	// Flip one payload bit so the stored 0xffff no longer verifies; the
	// translator must refuse the sample rather than emit it.
	broken := append([]byte(nil), ip...)
	broken[20+8] ^= 0x01
	binary.BigEndian.PutUint16(broken[10:12], 0)
	binary.BigEndian.PutUint16(broken[10:12], ipHeaderChecksum(broken[:20]))
	badOut, err := TranslateEthernet(buildEthernet(t, broken, 0), 0, Table{src: newSrc, dst: newDst})
	if !errors.Is(err, ErrBadChecksum) {
		t.Fatalf("error = %v, want ErrBadChecksum", err)
	}
	if badOut != nil {
		t.Fatal("bad checksum returned a fragment")
	}
}

// --- rejection paths --------------------------------------------------------

func TestRejectIncompleteHeadersWithoutLeakingOriginal(t *testing.T) {
	oldSrc := mustAddr(t, "192.168.5.5")
	oldDst := mustAddr(t, "192.168.6.6")
	table := Table{oldSrc: mustAddr(t, "198.18.5.5"), oldDst: mustAddr(t, "198.18.6.6")}
	sample := buildEthernet(t, buildIPv4UDP(t, udpFixture{
		src: oldSrc, dst: oldDst, payload: []byte("payload"), udpChecksum: true,
	}), 0)
	headerEnd := headerEndFor(t, sample, LinkEthernet)

	cases := map[string]struct {
		sample     []byte
		captureLen int
		lackBytes  int // slice sample to this length, if > 0
	}{
		"capture cuts IP header":  {captureLen: 14 + 10},
		"capture cuts UDP header": {captureLen: headerEnd - 1},
		"sample cuts IP header":   {sample: sample, lackBytes: 14 + 19},
		"sample cuts UDP header":  {sample: sample, lackBytes: headerEnd - 1},
		"empty sample":            {sample: []byte{}},
		"capture beyond sample":   {captureLen: len(sample) + 1},
		"negative capture length": {captureLen: -1},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			pkt := tc.sample
			if pkt == nil {
				pkt = sample
			}
			if tc.lackBytes > 0 {
				pkt = pkt[:tc.lackBytes]
			}
			out, err := TranslateEthernet(pkt, tc.captureLen, table)
			if err == nil {
				t.Fatalf("expected error, got %d bytes", len(out))
			}
			if out != nil {
				t.Fatal("rejected sample returned a non-nil fragment")
			}
			if bytes.Contains(out, oldSrc[:]) || bytes.Contains(out, oldDst[:]) {
				t.Fatal("rejected sample leaked an original address")
			}
		})
	}
}

func TestRejectMissingMappings(t *testing.T) {
	oldSrc := mustAddr(t, "10.10.10.1")
	oldDst := mustAddr(t, "10.10.10.2")
	sample := buildEthernet(t, buildIPv4UDP(t, udpFixture{
		src: oldSrc, dst: oldDst, payload: []byte("x"), udpChecksum: true,
	}), 0)

	cases := map[string]Table{
		"empty table":         {},
		"missing destination": {oldSrc: mustAddr(t, "198.18.0.1")},
		"missing source":      {oldDst: mustAddr(t, "198.18.0.2")},
		"unrelated entries":   {mustAddr(t, "1.2.3.4"): mustAddr(t, "5.6.7.8")},
	}
	for name, table := range cases {
		t.Run(name, func(t *testing.T) {
			out, err := TranslateEthernet(sample, 0, table)
			if !errors.Is(err, ErrMissingMapping) && !errors.Is(err, ErrInvalidTable) {
				t.Fatalf("error = %v, want missing mapping or invalid table", err)
			}
			if out != nil {
				t.Fatal("missing mapping returned a fragment")
			}
		})
	}

	// A table that maps both endpoints is accepted even with extra entries.
	okTable := Table{
		oldSrc:                 mustAddr(t, "198.18.0.1"),
		oldDst:                 mustAddr(t, "198.18.0.2"),
		mustAddr(t, "8.8.8.8"): mustAddr(t, "9.9.9.9"),
	}
	newSrc, newDst := mustAddr(t, "198.18.0.1"), mustAddr(t, "198.18.0.2")
	out, err := TranslateEthernet(sample, 0, okTable)
	if err != nil {
		t.Fatalf("complete table rejected: %v", err)
	}
	if !bytes.Equal(out[14+12:14+16], newSrc[:]) {
		t.Fatal("source was not mapped")
	}
	if !bytes.Equal(out[14+16:14+20], newDst[:]) {
		t.Fatal("destination was not mapped")
	}
}

func TestRejectBadOriginalChecksums(t *testing.T) {
	oldSrc := mustAddr(t, "172.20.0.1")
	oldDst := mustAddr(t, "172.20.0.2")
	table := Table{oldSrc: mustAddr(t, "198.18.1.1"), oldDst: mustAddr(t, "198.18.1.2")}

	base := buildIPv4UDP(t, udpFixture{
		src: oldSrc, dst: oldDst, payload: []byte("checksum fixture"), udpChecksum: true,
	})

	badIP := append([]byte(nil), base...)
	badIP[10] ^= 0xff

	badUDP := append([]byte(nil), base...)
	badUDP[len(base)-1] ^= 0xff

	for name, ip := range map[string][]byte{"bad IPv4": badIP, "bad UDP": badUDP} {
		t.Run(name, func(t *testing.T) {
			out, err := TranslateEthernet(buildEthernet(t, ip, 0), 0, table)
			if !errors.Is(err, ErrBadChecksum) {
				t.Fatalf("error = %v, want ErrBadChecksum", err)
			}
			if out != nil {
				t.Fatal("bad checksum returned a fragment")
			}
		})
	}

	// A truncated payload cannot invalidate the original UDP checksum, so the
	// same translated header must still be produced.
	badUDPFrame := buildEthernet(t, badUDP, 0)
	partial := badUDPFrame[:headerEndFor(t, badUDPFrame, LinkEthernet)+3]
	if _, err := TranslateEthernet(partial, 0, table); err != nil {
		t.Fatalf("partial capture rejected on unverifiable UDP payload: %v", err)
	}
}

func TestRejectFragmentedAndNonUDPAndWrongLengths(t *testing.T) {
	src := mustAddr(t, "192.168.1.1")
	dst := mustAddr(t, "192.168.1.2")
	table := Table{src: mustAddr(t, "198.18.2.1"), dst: mustAddr(t, "198.18.2.2")}

	base := buildIPv4UDP(t, udpFixture{src: src, dst: dst, payload: []byte("abcd"), udpChecksum: true})

	fragMF := append([]byte(nil), base...)
	fragMF[6] = 0x20 // MF set
	fragOff := append([]byte(nil), base...)
	fragOff[7] = 0x10 // fragment offset = 16
	recomputeIP(t, fragMF)
	recomputeIP(t, fragOff)

	tcp := append([]byte(nil), base...)
	tcp[9] = 6
	recomputeIP(t, tcp)

	badIPv4 := append([]byte(nil), base...)
	badIPv4[0] = 0x65 // version 6, IHL 5
	recomputeIP(t, badIPv4)

	shortIHL := append([]byte(nil), base...)
	shortIHL[0] = 0x44 // IHL 4 -> 16-byte header
	recomputeIP(t, shortIHL)

	badUDPInner := append([]byte(nil), base...)
	badUDPInner[20+5] = 7 // UDP length off by one
	recomputeIP(t, badUDPInner)

	mismatch := append([]byte(nil), base...)
	binary.BigEndian.PutUint16(mismatch[2:4], uint16(len(base)+4)) // IP total length lies
	recomputeIP(t, mismatch)

	arp := append([]byte(nil), buildEthernet(t, base, 0)...)
	binary.BigEndian.PutUint16(arp[12:14], 0x0806) // ARP EtherType

	cases := []struct {
		name  string
		ip    []byte // wrapped in Ethernet unless frame is set
		frame []byte
		want  error
	}{
		{name: "MF fragment", ip: fragMF, want: ErrFragmented},
		{name: "fragment offset", ip: fragOff, want: ErrFragmented},
		{name: "non-UDP protocol", ip: tcp, want: ErrNotUDP},
		{name: "bad IPv4 version", ip: badIPv4, want: ErrNotIPv4},
		{name: "IPv4 header shorter than 20", ip: shortIHL, want: ErrInvalidHeader},
		{name: "wrong UDP length", ip: badUDPInner, want: ErrInvalidLengths},
		{name: "wrong IP total length", ip: mismatch, want: ErrInvalidLengths},
		{name: "non-IPv4 EtherType", frame: arp, want: ErrNotIPv4},
		{name: "truncated Ethernet header", frame: buildEthernet(t, base, 0)[:12], want: ErrTruncatedHeader},
		{name: "truncated VLAN tag", frame: func() []byte {
			return buildEthernet(t, base, 1)[:16]
		}(), want: ErrTruncatedHeader},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			pkt := tc.frame
			if pkt == nil {
				pkt = buildEthernet(t, tc.ip, 0)
			}
			out, err := Translate(pkt, 0, table, LinkEthernet)
			if !errors.Is(err, tc.want) {
				t.Fatalf("error = %v, want %v", err, tc.want)
			}
			if out != nil {
				t.Fatal("rejected packet returned a fragment")
			}
			if bytes.Contains(out, src[:]) || bytes.Contains(out, dst[:]) {
				t.Fatal("rejected packet leaked an original address")
			}
		})
	}

	// Unknown link selector is rejected.
	if _, err := Translate(base, 0, table, LinkLayer(42)); !errors.Is(err, ErrUnsupportedLink) {
		t.Fatalf("error = %v, want ErrUnsupportedLink", err)
	}
}

func recomputeIP(t *testing.T, ipPacket []byte) {
	t.Helper()
	if len(ipPacket) < 20 {
		t.Fatal("packet too short")
	}
	binary.BigEndian.PutUint16(ipPacket[10:12], 0)
	binary.BigEndian.PutUint16(ipPacket[10:12], ipHeaderChecksum(ipPacket[:20]))
}

// --- link-layer and option coverage -----------------------------------------

func TestLinkVariants(t *testing.T) {
	src := mustAddr(t, "10.50.0.1")
	dst := mustAddr(t, "10.50.0.2")
	newSrc := mustAddr(t, "198.18.50.1")
	newDst := mustAddr(t, "198.18.50.2")
	table := Table{src: newSrc, dst: newDst}

	ip := buildIPv4UDP(t, udpFixture{src: src, dst: dst, payload: []byte("link variants"), udpChecksum: true})

	t.Run("raw IPv4", func(t *testing.T) {
		out, err := TranslateRawIPv4(ip, 0, table)
		if err != nil {
			t.Fatalf("TranslateRawIPv4: %v", err)
		}
		h, err := parseHeaders(out, LinkRawIPv4)
		if err != nil {
			t.Fatalf("re-parse: %v", err)
		}
		if h.src != newSrc || h.dst != newDst {
			t.Fatalf("endpoints = %s -> %s", h.src, h.dst)
		}
		if !udpChecksumValid(newSrc, newDst, h.udpLen, out[h.udpStart:h.udpEnd]) {
			t.Fatal("UDP checksum invalid")
		}
	})

	for _, tags := range []int{0, 1, 2} {
		frame := buildEthernet(t, ip, tags)
		out, err := TranslateEthernet(frame, 0, table)
		if err != nil {
			t.Fatalf("vlan tags %d: %v", tags, err)
		}
		h, err := parseHeaders(out, LinkEthernet)
		if err != nil {
			t.Fatalf("vlan tags %d re-parse: %v", tags, err)
		}
		if h.src != newSrc || h.dst != newDst {
			t.Fatalf("vlan tags %d endpoints = %s -> %s", tags, h.src, h.dst)
		}
		// The Ethernet/VLAN framing is untouched.
		if !bytes.Equal(out[:h.ipStart], frame[:h.ipStart]) {
			t.Fatalf("vlan tags %d altered link framing", tags)
		}
	}
}

func TestIPv4OptionsHandled(t *testing.T) {
	src := mustAddr(t, "10.60.0.1")
	dst := mustAddr(t, "10.60.0.2")
	newSrc := mustAddr(t, "198.18.60.1")
	newDst := mustAddr(t, "198.18.60.2")
	payload := []byte("options payload")

	// Build an IPv4 packet with a 4-byte option (IHL = 6).
	udpLen := 8 + len(payload)
	total := 24 + udpLen
	pkt := make([]byte, total)
	pkt[0] = 0x46 // version 4, IHL 6
	binary.BigEndian.PutUint16(pkt[2:4], uint16(total))
	binary.BigEndian.PutUint16(pkt[6:8], 0x4000)
	pkt[8] = 63
	pkt[9] = protocolUDP
	copy(pkt[12:16], src[:])
	copy(pkt[16:20], dst[:])
	pkt[20] = 0x01 // NOP
	pkt[21] = 0x01
	pkt[22] = 0x01
	pkt[23] = 0x00
	binary.BigEndian.PutUint16(pkt[10:12], ipHeaderChecksum(pkt[:24]))
	udp := pkt[24:]
	binary.BigEndian.PutUint16(udp[0:2], 1234)
	binary.BigEndian.PutUint16(udp[2:4], 5678)
	binary.BigEndian.PutUint16(udp[4:6], uint16(udpLen))
	copy(udp[8:], payload)
	binary.BigEndian.PutUint16(udp[6:8], udpChecksum(src, dst, udpLen, udp))

	out, err := TranslateRawIPv4(pkt, 0, Table{src: newSrc, dst: newDst})
	if err != nil {
		t.Fatalf("TranslateRawIPv4: %v", err)
	}
	h, err := parseHeaders(out, LinkRawIPv4)
	if err != nil {
		t.Fatalf("re-parse: %v", err)
	}
	if h.udpStart != 24 {
		t.Fatalf("UDP start = %d, want 24", h.udpStart)
	}
	if !ipChecksumValid(out[:24]) {
		t.Fatal("IPv4 checksum invalid with options")
	}
	if !udpChecksumValid(newSrc, newDst, udpLen, out[24:]) {
		t.Fatal("UDP checksum invalid with options")
	}
	if !bytes.Equal(out[20:24], pkt[20:24]) {
		t.Fatal("IPv4 options were modified")
	}
}

// --- interleaving across a shared table ------------------------------------

func TestSharedTableInterleavedFlows(t *testing.T) {
	a := mustAddr(t, "192.168.1.10")
	b := mustAddr(t, "192.168.1.20")
	server := mustAddr(t, "10.0.0.5")
	serverNew := mustAddr(t, "203.0.113.50")
	aNew := mustAddr(t, "203.0.113.10")
	bNew := mustAddr(t, "203.0.113.20")

	table := Table{a: aNew, b: bNew, server: serverNew}

	// Three flows sharing endpoints in both directions, plus one truncated
	// capture in the middle of the sequence.
	flows := []struct {
		name    string
		src     Addr
		dst     Addr
		payload []byte
		cut     int // extra bytes beyond the header, -1 = full
	}{
		{"a->server full", a, server, []byte("hello server"), -1},
		{"b->server truncated", b, server, []byte("partial upload payload"), 5},
		{"server->a", server, a, []byte("response"), -1},
		{"server->b truncated", server, b, []byte("response partial"), 0},
	}

	for _, f := range flows {
		ip := buildIPv4UDP(t, udpFixture{src: f.src, dst: f.dst, payload: f.payload, udpChecksum: true})
		frame := buildEthernet(t, ip, 0)
		captureLen := 0
		if f.cut >= 0 {
			captureLen = headerEndFor(t, frame, LinkEthernet) + f.cut
		}
		out, err := TranslateEthernet(frame, captureLen, table)
		if err != nil {
			t.Fatalf("%s: %v", f.name, err)
		}
		if f.cut >= 0 && len(out) != captureLen {
			t.Fatalf("%s: output length %d, want %d", f.name, len(out), captureLen)
		}
		h, err := parseHeaders(out, LinkEthernet)
		if err != nil {
			t.Fatalf("%s: re-parse: %v", f.name, err)
		}
		if h.src != table[f.src] {
			t.Fatalf("%s: source = %s, want %s", f.name, h.src, table[f.src])
		}
		if h.dst != table[f.dst] {
			t.Fatalf("%s: destination = %s, want %s", f.name, h.dst, table[f.dst])
		}
		if !ipChecksumValid(out[h.ipStart:h.ipHeaderEnd]) {
			t.Fatalf("%s: invalid IPv4 checksum", f.name)
		}
		// No mapping target may collide with a different original endpoint,
		// and no original address may survive anywhere in the header.
		if bytes.Contains(out[h.ipStart:h.ipHeaderEnd], f.src[:]) || bytes.Contains(out[h.ipStart:h.ipHeaderEnd], f.dst[:]) {
			t.Fatalf("%s: original endpoint survived", f.name)
		}
		if f.cut < 0 && !udpChecksumValid(h.src, h.dst, h.udpLen, out[h.udpStart:h.udpEnd]) {
			t.Fatalf("%s: invalid UDP checksum", f.name)
		}
	}
}

func TestTranslateDoesNotMutateInput(t *testing.T) {
	src := mustAddr(t, "10.70.0.1")
	dst := mustAddr(t, "10.70.0.2")
	ip := buildIPv4UDP(t, udpFixture{src: src, dst: dst, payload: []byte("immutable"), udpChecksum: true})
	frame := buildEthernet(t, ip, 0)
	snapshot := append([]byte(nil), frame...)

	out, err := TranslateEthernet(frame, 0, Table{src: mustAddr(t, "198.18.70.1"), dst: mustAddr(t, "198.18.70.2")})
	if err != nil {
		t.Fatalf("TranslateEthernet: %v", err)
	}
	if !bytes.Equal(frame, snapshot) {
		t.Fatal("input sample was mutated")
	}
	out[0] ^= 0xff
	if !bytes.Equal(frame, snapshot) {
		t.Fatal("output aliases input")
	}
}

func TestIncrementalUpdateMatchesFullRecomputation(t *testing.T) {
	// Randomized-but-deterministic sweep of endpoint pairs and payload
	// lengths, including odd payloads and zeros, comparing the truncated
	// rewrite against the full rewrite.
	pairs := [][2]Addr{
		{mustAddr(t, "10.0.0.1"), mustAddr(t, "10.0.0.2")},
		{mustAddr(t, "172.16.255.254"), mustAddr(t, "192.168.0.1")},
		{mustAddr(t, "0.0.0.0"), mustAddr(t, "255.255.255.255")},
	}
	replacements := [][2]Addr{
		{mustAddr(t, "203.0.113.1"), mustAddr(t, "203.0.113.2")},
		{mustAddr(t, "0.0.0.0"), mustAddr(t, "255.255.255.255")},
	}
	lengths := []int{0, 1, 2, 3, 7, 8, 9, 32, 100}

	for _, orig := range pairs {
		for _, repl := range replacements {
			for _, n := range lengths {
				payload := make([]byte, n)
				for i := range payload {
					payload[i] = byte(i*7 + n)
				}
				table := Table{orig[0]: repl[0], orig[1]: repl[1]}
				ip := buildIPv4UDP(t, udpFixture{src: orig[0], dst: orig[1], payload: payload, udpChecksum: true})
				sample := buildEthernet(t, ip, 0)
				full, err := TranslateEthernet(sample, 0, table)
				if err != nil {
					t.Fatalf("%s->%s len %d: %v", orig[0], orig[1], n, err)
				}
				headerEnd := headerEndFor(t, sample, LinkEthernet)
				for _, captureLen := range []int{headerEnd, headerEnd + n/2, len(sample)} {
					out, err := TranslateEthernet(sample, captureLen, table)
					if err != nil {
						t.Fatalf("%s->%s len %d cut %d: %v", orig[0], orig[1], n, captureLen, err)
					}
					if !bytes.Equal(out, full[:captureLen]) {
						t.Fatalf("%s->%s len %d cut %d: incremental rewrite diverged",
							orig[0], orig[1], n, captureLen)
					}
				}
				if !udpChecksumValid(repl[0], repl[1], len(full)-14-20, full[34:]) {
					t.Fatalf("%s->%s len %d: full rewrite checksum invalid", orig[0], orig[1], n)
				}
			}
		}
	}
}

func TestValidateTableRejectsNonInjectiveTables(t *testing.T) {
	a, b := mustAddr(t, "10.0.0.1"), mustAddr(t, "10.0.0.2")
	same := mustAddr(t, "203.0.113.1")

	// Two originals collapsing onto one replacement is not one-to-one.
	if err := ValidateTable(Table{a: same, b: same}); !errors.Is(err, ErrInvalidTable) {
		t.Fatalf("error = %v, want ErrInvalidTable", err)
	}
	if err := ValidateTable(Table{}); !errors.Is(err, ErrInvalidTable) {
		t.Fatalf("empty table error = %v, want ErrInvalidTable", err)
	}
	if err := ValidateTable(Table{a: same, b: mustAddr(t, "203.0.113.2")}); err != nil {
		t.Fatalf("injective table rejected: %v", err)
	}

	// The same failure must surface through Translate, before any bytes are
	// produced.
	sample := buildEthernet(t, buildIPv4UDP(t, udpFixture{
		src: a, dst: b, payload: []byte("dup"), udpChecksum: true,
	}), 0)
	out, err := TranslateEthernet(sample, 0, Table{a: same, b: same})
	if !errors.Is(err, ErrInvalidTable) {
		t.Fatalf("Translate error = %v, want ErrInvalidTable", err)
	}
	if out != nil {
		t.Fatal("invalid table returned a fragment")
	}
}
