package sigdispatch

import (
	"sync"
	"testing"
)

// ---------------------------------------------------------------------------
// Normal-path semantics: coalescing standard signals, queuing real-time ones.
// ---------------------------------------------------------------------------

func TestStandardSignalCoalescesAndKeepsFirstPayload(t *testing.T) {
	rt := NewRuntime()
	th := rt.NewThread()

	first := rt.SendProcess(2, 100, "first")
	rt.SendProcess(2, 200, "second")
	rt.SendProcess(2, 300, "third")

	view := rt.ProcessPending()
	if len(view.Standard) != 1 {
		t.Fatalf("standard pending entries = %d, want 1", len(view.Standard))
	}
	got, ok := view.Standard[2]
	if !ok {
		t.Fatalf("signal 2 missing from process pending view")
	}
	if got != first {
		t.Fatalf("kept payload = %+v, want first occurrence %+v", got, first)
	}

	s, p, ok := th.Take()
	if !ok || s != 2 || p != first {
		t.Fatalf("Take = (%d, %+v, %v), want (2, first, true)", s, p, ok)
	}
	if _, _, ok := th.Take(); ok {
		t.Fatalf("coalesced duplicates must not be deliverable")
	}
}

func TestRealtimeSignalsQueueEveryOccurrenceInOrder(t *testing.T) {
	rt := NewRuntime()
	th := rt.NewThread()
	r := RTMin + 1

	sent := make([]Payload, 0, 4)
	for i := 0; i < 4; i++ {
		sent = append(sent, rt.SendProcess(r, i, i))
	}

	view := rt.ProcessPending()
	q := view.Realtime[r]
	if len(q) != 4 {
		t.Fatalf("queued real-time entries = %d, want 4", len(q))
	}
	for i, p := range q {
		if p != sent[i] {
			t.Fatalf("queue position %d = %+v, want %+v", i, p, sent[i])
		}
	}

	for i := 0; i < 4; i++ {
		s, p, ok := th.Take()
		if !ok || s != r || p != sent[i] {
			t.Fatalf("take %d = (%d, %+v, %v), want (%d, %+v, true)", i, s, p, ok, r, sent[i])
		}
	}
	if _, _, ok := th.Take(); ok {
		t.Fatalf("queue must be empty after draining")
	}
}

func TestStandardAndRealtimePendingAreIndependent(t *testing.T) {
	rt := NewRuntime()
	rt.SendProcess(4, 1, nil)
	rt.SendProcess(RTMin, 1, nil)
	rt.SendProcess(RTMin, 2, nil)

	view := rt.ProcessPending()
	if len(view.Standard) != 1 || len(view.Realtime) != 1 || len(view.Realtime[RTMin]) != 2 {
		t.Fatalf("unexpected pending view: %+v", view)
	}
}

// ---------------------------------------------------------------------------
// Boundary: private queues, masks, smallest selection, one-consumer rule.
// ---------------------------------------------------------------------------

func TestThreadPrivateQueueIsIndependent(t *testing.T) {
	rt := NewRuntime()
	th := rt.NewThread()

	first := th.SendThread(9, 1, "private")
	th.SendThread(9, 2, "dropped-dup")

	if len(rt.ProcessPending().Standard) != 0 {
		t.Fatalf("thread-directed signal leaked into process queue")
	}
	pv := th.Pending()
	if pv.Standard[9] != first {
		t.Fatalf("private pending = %+v, want first payload %+v", pv.Standard, first)
	}

	s, p, ok := th.Take()
	if !ok || s != 9 || p != first {
		t.Fatalf("private Take = (%d, %+v, %v), want (9, first, true)", s, p, ok)
	}
	if _, _, ok := th.Take(); ok {
		t.Fatalf("private duplicate should have coalesced")
	}
}

