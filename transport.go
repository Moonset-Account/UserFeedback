package recordlayer

import (
	"errors"
	"io"
	"sync"
)

// Transport is the byte-oriented transport underneath the record layer.
//
// Implementations are allowed to perform short writes: Write must return the
// number of bytes actually written (0 <= n <= len(p)) together with a non-nil
// error if n < len(p). The coordinator retries until a full record header or
// sealed payload has been written, so a record already being segmented out is
// always finished completely.
type Transport interface {
	io.Writer
	io.Reader
	io.Closer
}

// MemPipe is an in-memory, bounded, reliable byte channel between two
// endpoints. A Write only accepts bytes while room remains, which lets a test
// cap how many bytes leave the coordinator on each call and force short writes.
type MemPipe struct {
	mu     sync.Mutex
	cond   *sync.Cond
	buf    []byte
	limit  int
	closed bool
}

// NewBoundedPipe creates a pipe whose internal buffer never grows beyond
// limit bytes. A limit of zero means unlimited buffering.
func NewBoundedPipe(limit int) *MemPipe {
	p := &MemPipe{limit: limit}
	p.cond = sync.NewCond(&p.mu)
	return p
}

// pipeEnd is one direction of a paired memory transport.
type pipeEnd struct {
	pipe *MemPipe
	once sync.Once
}

// Write appends as many bytes as currently fit. It returns a short write when
// the bounded buffer fills up, and io.ErrClosedPipe once the pipe is closed.
func (e *pipeEnd) Write(p []byte) (int, error) {
	e.pipe.mu.Lock()
	defer e.pipe.mu.Unlock()
	for len(e.pipe.buf) == e.pipe.limit && e.pipe.limit > 0 && !e.pipe.closed {
		e.pipe.cond.Wait()
	}
	if e.pipe.closed {
		return 0, io.ErrClosedPipe
	}
	if len(p) == 0 {
		return 0, nil
	}
	room := len(p)
	if e.pipe.limit > 0 {
		if avail := e.pipe.limit - len(e.pipe.buf); avail < room {
			room = avail
		}
	}
	if room == 0 {
		return 0, errors.New("recordlayer: transport buffer full")
	}
	e.pipe.buf = append(e.pipe.buf, p[:room]...)
	e.pipe.cond.Broadcast()
	if room < len(p) {
		return room, io.ErrShortWrite
	}
	return room, nil
}

// Read returns buffered bytes, blocking while the pipe is empty.
func (e *pipeEnd) Read(p []byte) (int, error) {
	e.pipe.mu.Lock()
	defer e.pipe.mu.Unlock()
	for len(e.pipe.buf) == 0 && !e.pipe.closed {
		e.pipe.cond.Wait()
	}
	if len(e.pipe.buf) == 0 {
		return 0, io.EOF
	}
	n := copy(p, e.pipe.buf)
	e.pipe.buf = e.pipe.buf[n:]
	e.pipe.cond.Broadcast()
	return n, nil
}

// Close marks the pipe closed. It is idempotent.
func (e *pipeEnd) Close() error {
	e.once.Do(func() {
		e.pipe.mu.Lock()
		e.pipe.closed = true
		e.pipe.cond.Broadcast()
		e.pipe.mu.Unlock()
	})
	return nil
}

// NewPipePair builds two Transports connected by two bounded pipes, one per
// direction, modelling the two independent byte channels of a full duplex
// connection.
func NewPipePair(bufferLimit int) (a, b Transport) {
	aToB := NewBoundedPipe(bufferLimit)
	bToA := NewBoundedPipe(bufferLimit)
	aEnd := &pipeEnd{pipe: aToB}
	bEnd := &pipeEnd{pipe: bToA}
	// a's reads come from b's writes and vice versa.
	return &pairedEnd{w: aEnd, r: &pipeEnd{pipe: bToA}}, &pairedEnd{w: bEnd, r: &pipeEnd{pipe: aToB}}
}

// pairedEnd joins a write side and a read side of different pipes.
type pairedEnd struct {
	w *pipeEnd
	r *pipeEnd
}

func (e *pairedEnd) Write(p []byte) (int, error) { return e.w.Write(p) }
func (e *pairedEnd) Read(p []byte) (int, error)  { return e.r.Read(p) }

// Close closes both underlying directions.
func (e *pairedEnd) Close() error {
	e.w.Close()
	e.r.Close()
	return nil
}
