package forwarder

import "sync"

// Conn is a handle to one live connection.  A connection is bound to a bucket
// at connect time and keeps that bucket's next hop until it is closed or its
// next hop fails and Rebalance reroutes it.
//
// Rerouting does not mutate a handle in place: the ring retires the old handle
// and installs a new one for the same connection ID.  The retired handle is a
// tombstone whose Close is a no-op, so a late close of the connection's old
// route can never decrement the counters of the route it was moved to.  Call
// Ring.Conn or Ring.Connections to obtain the live handle after a reroute.
//
// Conn is safe for concurrent use.
type Conn struct {
	mu     sync.Mutex
	id     string
	bucket int
	target string
	closed bool
	stale  bool
	ring   *Ring
}

// ID returns the connection identifier.
func (c *Conn) ID() string { return c.id }

// Bucket returns the bucket the connection is currently bound to.
func (c *Conn) Bucket() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.bucket
}

// Target returns the next hop the connection is currently routed to.
func (c *Conn) Target() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.target
}

// Closed reports whether this handle has been closed.
func (c *Conn) Closed() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.closed
}

// Stale reports whether this handle was superseded by a reroute.  A stale
// handle no longer owns the live connection.
func (c *Conn) Stale() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.stale
}

// Close releases the connection.  It is idempotent, and closing a handle that
// a reroute already superseded is a no-op: rerouting settled the old route's
// bookkeeping and re-charged the connection to its new next hop, so a late
// close of the retired handle must not touch that new charge.
//
// Close takes the ring lock before the handle lock.  Reroute marks handles
// stale while holding the ring lock, so "was I superseded?" and "remove my
// ring entry" are decided atomically against concurrent reroutes.
func (c *Conn) Close() {
	r := c.ring
	r.mu.Lock()
	defer r.mu.Unlock()

	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return
	}
	c.closed = true
	stale := c.stale
	c.mu.Unlock()

	if stale {
		return
	}
	// Identity check: only the handle that owns the ring entry removes it.
	if live, ok := r.conns[c.id]; ok && live == c {
		delete(r.conns, c.id)
	}
}

// markStale retires a handle that has been rerouted away.  The handle keeps
// its old bucket/target for inspection but no longer owns the live connection.
// The caller must hold the ring lock.
func (c *Conn) markStale() {
	c.mu.Lock()
	c.stale = true
	c.mu.Unlock()
}
