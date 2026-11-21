package ipfrag

import (
	"bytes"
	"errors"
	"reflect"
	"testing"
)

func baseHeader() *Header {
	return &Header{
		ID:       0x1234,
		TTL:      64,
		Protocol: 17,
		Src:      [4]byte{192, 0, 2, 1},
		Dst:      [4]byte{198, 51, 100, 7},
	}
}

func payloadOf(n int) []byte {
	p := make([]byte, n)
	for i := range p {
		p[i] = byte(i)
	}
	return p
}

// withTotal sets TotalLength to header+payload so the input is valid.
func withTotal(t *testing.T, h *Header, payload []byte) {
	t.Helper()
	hl, err := HeaderLen(h.Options)
	if err != nil {
		t.Fatalf("HeaderLen: %v", err)
	}
	h.TotalLength = uint16(hl + len(payload))
}

// verifyFragment checks checksum validity and internal consistency of one
// emitted datagram.
func verifyFragment(t *testing.T, d Datagram) {
	t.Helper()
	raw, err := d.Header.Marshal()
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	if !ValidChecksum(raw) {
		t.Fatalf("header checksum does not verify: %x", raw)
	}
	hl, err := HeaderLen(d.Header.Options)
	if err != nil {
		t.Fatalf("HeaderLen: %v", err)
	}
	if got, want := int(d.Header.TotalLength), hl+len(d.Payload); got != want {
		t.Fatalf("TotalLength=%d, want %d", got, want)
	}
	if hl%4 != 0 {
		t.Fatalf("header length %d not aligned to 4 bytes", hl)
	}
}

// TestFitsReturnedAsIs: datagram within MTU comes back unchanged.
func TestFitsReturnedAsIs(t *testing.T) {
	h := baseHeader()
	p := payloadOf(40)
	withTotal(t, h, p)
	h.Checksum = 0xBEEF // untouched on the pass-through path

	out, err := Fragment(h, p, 576)
	if err != nil {
		t.Fatalf("Fragment: %v", err)
	}
	if len(out) != 1 {
		t.Fatalf("got %d datagrams, want 1", len(out))
	}
	if !reflect.DeepEqual(out[0].Header, *h) {
		t.Fatalf("header changed: got %+v want %+v", out[0].Header, *h)
	}
	if !bytes.Equal(out[0].Payload, p) {
		t.Fatal("payload changed")
	}
}

// TestExactFitBoundary: total length exactly equal to MTU still passes
// through; one byte more forces fragmentation.
func TestExactFitBoundary(t *testing.T) {
	h := baseHeader()
	p := payloadOf(80)
	withTotal(t, h, p)

	out, err := Fragment(h, p, 100) // 20+80 == mtu
	if err != nil {
		t.Fatalf("Fragment: %v", err)
	}
	if len(out) != 1 {
		t.Fatalf("exact fit: got %d datagrams, want 1", len(out))
	}

	out, err = Fragment(h, p, 99)
	if err != nil {
		t.Fatalf("Fragment: %v", err)
	}
	if len(out) != 2 {
		t.Fatalf("one byte over: got %d datagrams, want 2", len(out))
	}
	for _, d := range out {
		verifyFragment(t, d)
	}
	if got := len(out[0].Payload); got != 72 { // (99-20) rounded down to 8
		t.Fatalf("first payload %d bytes, want 72", got)
	}
	if got := len(out[1].Payload); got != 8 {
		t.Fatalf("second payload %d bytes, want 8", got)
	}
}

// TestDFSetReturnsMTU: oversized datagram with DF reports the egress MTU.
func TestDFSetReturnsMTU(t *testing.T) {
	h := baseHeader()
	h.Flags = FlagDF
	p := payloadOf(200)
	withTotal(t, h, p)

	_, err := Fragment(h, p, 100)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	var nf *NeedsFragmentationError
	if !errors.As(err, &nf) {
		t.Fatalf("error type %T, want *NeedsFragmentationError", err)
	}
	if nf.MTU != 100 {
		t.Fatalf("reported MTU %d, want 100", nf.MTU)
	}
}

