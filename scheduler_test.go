package drr

import (
	"errors"
	"reflect"
	"testing"
)

func mustScheduler(t *testing.T, order []string, quanta map[string]int) *Scheduler {
	t.Helper()
	s, err := NewScheduler(order, quanta)
	if err != nil {
		t.Fatalf("NewScheduler(%v, %v) unexpected error: %v", order, quanta, err)
	}
	return s
}

func mustEnqueue(t *testing.T, s *Scheduler, queueID string, p Packet) {
	t.Helper()
	if err := s.Enqueue(queueID, p); err != nil {
		t.Fatalf("Enqueue(%q, %+v) unexpected error: %v", queueID, p, err)
	}
}

// Acceptable result: two queues with different quanta, one packet larger
// than a whole round's quantum. Verifies the exact emission sequence of
// (queue, packet, remaining balance).
func TestSchedulerByteAccountingSequence(t *testing.T) {
	s := mustScheduler(t, []string{"A", "B"}, map[string]int{"A": 100, "B": 150})
	mustEnqueue(t, s, "A", Packet{ID: 1, Size: 250}) // bigger than A's quantum
	mustEnqueue(t, s, "A", Packet{ID: 2, Size: 50})
	mustEnqueue(t, s, "B", Packet{ID: 1, Size: 100})
	mustEnqueue(t, s, "B", Packet{ID: 2, Size: 200})

	got := s.Next(10)
	want := []Event{
		// Round 1: A credited 100 < 250, keeps balance. B credited 150, sends b1.
		{QueueID: "B", PacketID: 1, Remaining: 50},
		// Round 2: A 200 < 250. B 50+150 = 200, sends b2, drains, resets.
		{QueueID: "B", PacketID: 2, Remaining: 0},
		// Round 3: A 200+100 = 300, sends a1 then a2 back to back.
		{QueueID: "A", PacketID: 1, Remaining: 50},
		{QueueID: "A", PacketID: 2, Remaining: 0},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Next(10) = %+v, want %+v", got, want)
	}
	if rest := s.Next(10); len(rest) != 0 {
		t.Fatalf("Next after drain = %+v, want empty", rest)
	}
}

// Rejected results: invalid configuration and invalid inputs must be
// refused with the documented errors.
func TestSchedulerRejectsInvalidInput(t *testing.T) {
	cases := []struct {
		name   string
		order  []string
		quanta map[string]int
		want   error
	}{
		{"empty order", nil, map[string]int{}, ErrNoQueues},
		{"duplicate queue", []string{"A", "A"}, map[string]int{"A": 100}, ErrDuplicateQueue},
		{"missing quantum", []string{"A", "B"}, map[string]int{"A": 100}, ErrMissingQuantum},
		{"zero quantum", []string{"A"}, map[string]int{"A": 0}, ErrInvalidQuantum},
		{"negative quantum", []string{"A"}, map[string]int{"A": -5}, ErrInvalidQuantum},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := NewScheduler(tc.order, tc.quanta); !errors.Is(err, tc.want) {
				t.Fatalf("NewScheduler error = %v, want %v", err, tc.want)
			}
		})
	}

	s := mustScheduler(t, []string{"A"}, map[string]int{"A": 100})
	if err := s.Enqueue("ghost", Packet{ID: 1, Size: 10}); !errors.Is(err, ErrUnknownQueue) {
		t.Fatalf("Enqueue unknown queue error = %v, want %v", err, ErrUnknownQueue)
	}
	if err := s.Enqueue("A", Packet{ID: 1, Size: 0}); !errors.Is(err, ErrInvalidSize) {
		t.Fatalf("Enqueue zero size error = %v, want %v", err, ErrInvalidSize)
	}
	if err := s.Enqueue("A", Packet{ID: 1, Size: -1}); !errors.Is(err, ErrInvalidSize) {
		t.Fatalf("Enqueue negative size error = %v, want %v", err, ErrInvalidSize)
	}
}

