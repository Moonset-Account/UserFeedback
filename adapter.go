package pppframe

import (
	"errors"
	"io"
	"sync"
)

// Errors returned by high-level adapter operations.
var (
	ErrNoOutput         = errors.New("pppframe: adapter has no serial output writer")
	ErrInvalidDirection = errors.New("pppframe: invalid negotiation direction")
)

// Adapter is a bidirectional PPP framing endpoint. Outbound payloads are
// encoded with the confirmed send map and written to an io.Writer. Inbound
// serial byte batches are decoded by an embedded Receiver.
type Adapter struct {
	out     io.Writer
	rx      *Receiver
	sendMap ControlMap

	mu sync.RWMutex
}

// NewAdapter creates a bidirectional adapter. A nil out is permitted for a
// receive-only endpoint; SendFrame/Write then return ErrNoOutput.
func NewAdapter(out io.Writer, opts ...Option) *Adapter {
	s := defaultSettings()
	for _, opt := range opts {
		opt(&s)
	}
	return &Adapter{
		out:     out,
		rx:      NewReceiver(s.maps.Receive, opts...),
		sendMap: s.maps.Send,
	}
}

// SendFrame encodes payload as one PPP frame and writes the complete wire
// representation to the serial output. The returned count is wire bytes
// written. The send map is snapshotted for the whole frame; a confirmation
// installed while a large write is in progress affects only a later frame.
func (a *Adapter) SendFrame(payload []byte) (int, error) {
	a.mu.RLock()
	out := a.out
	sendMap := a.sendMap
	a.mu.RUnlock()

	if out == nil {
		return 0, ErrNoOutput
	}

	frame := EncodeFrame(payload, sendMap)
	return a.writeWireWith(out, frame)
}

func (a *Adapter) writeWire(frame []byte) (int, error) {
	a.mu.RLock()
	out := a.out
	a.mu.RUnlock()
	if out == nil {
		return 0, ErrNoOutput
	}
	return a.writeWireWith(out, frame)
}

func (a *Adapter) writeWireWith(out io.Writer, frame []byte) (int, error) {
	remaining := frame
	written := 0
	for len(remaining) > 0 {
		n, err := out.Write(remaining)
		if n > 0 {
			if n > len(remaining) {
				n = len(remaining)
			}
			written += n
			remaining = remaining[n:]
		}
		if err != nil {
			return written, err
		}
		if n <= 0 {
			return written, io.ErrShortWrite
		}
	}
	return written, nil
}

// Write implements io.Writer by framing p as exactly one packet/frame.
// A Write is therefore a message boundary; it is not a byte stream of
// independent frame fragments. Use SendFrame for the equivalent explicit API.
func (a *Adapter) Write(p []byte) (int, error) {
	if _, err := a.SendFrame(p); err != nil {
		return 0, err
	}
	return len(p), nil
}

// FeedSerial delivers a batch of bytes received from the serial side.
func (a *Adapter) FeedSerial(chunk []byte) Report {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.rx.Feed(chunk)
}

// Reset puts the receive direction into flag-resynchronization mode.
func (a *Adapter) Reset() (DroppedFrame, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.rx.Reset()
}

// Receiver exposes the low-level receiver for callers that do not need
// Adapter-level locking. Most callers should use FeedSerial and Reset.
func (a *Adapter) Receiver() *Receiver {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.rx
}

// ConfirmedMaps returns the active send and receive maps. A staged receive
// map is not returned as active; use PendingReceiveMap for that state.
func (a *Adapter) ConfirmedMaps() Maps {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return Maps{Send: a.sendMap, Receive: a.rx.ReceiveMap()}
}

// PendingReceiveMap returns a confirmed receive map waiting for a boundary.
func (a *Adapter) PendingReceiveMap() (ControlMap, bool) {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return a.rx.PendingReceiveMap()
}

// ConfirmDirectionMap records m as the confirmed map for exactly dir.
//
// Send confirmation is active for the next outbound frame. Receive
// confirmation is immediate only when the receiver is at a frame boundary;
// otherwise it is staged until the next flag. For DirectionBoth the returned
// bool reports whether the receive half became active immediately.
func (a *Adapter) ConfirmDirectionMap(dir Direction, m ControlMap) (bool, error) {
	if !dir.Valid() {
		return false, ErrInvalidDirection
	}

	a.mu.Lock()
	defer a.mu.Unlock()

	receiveApplied := true
	if dir&DirectionSend != 0 {
		a.sendMap = m
	}
	if dir&DirectionReceive != 0 {
		receiveApplied = a.rx.UpdateReceiveMap(m)
	}
	return receiveApplied, nil
}

// UpdateConfirmedMap is a concise alias for ConfirmDirectionMap.
func (a *Adapter) UpdateConfirmedMap(dir Direction, m ControlMap) (bool, error) {
	return a.ConfirmDirectionMap(dir, m)
}

// SetSendMap replaces the confirmed send map for the next frame.
func (a *Adapter) SetSendMap(m ControlMap) {
	a.mu.Lock()
	a.sendMap = m
	a.mu.Unlock()
}

// SetReceiveMap replaces the receive map at the next safe frame boundary.
// It reports whether the map became active immediately.
func (a *Adapter) SetReceiveMap(m ControlMap) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.rx.UpdateReceiveMap(m)
}