// TestBasicFragmentation: offsets, MF flags, 8-byte multiples, last
// fragment exempt from the multiple-of-8 rule, payload reassembly.
func TestBasicFragmentation(t *testing.T) {
	h := baseHeader()
	p := payloadOf(196)
	withTotal(t, h, p)

	out, err := Fragment(h, p, 100) // capacity 80 per fragment
	if err != nil {
		t.Fatalf("Fragment: %v", err)
	}
	if len(out) != 3 {
		t.Fatalf("got %d datagrams, want 3", len(out))
	}
	wantLens := []int{80, 80, 36}
	wantOffs := []uint16{0, 10, 20}
	wantMF := []bool{true, true, false}
	var reassembled []byte
	for i, d := range out {
		verifyFragment(t, d)
		if len(d.Payload) != wantLens[i] {
			t.Fatalf("frag %d payload %d bytes, want %d", i, len(d.Payload), wantLens[i])
		}
		if d.Header.FragmentOffset != wantOffs[i] {
			t.Fatalf("frag %d offset %d, want %d", i, d.Header.FragmentOffset, wantOffs[i])
		}
		if got := d.Header.Flags&FlagMF != 0; got != wantMF[i] {
			t.Fatalf("frag %d MF=%v, want %v", i, got, wantMF[i])
		}
		if i < len(out)-1 && len(d.Payload)%8 != 0 {
			t.Fatalf("frag %d payload %d not a multiple of 8", i, len(d.Payload))
		}
		if d.Header.ID != h.ID {
			t.Fatalf("frag %d ID %#x, want %#x", i, d.Header.ID, h.ID)
		}
		reassembled = append(reassembled, d.Payload...)
	}
	if !bytes.Equal(reassembled, p) {
		t.Fatal("reassembled payload differs from original")
	}
}

// TestOptionCopyRules: the offset-zero fragment keeps every option, later
// fragments keep only copied ones, and headers re-align to 4 bytes.
func TestOptionCopyRules(t *testing.T) {
	h := baseHeader()
	h.Options = []Option{
		{Type: 7, Data: []byte{4, 192, 0, 2, 1}}, // record route, not copied, wire 6
		{Type: 0x83, Data: []byte{0xAA}},         // copied, wire 3
		{Type: 1},                                // NOP, wire 1
	}
	p := payloadOf(120)
	withTotal(t, h, p)

	out, err := Fragment(h, p, 60)
	if err != nil {
		t.Fatalf("Fragment: %v", err)
	}
	if len(out) < 2 {
		t.Fatalf("got %d datagrams, want >= 2", len(out))
	}
	// First fragment: all options, 20+10 -> padded to 32 bytes.
	first := out[0]
	if len(first.Header.Options) != 3 {
		t.Fatalf("first fragment kept %d options, want 3", len(first.Header.Options))
	}
	if hl, _ := HeaderLen(first.Header.Options); hl != 32 {
		t.Fatalf("first fragment header %d bytes, want 32", hl)
	}
	// Later fragments: only the copied option, 20+3 -> padded to 24.
	for i, d := range out[1:] {
		if len(d.Header.Options) != 1 || d.Header.Options[0].Type != 0x83 {
			t.Fatalf("frag %d options %+v, want only type 0x83", i+1, d.Header.Options)
		}
		if hl, _ := HeaderLen(d.Header.Options); hl != 24 {
			t.Fatalf("frag %d header %d bytes, want 24", i+1, hl)
		}
	}
	// Capacity differs per fragment because header sizes differ:
	// first holds 60-32=28 -> 24 (multiple of 8), rest hold 60-24=36 -> 32.
	if got := len(first.Payload); got != 24 {
		t.Fatalf("first payload %d bytes, want 24", got)
	}
	if got := len(out[1].Payload); got != 32 {
		t.Fatalf("second payload %d bytes, want 32", got)
	}
	for _, d := range out {
		verifyFragment(t, d)
	}
	var reassembled []byte
	for _, d := range out {
		reassembled = append(reassembled, d.Payload...)
	}
	if !bytes.Equal(reassembled, p) {
		t.Fatal("reassembled payload differs from original")
	}
}

// TestIntermediateFragment: an incoming middle fragment (offset != 0, MF
// set) is split further. Child offsets continue from the input position,
// no child sits at offset zero so all keep only copied options, and the
// last child inherits MF=1.
func TestIntermediateFragment(t *testing.T) {
	h := baseHeader()
	h.Flags = FlagMF
	h.FragmentOffset = 20 // starts at byte 160 of the original datagram
	h.Options = []Option{
		{Type: 7, Data: []byte{4, 192, 0, 2, 1}}, // not copied
		{Type: 0x83, Data: []byte{0xAA}},         // copied
	}
	p := payloadOf(64)
	withTotal(t, h, p)

	out, err := Fragment(h, p, 44) // child header 24 -> capacity 20 -> 16
	if err != nil {
		t.Fatalf("Fragment: %v", err)
	}
	if len(out) != 4 {
		t.Fatalf("got %d datagrams, want 4", len(out))
	}
	for i, d := range out {
		verifyFragment(t, d)
		if want := uint16(20 + 2*i); d.Header.FragmentOffset != want {
			t.Fatalf("frag %d offset %d, want %d", i, d.Header.FragmentOffset, want)
		}
		if len(d.Header.Options) != 1 || d.Header.Options[0].Type != 0x83 {
			t.Fatalf("frag %d options %+v, want only copied option", i, d.Header.Options)
		}
		if len(d.Payload) != 16 {
			t.Fatalf("frag %d payload %d bytes, want 16", i, len(d.Payload))
		}
	}
	for i, d := range out[:3] {
		if d.Header.Flags&FlagMF == 0 {
			t.Fatalf("frag %d lost MF", i)
		}
	}
	if out[3].Header.Flags&FlagMF == 0 {
		t.Fatal("last child did not inherit input MF")
	}
}