// Interleaved scenario: draining N packets in one call must produce the
// same sequence as N calls of one packet each, because a paused call
// resumes the same queue with the same balance.
func TestSchedulerBatchEqualsOneByOne(t *testing.T) {
	build := func() *Scheduler {
		s := mustScheduler(t,
			[]string{"A", "B", "C"},
			map[string]int{"A": 100, "B": 60, "C": 250})
		mustEnqueue(t, s, "A", Packet{ID: 1, Size: 90})
		mustEnqueue(t, s, "A", Packet{ID: 2, Size: 120})
		mustEnqueue(t, s, "A", Packet{ID: 3, Size: 30})
		mustEnqueue(t, s, "B", Packet{ID: 1, Size: 60})
		mustEnqueue(t, s, "B", Packet{ID: 2, Size: 61}) // just over one quantum
		mustEnqueue(t, s, "C", Packet{ID: 1, Size: 700})
		mustEnqueue(t, s, "C", Packet{ID: 2, Size: 10})
		return s
	}

	const total = 7
	batch := build()
	gotBatch := batch.Next(total)

	trickled := build()
	var gotTrickled []Event
	for i := 0; i < total; i++ {
		gotTrickled = append(gotTrickled, trickled.Next(1)...)
	}

	if len(gotBatch) != total {
		t.Fatalf("Next(%d) emitted %d events, want %d", total, len(gotBatch), total)
	}
	if !reflect.DeepEqual(gotBatch, gotTrickled) {
		t.Fatalf("batch = %+v\ntrickled = %+v\nwant identical sequences", gotBatch, gotTrickled)
	}
}

// Boundary: a forced pause while credit still covers the head packet
// must resume the same queue with the same balance — no fresh quantum.
func TestSchedulerResumeKeepsBalance(t *testing.T) {
	s := mustScheduler(t, []string{"A"}, map[string]int{"A": 1000})
	mustEnqueue(t, s, "A", Packet{ID: 1, Size: 100})
	mustEnqueue(t, s, "A", Packet{ID: 2, Size: 100})
	mustEnqueue(t, s, "A", Packet{ID: 3, Size: 100})

	got := s.Next(2)
	want := []Event{
		{QueueID: "A", PacketID: 1, Remaining: 900},
		{QueueID: "A", PacketID: 2, Remaining: 800},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Next(2) = %+v, want %+v", got, want)
	}
	// Resume: balance 800 still covers the head, so no new quantum is
	// granted; remaining must be 700, not 1700.
	got = s.Next(1)
	want = []Event{{QueueID: "A", PacketID: 3, Remaining: 700}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Next(1) after pause = %+v, want %+v", got, want)
	}
}

// Boundary: a queue drained and then refilled restarts from zero credit.
func TestSchedulerRefilledQueueStartsFromZero(t *testing.T) {
	s := mustScheduler(t, []string{"A"}, map[string]int{"A": 1000})
	mustEnqueue(t, s, "A", Packet{ID: 1, Size: 100})
	if got := s.Next(1); !reflect.DeepEqual(got, []Event{{QueueID: "A", PacketID: 1, Remaining: 900}}) {
		t.Fatalf("first Next(1) = %+v", got)
	}
	// Queue is now drained; its 900 leftover balance must be discarded.
	mustEnqueue(t, s, "A", Packet{ID: 2, Size: 500})
	got := s.Next(1)
	want := []Event{{QueueID: "A", PacketID: 2, Remaining: 500}} // 0+1000-500, not 900+1000-500
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Next(1) after refill = %+v, want %+v", got, want)
	}
}

// Boundary: a packet bigger than one quantum is sent only after the
// balance accumulates over multiple rounds within a single Next call.
func TestSchedulerPacketLargerThanQuantum(t *testing.T) {
	s := mustScheduler(t, []string{"A"}, map[string]int{"A": 100})
	mustEnqueue(t, s, "A", Packet{ID: 1, Size: 250})
	got := s.Next(1)
	want := []Event{{QueueID: "A", PacketID: 1, Remaining: 50}} // 3 rounds of credit: 300-250
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Next(1) = %+v, want %+v", got, want)
	}
}

// Boundary: non-positive limits and an empty scheduler emit nothing.
func TestSchedulerEmptyAndZeroLimit(t *testing.T) {
	s := mustScheduler(t, []string{"A"}, map[string]int{"A": 100})
	if got := s.Next(5); len(got) != 0 {
		t.Fatalf("Next on empty scheduler = %+v, want empty", got)
	}
	mustEnqueue(t, s, "A", Packet{ID: 1, Size: 10})
	if got := s.Next(0); len(got) != 0 {
		t.Fatalf("Next(0) = %+v, want empty", got)
	}
	if got := s.Next(-3); len(got) != 0 {
		t.Fatalf("Next(-3) = %+v, want empty", got)
	}
	// The packet must still be there after the zero-limit calls.
	got := s.Next(1)
	want := []Event{{QueueID: "A", PacketID: 1, Remaining: 90}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("Next(1) = %+v, want %+v", got, want)
	}
}
