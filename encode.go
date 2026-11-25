package pppframe

// HDLC-like framing constants used by PPP asynchronous framing.
const (
	Flag      byte = 0x7e
	Escape    byte = 0x7d
	EscapeXOR byte = 0x20
)

// AppendEmptyDelimiter appends one flag/octet delimiter and returns dst.
// Repeated calls create consecutive empty delimiters, which a conforming
// receiver counts but does not deliver as zero-length payload frames.
func AppendEmptyDelimiter(dst []byte) []byte {
	return append(dst, Flag)
}

func needsEscape(b byte, m ControlMap) bool {
	return b == Flag || b == Escape || m.Has(b)
}

func appendEscaped(dst []byte, b byte, m ControlMap) []byte {
	if needsEscape(b, m) {
		return append(dst, Escape, b^EscapeXOR)
	}
	return append(dst, b)
}

// AppendFrame appends a self-contained frame to dst:
//
//	Flag | byte-stuffed(payload || FCS-low || FCS-high) | Flag
//
// The payload is arbitrary bytes. The FCS covers payload only; this adapter
// does not add or interpret PPP Address/Control/Protocol fields.
func AppendFrame(dst []byte, payload []byte, sendMap ControlMap) []byte {
	dst = append(dst, Flag)

	fcs := FCS16(payload)
	var fcsBytes [2]byte
	fcsBytes[0] = byte(fcs)
	fcsBytes[1] = byte(fcs >> 8)

	for _, b := range payload {
		dst = appendEscaped(dst, b, sendMap)
	}
	for _, b := range fcsBytes {
		dst = appendEscaped(dst, b, sendMap)
	}

	return append(dst, Flag)
}

// EncodeFrame returns a complete framed serial byte sequence.
func EncodeFrame(payload []byte, sendMap ControlMap) []byte {
	return AppendFrame(make([]byte, 0, len(payload)*2+8), payload, sendMap)
}

// Encode is a concise alias for EncodeFrame.
func Encode(payload []byte, sendMap ControlMap) []byte {
	return EncodeFrame(payload, sendMap)
}
