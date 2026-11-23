package recdns

import "time"

// The functions in this file are small introspection helpers intended for
// tests asserting on cache contents and delegation generations. They never
// mutate resolver state.

// DelegationGeneration returns the current generation of zone's cut and
// whether a live delegation exists.
func (r *Resolver) DelegationGeneration(zone Name) (int, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	e, ok := r.delegs[zone]
	if !ok || !r.now().Before(e.expires) {
		return 0, false
	}
	return e.gen, true
}

// HasCachedRR reports whether a live RR identified by (name, type, value) is
// cached, along with its original zone.
func (r *Resolver) HasCachedRR(n Name, t Type, value string) (RR, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	now := r.now()
	for _, e := range r.rrs[rrKey{n, t}] {
		if now.Before(e.expires) && e.rr.Value == value {
			return e.rr, true
		}
	}
	return RR{}, false
}

// NegativeCacheEntry exposes a live negative-cache entry.
func (r *Resolver) NegativeCacheEntry(n Name, t Type) (RCode, Name, time.Duration, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	neg, ok := r.negative[rrKey{n, t}]
	now := r.now()
	if !ok || !now.Before(neg.expires) {
		return "", "", 0, false
	}
	return neg.rcode, neg.origin, neg.expires.Sub(now), true
}

// CachedRRCount returns the number of RR entries stored under a key
// (including expired ones), used to prove stale writes were rejected.
func (r *Resolver) CachedRRCount(n Name, t Type) int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.rrs[rrKey{n, t}])
}
