package recordlayer

import (
	"errors"
	"io"
	"sync"
)

// ErrClosed is returned by writes after the coordinator has been closed.
var ErrClosed = errors.New("recordlayer: connection closed")

// Snapshot is a point-in-time, lock-free view of coordinator state, intended
// for tests and diagnostics.
type Snapshot struct {
	ReadGeneration  uint64
	ReadSequence    uint64
	WriteGeneration uint64
	WriteSequence   uint64
	PendingResponse bool
	Closed          bool
	ServeDone       bool
	AppQueueLength  int
}

// Conn coordinates the record layer of one endpoint.
//
// Each direction keeps an independent key generation and record sequence
// counter. Key-rotation control records are protected by the generation that
// is current while they are sealed; the first record sealed afterwards uses
// the new generation with a sequence number of zero.
type Conn struct {
	tr     Transport
	cipher Cipher

	mu       sync.Mutex
	cond     *sync.Cond // wakes ReadApplication when application data arrives
	readGen  uint64
	readSeq  uint64
	writeGen uint64
	writeSeq uint64

	// pendingResponse is set when a TypeRotateRequest arrives. The response is
	// flushed before the next application record that has not yet started
	// sealing.
	pendingResponse bool

	closed    bool
	serveDone bool
	appQ      [][]byte

	// onGenerationChange, when set, is invoked while holding mu immediately
	// after a direction advances to a new generation.
	onGenerationChange func(direction string, oldGen, newGen uint64)

	// testReceiveGate is a white-box test seam. When non-nil it runs after a
	// frame has been fully read from the transport and before the coordinator
	// lock is taken; blocking inside it holds the receive loop at a precise
	// interleaving point.
	testReceiveGate func(hdr Header)
}

// NewConn creates a coordinator starting at generation zero on both
// directions.
func NewConn(tr Transport, cipher Cipher) *Conn {
	c := &Conn{tr: tr, cipher: cipher}
	c.cond = sync.NewCond(&c.mu)
	return c
}

// SetGenerationChangeHook installs an observation hook for key-generation
// changes. It must be called before Serve and WriteApplication.
func (c *Conn) SetGenerationChangeHook(fn func(direction string, oldGen, newGen uint64)) {
	c.mu.Lock()
	c.onGenerationChange = fn
	c.mu.Unlock()
}

// Rotate seals and sends a TypeRotateRequest under the current write
// generation, then advances the write generation and restarts its sequence
// counter at zero. The first record written afterwards is therefore protected
// by the new generation.
func (c *Conn) Rotate() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return ErrClosed
	}
	if err := c.writeFrameLocked(TypeRotateRequest, nil); err != nil {
		return err
	}
	old := c.writeGen
	c.writeGen++
	c.writeSeq = 0
	if c.onGenerationChange != nil {
		c.onGenerationChange("write", old, c.writeGen)
	}
	return nil
}

// WriteApplication seals and writes one application record. If the peer asked
// for a key rotation and this endpoint has not yet answered, the
// TypeRotateResponse is flushed first, while still inside the write lock, so
// it cannot overtake an in-progress segmented record.
func (c *Conn) WriteApplication(plaintext []byte) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return ErrClosed
	}
	if c.pendingResponse {
		// Answer the peer before sealing any new application record.
		if err := c.writeFrameLocked(TypeRotateResponse, nil); err != nil {
			return err
		}
		c.pendingResponse = false
	}
	return c.writeFrameLocked(TypeApplication, plaintext)
}

// writeFrameLocked builds one record under the current write generation and
// writes it completely. Transport short writes are retried here, so a record
// that has started being segmented out is always finished before the lock is
// released.
func (c *Conn) writeFrameLocked(typ RecordType, payload []byte) error {
	sealed, err := c.cipher.Seal(c.writeGen, c.writeSeq, typ, payload)
	if err != nil {
		return err
	}
	hdr := Header{
		Type:       typ,
		Generation: c.writeGen,
		Sequence:   c.writeSeq,
		PayloadLen: uint32(len(sealed)),
	}
	wire := EncodeHeader(nil, hdr)
	wire = append(wire, sealed...)
	if err := writeAll(c.tr, wire); err != nil {
		return err
	}
	c.writeSeq++
	return nil
}

// writeAll writes the full buffer, transparently retrying short writes.
func writeAll(w io.Writer, p []byte) error {
	for len(p) > 0 {
		n, err := w.Write(p)
		if n > 0 {
			p = p[n:]
		}
		if err != nil && !errors.Is(err, io.ErrShortWrite) {
			return err
		}
		if n == 0 && err == nil {
			return errors.New("recordlayer: transport wrote zero bytes without error")
		}
	}
	return nil
}

