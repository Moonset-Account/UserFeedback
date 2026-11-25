package framemux

import (
	"context"
	"errors"
	"sync"
)

// Default values used by Dial when the corresponding Config field is left at
// its zero value.
const (
	DefaultFrameMax   = 131072
	DefaultWriteLimit = 256
)

// Errors returned by the connection and its channels.
var (
	ErrClosed         = errors.New("framemux: connection is closed")
	ErrUnknownChannel = errors.New("framemux: unknown channel")
	ErrFrameMax       = errors.New("framemux: frame max must be at least 1 octet")
	ErrWriteLimit     = errors.New("framemux: write limit must be at least 1 octet")
)

// Transport is the sink for serialized bytes. Implementations receive at most
// WriteLimit octets per call and must process all of them or return an error.
// It is never called concurrently.
type Transport func(b []byte) error

// Config configures a sending connection.
type Config struct {
	// FrameMax is the maximum payload size of a newly encapsulated body
	// frame. It may be changed at runtime via Conn.SetFrameMax; the new
	// value only affects body frames whose encapsulation has not started.
	FrameMax int
	// WriteLimit bounds how many octets a single Transport call receives.
	WriteLimit int
}

// ChannelStats is a point-in-time snapshot of one channel's sender state.
type ChannelStats struct {
	ID       uint16
	Paused   bool
	Queued   int
	InFlight bool
	FrameMax int
}

// Stats is a point-in-time snapshot of the whole sending connection.
type Stats struct {
	Closed       bool
	FrameMax     int
	WriteLimit   int
	PendingHeart int
	Channels     []ChannelStats
}

// channel holds the per-channel outbound queue and the state of the message
// that is currently being serialized.
type channel struct {
	id     uint16
	paused bool
	queue  []*outMsg
	// inFlight is the message whose method frame has started (or is about
	// to start). A paused channel is still allowed to drain this message.
	inFlight *outMsg
}

// outMsg is a message while it is being serialized onto the transport.
type outMsg struct {
	msg *Message
	// phase is the next frame to emit: method, header, then body frames.
	phase byte
	// bodyOff is the offset into msg.Body of the next body frame.
	bodyOff int
}

const (
	phaseMethod = iota
	phaseHeader
	phaseBody
	phaseDone
)

// Conn is the multiplexed sending side of a connection. Its zero value is not
// usable; create one with Dial.
type Conn struct {
	mu        sync.Mutex
	cond      *sync.Cond
	transport Transport
	frameMax  int
	writeLim  int

	channels map[uint16]*channel
	// order is the round-robin ring of channel ids; cursor points at the
	// candidate inspected first on the next pump iteration.
	order  []uint16
	cursor int

	heartbeats int
	closed     bool
	writeErr   error
	done       chan struct{}

	// A frame already encapsulated must keep its exact bytes even when
	// SetFrameMax runs while it is being written. pendingFrame is that frame
	// together with the channel that owns it; the pump drains it fully before
	// encapsulating anything new.
	pendingFrame    []byte
	pendingChannel  *channel
	pendingFinished bool
}

// Dial creates a connection and starts its single writer goroutine.
func Dial(transport Transport, cfg Config) *Conn {
	if cfg.FrameMax <= 0 {
		cfg.FrameMax = DefaultFrameMax
	}
	if cfg.WriteLimit <= 0 {
		cfg.WriteLimit = DefaultWriteLimit
	}
	c := &Conn{
		transport: transport,
		frameMax:  cfg.FrameMax,
		writeLim:  cfg.WriteLimit,
		channels:  make(map[uint16]*channel),
		done:      make(chan struct{}),
	}
	c.cond = sync.NewCond(&c.mu)
	go c.pump()
	return c
}

// Open registers a logical channel. Opening an existing id is a no-op.
func (c *Conn) Open(id uint16) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return ErrClosed
	}
	if _, ok := c.channels[id]; !ok {
		c.channels[id] = &channel{id: id}
		c.order = append(c.order, id)
		c.cond.Broadcast()
	}
	return nil
}

func (c *Conn) channelLocked(id uint16) (*channel, error) {
	ch, ok := c.channels[id]
	if !ok {
		return nil, ErrUnknownChannel
	}
	return ch, nil
}

