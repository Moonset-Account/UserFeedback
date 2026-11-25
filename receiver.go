package framemux

import (
	"encoding/binary"
	"errors"
	"sync"
)

// Reassembly errors. All of them are defensive checks against a malformed or
// out-of-spec byte stream; they never act on the peer beyond rejecting input.
var (
	ErrBadFrameEnd      = errors.New("framemux: missing frame end marker")
	ErrBadMethodFrame   = errors.New("framemux: method frame must carry a 4-octet payload")
	ErrBadHeaderFrame   = errors.New("framemux: header frame must carry a 22-octet payload")
	ErrUnexpectedFrame  = errors.New("framemux: frame out of order, expected method frame")
	ErrHeaderOrder      = errors.New("framemux: frame out of order, expected content header")
	ErrBodyOverflow     = errors.New("framemux: more body octets than declared in content header")
	ErrBodyUnderflow    = errors.New("framemux: method frame started while body octets were still expected")
	ErrHeartbeatPayload = errors.New("framemux: heartbeat frames must have an empty payload and use channel 0")
	ErrUnknownFrameType = errors.New("framemux: unknown frame type")
)

type recvPhase int

const (
	recvExpectMethod recvPhase = iota
	recvExpectHeader
	recvExpectBody
)

type recvChannel struct {
	phase     recvPhase
	cur       Message
	remaining uint64
}

// Receiver reassembles the byte stream produced by a Conn back into whole
// messages. It is safe for concurrent use; Write may be fed from a transport
// callback and Messages read from any goroutine.
type Receiver struct {
	mu        sync.Mutex
	buf       []byte
	channels  map[uint16]*recvChannel
	onMessage func(channelID uint16, m Message)
	onFrame   func(f Frame)
}

// NewReceiver builds a Receiver. onMessage is invoked once per fully
// reassembled message in channel order; onFrame, when non-nil, is invoked for
// every well-formed frame before reassembly.
func NewReceiver(onMessage func(channelID uint16, m Message), onFrame func(f Frame)) *Receiver {
	return &Receiver{
		channels:  make(map[uint16]*recvChannel),
		onMessage: onMessage,
		onFrame:   onFrame,
	}
}

// Write feeds inbound bytes. Frames that are only partially present are
// retained internally until the remaining octets arrive, so Write always
// accepts a stream chopped at arbitrary boundaries. A non-nil error means the
// stream is malformed and the receiver must not be fed further.
func (r *Receiver) Write(p []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	r.buf = append(r.buf, p...)
	consumed := len(p)
	for len(r.buf) >= HeaderSize {
		typ := r.buf[0]
		channelID := binary.BigEndian.Uint16(r.buf[1:3])
		size := int(binary.BigEndian.Uint32(r.buf[3:7]))
		total := HeaderSize + size
		if len(r.buf) < total {
			// Wait for the rest of the frame.
			return consumed, nil
		}
		payload := make([]byte, size)
		copy(payload, r.buf[7:7+size])
		if r.buf[total-1] != FrameEnd {
			return consumed, ErrBadFrameEnd
		}
		if err := r.dispatch(typ, channelID, payload); err != nil {
			return consumed, err
		}
		if r.onFrame != nil {
			r.onFrame(Frame{Type: typ, ChannelID: channelID, Payload: payload})
		}
		r.buf = r.buf[total:]
	}
	return consumed, nil
}

func (r *Receiver) dispatch(typ byte, channelID uint16, payload []byte) error {
	// Heartbeats are connection-level frames on channel 0 with no payload.
	// Channel 0 may still carry ordinary method/header/body frames.
	if typ == FrameHeartbeat {
		if channelID != 0 || len(payload) != 0 {
			return ErrHeartbeatPayload
		}
		return nil
	}

	ch := r.channels[channelID]
	if ch == nil {
		ch = &recvChannel{phase: recvExpectMethod}
		r.channels[channelID] = ch
	}

	switch typ {
	case FrameMethod:
		if ch.phase != recvExpectMethod {
			return ErrBodyUnderflow
		}
		if len(payload) != 4 {
			return ErrBadMethodFrame
		}
		ch.cur = Message{
			Method: Method{
				ClassID:  binary.BigEndian.Uint16(payload[0:2]),
				MethodID: binary.BigEndian.Uint16(payload[2:4]),
			},
		}
		ch.phase = recvExpectHeader
	case FrameHeader:
		if ch.phase != recvExpectHeader {
			return ErrHeaderOrder
		}
		if len(payload) != 22 {
			return ErrBadHeaderFrame
		}
		h := Header{
			ClassID:   binary.BigEndian.Uint16(payload[0:2]),
			BodySize:  binary.BigEndian.Uint64(payload[4:12]),
			Flags:     binary.BigEndian.Uint16(payload[12:14]),
			MessageID: binary.BigEndian.Uint64(payload[14:22]),
		}
		ch.cur.Header = h
		ch.cur.ID = h.MessageID
		ch.remaining = h.BodySize
		if h.BodySize == 0 {
			r.deliver(channelID, ch)
		} else {
			ch.cur.Body = make([]byte, 0, h.BodySize)
			ch.phase = recvExpectBody
		}
	case FrameBody:
		if ch.phase != recvExpectBody {
			return ErrUnexpectedFrame
		}
		if uint64(len(payload)) > ch.remaining {
			return ErrBodyOverflow
		}
		ch.cur.Body = append(ch.cur.Body, payload...)
		ch.remaining -= uint64(len(payload))
		if ch.remaining == 0 {
			r.deliver(channelID, ch)
		}
	default:
		return ErrUnknownFrameType
	}
	return nil
}

func (r *Receiver) deliver(channelID uint16, ch *recvChannel) {
	m := ch.cur
	// Reset the per-channel state before invoking the callback; m is a value
	// copy, so the callback still receives the fully reassembled message.
	ch.cur = Message{}
	ch.remaining = 0
	ch.phase = recvExpectMethod
	if r.onMessage != nil {
		r.onMessage(channelID, m)
	}
}

// Pending reports how many buffered bytes belong to an incomplete frame.
func (r *Receiver) Pending() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.buf)
}