// Serve runs the receive loop until the transport reaches end of file or a
// record fails authentication/framing, or Close is called. The returned error
// is nil on a clean end of stream.
func (c *Conn) Serve() error {
	for {
		hdr, sealed, err := c.readFrame()
		if err != nil {
			// End of stream, or our own Close shutting the transport down,
			// are both clean terminations.
			if errors.Is(err, io.EOF) || errors.Is(err, io.ErrClosedPipe) {
				c.markDone()
				return nil
			}
			c.markDone()
			return err
		}
		if c.testReceiveGate != nil {
			c.testReceiveGate(hdr)
		}
		c.mu.Lock()
		if c.closed {
			c.mu.Unlock()
			c.markDone()
			return nil
		}
		err = c.handleFrameLocked(hdr, sealed)
		c.mu.Unlock()
		if err != nil {
			c.markDone()
			return err
		}
	}
}

// markDone wakes any blocked ReadApplication when the receive loop has ended.
func (c *Conn) markDone() {
	c.mu.Lock()
	c.serveDone = true
	c.cond.Broadcast()
	c.mu.Unlock()
}

func (c *Conn) handleFrameLocked(hdr Header, sealed []byte) error {
	// Sequence continuity per read generation: every received record must use
	// exactly the next expected number.
	if hdr.Generation != c.readGen || hdr.Sequence != c.readSeq {
		return errors.New("recordlayer: unexpected record generation or sequence")
	}
	plaintext, err := c.cipher.Open(hdr.Generation, hdr.Sequence, hdr.Type, sealed)
	if err != nil {
		return err
	}
	c.readSeq++
	switch hdr.Type {
	case TypeApplication:
		c.appQ = append(c.appQ, plaintext)
		c.cond.Broadcast()
	case TypeRotateRequest:
		// Any rotation control record advances the read generation, because
		// the peer's Rotate() already advanced its write generation after
		// sealing this record under the old one.
		old := c.readGen
		c.readGen++
		c.readSeq = 0
		c.pendingResponse = true
		if c.onGenerationChange != nil {
			c.onGenerationChange("read", old, c.readGen)
		}
	case TypeRotateResponse:
		// A plain response is an ordinary control record: it consumed a
		// sequence number but requests nothing, so it cannot cause another
		// response and cannot loop.
	default:
		return errors.New("recordlayer: unknown record type")
	}
	return nil
}

// readFrame reads one framed record without holding the coordinator lock.
func (c *Conn) readFrame() (Header, []byte, error) {
	var hdrBuf [HeaderSize]byte
	if _, err := io.ReadFull(c.tr, hdrBuf[:]); err != nil {
		return Header{}, nil, err
	}
	hdr, err := DecodeHeader(hdrBuf[:])
	if err != nil {
		return Header{}, nil, err
	}
	sealed := make([]byte, hdr.PayloadLen)
	if _, err := io.ReadFull(c.tr, sealed); err != nil {
		return Header{}, nil, err
	}
	return hdr, sealed, nil
}

// ReadApplication blocks until an application record is available and returns
// its decrypted payload. It returns io.EOF after the receive loop has ended
// and the queue has drained.
func (c *Conn) ReadApplication() ([]byte, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for len(c.appQ) == 0 && !c.closed && !c.serveDone {
		// Serve's markDone broadcasts on this cond when the receive loop ends.
		c.cond.Wait()
	}
	if len(c.appQ) > 0 {
		out := c.appQ[0]
		c.appQ = c.appQ[1:]
		return out, nil
	}
	return nil, io.EOF
}

// Close shuts the coordinator down and closes the transport so a blocked
// Serve returns. It is idempotent.
func (c *Conn) Close() error {
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return nil
	}
	c.closed = true
	c.cond.Broadcast()
	tr := c.tr
	c.mu.Unlock()
	return tr.Close()
}

// Snapshot returns a consistent copy of the coordinator state.
func (c *Conn) Snapshot() Snapshot {
	c.mu.Lock()
	defer c.mu.Unlock()
	return Snapshot{
		ReadGeneration:  c.readGen,
		ReadSequence:    c.readSeq,
		WriteGeneration: c.writeGen,
		WriteSequence:   c.writeSeq,
		PendingResponse: c.pendingResponse,
		Closed:          c.closed,
		ServeDone:       c.serveDone,
		AppQueueLength:  len(c.appQ),
	}
}

// Snapshot returns a consistent copy of the coordinator state.
