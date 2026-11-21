// Package drr implements a byte-accounting deficit round-robin (DRR)
// packet scheduler over multiple FIFO queues.
//
// Each queue has its own quantum. Whenever a non-empty queue gets its
// turn in the caller-specified rotation order, its balance is credited
// by one quantum; while the balance covers the head packet, packets are
// sent back to back. When the scheduler leaves a queue that is still
// non-empty, the leftover balance is kept; a queue that has been drained
// has its balance reset to zero, so a queue refilled after being emptied
// restarts from zero credit.
//
// The downstream may accept only a limited number of packets per call.
// If a call must pause while credit remains, the next call resumes from
// the same queue with the same balance (no extra credit is granted).
// Therefore draining N packets in one call yields exactly the same
// sequence as N calls of one packet each.
package drr

import "errors"

// Packet is a unit of work waiting in a queue. Size is in bytes.
type Packet struct {
	ID   int
	Size int
}

// Event describes one emitted packet: which queue it came from, the
// packet ID, and the queue's remaining balance right after the send.
type Event struct {
	QueueID   string
	PacketID  int
	Remaining int
}

var (
	ErrNoQueues       = errors.New("drr: rotation order must not be empty")
	ErrDuplicateQueue = errors.New("drr: duplicate queue in rotation order")
	ErrMissingQuantum = errors.New("drr: missing quantum for queue in rotation order")
	ErrInvalidQuantum = errors.New("drr: quantum must be positive")
	ErrUnknownQueue   = errors.New("drr: unknown queue")
	ErrInvalidSize    = errors.New("drr: packet size must be positive")
)

type queue struct {
	quantum int
	pending []Packet
	balance int
}

// Scheduler is a resumable byte-accounting DRR scheduler.
type Scheduler struct {
	order   []string
	queues  map[string]*queue
	pos     int  // index into order where service resumes
	serving bool // queue at pos was already credited this turn
}

// NewScheduler builds a scheduler whose queues rotate in the given
// caller-specified order. Every queue in order must have a positive
// quantum in quanta; duplicates and empty orders are rejected.
func NewScheduler(order []string, quanta map[string]int) (*Scheduler, error) {
	if len(order) == 0 {
		return nil, ErrNoQueues
	}
	queues := make(map[string]*queue, len(order))
	for _, id := range order {
		if _, dup := queues[id]; dup {
			return nil, ErrDuplicateQueue
		}
		q, ok := quanta[id]
		if !ok {
			return nil, ErrMissingQuantum
		}
		if q <= 0 {
			return nil, ErrInvalidQuantum
		}
		queues[id] = &queue{quantum: q}
	}
	return &Scheduler{
		order:  append([]string(nil), order...),
		queues: queues,
	}, nil
}

// Enqueue appends a packet to the tail of a queue's FIFO. The queue
// must be part of the rotation order and the size must be positive.
func (s *Scheduler) Enqueue(queueID string, p Packet) error {
	q, ok := s.queues[queueID]
	if !ok {
		return ErrUnknownQueue
	}
	if p.Size <= 0 {
		return ErrInvalidSize
	}
	q.pending = append(q.pending, p)
	return nil
}

// Next emits up to n packets, honoring the downstream limit. It returns
// one Event per emitted packet, in emission order. State is preserved
// between calls: if the call pauses while the current queue still has
// usable credit, the next call resumes that same queue with the same
// balance instead of granting a fresh quantum.
func (s *Scheduler) Next(n int) []Event {
	if n <= 0 {
		return nil
	}
	events := make([]Event, 0, n)
	for len(events) < n {
		ev, ok := s.step()
		if !ok {
			break
		}
		events = append(events, ev)
	}
	return events
}

// step emits exactly one packet, advancing rounds as needed. It reports
// false only when every queue is empty.
func (s *Scheduler) step() (Event, bool) {
	for {
		hasWork := false
		for scanned := 0; scanned < len(s.order); scanned++ {
			id := s.order[s.pos]
			q := s.queues[id]
			if len(q.pending) == 0 {
				// A drained queue loses its balance; if it is
				// refilled later it restarts from zero credit.
				q.balance = 0
				s.serving = false
				s.pos = (s.pos + 1) % len(s.order)
				continue
			}
			hasWork = true
			if !s.serving {
				q.balance += q.quantum
				s.serving = true
			}
			head := q.pending[0]
			if q.balance < head.Size {
				// Leave the queue but keep the remaining balance.
				s.serving = false
				s.pos = (s.pos + 1) % len(s.order)
				continue
			}
			q.pending = q.pending[1:]
			q.balance -= head.Size
			ev := Event{QueueID: id, PacketID: head.ID, Remaining: q.balance}
			if len(q.pending) == 0 {
				q.balance = 0
				s.serving = false
				s.pos = (s.pos + 1) % len(s.order)
			}
			// Otherwise stay on this queue with serving=true so the
			// next step continues sending while the balance suffices.
			return ev, true
		}
		if !hasWork {
			return Event{}, false
		}
		// No queue could send this round; balances have accumulated,
		// so run another round.
	}
}
