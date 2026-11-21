package readymux_test

import (
	"errors"
	"fmt"
	"sync"
	"testing"

	"readymux"
)

func mustAdd(t *testing.T, m *readymux.Mux, sub readymux.Subscription) {
	t.Helper()
	if err := m.Add(sub); err != nil {
		t.Fatalf("Add(%+v): unexpected error: %v", sub, err)
	}
}

func mustReady(t *testing.T, m *readymux.Mux, id int, ready readymux.Events) {
	t.Helper()
	if err := m.SetReady(id, ready); err != nil {
		t.Fatalf("SetReady(%d, %v): unexpected error: %v", id, ready, err)
	}
}

func eventStrings(evs []readymux.Event) []string {
	out := make([]string, len(evs))
	for i, e := range evs {
		out[i] = e.String()
	}
	return out
}

func assertEvents(t *testing.T, got []readymux.Event, want ...string) {
	t.Helper()
	gotStr := eventStrings(got)
	if len(gotStr) != len(want) {
		t.Fatalf("events = %v, want %v", gotStr, want)
	}
	for i := range want {
		if gotStr[i] != want[i] {
			t.Fatalf("events = %v, want %v (mismatch at index %d)", gotStr, want, i)
		}
	}
}

// TestLevelReportsWhileReady covers the basic accepted case: a level
// subscription is reported on every poll while its resource stays ready, and
// the mask is the intersection of readiness and interest.
func TestLevelReportsWhileReady(t *testing.T) {
	m := readymux.New()
	mustAdd(t, m, readymux.Subscription{ID: 7, Interest: readymux.Readable, Mode: readymux.Level})

	if got := m.Poll(4); got != nil {
		t.Fatalf("Poll before readiness = %v, want nil", eventStrings(got))
	}

	mustReady(t, m, 7, readymux.Readable|readymux.Writable)
	assertEvents(t, m.Poll(4), "7:readable")
	assertEvents(t, m.Poll(4), "7:readable")

	mustReady(t, m, 7, readymux.Writable) // interest not satisfied
	if got := m.Poll(4); got != nil {
		t.Fatalf("Poll with interest unsatisfied = %v, want nil", eventStrings(got))
	}
}

// TestLevelInterestMask is the mask boundary: a resource ready for both
// operations reports only the bits the caller subscribed to.
func TestLevelInterestMask(t *testing.T) {
	m := readymux.New()
	mustAdd(t, m, readymux.Subscription{ID: 1, Interest: readymux.Writable, Mode: readymux.Level})
	mustAdd(t, m, readymux.Subscription{ID: 2, Interest: readymux.Readable | readymux.Writable, Mode: readymux.Level})

	mustReady(t, m, 1, readymux.Readable|readymux.Writable)
	mustReady(t, m, 2, readymux.Readable)

	assertEvents(t, m.Poll(10), "1:writable", "2:readable")
}

// TestEdgeOnlyNotifiesNewTransitions checks that edge subscriptions report a
// bit only when it transitions from not-ready to ready, and that repeated
// reports of the same state are silent.
func TestEdgeOnlyNotifiesNewTransitions(t *testing.T) {
	m := readymux.New()
	mustAdd(t, m, readymux.Subscription{ID: 3, Interest: readymux.Readable | readymux.Writable, Mode: readymux.Edge})

	mustReady(t, m, 3, readymux.Readable)
	assertEvents(t, m.Poll(5), "3:readable")

	mustReady(t, m, 3, readymux.Readable) // no new bit
	if got := m.Poll(5); got != nil {
		t.Fatalf("repeated state produced %v, want nil", eventStrings(got))
	}

	mustReady(t, m, 3, readymux.Readable|readymux.Writable)
	assertEvents(t, m.Poll(5), "3:writable")

	mustReady(t, m, 3, 0)
	mustReady(t, m, 3, readymux.Readable|readymux.Writable)
	assertEvents(t, m.Poll(5), "3:readable|writable")
}

