// Package framemux implements the sending side of a multiplexed message
// broker connection. A single byte stream (the Transport callback) carries
// frames that belong to many independent logical channels.
//
// On each channel a full message is always written as an ordered sequence of:
//
//  1. one method frame
//  2. one content header frame
//  3. zero or more content body frames
//
// Frames belonging to different channels may be interleaved, but frames of a
// single message are never split apart by another channel's data.
//
// The wire layout of every frame is:
//
//	octet    frame type
//	short    channel id        (big endian; 0 for connection-level frames)
//	long     payload size       (big endian; number of payload octets)
//	size     payload
//	octet    frame end marker (0xCE)
//
// Heartbeat frames use channel 0, an empty payload and type FrameHeartbeat.
package framemux

import "encoding/binary"

// Frame types as they appear in the first octet of a frame.
const (
	FrameMethod    byte = 1
	FrameHeader    byte = 2
	FrameBody      byte = 3
	FrameHeartbeat byte = 8
)

// FrameEnd marks the end of every frame on the wire.
const FrameEnd byte = 0xCE

// HeaderSize is the fixed overhead of a frame in octets (type, channel,
// size and the end marker).
const HeaderSize = 1 + 2 + 4 + 1

// Method identifies which logical method a message carries.
type Method struct {
	ClassID  uint16
	MethodID uint16
}

// Header is the content header accompanying every message.
type Header struct {
	ClassID   uint16
	BodySize  uint64
	Flags     uint16
	MessageID uint64
}

// Message is one logical message published on a channel.
type Message struct {
	Method Method
	Header Header
	Body   []byte
	ID     uint64
}

// Frame describes one fully assembled frame handed to an observer.
type Frame struct {
	Type      byte
	ChannelID uint16
	Payload   []byte
}

// Size reports the on-wire size of the frame in octets.
func (f Frame) Size() int {
	return HeaderSize + len(f.Payload)
}

// encodeFrame serializes a frame. The returned slice is freshly allocated and
// may be retained by callers.
func encodeFrame(typ byte, channelID uint16, payload []byte) []byte {
	out := make([]byte, HeaderSize+len(payload))
	out[0] = typ
	binary.BigEndian.PutUint16(out[1:3], channelID)
	binary.BigEndian.PutUint32(out[3:7], uint32(len(payload)))
	copy(out[7:], payload)
	out[len(out)-1] = FrameEnd
	return out
}

func encodeMethod(m Method) []byte {
	p := make([]byte, 4)
	binary.BigEndian.PutUint16(p[0:2], m.ClassID)
	binary.BigEndian.PutUint16(p[2:4], m.MethodID)
	return p
}

func encodeHeader(h Header) []byte {
	// class-id (2), weight (2), body-size (8), flags (2), message-id (8).
	p := make([]byte, 22)
	binary.BigEndian.PutUint16(p[0:2], h.ClassID)
	// Weight, reserved in this implementation, always zero.
	binary.BigEndian.PutUint64(p[4:12], h.BodySize)
	binary.BigEndian.PutUint16(p[12:14], h.Flags)
	binary.BigEndian.PutUint64(p[14:22], h.MessageID)
	return p
}
