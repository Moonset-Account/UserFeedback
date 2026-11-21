package relationindex

import (
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"math"
)

const cursorVersion = uint64(1)

// Cursor wire format (before base64):
//
//	uvarint version
//	uvarint snapshot ID
//	uvarint region length | region bytes
//	byte hasScore (0 or 1) | [8-byte big-endian float bits]
//	uvarint primary key length | primary key bytes
//
// Length prefixes keep the format unambiguous even when regions or keys
// contain arbitrary bytes.
func encodeCursor(snapshotID uint64, key indexKey) string {
	buf := make([]byte, 0, 32+len(key.region)+len(key.id))
	buf = binary.AppendUvarint(buf, cursorVersion)
	buf = binary.AppendUvarint(buf, snapshotID)
	buf = appendLengthPrefixed(buf, key.region)
	if key.hasScore {
		buf = append(buf, 1)
		buf = binary.BigEndian.AppendUint64(buf, math.Float64bits(key.score))
	} else {
		buf = append(buf, 0)
	}
	buf = appendLengthPrefixed(buf, key.id)
	return base64.RawURLEncoding.EncodeToString(buf)
}

func appendLengthPrefixed(buf []byte, s string) []byte {
	buf = binary.AppendUvarint(buf, uint64(len(s)))
	return append(buf, s...)
}

func decodeCursor(cursor string) (indexKey, uint64, error) {
	raw, err := base64.RawURLEncoding.DecodeString(cursor)
	if err != nil {
		return indexKey{}, 0, fmt.Errorf("%w: %v", ErrInvalidCursor, err)
	}
	r := cursorReader{buf: raw}

	version, ok := r.uvarint()
	if !ok || version != cursorVersion {
		return indexKey{}, 0, fmt.Errorf("%w: unsupported version", ErrInvalidCursor)
	}
	snapshotID, ok := r.uvarint()
	if !ok {
		return indexKey{}, 0, fmt.Errorf("%w: missing snapshot id", ErrInvalidCursor)
	}
	region, ok := r.lengthPrefixed()
	if !ok {
		return indexKey{}, 0, fmt.Errorf("%w: bad region", ErrInvalidCursor)
	}
	flag, ok := r.byte()
	if !ok {
		return indexKey{}, 0, fmt.Errorf("%w: missing score flag", ErrInvalidCursor)
	}

	key := indexKey{region: string(region)}
	switch flag {
	case 0:
		// No score.
	case 1:
		bits, ok := r.uint64()
		if !ok {
			return indexKey{}, 0, fmt.Errorf("%w: truncated score", ErrInvalidCursor)
		}
		key.score = math.Float64frombits(bits)
		key.hasScore = true
	default:
		return indexKey{}, 0, fmt.Errorf("%w: bad score flag", ErrInvalidCursor)
	}

	id, ok := r.lengthPrefixed()
	if !ok {
		return indexKey{}, 0, fmt.Errorf("%w: bad primary key", ErrInvalidCursor)
	}
	key.id = string(id)
	if key.id == "" {
		return indexKey{}, 0, fmt.Errorf("%w: empty primary key", ErrInvalidCursor)
	}
	if !r.done() {
		return indexKey{}, 0, fmt.Errorf("%w: trailing bytes", ErrInvalidCursor)
	}
	return key, snapshotID, nil
}

type cursorReader struct {
	buf []byte
	off int
}

func (r *cursorReader) uvarint() (uint64, bool) {
	v, n := binary.Uvarint(r.buf[r.off:])
	if n <= 0 {
		return 0, false
	}
	r.off += n
	return v, true
}

func (r *cursorReader) uint64() (uint64, bool) {
	if len(r.buf)-r.off < 8 {
		return 0, false
	}
	v := binary.BigEndian.Uint64(r.buf[r.off:])
	r.off += 8
	return v, true
}

func (r *cursorReader) byte() (byte, bool) {
	if r.off >= len(r.buf) {
		return 0, false
	}
	b := r.buf[r.off]
	r.off++
	return b, true
}

func (r *cursorReader) lengthPrefixed() ([]byte, bool) {
	n, ok := r.uvarint()
	if !ok {
		return nil, false
	}
	if n > uint64(len(r.buf)-r.off) {
		return nil, false
	}
	out := r.buf[r.off : r.off+int(n)]
	r.off += int(n)
	return out, true
}

func (r *cursorReader) done() bool {
	return r.off == len(r.buf)
}