// Publish queues a message on a channel. The message body is defensively
// copied, so later mutation by the caller cannot affect the wire bytes.
func (c *Conn) Publish(id uint16, m Message) error {
	cp := make([]byte, len(m.Body))
	copy(cp, m.Body)
	hdr := m.Header
	hdr.BodySize = uint64(len(cp))
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return ErrClosed
	}
	ch, err := c.channelLocked(id)
	if err != nil {
		return err
	}
	ch.queue = append(ch.queue, &outMsg{
		msg:   &Message{Method: m.Method, Header: hdr, Body: cp, ID: m.ID},
		phase: phaseMethod,
	})
	c.cond.Broadcast()
	return nil
}

// QueueHeartbeat asks the writer to insert a heartbeat frame at the next whole
// frame boundary. Heartbeats never split an in-progress frame.
func (c *Conn) QueueHeartbeat() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return ErrClosed
	}
	c.heartbeats++
	c.cond.Broadcast()
	return nil
}

// Pause stops a channel from starting new messages. A message whose method
// frame has already started is still finished to completion.
func (c *Conn) Pause(id uint16) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return ErrClosed
	}
	ch, err := c.channelLocked(id)
	if err != nil {
		return err
	}
	ch.paused = true
	return nil
}

// Resume lifts a Pause.
func (c *Conn) Resume(id uint16) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return ErrClosed
	}
	ch, err := c.channelLocked(id)
	if err != nil {
		return err
	}
	ch.paused = false
	c.cond.Broadcast()
	return nil
}

// SetFrameMax changes the maximum body-frame payload. Frames already being
// written keep their old boundaries; only body frames whose encapsulation has
// not started use the new limit. Method and header frames are unaffected.
func (c *Conn) SetFrameMax(n int) error {
	if n <= 0 {
		return ErrFrameMax
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return ErrClosed
	}
	c.frameMax = n
	return nil
}

// Stats returns a consistent snapshot of the sender's internal state. It is
// intended for tests and diagnostics.
func (c *Conn) Stats() Stats {
	c.mu.Lock()
	defer c.mu.Unlock()
	s := Stats{
		Closed:       c.closed,
		FrameMax:     c.frameMax,
		WriteLimit:   c.writeLim,
		PendingHeart: c.heartbeats,
		Channels:     make([]ChannelStats, 0, len(c.order)),
	}
	for _, id := range c.order {
		ch := c.channels[id]
		s.Channels = append(s.Channels, ChannelStats{
			ID:       id,
			Paused:   ch.paused,
			Queued:   len(ch.queue),
			InFlight: ch.inFlight != nil,
			FrameMax: c.frameMax,
		})
	}
	return s
}

// allIdleLocked reports whether there is genuinely nothing left to do: no
// frame mid-write, no in-flight message, no queued message and no pending
// heartbeat.
func (c *Conn) allIdleLocked() bool {
	if c.pendingFrame != nil || c.heartbeats != 0 {
		return false
	}
	for _, id := range c.order {
		ch := c.channels[id]
		if ch.inFlight != nil || len(ch.queue) != 0 {
			return false
		}
	}
	return true
}

// Idle blocks until every queued and in-flight message has been fully written
// and all queued heartbeats have been emitted, or ctx is cancelled. It returns
// true on quiescence and false on timeout. Messages published while Idle is
// waiting extend the wait.
func (c *Conn) Idle(ctx context.Context) bool {
	stop := make(chan struct{})
	defer close(stop)
	go func() {
		select {
		case <-ctx.Done():
			c.mu.Lock()
			c.cond.Broadcast()
			c.mu.Unlock()
		case <-stop:
		}
	}()

	c.mu.Lock()
	defer c.mu.Unlock()
	for !c.allIdleLocked() && ctx.Err() == nil {
		c.cond.Wait()
	}
	return c.allIdleLocked()
}

// Done is closed once the writer goroutine has exited.
func (c *Conn) Done() <-chan struct{} {
	return c.done
}

// Err returns the transport error that stopped the writer, if any.
func (c *Conn) Err() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.writeErr
}

// Close lets the writer drain all outstanding work and then stops it. It
// returns the transport error if one occurred while draining.
func (c *Conn) Close() error {
	c.mu.Lock()
	if c.closed {
		err := c.writeErr
		c.mu.Unlock()
		<-c.done
		return err
	}
	c.closed = true
	c.cond.Broadcast()
	c.mu.Unlock()

	<-c.done

	c.mu.Lock()
	err := c.writeErr
	c.mu.Unlock()
	return err
}

