// Package recordlayer implements a record layer coordinator for an encrypted
// channel. The coordinator owns key generations, per-direction record sequence
// numbers and key-rotation control flow; the actual cryptographic work is
// delegated to a replaceable Cipher callback.
package recordlayer

import (
	"encoding/binary"
	"errors"
)

// RecordType identifies the content carried by a record.
type RecordType uint8

const (
	// TypeApplication carries application data.
	TypeApplication RecordType = 0x01
	// TypeRotateRequest asks the peer to advance to a new key generation and
	// requests a confirmation response before the peer's next application record.
	TypeRotateRequest RecordType = 0x02
	// TypeRotateResponse confirms a received TypeRotateRequest. It never asks
	// for another response, so responses cannot trigger responses.
	TypeRotateResponse RecordType = 0x03
)

// HeaderSize is the fixed, plaintext record header size in bytes:
// 1 byte type, 8 bytes generation, 8 bytes sequence, 4 bytes payload length.
const HeaderSize = 21

// Header is the plaintext framing placed in front of every sealed payload.
type Header struct {
	Type       RecordType
	Generation uint64
	Sequence   uint64
	PayloadLen uint32
}

// EncodeHeader appends the 21-byte wire header to dst and returns the new
// slice. The header is not secret; generation and sequence number are also
// authenticated by the Cipher as associated data.
func EncodeHeader(dst []byte, h Header) []byte {
	dst = append(dst, byte(h.Type))
	var buf [8]byte
	binary.BigEndian.PutUint64(buf[:], h.Generation)
	dst = append(dst, buf[:]...)
	binary.BigEndian.PutUint64(buf[:], h.Sequence)
	dst = append(dst, buf[:]...)
	var lenBuf [4]byte
	binary.BigEndian.PutUint32(lenBuf[:], h.PayloadLen)
	return append(dst, lenBuf[:]...)
}

// DecodeHeader parses a Header from exactly HeaderSize bytes.
func DecodeHeader(p []byte) (Header, error) {
	if len(p) < HeaderSize {
		return Header{}, errors.New("recordlayer: short record header")
	}
	return Header{
		Type:       RecordType(p[0]),
		Generation: binary.BigEndian.Uint64(p[1:9]),
		Sequence:   binary.BigEndian.Uint64(p[9:17]),
		PayloadLen: binary.BigEndian.Uint32(p[17:21]),
	}, nil
}
