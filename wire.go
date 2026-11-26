package causalcodec

import (
	"bytes"
	"encoding/binary"
	"hash/crc32"
	"io"
	"math"
)

const (
	wireMagic   = "CCX1"
	wireVersion = byte(1)
	// DefaultMaxBytes is the fixed budget used by a zero-valued Codec.
	DefaultMaxBytes = 4096
)

const (
	flagSegmentAnchor = byte(1 << iota)
	flagSegmentGap
	flagSegmentExtended
)

type wireWriter struct{ bytes.Buffer }

func (w *wireWriter) u8(v byte) { w.WriteByte(v) }

func (w *wireWriter) u16(v int) error {
	if v < 0 || v > math.MaxUint16 {
		return malformed("u16 value %d out of range", v)
	}
	var b [2]byte
	binary.BigEndian.PutUint16(b[:], uint16(v))
	_, err := w.Write(b[:])
	return err
}

func (w *wireWriter) u32(v uint32) {
	var b [4]byte
	binary.BigEndian.PutUint32(b[:], v)
	_, _ = w.Write(b[:])
}

func (w *wireWriter) i32(v Priority) { w.u32(uint32(int32(v))) }

func (w *wireWriter) i64(v int64) {
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], uint64(v))
	_, _ = w.Write(b[:])
}

func (w *wireWriter) str(s string) error {
	if len(s) > math.MaxUint32 {
		return malformed("string too long: %d", len(s))
	}
	w.u32(uint32(len(s)))
	_, err := w.WriteString(s)
	return err
}

func (w *wireWriter) ref(r AnchorRef) error {
	if err := w.str(r.Name); err != nil {
		return err
	}
	if err := w.str(r.Version); err != nil {
		return err
	}
	return w.str(r.Digest)
}

func (w *wireWriter) source(s Source) error {
	for _, v := range []string{s.Bus, s.Topic, s.Consumer} {
		if err := w.str(v); err != nil {
			return err
		}
	}
	w.i32(Priority(s.Partition))
	w.i64(s.Offset)
	w.u32(s.Attempt)
	return nil
}

func (w *wireWriter) cause(c Cause) error {
	if err := w.str(c.ID); err != nil {
		return err
	}
	if err := w.str(c.Stage); err != nil {
		return err
	}
	if err := w.str(c.Kind); err != nil {
		return err
	}
	if err := w.str(c.Message); err != nil {
		return err
	}
	if err := w.source(c.Source); err != nil {
		return err
	}
	w.i32(c.Priority)
	if c.Retryable {
		w.u8(1)
	} else {
		w.u8(0)
	}
	return nil
}

func encodePacket(r *Report) ([]byte, error) {
	var body wireWriter
	if err := body.str(r.Delivery.ID); err != nil {
		return nil, err
	}
	if err := body.str(r.Delivery.EventID); err != nil {
		return nil, err
	}
	if err := body.source(r.Delivery.Source); err != nil {
		return nil, err
	}

	if len(r.Causes) > math.MaxUint32 {
		return nil, malformed("too many causes")
	}
	body.u32(uint32(len(r.Causes)))
	index := make(map[string]int, len(r.Causes))
	for i, c := range r.Causes {
		index[c.ID] = i
		if err := body.cause(c); err != nil {
			return nil, err
		}
	}

	if len(r.Paths) > math.MaxUint32 {
		return nil, malformed("too many paths")
	}
	body.u32(uint32(len(r.Paths)))
	for _, p := range r.Paths {
		block, err := encodePath(p, index)
		if err != nil {
			return nil, err
		}
		body.u32(uint32(len(block)))
		_, _ = body.Write(block)
	}

	var out wireWriter
	out.WriteString(wireMagic)
	out.u8(wireVersion)
	out.u32(uint32(body.Len()))
	out.u32(crc32.ChecksumIEEE(body.Bytes()))
	_, _ = out.Write(body.Bytes())
	return out.Bytes(), nil
}

func encodePath(p Path, index map[string]int) ([]byte, error) {
	var b wireWriter
	if err := b.str(p.ID); err != nil {
		return nil, err
	}
	if err := b.str(p.Label); err != nil {
		return nil, err
	}
	if err := b.u16(len(p.Segments)); err != nil {
		return nil, err
	}
	for _, s := range p.Segments {
		if err := b.str(s.Stage); err != nil {
			return nil, err
		}
		var flags byte
		if s.Anchor != nil {
			flags |= flagSegmentAnchor
		}
		if s.Gap != nil {
			flags |= flagSegmentGap
			if s.Gap.Extended {
				flags |= flagSegmentExtended
			}
		}
		b.u8(flags)

		if err := b.u16(len(s.CauseIDs)); err != nil {
			return nil, err
		}
		for _, id := range s.CauseIDs {
			idx, ok := index[id]
			if !ok {
				return nil, malformed("cause %q missing from wire table", id)
			}
			if err := b.u16(idx); err != nil {
				return nil, err
			}
		}

		if err := b.u16(len(s.Omitted)); err != nil {
			return nil, err
		}
		for _, om := range s.Omitted {
			if err := b.str(om.ID); err != nil {
				return nil, err
			}
			b.i32(om.Priority)
			b.u8(omissionReasonCode(om.Reason))
		}

		if s.Anchor != nil {
			if err := b.ref(*s.Anchor); err != nil {
				return nil, err
			}
		}
		if s.Gap != nil {
			b.u8(gapKindCode(s.Gap.Kind))
			b.i32(s.Gap.Threshold)
			if s.Gap.Extended {
				if err := b.str(s.Gap.ExtensionReason); err != nil {
					return nil, err
				}
			}
		}
	}
	return b.Bytes(), nil
}

