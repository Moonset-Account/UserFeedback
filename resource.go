package coordination

import (
	"errors"
	"fmt"
	"sync"
	"time"
)

var (
	// ErrStaleHolder is returned when a write carries a fencing token smaller
	// than the highest token the resource has already accepted.
	ErrStaleHolder = errors.New("coordination: stale holder rejected")
	// ErrInvalidFence is returned for a zero fencing token.
	ErrInvalidFence = errors.New("coordination: invalid fencing token")
)

// WriteRecord is one completed or rejected write attempt.
type WriteRecord struct {
	SessionID string
	Token     uint64
	Data      string
	Accepted  bool
	Reason    string
}

// Resource is the protected resource. It applies fencing tokens: after a newer
// holder's first write, every write carrying a smaller sequence is rejected,
// even if the older process has not yet noticed that its session expired.
//
// The Resource knows nothing about sessions on purpose. A stale holder cannot
// escape rejection by believing it is still alive; only the monotonic token
// matters.
type Resource struct {
	mu        sync.Mutex
	highWater uint64
	holder    string
	data      string
	ver       uint64
	history   []WriteRecord

	// AcquireTimes records when each fencing token was granted, in order.
	grants []Grant
}

// Grant is one successful hand-off recorded by the resource.
type Grant struct {
	SessionID string
	Token     uint64
	At        time.Time
}

// NewResource returns an empty protected resource.
func NewResource() *Resource {
	return &Resource{}
}

// Acquire registers a newly elected holder with the resource. It fails only if
// the token is not positive or is already superseded by a newer write. Acquire
// itself does not raise the write barrier: the barrier moves when the new holder
// performs its first write.
func (r *Resource) Acquire(sessionID string, token uint64) error {
	if token == 0 {
		return ErrInvalidFence
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if token < r.highWater {
		return fmt.Errorf("%w: token %d < high-water %d", ErrStaleHolder, token, r.highWater)
	}
	r.grants = append(r.grants, Grant{SessionID: sessionID, Token: token, At: time.Now()})
	return nil
}

// Write attempts a fenced write. A write is accepted when token >= highWater;
// it then raises highWater to token. A write with token < highWater is rejected
// with ErrStaleHolder and leaves the resource data untouched.
func (r *Resource) Write(sessionID string, token uint64, data string) error {
	if token == 0 {
		return ErrInvalidFence
	}
	r.mu.Lock()
	defer r.mu.Unlock()

	if token < r.highWater {
		r.history = append(r.history, WriteRecord{
			SessionID: sessionID,
			Token:     token,
			Data:      data,
			Accepted:  false,
			Reason:    fmt.Sprintf("token %d below fence %d", token, r.highWater),
		})
		return fmt.Errorf("%w: token %d < high-water %d", ErrStaleHolder, token, r.highWater)
	}

	r.highWater = token
	r.holder = sessionID
	r.data = data
	r.ver++
	r.history = append(r.history, WriteRecord{
		SessionID: sessionID,
		Token:     token,
		Data:      data,
		Accepted:  true,
	})
	return nil
}

// Fence returns the highest accepted fencing token, i.e. the write barrier.
func (r *Resource) Fence() uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.highWater
}

// Holder returns the session that performed the most recent accepted write.
func (r *Resource) Holder() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.holder
}

// Data returns the current resource payload.
func (r *Resource) Data() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.data
}

// Version increments on every accepted write.
func (r *Resource) Version() uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.ver
}

// History returns a copy of every write attempt, accepted or rejected.
func (r *Resource) History() []WriteRecord {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]WriteRecord, len(r.history))
	copy(out, r.history)
	return out
}

// Grants returns a copy of the hand-offs registered with the resource.
func (r *Resource) Grants() []Grant {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]Grant, len(r.grants))
	copy(out, r.grants)
	return out
}

// TakeOver performs the client side of the hand-off: it tells the resource that
// this contender now holds the lock with its current fencing token, then performs
// the new holder's first write. It returns an error if the hand-off itself is
// stale or if the first write is rejected.
func (c *Contender) TakeOver(res *Resource, firstWrite string) error {
	if res == nil {
		return errors.New("coordination: nil resource")
	}
	token := c.Fence()
	if token == 0 {
		return errors.New("coordination: contender has no fencing token")
	}
	if err := res.Acquire(c.SessionID(), token); err != nil {
		return err
	}
	return res.Write(c.SessionID(), token, firstWrite)
}

// WriteToResource attempts a write using this contender's fencing token. Unlike
// TakeOver it does not call Acquire; it is the path a stale holder takes when it
// finally notices (or fails to notice) its own expiry.
func (c *Contender) WriteToResource(res *Resource, data string) error {
	if res == nil {
		return errors.New("coordination: nil resource")
	}
	token := c.Fence()
	if token == 0 {
		return errors.New("coordination: contender has no fencing token")
	}
	return res.Write(c.SessionID(), token, data)
}