func TestTakeChoosesSmallestUnblockedAndPrivateWinsOnTie(t *testing.T) {
	rt := NewRuntime()
	th := rt.NewThread()

	priv7 := th.SendThread(7, 1, "private-7")
	proc7 := rt.SendProcess(7, 2, "process-7")
	proc5 := rt.SendProcess(5, 3, "process-5")

	// Signal 5 is the smallest pending number but is blocked: skipped.
	// On number 7 the thread-private entry must win the tie.
	th.Block(5)
	s, p, ok := th.Take()
	if !ok || s != 7 || p != priv7 {
		t.Fatalf("first Take = (%d, %+v, %v), want private 7", s, p, ok)
	}
	if _, still := rt.ProcessPending().Standard[7]; !still || rt.ProcessPending().Standard[7] != proc7 {
		t.Fatalf("process-wide 7 must remain after private 7 was taken")
	}

	s, p, ok = th.Take()
	if !ok || s != 7 || p != proc7 {
		t.Fatalf("second Take = (%d, %+v, %v), want process 7", s, p, ok)
	}
	if _, _, ok := th.Take(); ok {
		t.Fatalf("blocked signal 5 must not be deliverable while masked")
	}

	th.Unblock(5)
	s, p, ok = th.Take()
	if !ok || s != 5 || p != proc5 {
		t.Fatalf("third Take = (%d, %+v, %v), want process 5 after unblock", s, p, ok)
	}
	if _, _, ok := th.Take(); ok {
		t.Fatalf("nothing should remain pending")
	}
}

func TestProcessSignalGoesToExactlyOneThread(t *testing.T) {
	rt := NewRuntime()
	a := rt.NewThread()
	b := rt.NewThread()

	want := rt.SendProcess(3, 7, "once")

	s, p, ok := a.Take()
	if !ok || s != 3 || p != want {
		t.Fatalf("thread A Take = (%d, %+v, %v), want the single process item", s, p, ok)
	}
	if _, _, ok := b.Take(); ok {
		t.Fatalf("thread B received a process signal already consumed by A")
	}
}

func TestBlockedThreadLeavesProcessSignalForAnotherThread(t *testing.T) {
	rt := NewRuntime()
	a := rt.NewThread()
	b := rt.NewThread()
	a.Block(11)
	want := rt.SendProcess(11, 0, nil)

	if _, _, ok := a.Take(); ok {
		t.Fatalf("thread A took a signal it has blocked")
	}
	s, p, ok := b.Take()
	if !ok || s != 11 || p != want {
		t.Fatalf("thread B Take = (%d, %+v, %v), want the process item", s, p, ok)
	}
}

func TestDispatchWithoutHandlerStillConsumesAndRestoresMask(t *testing.T) {
	rt := NewRuntime()
	th := rt.NewThread()
	rt.SendProcess(8, 0, nil)

	if !th.Dispatch() {
		t.Fatalf("Dispatch reported no signal while one was pending")
	}
	if th.Dispatch() {
		t.Fatalf("Dispatch should report empty after consuming the only item")
	}
	if len(th.Mask()) != 0 {
		t.Fatalf("mask leaked after handler frame: %v", th.Mask())
	}
}

// ---------------------------------------------------------------------------
// Interleaving: nested handler frames and temporary masks.
// ---------------------------------------------------------------------------