type wireReader struct {
	*bytes.Reader
	err error
}

func newWireReader(b []byte) *wireReader {
	return &wireReader{Reader: bytes.NewReader(b)}
}

func (r *wireReader) readByteValue() byte {
	if r.err != nil {
		return 0
	}
	v, err := r.ReadByte()
	if err != nil {
		r.err = malformed("read byte: %v", err)
	}
	return v
}

func (r *wireReader) u16() int {
	if r.err != nil {
		return 0
	}
	var b [2]byte
	if _, err := readFull(r.Reader, b[:]); err != nil {
		r.err = malformed("read u16: %v", err)
		return 0
	}
	return int(binary.BigEndian.Uint16(b[:]))
}

func (r *wireReader) u32() uint32 {
	if r.err != nil {
		return 0
	}
	var b [4]byte
	if _, err := readFull(r.Reader, b[:]); err != nil {
		r.err = malformed("read u32: %v", err)
		return 0
	}
	return binary.BigEndian.Uint32(b[:])
}

func (r *wireReader) i32() Priority { return Priority(int32(r.u32())) }

func (r *wireReader) i64() int64 {
	if r.err != nil {
		return 0
	}
	var b [8]byte
	if _, err := readFull(r.Reader, b[:]); err != nil {
		r.err = malformed("read i64: %v", err)
		return 0
	}
	return int64(binary.BigEndian.Uint64(b[:]))
}

func (r *wireReader) str() string {
	if r.err != nil {
		return ""
	}
	n := r.u32()
	if r.err != nil {
		return ""
	}
	if uint64(n) > uint64(r.Len()) {
		r.err = malformed("string length %d exceeds remaining %d", n, r.Len())
		return ""
	}
	b := make([]byte, n)
	if _, err := readFull(r.Reader, b); err != nil {
		r.err = malformed("read string: %v", err)
		return ""
	}
	return string(b)
}

func (r *wireReader) ref() AnchorRef {
	name := r.str()
	version := r.str()
	digest := r.str()
	return AnchorRef{Name: name, Version: version, Digest: digest}
}

func (r *wireReader) source() Source {
	s := Source{Bus: r.str(), Topic: r.str(), Consumer: r.str()}
	s.Partition = int32(r.i32())
	s.Offset = r.i64()
	s.Attempt = r.u32()
	return s
}

func (r *wireReader) cause() Cause {
	c := Cause{ID: r.str(), Stage: r.str(), Kind: r.str(), Message: r.str(), Source: r.source()}
	c.Priority = r.i32()
	c.Retryable = r.readByteValue() == 1
	return c
}

func readFull(r *bytes.Reader, b []byte) (int, error) {
	return io.ReadFull(r, b)
}

const (
	omitReasonThresholdCode = byte(1)
	omitReasonBudgetCode    = byte(2)
)

func omissionReasonCode(reason string) byte {
	switch reason {
	case OmitReasonPriorityThreshold:
		return omitReasonThresholdCode
	case OmitReasonByteBudget:
		return omitReasonBudgetCode
	default:
		return 0
	}
}

func omissionReasonFromCode(code byte) string {
	switch code {
	case omitReasonThresholdCode:
		return OmitReasonPriorityThreshold
	case omitReasonBudgetCode:
		return OmitReasonByteBudget
	default:
		return ""
	}
}

func gapKindCode(kind GapKind) byte {
	switch kind {
	case GapPriority:
		return 1
	case GapMissingAnchor:
		return 2
	case GapMalformedBranch:
		return 3
	case GapEmpty:
		return 4
	default:
		return 0
	}
}

func gapKindFromCode(code byte) GapKind {
	switch code {
	case 1:
		return GapPriority
	case 2:
		return GapMissingAnchor
	case 3:
		return GapMalformedBranch
	case 4:
		return GapEmpty
	default:
		return GapKind("unknown")
	}
}

func defaultGapReason(kind GapKind) string {
	switch kind {
	case GapPriority:
		return "lower-priority causes disconnected by retention policy"
	case GapMissingAnchor:
		return "exact anchor unavailable"
	case GapMalformedBranch:
		return "length-framed branch could not be decoded"
	case GapEmpty:
		return "segment has no recoverable cause"
	default:
		return "causal chain gap"
	}
}
