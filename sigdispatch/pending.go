package sigdispatch

// PendingView is an immutable snapshot of a pending set.
type PendingView struct {
	// Standard holds at most one payload per standard signal number.
	Standard map[Sig]Payload
	// Realtime holds every queued occurrence per real-time signal
	// number, in arrival (oldest-first) order.
	Realtime map[Sig][]Payload
}

// pendingSet is the per-process and per-thread pending storage.
// All access happens under Runtime.mu.
type pendingSet struct {
	standard map[Sig]Payload
	realtime map[Sig][]Payload
}

func newPendingSet() pendingSet {
	return pendingSet{
		standard: make(map[Sig]Payload),
		realtime: make(map[Sig][]Payload),
	}
}

// add stores one occurrence. For standard signals only the first
// occurrence's payload is kept; later occurrences are dropped while
// the first one remains undelivered. Real-time signals append every
// occurrence so arrival order is preserved.
func (p *pendingSet) add(s Sig, payload Payload) {
	if s.IsRT() {
		p.realtime[s] = append(p.realtime[s], payload)
		return
	}
	if _, exists := p.standard[s]; exists {
		return
	}
	p.standard[s] = payload
}

// has reports whether at least one occurrence of s is pending.
func (p *pendingSet) has(s Sig) bool {
	if s.IsRT() {
		return len(p.realtime[s]) > 0
	}
	_, ok := p.standard[s]
	return ok
}

// remove takes the oldest occurrence of s out of the set and reports
// whether one existed.
func (p *pendingSet) remove(s Sig) (Payload, bool) {
	if s.IsRT() {
		q := p.realtime[s]
		if len(q) == 0 {
			return Payload{}, false
		}
		head := q[0]
		if len(q) == 1 {
			delete(p.realtime, s)
		} else {
			p.realtime[s] = q[1:]
		}
		return head, true
	}
	head, ok := p.standard[s]
	if ok {
		delete(p.standard, s)
	}
	return head, ok
}

// view copies the set into a PendingView safe to use without the lock.
func (p *pendingSet) view() PendingView {
	v := PendingView{
		Standard: make(map[Sig]Payload, len(p.standard)),
		Realtime: make(map[Sig][]Payload, len(p.realtime)),
	}
	for s, payload := range p.standard {
		v.Standard[s] = payload
	}
	for s, q := range p.realtime {
		cp := make([]Payload, len(q))
		copy(cp, q)
		v.Realtime[s] = cp
	}
	return v
}
