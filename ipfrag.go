// Package ipfrag models IPv4 fragmentation against an egress MTU.
//
// The input is the header and payload of one valid datagram or of an
// intermediate fragment, plus the egress MTU. If the datagram fits it is
// returned unchanged. If it does not fit and DF is set, the egress MTU is
// reported back to the caller via NeedsFragmentationError. Otherwise the
// payload is split into child fragments whose headers are re-aligned to
// 4 bytes with the options each of them keeps: the fragment at offset zero
// of the original datagram keeps every option, the others keep only the
// options flagged as copied. Payload of every fragment except the last one
// of the original datagram is a multiple of 8 bytes. Fragment offsets
// continue from the input fragment's position in the original datagram and
// the last child inherits the input's more-fragments flag. Every emitted
// header carries a verifiable checksum. If the MTU is too small to hold
// one legal fragment, the whole call fails.
package ipfrag

import (
	"encoding/binary"
	"errors"
	"fmt"
)

// Header flag bits (the 3-bit flags field of an IPv4 header).
const (
	FlagMF uint8 = 1 // more fragments
	FlagDF uint8 = 2 // don't fragment
)

const (
	minHeaderLen = 20
	maxHeaderLen = 60
	maxOffset    = 0x1FFF // 13-bit fragment offset field, in 8-byte units
)

// Option is one IPv4 option. Type is the full option-type octet, including
// the copied flag in its top bit. Data holds the option payload, excluding
// the type and length octets. End-of-list (type 0) and NOP (type 1) are
// single-octet options and must have empty Data.
type Option struct {
	Type uint8
	Data []byte
}

// Copied reports whether the option is flagged to be copied into every
// fragment (top bit of the option-type octet).
func (o Option) Copied() bool { return o.Type&0x80 != 0 }

// wireLen is the option's length on the wire in bytes.
func (o Option) wireLen() int {
	if o.Type == 0 || o.Type == 1 { // EOL, NOP: single octet
		return 1
	}
	return 2 + len(o.Data)
}

func (o Option) appendTo(b []byte) []byte {
	if o.Type == 0 || o.Type == 1 {
		return append(b, o.Type)
	}
	b = append(b, o.Type, byte(2+len(o.Data)))
	return append(b, o.Data...)
}

// Header is an IPv4 header model. The version is always 4; IHL is derived
// from Options; FragmentOffset is in 8-byte units as on the wire.
type Header struct {
	TOS            uint8
	TotalLength    uint16
	ID             uint16
	Flags          uint8 // only FlagDF and FlagMF may be set
	FragmentOffset uint16
	TTL            uint8
	Protocol       uint8
	Checksum       uint16
	Src            [4]byte
	Dst            [4]byte
	Options        []Option
}

// Datagram is one (possibly fragmented) IPv4 packet: a header and its
// payload.
type Datagram struct {
	Header  Header
	Payload []byte
}

// NeedsFragmentationError is returned when the datagram exceeds the egress
// MTU but DF is set. It carries the egress MTU so the caller can, for
// example, source a Packet-Too-Big style indication.
type NeedsFragmentationError struct {
	MTU int
}

func (e *NeedsFragmentationError) Error() string {
	return fmt.Sprintf("ipfrag: datagram exceeds egress MTU %d and DF is set", e.MTU)
}

// MTUTooSmallError is returned when the egress MTU cannot hold even one
// legal fragment (header plus, when more data remains, an 8-byte multiple
// of payload).
type MTUTooSmallError struct {
	MTU       int
	HeaderLen int
}

func (e *MTUTooSmallError) Error() string {
	return fmt.Sprintf("ipfrag: egress MTU %d too small for %d-byte fragment header", e.MTU, e.HeaderLen)
}

// optionsWire serializes opts and pads with end-of-list octets to a
// 4-byte boundary.
func optionsWire(opts []Option) ([]byte, error) {
	var b []byte
	for _, o := range opts {
		if o.Type != 0 && o.Type != 1 && len(o.Data)+2 > 255 {
			return nil, fmt.Errorf("ipfrag: option type %d too long", o.Type)
		}
		b = o.appendTo(b)
	}
	for len(b)%4 != 0 {
		b = append(b, 0) // pad with EOL
	}
	if minHeaderLen+len(b) > maxHeaderLen {
		return nil, fmt.Errorf("ipfrag: options need %d bytes, header would exceed %d bytes", len(b), maxHeaderLen)
	}
	return b, nil
}

// HeaderLen returns the on-wire header length implied by opts, aligned to
// 4 bytes.
func HeaderLen(opts []Option) (int, error) {
	b, err := optionsWire(opts)
	if err != nil {
		return 0, err
	}
	return minHeaderLen + len(b), nil
}

// checksum is the standard IPv4 header one's-complement checksum. The
// checksum field of b (octets 10-11) must be zero, or the result is the
// verification residue.
func checksum(b []byte) uint16 {
	var sum uint32
	for i := 0; i+1 < len(b); i += 2 {
		sum += uint32(binary.BigEndian.Uint16(b[i:]))
	}
	if len(b)%2 != 0 {
		sum += uint32(b[len(b)-1]) << 8
	}
	for sum>>16 != 0 {
		sum = (sum & 0xFFFF) + (sum >> 16)
	}
	return ^uint16(sum)
}