// TestLastChildInheritsClearedMF: same shape, but the input is the final
// fragment of its datagram, so the last child must clear MF.
func TestLastChildInheritsClearedMF(t *testing.T) {
	h := baseHeader()
	h.FragmentOffset = 5 // byte 40 of the original datagram
	p := payloadOf(48)
	withTotal(t, h, p)

	out, err := Fragment(h, p, 40) // capacity 20 -> 16 per fragment
	if err != nil {
		t.Fatalf("Fragment: %v", err)
	}
	if len(out) != 3 {
		t.Fatalf("got %d datagrams, want 3", len(out))
	}
	if out[2].Header.Flags&FlagMF != 0 {
		t.Fatal("last child must clear MF when input MF is clear")
	}
	if out[0].Header.FragmentOffset != 5 || out[2].Header.FragmentOffset != 9 {
		t.Fatalf("offsets %d..%d, want 5..9",
			out[0].Header.FragmentOffset, out[2].Header.FragmentOffset)
	}
}

// TestMTUTooSmall: the MTU cannot hold one legal fragment, so the whole
// call fails with MTUTooSmallError.
func TestMTUTooSmall(t *testing.T) {
	cases := []struct {
		name    string
		options []Option
		mtu     int
	}{
		{"header larger than mtu", []Option{{Type: 0x83, Data: []byte{1, 2, 3, 4, 5, 6}}}, 24}, // hdr 28 > 24
		{"no room for payload", []Option{{Type: 0x83, Data: []byte{1, 2, 3, 4, 5, 6}}}, 28},    // cap 0
		{"less than one 8-byte block", nil, 24},                                                // cap 4 < 8
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h := baseHeader()
			h.Options = tc.options
			p := payloadOf(64)
			withTotal(t, h, p)
			_, err := Fragment(h, p, tc.mtu)
			var ms *MTUTooSmallError
			if !errors.As(err, &ms) {
				t.Fatalf("error %v (%T), want *MTUTooSmallError", err, err)
			}
			if ms.MTU != tc.mtu {
				t.Fatalf("reported MTU %d, want %d", ms.MTU, tc.mtu)
			}
		})
	}
}

// TestEmptyPayloadHeaderExceedsMTU: header alone over the MTU fails.
func TestEmptyPayloadHeaderExceedsMTU(t *testing.T) {
	h := baseHeader()
	h.Options = []Option{{Type: 0x83, Data: []byte{1, 2, 3, 4, 5, 6}}} // hdr 28
	withTotal(t, h, nil)
	_, err := Fragment(h, nil, 24)
	var ms *MTUTooSmallError
	if !errors.As(err, &ms) {
		t.Fatalf("error %v (%T), want *MTUTooSmallError", err, err)
	}
}

// TestInvalidInputRejected: inconsistent input is reported, not fragmented.
func TestInvalidInputRejected(t *testing.T) {
	h := baseHeader()
	p := payloadOf(40)
	withTotal(t, h, p)
	h.TotalLength++ // now inconsistent
	if _, err := Fragment(h, p, 576); err == nil {
		t.Fatal("expected error for inconsistent TotalLength")
	}

	h2 := baseHeader()
	h2.Flags = FlagMF
	p2 := payloadOf(12) // non-final fragment with payload not a multiple of 8
	withTotal(t, h2, p2)
	if _, err := Fragment(h2, p2, 576); err == nil {
		t.Fatal("expected error for misaligned non-final fragment")
	}
}

// TestChecksumFieldMatchesMarshal: the Checksum field filled by Fragment
// equals the checksum on the wire.
func TestChecksumFieldMatchesMarshal(t *testing.T) {
	h := baseHeader()
	p := payloadOf(100)
	withTotal(t, h, p)
	out, err := Fragment(h, p, 60)
	if err != nil {
		t.Fatalf("Fragment: %v", err)
	}
	for i, d := range out {
		raw, err := d.Header.Marshal()
		if err != nil {
			t.Fatalf("Marshal: %v", err)
		}
		if got := uint16(raw[10])<<8 | uint16(raw[11]); got != d.Header.Checksum {
			t.Fatalf("frag %d Checksum field %#x, wire %#x", i, d.Header.Checksum, got)
		}
		if !ValidChecksum(raw) {
			t.Fatalf("frag %d checksum does not verify", i)
		}
	}
}
