package pppframe

import "fmt"

// ControlMap is the 32-bit PPP Async-Control-Character-Map.
// Bit n is set when byte n (0x00..0x1f) must be control-escaped.
type ControlMap uint32

const (
	// ControlMapNone maps no standard ASCII control byte. Flag and escape
	// bytes are still escaped because they have framing meaning.
	ControlMapNone ControlMap = 0

	// ControlMapAll maps all 32 ASCII control bytes.
	ControlMapAll ControlMap = 0xffffffff

	// DefaultControlMap is the conservative PPP-like map for all C0
	// control characters.
	DefaultControlMap = ControlMapAll
)

// MapFor returns a control map containing the supplied bytes. Bytes outside
// 0x00..0x1f have no ACCM bit and are ignored.
func MapFor(bytes ...byte) ControlMap {
	var m ControlMap
	for _, b := range bytes {
		m.Set(b)
	}
	return m
}

// Has reports whether b is selected by the map.
func (m ControlMap) Has(b byte) bool {
	return b < 0x20 && m&(ControlMap(1)<<b) != 0
}

// Set sets the bit for b when b is an ASCII control byte.
func (m *ControlMap) Set(b byte) {
	if b < 0x20 {
		*m |= ControlMap(1) << b
	}
}

// Clear clears the bit for b.
func (m *ControlMap) Clear(b byte) {
	if b < 0x20 {
		*m &^= ControlMap(1) << b
	}
}

func (m ControlMap) String() string {
	return fmt.Sprintf("ACCM(0x%08x)", uint32(m))
}

// Direction selects send and/or receive map state.
type Direction uint8

const (
	// DirectionReceive selects the map used by Feed/FeedSerial.
	DirectionReceive Direction = 1 << iota
	// DirectionSend selects the map used by Send/WriteFrame.
	DirectionSend
	// DirectionBoth selects both directions.
	DirectionBoth = DirectionReceive | DirectionSend
)

// Valid reports whether d contains at least one known direction.
func (d Direction) Valid() bool {
	return d == DirectionReceive || d == DirectionSend || d == DirectionBoth
}

// Maps contains the independently confirmed directional control maps.
type Maps struct {
	Send    ControlMap
	Receive ControlMap
}