// Marshal serializes h, computing the header checksum from h's fields
// (h.Checksum is ignored). The returned slice is the header only, without
// payload.
func (h *Header) Marshal() ([]byte, error) {
	opts, err := optionsWire(h.Options)
	if err != nil {
		return nil, err
	}
	if h.Flags&^0x7 != 0 {
		return nil, fmt.Errorf("ipfrag: unknown flag bits set: %#x", h.Flags)
	}
	if h.FragmentOffset > maxOffset {
		return nil, fmt.Errorf("ipfrag: fragment offset %d out of range", h.FragmentOffset)
	}
	b := make([]byte, minHeaderLen+len(opts))
	b[0] = 4<<4 | byte((minHeaderLen+len(opts))/4)
	b[1] = h.TOS
	binary.BigEndian.PutUint16(b[2:], h.TotalLength)
	binary.BigEndian.PutUint16(b[4:], h.ID)
	binary.BigEndian.PutUint16(b[6:], uint16(h.Flags&0x7)<<13|h.FragmentOffset)
	b[8] = h.TTL
	b[9] = h.Protocol
	copy(b[12:16], h.Src[:])
	copy(b[16:20], h.Dst[:])
	copy(b[20:], opts)
	binary.BigEndian.PutUint16(b[10:], checksum(b))
	return b, nil
}

// ValidChecksum verifies the header checksum of a serialized header
// (as produced by Marshal, including its checksum field).
func ValidChecksum(b []byte) bool {
	if len(b) < minHeaderLen || len(b)%4 != 0 {
		return false
	}
	return checksum(b) == 0
}

// childOptions selects the options a fragment at original-datagram offset
// fragOff (8-byte units) keeps: everything at offset zero, copied-only
// elsewhere.
func childOptions(h *Header, fragOff uint16) []Option {
	if fragOff == 0 {
		return append([]Option(nil), h.Options...)
	}
	var out []Option
	for _, o := range h.Options {
		if o.Copied() {
			out = append(out, o)
		}
	}
	return out
}

// validate checks that h plus payload form one valid datagram or
// intermediate fragment.
func validate(h *Header, payload []byte) (hdrLen int, err error) {
	if h == nil {
		return 0, errors.New("ipfrag: nil header")
	}
	if h.Flags&^0x7 != 0 {
		return 0, fmt.Errorf("ipfrag: unknown flag bits set: %#x", h.Flags)
	}
	if h.FragmentOffset > maxOffset {
		return 0, fmt.Errorf("ipfrag: fragment offset %d out of range", h.FragmentOffset)
	}
	hdrLen, err = HeaderLen(h.Options)
	if err != nil {
		return 0, err
	}
	total := hdrLen + len(payload)
	if total > 0xFFFF {
		return 0, fmt.Errorf("ipfrag: datagram of %d bytes exceeds 65535", total)
	}
	if int(h.TotalLength) != total {
		return 0, fmt.Errorf("ipfrag: total length %d does not match header+payload %d", h.TotalLength, total)
	}
	if h.Flags&FlagMF != 0 && len(payload)%8 != 0 {
		return 0, errors.New("ipfrag: non-final fragment payload is not a multiple of 8 bytes")
	}
	return hdrLen, nil
}

// Fragment splits the datagram (h, payload) so every piece fits the egress
// MTU, following the rules documented for the package. On success it
// returns one or more datagrams in order; each header's Checksum field is
// filled in and matches Marshal output.
func Fragment(h *Header, payload []byte, mtu int) ([]Datagram, error) {
	hdrLen, err := validate(h, payload)
	if err != nil {
		return nil, err
	}
	total := hdrLen + len(payload)
	if total <= mtu {
		// Fits: return as-is.
		return []Datagram{{Header: *h, Payload: payload}}, nil
	}
	if h.Flags&FlagDF != 0 {
		return nil, &NeedsFragmentationError{MTU: mtu}
	}
	if len(payload) == 0 {
		// Header alone exceeds the MTU: no legal fragment exists.
		return nil, &MTUTooSmallError{MTU: mtu, HeaderLen: hdrLen}
	}

	var out []Datagram
	for off := 0; off < len(payload); {
		fragOff := h.FragmentOffset + uint16(off/8)
		opts := childOptions(h, fragOff)
		childHdrLen, err := HeaderLen(opts)
		if err != nil {
			return nil, err
		}
		cap := mtu - childHdrLen
		if cap < 0 {
			return nil, &MTUTooSmallError{MTU: mtu, HeaderLen: childHdrLen}
		}
		remaining := len(payload) - off
		var n int
		last := false
		switch {
		case remaining <= cap:
			n, last = remaining, true
		default:
			n = cap &^ 7 // largest multiple of 8 that fits
			if n == 0 {
				return nil, &MTUTooSmallError{MTU: mtu, HeaderLen: childHdrLen}
			}
		}
		flags := h.Flags & FlagDF
		if last {
			flags |= h.Flags & FlagMF // last child inherits the input's MF
		} else {
			flags |= FlagMF
		}
		child := Header{
			TOS:            h.TOS,
			TotalLength:    uint16(childHdrLen + n),
			ID:             h.ID,
			Flags:          flags,
			FragmentOffset: fragOff,
			TTL:            h.TTL,
			Protocol:       h.Protocol,
			Src:            h.Src,
			Dst:            h.Dst,
			Options:        opts,
		}
		raw, err := child.Marshal()
		if err != nil {
			return nil, err
		}
		child.Checksum = binary.BigEndian.Uint16(raw[10:12])
		out = append(out, Datagram{Header: child, Payload: payload[off : off+n]})
		off += n
	}
	return out, nil
}