func TestHandlerMaskNestingAndRestoration(t *testing.T) {
	rt := NewRuntime()
	th := rt.NewThread()

	var mu sync.Mutex
	var events []string
	snapshot := func(tag string) {
		mu.Lock()
		defer mu.Unlock()
		events = append(events, tag)
	}

	// Handler 1 blocks signal 2 while running; signal 3 stays unblocked
	// and is dispatched recursively from inside it.
	rt.SetHandler(1, Handler{
		Mask: []Sig{2},
		Fn: func(tt *Thread, s Sig, _ Payload) {
			snapshot("outer-enter")
			if !tt.Blocked(1) || !tt.Blocked(2) {
				t.Errorf("outer frame: self signal 1 and mask signal 2 must be blocked")
			}
			if tt.Blocked(3) {
				t.Errorf("outer frame: unrelated signal 3 must stay unblocked")
			}
			// A fresh occurrence of the in-flight standard signal is
			// recorded but cannot be selected while the signal is blocked.
			fresh := rt.SendProcess(1, 9, "fresh")
			// The blocked signal 2 stays pending for later as well.
			rt.SendProcess(2, 0, nil)
			inside := rt.ProcessPending()
			if inside.Standard[1] != fresh {
				t.Errorf("blocked fresh signal 1 must remain pending inside its handler")
			}
			if _, ok := inside.Standard[2]; !ok {
				t.Errorf("mask-blocked signal 2 must remain pending inside the outer frame")
			}
			// Nested dispatch of the unblocked signal 3, which was
			// already pending before the outer frame was entered.
			if !tt.Dispatch() {
				t.Errorf("nested Dispatch found no pending signal 3")
			}
			if got := tt.Mask(); len(got) != 2 || got[0] != 1 || got[1] != 2 {
				t.Errorf("mask after inner frame = %v, want [1 2]", got)
			}
			snapshot("outer-exit")
			_ = fresh
		},
	})
	rt.SetHandler(3, Handler{
		Mask: []Sig{4},
		Fn: func(tt *Thread, s Sig, _ Payload) {
			snapshot("inner-enter")
			got := tt.Mask()
			want := []Sig{1, 2, 3, 4}
			if len(got) != len(want) {
				t.Errorf("inner mask = %v, want %v", got, want)
				return
			}
			for i := range want {
				if got[i] != want[i] {
					t.Errorf("inner mask = %v, want %v", got, want)
					return
				}
			}
		},
	})

	rt.SendProcess(1, 0, "original")
	rt.SendProcess(3, 0, nil)
	if !th.Dispatch() {
		t.Fatalf("outer Dispatch found no pending signal")
	}

	wantOrder := []string{"outer-enter", "inner-enter", "outer-exit"}
	if len(events) != len(wantOrder) {
		t.Fatalf("handler events = %v, want %v", events, wantOrder)
	}
	for i := range wantOrder {
		if events[i] != wantOrder[i] {
			t.Fatalf("handler events = %v, want %v", events, wantOrder)
		}
	}
	if len(th.Mask()) != 0 {
		t.Fatalf("mask after all frames = %v, want empty", th.Mask())
	}

	// The fresh occurrence of signal 1 sent inside its own handler is
	// delivered first now that the frame has restored the previous mask
	// (smallest unblocked number wins).
	s, p, ok := th.Take()
	if !ok || s != 1 || p.Data != "fresh" {
		t.Fatalf("post-frame Take = (%d, %+v, %v), want fresh signal 1", s, p, ok)
	}
	// Signal 2 was blocked during the outer frame and follows next.
	s, _, ok = th.Take()
	if !ok || s != 2 {
		t.Fatalf("post-frame Take = (%d, %v), want deferred signal 2", s, ok)
	}
	if _, _, ok := th.Take(); ok {
		t.Fatalf("all signals should have been consumed")
	}
}

// ---------------------------------------------------------------------------
// The headline scenario: two threads blocking different numbers, then
// accounting for the surviving standard payload and the RT queue length.
// ---------------------------------------------------------------------------

func TestTwoThreadsDifferentMasksFullScenario(t *testing.T) {
	rt := NewRuntime()
	threadA := rt.NewThread()
	threadB := rt.NewThread()

	const (
		sig1  Sig = 1 // standard, blocked by B
		sig2  Sig = 2 // standard, blocked by A
		rtSig     = RTMin + 7
	)
	threadA.Block(sig2)
	threadB.Block(sig1)

	// Three standard occurrences of each standard signal: only the
	// first payload of each may survive.
	s1First := rt.SendProcess(sig1, 1, "s1-1")
	rt.SendProcess(sig1, 2, "s1-2")
	rt.SendProcess(sig1, 3, "s1-3")
	s2First := rt.SendProcess(sig2, 1, "w1-1")
	rt.SendProcess(sig2, 2, "w1-2")

	// Three real-time occurrences: all three must remain queued.
	rtBatch := make([]Payload, 0, 3)
	for i := 0; i < 3; i++ {
		rtBatch = append(rtBatch, rt.SendProcess(rtSig, i, i))
	}

	initial := rt.ProcessPending()
	if len(initial.Standard) != 2 || initial.Standard[sig1] != s1First || initial.Standard[sig2] != s2First {
		t.Fatalf("standard coalescing wrong: %+v", initial.Standard)
	}
	if len(initial.Realtime[rtSig]) != 3 {
		t.Fatalf("real-time queue length = %d, want 3", len(initial.Realtime[rtSig]))
	}

	// A cannot take sig2; sig1 is unblocked and is the smallest number.
	s, p, ok := threadA.Take()
	if !ok || s != sig1 || p != s1First {
		t.Fatalf("A first take = (%d, %+v, %v), want first payload of sig1", s, p, ok)
	}
	if _, remains := rt.ProcessPending().Standard[sig1]; remains {
		t.Fatalf("sig1 duplicates should have vanished with the first item")
	}

	// sig2 (number 2) is smaller than the RT number and unblocked on B:
	// B must receive the coalesced first payload of sig2 first.
	s, p, ok = threadB.Take()
	if !ok || s != sig2 || p != s2First {
		t.Fatalf("B first take = (%d, %+v, %v), want first payload of sig2", s, p, ok)
	}

	// Only the three RT copies remain; A and B drain them alternately in
	// strict arrival order.
	for i, owner := range []*Thread{threadA, threadB, threadA} {
		s, p, ok = owner.Take()
		if !ok || s != rtSig || p != rtBatch[i] {
			t.Fatalf("RT take %d by thread %d = (%d, %+v, %v), want %+v",
				i, owner.ID(), s, p, ok, rtBatch[i])
		}
		if left := len(rt.ProcessPending().Realtime[rtSig]); left != 2-i {
			t.Fatalf("RT queue length after take %d = %d, want %d", i, left, 2-i)
		}
	}

	// Every queue is now empty, so neither thread has anything to take.
	if _, _, ok := threadA.Take(); ok {
		t.Fatalf("thread A still had a deliverable signal")
	}
	if _, _, ok := threadB.Take(); ok {
		t.Fatalf("thread B still had a deliverable signal")
	}
	final := rt.ProcessPending()
	if len(final.Standard) != 0 || len(final.Realtime) != 0 {
		t.Fatalf("process pending not empty at end: %+v", final)
	}

	// Explicit blocked-mask boundary: while A blocks sig2, a fresh sig2
	// process signal is invisible to A but visible (and consumable) to B.
	freshS2 := rt.SendProcess(sig2, 9, "fresh-s2")
	if _, _, ok := threadA.Take(); ok {
		t.Fatalf("A must not take sig2 while it is blocked on A")
	}
	if got := rt.ProcessPending().Standard[sig2]; got != freshS2 {
		t.Fatalf("blocked sig2 payload = %+v, want %+v", got, freshS2)
	}
	s, p, ok = threadB.Take()
	if !ok || s != sig2 || p != freshS2 {
		t.Fatalf("B fresh sig2 take = (%d, %+v, %v), want %+v", s, p, ok, freshS2)
	}
	if _, _, ok := threadA.Take(); ok {
		t.Fatalf("thread A still had a deliverable signal after B consumed sig2")
	}
}

