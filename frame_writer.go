package pppframe

import (
	"errors"
	"io"
)

// ErrFrameClosed is returned when a FrameWriter is written or closed after
// its frame has already been sent.
var ErrFrameClosed = errors.New("pppframe: frame writer already closed")

// FrameWriter accumulates one upper-layer payload and sends it as one frame
// when Finish or Close is called. Writes may split one logical payload into
// arbitrary halves. The confirmed send map is snapshotted at BeginFrame, so
// a negotiation confirmation arriving before Finish applies only to a later
// frame boundary.
type FrameWriter struct {
	adapter *Adapter
	payload []byte
	sendMap ControlMap
	closed  bool
}

// BeginFrame starts a transactional outbound frame.
func (a *Adapter) BeginFrame() *FrameWriter {
	a.mu.RLock()
	m := a.sendMap
	a.mu.RUnlock()
	return &FrameWriter{adapter: a, sendMap: m}
}

// Write appends payload bytes to the unfinished frame.
func (w *FrameWriter) Write(p []byte) (int, error) {
	if w.closed {
		return 0, ErrFrameClosed
	}
	w.payload = append(w.payload, p...)
	return len(p), nil
}

// Finish encodes FCS and framing with the map snapshotted at BeginFrame and
// writes the complete frame to the serial output. It returns wire bytes
// written.
func (w *FrameWriter) Finish() (int, error) {
	if w.closed {
		return 0, ErrFrameClosed
	}
	w.closed = true
	frame := EncodeFrame(w.payload, w.sendMap)
	return w.adapter.writeWire(frame)
}

// Close sends the frame. It implements io.Closer and is idempotent in the
// limited sense that a second close returns ErrFrameClosed.
func (w *FrameWriter) Close() error {
	_, err := w.Finish()
	return err
}

var _ io.WriteCloser = (*FrameWriter)(nil)