// TestEdgeCoalescesBeforePoll is the edge boundary where a bit flaps back and
// forth before the poll: the latched transition is reported once, not twice.
func TestEdgeCoalescesBeforePoll(t *testing.T) {
	m := readymux.New()
	mustAdd(t, m, readymux.Subscription{ID: 4, Interest: readymux.Writable, Mode: readymux.Edge})

	mustReady(t, m, 4, readymux.Writable)
	mustReady(t, m, 4, 0)
	mustReady(t, m, 4, readymux.Writable)

	assertEvents(t, m.Poll(5), "4:writable")
	if got := m.Poll(5); got != nil {
		t.Fatalf("second poll = %v, want nil", eventStrings(got))
	}
}

// TestLevelFairness reproduces the ordering scenario from the requirement: one
// read end is permanently readable while a write end toggles, and repeated
// limited polls must alternate instead of starving the later resource.
func TestLevelFairness(t *testing.T) {
	m := readymux.New()
	mustAdd(t, m, readymux.Subscription{ID: 10, Interest: readymux.Readable | readymux.Writable, Mode: readymux.Level})
	mustAdd(t, m, readymux.Subscription{ID: 20, Interest: readymux.Readable | readymux.Writable, Mode: readymux.Level})

	// Reader 10 is always readable.
	mustReady(t, m, 10, readymux.Readable)

	steps := []struct {
		ready int // resource to update, 0 means "just poll twice"
		state readymux.Events
		want  []string
	}{
		{ready: 20, state: readymux.Writable, want: []string{"10:readable"}},
		{state: 0, want: []string{"20:writable"}},
		{ready: 20, state: 0, want: []string{"10:readable"}},
		// The toggled write end is skipped while not ready; the permanently
		// readable end still rotates to the front.
		{state: 0, want: []string{"10:readable"}},
		{ready: 20, state: readymux.Writable, want: []string{"10:readable"}},
		{state: 0, want: []string{"20:writable"}},
	}

	for i, step := range steps {
		if step.ready != 0 {
			mustReady(t, m, step.ready, step.state)
		}
		got := m.Poll(1)
		if len(step.want) == 0 {
			if got != nil {
				t.Fatalf("step %d: Poll(1) = %v, want nil", i, eventStrings(got))
			}
			continue
		}
		assertEvents(t, got, step.want...)
	}

	// A single poll with a generous limit reports the rotating order without
	// duplicating either level subscription.
	mustReady(t, m, 20, readymux.Writable)
	assertEvents(t, m.Poll(10), "10:readable", "20:writable")
	assertEvents(t, m.Poll(1), "10:readable")
	assertEvents(t, m.Poll(1), "20:writable")
}

// TestOneShotLevelRearmReobserves covers a one-shot subscription being
// disarmed by its first delivery, then re-armed while the resource is still
// ready: the already-ready state must produce a new notification.
func TestOneShotLevelRearmReobserves(t *testing.T) {
	m := readymux.New()
	mustAdd(t, m, readymux.Subscription{ID: 5, Interest: readymux.Readable, Mode: readymux.Level, OneShot: true})

	mustReady(t, m, 5, readymux.Readable)
	assertEvents(t, m.Poll(5), "5:readable")
	if got := m.Poll(5); got != nil {
		t.Fatalf("disarmed one-shot polled again = %v, want nil", eventStrings(got))
	}

	// The resource remains ready; only an explicit re-arm observes it again.
	mustReady(t, m, 5, readymux.Readable)
	if got := m.Poll(5); got != nil {
		t.Fatalf("disarmed one-shot observed readiness = %v, want nil", eventStrings(got))
	}

	if err := m.Rearm(5); err != nil {
		t.Fatalf("Rearm: %v", err)
	}
	assertEvents(t, m.Poll(5), "5:readable")

	// And it disarms itself again after the second delivery.
	assertEvents(t, m.Poll(5)) // no events
}

// TestOneShotEdgeRearmReobserves is the equivalent one-shot case for edge
// mode: re-arming re-reads the stored readiness, so an already-ready resource
// is delivered even without a fresh transition.
func TestOneShotEdgeRearmReobserves(t *testing.T) {
	m := readymux.New()
	mustAdd(t, m, readymux.Subscription{ID: 6, Interest: readymux.Writable, Mode: readymux.Edge, OneShot: true})

	mustReady(t, m, 6, readymux.Writable)
	assertEvents(t, m.Poll(5), "6:writable")
	assertEvents(t, m.Poll(5))

	// The resource goes not-ready and ready again while disarmed. Re-arming
	// must observe the current state even though the old subscription is not
	// listening to transitions.
	mustReady(t, m, 6, 0)
	mustReady(t, m, 6, readymux.Writable)
	assertEvents(t, m.Poll(5))

	if err := m.Rearm(6); err != nil {
		t.Fatalf("Rearm: %v", err)
	}
	assertEvents(t, m.Poll(5), "6:writable")
}