// ---------------------------------------------------------------------------
// Concurrency: multiple goroutine "threads", blocking Wait, one-consumer rule.
// ---------------------------------------------------------------------------

func TestConcurrentRealtimeDeliveryAcrossThreads(t *testing.T) {
	rt := NewRuntime()
	const workers = 4
	const perWorker = 25
	threads := make([]*Thread, workers)
	for i := range threads {
		threads[i] = rt.NewThread()
	}

	var wg sync.WaitGroup
	counts := make([]int, workers)
	for i := range threads {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			for n := 0; n < perWorker; n++ {
				s, _ := threads[idx].Wait()
				if s != RTMin+5 {
					t.Errorf("thread %d got signal %d, want %d", idx, s, RTMin+5)
					return
				}
				counts[idx]++
			}
		}(i)
	}

	var senders sync.WaitGroup
	for i := 0; i < workers*perWorker; i++ {
		senders.Add(1)
		go func() {
			defer senders.Done()
			rt.SendProcess(RTMin+5, 0, nil)
		}()
	}
	senders.Wait()
	wg.Wait()

	total := 0
	for i, c := range counts {
		if c != perWorker {
			t.Errorf("thread %d processed %d signals, want %d", i, c, perWorker)
		}
		total += c
	}
	if total != workers*perWorker {
		t.Fatalf("total deliveries = %d, want %d", total, workers*perWorker)
	}
	if view := rt.ProcessPending(); len(view.Standard) != 0 || len(view.Realtime) != 0 {
		t.Fatalf("undelivered signals remain: %+v", view)
	}
}

func TestConcurrentStandardSendsCoalesceToSingleItem(t *testing.T) {
	rt := NewRuntime()
	th := rt.NewThread()

	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			rt.SendProcess(6, i, i)
		}(i)
	}
	wg.Wait()

	view := rt.ProcessPending()
	if len(view.Standard) != 1 {
		t.Fatalf("concurrent standard sends produced %d items, want 1", len(view.Standard))
	}
	s, p, ok := th.Take()
	if !ok || s != 6 {
		t.Fatalf("Take = (%d, %+v, %v), want signal 6", s, p, ok)
	}
	if p != view.Standard[6] {
		t.Fatalf("delivered payload %+v differs from pending view %+v", p, view.Standard[6])
	}
	if _, _, ok := th.Take(); ok {
		t.Fatalf("only one standard occurrence may be delivered")
	}
}
