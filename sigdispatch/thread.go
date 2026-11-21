package sigdispatch

// Thread is one execution context with its own signal mask and
// thread-private pending set.
//
// A Thread value is safe for concurrent use: multiple goroutines may
// represent the runtime's threads, and every operation takes the
// runtime lock. Handler bodies run synchronously inside Dispatch/Wait
// and may re-enter the runtime.
type Thread struct {
	id      int
	rt      *Runtime
	pending pendingSet

	// mask carries a reference count per blocked signal so nested
	// handler frames can restore the exact previous mask.
	mask map[Sig]int
}

// ID returns the runtime-unique thread identifier.
func (t *Thread) ID() int { return t.id }

// Exit removes the thread from the runtime. Its private pending
// signals are discarded.
func (t *Thread) Exit() { t.rt.removeThread(t.id) }

// SendThread directs a signal at this thread only.
func (t *Thread) SendThread(s Sig, code int, data any) Payload {
	r := t.rt
	r.mu.Lock()
	defer r.mu.Unlock()
	p := r.enqueueLocked(&t.pending, s, code, data)
	r.cond.Broadcast()
	return p
}

// Block adds sigs to this thread's blocked mask.
func (t *Thread) Block(sigs ...Sig) {
	r := t.rt
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, s := range sigs {
		t.mask[s]++
	}
}

// Unblock removes one blocking reference for each of sigs. The signal
// becomes unblocked only when its reference count reaches zero, which
// keeps nested handler frame pushes balanced.
func (t *Thread) Unblock(sigs ...Sig) {
	r := t.rt
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, s := range sigs {
		switch t.mask[s] {
		case 0:
			delete(t.mask, s)
		case 1:
			delete(t.mask, s)
		default:
			t.mask[s]--
		}
	}
}

// Blocked reports whether s is currently blocked on this thread.
func (t *Thread) Blocked(s Sig) bool {
	r := t.rt
	r.mu.Lock()
	defer r.mu.Unlock()
	return t.blockedLocked(s)
}

// blockedLocked reports the mask state. Caller must hold r.mu.
func (t *Thread) blockedLocked(s Sig) bool {
	_, ok := t.mask[s]
	return ok
}

// Mask returns a sorted snapshot of the currently blocked signals.
func (t *Thread) Mask() []Sig {
	r := t.rt
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]Sig, 0, len(t.mask))
	for s := range t.mask {
		out = append(out, s)
	}
	sortSigs(out)
	return out
}

// Pending returns a snapshot of this thread's private pending signals.
func (t *Thread) Pending() PendingView {
	r := t.rt
	r.mu.Lock()
	defer r.mu.Unlock()
	return t.pending.view()
}

// Take performs one explicit, non-blocking signal take. It selects the
// smallest unblocked signal number from the thread-private and
// process-wide pending sets, preferring the private item on equal
// numbers, and removes exactly that one occurrence.
func (t *Thread) Take() (s Sig, p Payload, ok bool) {
	r := t.rt
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.selectLocked(t)
}

// Wait blocks until an unblocked signal is available to this thread and
// then takes and returns it, using the same selection rules as Take.
func (t *Thread) Wait() (Sig, Payload) {
	r := t.rt
	r.mu.Lock()
	defer r.mu.Unlock()
	for {
		if s, p, ok := r.selectLocked(t); ok {
			return s, p
		}
		r.cond.Wait()
	}
}

// Dispatch explicitly takes one signal and runs its handler. The
// handler runs with the signal itself and the handler's declared mask
// temporarily blocked; other signals stay unblocked and may be
// dispatched recursively from inside the handler. It reports whether
// any deliverable signal was pending.
func (t *Thread) Dispatch() bool {
	r := t.rt
	r.mu.Lock()
	s, p, ok := r.selectLocked(t)
	if !ok {
		r.mu.Unlock()
		return false
	}
	h, hasHandler := r.handlers[s]
	r.mu.Unlock()

	t.runHandler(s, p, h, hasHandler)
	return true
}

// WaitAndDispatch blocks until a signal is available, then runs its
// handler exactly like Dispatch.
func (t *Thread) WaitAndDispatch() (Sig, Payload) {
	s, p := t.Wait()
	r := t.rt
	r.mu.Lock()
	h, hasHandler := r.handlers[s]
	r.mu.Unlock()
	t.runHandler(s, p, h, hasHandler)
	return s, p
}

// runHandler pushes the temporary mask, invokes the user handler and
// pops the mask again even if the handler panics.
func (t *Thread) runHandler(s Sig, p Payload, h Handler, hasHandler bool) {
	r := t.rt
	r.mu.Lock()
	t.pushMask(s, h.Mask)
	r.mu.Unlock()

	defer func() {
		r.mu.Lock()
		t.popMask(s, h.Mask)
		r.mu.Unlock()
	}()

	if hasHandler && h.Fn != nil {
		h.Fn(t, s, p)
	}
}

// pushMask blocks the delivered signal and the handler's extra mask.
// Caller must hold r.mu.
func (t *Thread) pushMask(self Sig, extra []Sig) {
	t.mask[self]++
	for _, s := range extra {
		if s == self {
			continue // the delivered signal is already blocked once
		}
		t.mask[s]++
	}
}

// popMask releases the references added by the matching pushMask.
// Caller must hold r.mu.
func (t *Thread) popMask(self Sig, extra []Sig) {
	dec := func(s Sig) {
		switch t.mask[s] {
		case 0, 1:
			delete(t.mask, s)
		default:
			t.mask[s]--
		}
	}
	dec(self)
	for _, s := range extra {
		if s == self {
			continue
		}
		dec(s)
	}
}

// sortSigs orders a signal slice ascending without pulling in extra
// helper packages at call sites.
func sortSigs(s []Sig) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j-1] > s[j]; j-- {
			s[j-1], s[j] = s[j], s[j-1]
		}
	}
}
