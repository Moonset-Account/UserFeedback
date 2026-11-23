package multipathwrite

import (
	"context"
	"sync"
	"time"
)

// unknownQueryBackoff is the pause between status queries while a device
// keeps reporting StatusUnknown for a write.
const unknownQueryBackoff = 5 * time.Millisecond

// noPathBackoff bounds busy waiting when every path is down and the
// coordinator is waiting for a replacement path to be attached.
const noPathBackoff = 5 * time.Millisecond

// record tracks one coordinator-assigned write through its lifecycle.
type record struct {
	id     WriteID
	offset int
	end    int

	decided chan struct{}
	once    sync.Once

	mu       sync.Mutex
	err      error
	durable  bool
	lastPath PathInfo
}

func (r *record) finish(err error, p PathInfo) {
	r.once.Do(func() {
		r.mu.Lock()
		r.err = err
		// A finished write that did not error has been accepted and
		// persisted by the device.
		r.durable = err == nil
		r.lastPath = p
		r.mu.Unlock()
		close(r.decided)
	})
}

func (r *record) snapshot() (error, bool, PathInfo) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.err, r.durable, r.lastPath
}

func overlap(aOff, aEnd, bOff, bEnd int) bool {
	return aOff < bEnd && bOff < aEnd
}

// Coordinator serializes overlapping writes to one device while letting
// disjoint writes run in parallel, and drives path failover when write
// outcomes become uncertain.
type Coordinator struct {
	mu      sync.Mutex
	nextID  WriteID
	records []*record
	paths   []Path

	// openPath creates a fresh path (new generation) to the same device.
	// It may be nil when no replacement path can be provisioned.
	openPath func() Path
}

// NewCoordinator builds a coordinator over the given initial paths.
// openPath, when non-nil, is called to attach a replacement path after
// every existing path has disconnected.
func NewCoordinator(paths []Path, openPath func() Path) *Coordinator {
	ps := make([]Path, 0, len(paths)+1)
	ps = append(ps, paths...)
	return &Coordinator{
		nextID:   1,
		records:  make([]*record, 0),
		paths:    ps,
		openPath: openPath,
	}
}

// healthyPathLocked returns a connected path, provisioning a fresh one
// via openPath when all known paths are broken.
func (c *Coordinator) healthyPathLocked() Path {
	for _, p := range c.paths {
		if !p.Broken() {
			return p
		}
	}
	if c.openPath != nil {
		np := c.openPath()
		if np != nil {
			c.paths = append(c.paths, np)
			return np
		}
	}
	return nil
}

// healthyPath waits (respecting ctx) until a connected path exists.
func (c *Coordinator) healthyPath(ctx context.Context) (Path, error) {
	for {
		c.mu.Lock()
		p := c.healthyPathLocked()
		c.mu.Unlock()
		if p != nil {
			return p, nil
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(noPathBackoff):
		}
	}
}

// Write submits data at offset. Writes to disjoint ranges may run in
// parallel; writes whose ranges overlap are applied strictly in
// submission order. The returned WriteResult identifies the path that
// finally acknowledged the write.
func (c *Coordinator) Write(ctx context.Context, offset int, data []byte) (WriteID, WriteResult, error) {
	if offset < 0 {
		return 0, WriteResult{}, errInvalidOffset
	}
	if len(data) == 0 {
		return 0, WriteResult{}, errEmptyWrite
	}

	c.mu.Lock()
	id := c.nextID
	c.nextID++
	r := &record{
		id:      id,
		offset:  offset,
		end:     offset + len(data),
		decided: make(chan struct{}),
	}
	c.records = append(c.records, r)
	c.mu.Unlock()

	go c.runWrite(ctx, r, append([]byte(nil), data...))

	select {
	case <-r.decided:
	case <-ctx.Done():
		// Ensure waiters do not hang if the caller gave up. The
		// background attempt keeps running if the device may still
		// accept it, but its outcome is no longer delivered here.
		r.finish(ctx.Err(), PathInfo{})
	}
	err, _, p := r.snapshot()
	if err != nil {
		return id, WriteResult{}, err
	}
	return id, WriteResult{ID: id, Path: p}, nil
}