// pump is the single writer goroutine. All transport calls happen here.
func (c *Conn) pump() {
	defer close(c.done)
	c.mu.Lock()
	defer c.mu.Unlock()

	for {
		// Drain a frame whose encapsulation already started first, so its
		// bytes are immune to Pause/SetFrameMax racing the transport.
		if c.pendingFrame != nil {
			frame := c.pendingFrame
			if err := c.writeAllLocked(frame); err != nil {
				c.failLocked(err)
				return
			}
			c.pendingFrame = nil
			if c.pendingFinished {
				c.pendingChannel.inFlight = nil
				c.pendingFinished = false
			}
			c.pendingChannel = nil
			c.cond.Broadcast()
			continue
		}

		// Emit one heartbeat frame per loop while heartbeats are pending,
		// but only between whole frames.
		if c.heartbeats > 0 {
			c.heartbeats--
			c.pendingFrame = encodeFrame(FrameHeartbeat, 0, nil)
			continue
		}

		ch := c.nextReadyLocked()
		if ch == nil {
			if c.closed {
				return
			}
			c.cond.Wait()
			continue
		}

		if err := c.encapsulateLocked(ch); err != nil {
			c.failLocked(err)
			return
		}
	}
}

// failLocked records the first transport error and marks the connection
// closed so that further API calls fail fast.
func (c *Conn) failLocked(err error) {
	if c.writeErr == nil {
		c.writeErr = err
	}
	c.closed = true
	c.cond.Broadcast()
}

// nextReadyLocked walks the round-robin ring and returns a channel with work:
// either an in-flight message (always drainable, even when paused) or a queued
// message on a channel that is not paused. The cursor advances past the
// chosen channel so continuously backlogged channels take whole-frame turns.
func (c *Conn) nextReadyLocked() *channel {
	n := len(c.order)
	for i := 0; i < n; i++ {
		idx := (c.cursor + i) % n
		ch := c.channels[c.order[idx]]
		if ch.inFlight != nil || (!ch.paused && len(ch.queue) > 0) {
			c.cursor = (idx + 1) % n
			return ch
		}
	}
	return nil
}

// encapsulateLocked builds exactly the next whole frame of the channel's
// in-flight message and stores it as the connection's pending frame. The pump
// then writes the stored bytes to completion; a concurrent SetFrameMax can no
// longer change them. The live frameMax is read once, at encapsulation time,
// and only applies to body frames whose encapsulation has not started.
func (c *Conn) encapsulateLocked(ch *channel) error {
	if ch.inFlight == nil {
		om := ch.queue[0]
		ch.queue = ch.queue[1:]
		ch.inFlight = om
	}
	om := ch.inFlight

	var (
		frame    []byte
		finished bool
	)
	switch om.phase {
	case phaseMethod:
		frame = encodeFrame(FrameMethod, ch.id, encodeMethod(om.msg.Method))
		om.phase = phaseHeader
	case phaseHeader:
		frame = encodeFrame(FrameHeader, ch.id, encodeHeader(om.msg.Header))
		if len(om.msg.Body) == 0 {
			om.phase = phaseDone
		} else {
			om.phase = phaseBody
		}
	case phaseBody:
		lim := c.frameMax
		remaining := len(om.msg.Body) - om.bodyOff
		n := lim
		if remaining < n {
			n = remaining
		}
		frame = encodeFrame(FrameBody, ch.id, om.msg.Body[om.bodyOff:om.bodyOff+n])
		om.bodyOff += n
		if om.bodyOff >= len(om.msg.Body) {
			om.phase = phaseDone
		}
	}

	if om.phase == phaseDone {
		finished = true
	}
	c.pendingFrame = frame
	c.pendingChannel = ch
	c.pendingFinished = finished
	return nil
}

// writeAllLocked writes p through the transport in calls of at most writeLim
// octets. The mutex is released while the transport runs so control calls
// (Pause, SetFrameMax, ...) never block behind a slow or gated transport; the
// state those calls mutate only matters at the next frame boundary anyway.
func (c *Conn) writeAllLocked(p []byte) error {
	for len(p) > 0 {
		n := c.writeLim
		if n > len(p) {
			n = len(p)
		}
		chunk := p[:n]
		transport := c.transport
		c.mu.Unlock()
		err := transport(chunk)
		c.mu.Lock()
		if err != nil {
			return err
		}
		p = p[n:]
	}
	return nil
}