// TestRemoveThenReuseIDDiscardsStaleEntries is the cross-constraint test:
// remove a resource whose event is already queued, immediately reuse the same
// numeric ID for a different object, and confirm the stale queue entry is
// never reported for the new object.
func TestRemoveThenReuseIDDiscardsStaleEntries(t *testing.T) {
	m := readymux.New()
	mustAdd(t, m, readymux.Subscription{ID: 9, Interest: readymux.Readable, Mode: readymux.Level})
	mustReady(t, m, 9, readymux.Readable) // leaves a pending queue entry

	if err := m.Remove(9); err != nil {
		t.Fatalf("Remove: %v", err)
	}

	// Reuse the ID for a subscription that cares about a different mask.
	mustAdd(t, m, readymux.Subscription{ID: 9, Interest: readymux.Writable, Mode: readymux.Level})

	if got := m.Poll(5); got != nil {
		t.Fatalf("stale entry leaked to reused ID: %v", eventStrings(got))
	}

	mustReady(t, m, 9, readymux.Writable)
	assertEvents(t, m.Poll(5), "9:writable")
}

// TestRemoveThenReuseIDWithLiveQueue mixes a stale entry and a fresh one for
// the same numeric ID in a single poll; only the new generation is delivered,
// and only once.
func TestRemoveThenReuseIDWithLiveQueue(t *testing.T) {
	m := readymux.New()
	mustAdd(t, m, readymux.Subscription{ID: 2, Interest: readymux.Readable, Mode: readymux.Edge})
	mustReady(t, m, 2, readymux.Readable) // edge transition latched

	if err := m.Remove(2); err != nil {
		t.Fatalf("Remove: %v", err)
	}
	mustAdd(t, m, readymux.Subscription{ID: 2, Interest: readymux.Writable, Mode: readymux.Edge})
	mustReady(t, m, 2, readymux.Writable)

	assertEvents(t, m.Poll(10), "2:writable")
	assertEvents(t, m.Poll(10))
}

// TestRemoveDropsQueuedLevelEntry confirms that after Remove the removed ID is
// reported as unregistered and its remaining queue entries are inert.
func TestRemoveDropsQueuedLevelEntry(t *testing.T) {
	m := readymux.New()
	mustAdd(t, m, readymux.Subscription{ID: 8, Interest: readymux.Readable, Mode: readymux.Level})
	mustReady(t, m, 8, readymux.Readable)

	if err := m.Remove(8); err != nil {
		t.Fatalf("Remove: %v", err)
	}
	if err := m.Remove(8); !errors.Is(err, readymux.ErrNotRegistered) {
		t.Fatalf("second Remove error = %v, want ErrNotRegistered", err)
	}
	if got := m.Poll(5); got != nil {
		t.Fatalf("Poll after Remove = %v, want nil", eventStrings(got))
	}
	if err := m.SetReady(8, readymux.Readable); !errors.Is(err, readymux.ErrNotRegistered) {
		t.Fatalf("SetReady after Remove error = %v, want ErrNotRegistered", err)
	}
}