// runWrite enforces ordering, then drives the send/query/re-send state
// machine until the write is known-written, known-impossible, or the
// context ends.
func (c *Coordinator) runWrite(ctx context.Context, r *record, data []byte) {
	if err := c.awaitPriors(ctx, r); err != nil {
		r.finish(err, PathInfo{})
		return
	}

	var lastPath PathInfo
	for {
		if ctx.Err() != nil {
			r.finish(ctx.Err(), lastPath)
			return
		}
		p, err := c.healthyPath(ctx)
		if err != nil {
			r.finish(err, lastPath)
			return
		}
		res, err := p.Write(ctx, r.id, r.offset, data)
		if err == nil {
			r.finish(nil, res.Path)
			return
		}
		lastPath = p.Info()

		// A transport-level failure means the outcome is uncertain.
		// Fail over to a new path of the same device and query by the
		// stable write ID before sending anything again.
		status, qp, qerr := c.queryUntilKnown(ctx, r.id)
		if qerr != nil {
			r.finish(qerr, lastPath)
			return
		}
		lastPath = qp
		switch status {
		case StatusWritten:
			// Already durable: do not re-send, adopt the new path.
			r.finish(nil, qp)
			return
		case StatusNotStarted:
			// Never landed: loop and re-send with the original ID.
			continue
		case StatusUnknown:
			// queryUntilKnown only returns Unknown on ctx end.
			r.finish(ctx.Err(), lastPath)
			return
		default:
			r.finish(errInvalidStatus, lastPath)
			return
		}
	}
}

// awaitPriors blocks while any earlier, still-undecided write overlaps
// this record's range. Disjoint earlier writes never block this one.
func (c *Coordinator) awaitPriors(ctx context.Context, r *record) error {
	for {
		c.mu.Lock()
		var wait *record
		for _, prior := range c.records {
			if prior.id >= r.id {
				break
			}
			if overlap(prior.offset, prior.end, r.offset, r.end) {
				select {
				case <-prior.decided:
				default:
					wait = prior
				}
			}
		}
		c.mu.Unlock()
		if wait == nil {
			return nil
		}
		select {
		case <-wait.decided:
			if perr, _, _ := wait.snapshot(); perr != nil {
				return perr
			}
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// queryUntilKnown asks a healthy path about id until it reports
// Written/NotStarted. StatusUnknown is retried with backoff on a fresh
// path when possible; while it persists, overlapping operations stay
// blocked because the record never finishes.
func (c *Coordinator) queryUntilKnown(ctx context.Context, id WriteID) (WriteStatus, PathInfo, error) {
	var lastPath PathInfo
	for {
		if ctx.Err() != nil {
			return StatusUnknown, lastPath, ctx.Err()
		}
		p, err := c.healthyPath(ctx)
		if err != nil {
			return StatusUnknown, lastPath, err
		}
		res, err := p.Query(ctx, id)
		if err == nil {
			lastPath = res.Path
			if res.Status != StatusUnknown {
				return res.Status, res.Path, nil
			}
		} else {
			lastPath = p.Info()
		}
		select {
		case <-ctx.Done():
			return StatusUnknown, lastPath, ctx.Err()
		case <-time.After(unknownQueryBackoff):
		}
	}
}

// Read fetches len(buf) bytes at offset, failing over to another path
// when the serving path disconnects.
func (c *Coordinator) Read(ctx context.Context, offset int, buf []byte) (ReadResult, error) {
	if offset < 0 || len(buf) == 0 {
		return ReadResult{}, errInvalidRead
	}
	for {
		if ctx.Err() != nil {
			return ReadResult{}, ctx.Err()
		}
		p, err := c.healthyPath(ctx)
		if err != nil {
			return ReadResult{}, err
		}
		res, err := p.Read(ctx, offset, buf)
		if err == nil {
			return res, nil
		}
		select {
		case <-ctx.Done():
			return ReadResult{}, ctx.Err()
		case <-time.After(noPathBackoff):
		}
	}
}

// Flush acknowledges only after every write submitted before this call is
// decided and durable, and the device has accepted a flush barrier.
// Writes submitted afterwards are not part of this flush and may proceed
// in parallel while the flush waits.
func (c *Coordinator) Flush(ctx context.Context) (FlushResult, error) {
	c.mu.Lock()
	snapshot := make([]*record, len(c.records))
	copy(snapshot, c.records)
	c.mu.Unlock()

	for _, r := range snapshot {
		select {
		case <-r.decided:
		case <-ctx.Done():
			return FlushResult{}, ctx.Err()
		}
		err, durable, _ := r.snapshot()
		if err != nil {
			return FlushResult{}, err
		}
		if !durable {
			return FlushResult{}, errNotDurable
		}
	}

	for {
		p, err := c.healthyPath(ctx)
		if err != nil {
			return FlushResult{}, err
		}
		res, err := p.Flush(ctx)
		if err == nil {
			return res, nil
		}
		select {
		case <-ctx.Done():
			return FlushResult{}, ctx.Err()
		case <-time.After(noPathBackoff):
		}
	}
}