// TestRejectedInputs covers the results the API must refuse.
func TestRejectedInputs(t *testing.T) {
	m := readymux.New()
	mustAdd(t, m, readymux.Subscription{ID: 1, Interest: readymux.Readable, Mode: readymux.Level})

	cases := []struct {
		name string
		err  error
		want error
	}{
		{"duplicate id", m.Add(readymux.Subscription{ID: 1, Interest: readymux.Readable, Mode: readymux.Level}), readymux.ErrDuplicateID},
		{"empty interest", m.Add(readymux.Subscription{ID: 2, Mode: readymux.Level}), readymux.ErrInvalidInterest},
		{"unknown interest bits", m.Add(readymux.Subscription{ID: 2, Interest: readymux.Events(8), Mode: readymux.Level}), readymux.ErrInvalidInterest},
		{"bad mode", m.Add(readymux.Subscription{ID: 2, Interest: readymux.Readable, Mode: readymux.Mode(9)}), readymux.ErrInvalidMode},
		{"unknown readiness bits", m.SetReady(1, readymux.Events(8)), readymux.ErrInvalidEvents},
		{"ready unknown id", m.SetReady(99, readymux.Readable), readymux.ErrNotRegistered},
		{"rearm unknown id", m.Rearm(99), readymux.ErrNotRegistered},
		{"remove unknown id", m.Remove(99), readymux.ErrNotRegistered},
	}
	for _, tc := range cases {
		if !errors.Is(tc.err, tc.want) {
			t.Errorf("%s: error = %v, want %v", tc.name, tc.err, tc.want)
		}
	}

	// None of the rejected Adds occupied an ID.
	mustAdd(t, m, readymux.Subscription{ID: 2, Interest: readymux.Readable, Mode: readymux.Level})
}

// TestPollLimitBoundaries covers limit handling: zero/negative limits consume
// nothing, and a limit larger than the ready set does not duplicate a level
// subscription within one poll.
func TestPollLimitBoundaries(t *testing.T) {
	m := readymux.New()
	mustAdd(t, m, readymux.Subscription{ID: 1, Interest: readymux.Readable, Mode: readymux.Level})
	mustReady(t, m, 1, readymux.Readable)

	if got := m.Poll(0); got != nil {
		t.Fatalf("Poll(0) = %v, want nil", eventStrings(got))
	}
	if got := m.Poll(-3); got != nil {
		t.Fatalf("Poll(-3) = %v, want nil", eventStrings(got))
	}
	assertEvents(t, m.Poll(100), "1:readable")
	// The level subscription was re-queued exactly once.
	assertEvents(t, m.Poll(100), "1:readable")
}

// TestEventsString exercises the helper rendering used in diagnostics.
func TestEventsString(t *testing.T) {
	cases := []struct {
		e    readymux.Events
		want string
	}{
		{0, "none"},
		{readymux.Readable, "readable"},
		{readymux.Writable, "writable"},
		{readymux.Readable | readymux.Writable, "readable|writable"},
		{readymux.Events(8), "0x8"},
	}
	for _, tc := range cases {
		if got := tc.e.String(); got != tc.want {
			t.Errorf("Events(%d).String() = %q, want %q", uint32(tc.e), got, tc.want)
		}
	}
	if got := (readymux.Mode(7)).String(); got != "mode(7)" {
		t.Errorf("Mode(7).String() = %q", got)
	}
}

// TestZeroValueAndConcurrentUse checks the documented zero-value behaviour and
// that concurrent producers and pollers do not corrupt the queue.
func TestZeroValueAndConcurrentUse(t *testing.T) {
	var m readymux.Mux
	const resources = 8
	for i := 0; i < resources; i++ {
		mustAdd(t, &m, readymux.Subscription{ID: i, Interest: readymux.Readable, Mode: readymux.Level})
	}

	var wg sync.WaitGroup
	for i := 0; i < resources; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			for n := 0; n < 50; n++ {
				if n%2 == 0 {
					_ = m.SetReady(id, readymux.Readable)
				} else {
					_ = m.SetReady(id, 0)
				}
			}
		}(i)
	}
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for n := 0; n < 50; n++ {
				for _, ev := range m.Poll(2) {
					if ev.ID < 0 || ev.ID >= resources {
						t.Errorf("poll returned out-of-range id %d", ev.ID)
					}
					if ev.Events&^readymux.Readable != 0 {
						t.Errorf("poll returned unexpected mask %v", ev.Events)
					}
				}
			}
		}()
	}
	wg.Wait()
}

func ExampleMux() {
	m := readymux.New()
	_ = m.Add(readymux.Subscription{ID: 1, Interest: readymux.Readable, Mode: readymux.Level})
	_ = m.SetReady(1, readymux.Readable|readymux.Writable)
	for _, ev := range m.Poll(1) {
		fmt.Println(ev)
	}
	// Output: 1:readable
}
