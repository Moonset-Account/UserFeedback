package recdns

import (
	"errors"
	"sync"
	"time"
)

// Sentinel errors returned by Resolve.
var (
	// ErrNoDelegation means no (non-expired) delegation can be reached, even
	// after walking back toward the root.
	ErrNoDelegation = errors.New("recdns: no usable delegation for name")
	// ErrNoResponse means every authoritative server for the current step
	// failed or returned SERVFAIL.
	ErrNoResponse = errors.New("recdns: no server gave an answer")
	// ErrStaleDelegation means the delegation used by this resolution attempt
	// was replaced before its in-flight answer arrived; callers restart from
	// the current delegation set. It is retried internally and never escapes
	// Resolve on its own.
	ErrStaleDelegation = errors.New("recdns: delegation superseded mid-resolution")
)

// Clock returns the resolver's notion of "now". Tests inject a manual clock.
type Clock func() time.Time

type rrKey struct {
	name Name
	typ  Type
}

type rrEntry struct {
	rr       RR
	storedAt time.Time
	expires  time.Time
}

type negEntry struct {
	rcode    RCode // RCodeNXDomain or RCodeOK (the latter denotes NODATA)
	origin   Name
	storedAt time.Time
	expires  time.Time
}

// delegEntry is one cached zone cut. gen is bumped whenever the delegation
// for the zone is replaced, which invalidates in-flight answers started under
// the previous generation.
type delegEntry struct {
	d        Delegation
	storedAt time.Time
	expires  time.Time
	gen      int
}

// Result is the outcome of one Resolve call.
type Result struct {
	Name      Name
	Type      Type
	Status    RCode
	Answers   []RR // terminal positive records, carrying their original zone
	CNAMEs    []RR // every alias record traversed, in traversal order
	Loop      []Name
	Servers   []Server      // every server contacted across all attempts, in order
	TTL       time.Duration // remaining validity at Resolve's end instant
	FromCache bool
}

// Resolver is a recursive resolver backed by a Fixture.
type Resolver struct {
	fx          *Fixture
	now         Clock
	maxSteps    int
	maxRetries  int
	maxRestarts int

	mu       sync.RWMutex
	delegs   map[Name]*delegEntry
	rrs      map[rrKey][]rrEntry
	negative map[rrKey]*negEntry
}

// NewResolver builds a resolver seeded with a root delegation.
func NewResolver(fx *Fixture, rootServers []Server, rootTTL time.Duration, clock Clock) *Resolver {
	r := &Resolver{
		fx:          fx,
		now:         clock,
		maxSteps:    16,
		maxRetries:  2,
		maxRestarts: 4,
		delegs:      make(map[Name]*delegEntry),
		rrs:         make(map[rrKey][]rrEntry),
		negative:    make(map[rrKey]*negEntry),
	}
	if r.now == nil {
		r.now = time.Now
	}
	r.InstallDelegation(Delegation{
		Zone:    ".",
		Servers: append([]Server(nil), rootServers...),
		TTL:     rootTTL,
		Origin:  ".",
	})
	return r
}

// InstallDelegation installs or replaces a zone cut. Replacement bumps the
// generation so answers still in flight under the old cut cannot be cached.
func (r *Resolver) InstallDelegation(d Delegation) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.installDelegationLocked(d)
}

func (r *Resolver) installDelegationLocked(d Delegation) {
	now := r.now()
	gen := 1
	if old, ok := r.delegs[d.Zone]; ok {
		gen = old.gen + 1
	}
	r.delegs[d.Zone] = &delegEntry{
		d:        d,
		storedAt: now,
		expires:  now.Add(d.TTL),
		gen:      gen,
	}
}

// generation returns the current generation token for zone.
func (r *Resolver) generation(zone Name) (gen int, ok bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	e, ok := r.delegs[zone]
	if !ok || !r.now().Before(e.expires) {
		return 0, false
	}
	return e.gen, true
}

// Resolve runs a recursive lookup.
func (r *Resolver) Resolve(n Name, t Type) (*Result, error) {
	var servers []Server
	for restart := 0; restart <= r.maxRestarts; restart++ {
		res, err := r.resolveOnce(n, t)
		servers = append(servers, res.Servers...)
		if err == nil {
			res.Servers = servers
			return res, nil
		}
		if !errors.Is(err, ErrStaleDelegation) {
			return nil, err
		}
		// Delegation changed underneath us: restart from scratch using the
		// current delegation set.
	}
	return nil, ErrNoDelegation
}

func (r *Resolver) resolveOnce(n Name, t Type) (*Result, error) {
	now := r.now()
	if res := r.cachedLookup(n, t, now); res != nil {
		return res, nil
	}

	// seen is the CNAME traversal set, pre-seeded with the queried name.
	seen := map[Name]bool{n: true}
	var chain []RR
	var chainExpiry time.Time
	var servers []Server
	steps := 0

	current := n
	for {
		steps++
		if steps > r.maxSteps {
			return &Result{Name: n, Type: t, Servers: servers}, ErrNoDelegation
		}

		entry, zone := r.longestDelegation(current)
		if entry == nil {
			return &Result{Name: n, Type: t, Servers: servers}, ErrNoDelegation
		}
		gen := entry.gen

		msg, err := r.queryStep(entry, Query{Name: current, Type: t, At: r.now()})
		servers = append(servers, msg.servers...)
		if err != nil {
			return &Result{Name: n, Type: t, Servers: servers}, err
		}

		// The zone cut may have been replaced while this answer was in flight.
		// No record from a superseded generation is cached: this check runs
		// before any write below.
		if curGen, ok := r.generation(zone); !ok || curGen != gen {
			return &Result{Name: n, Type: t, Servers: servers}, ErrStaleDelegation
		}

		// Generation verified: authoritative answers may now enter the cache.
		if msg.m.RCode == RCodeOK {
			storedAt := r.now()
			for i := range msg.m.Answers {
				rr := msg.m.Answers[i]
				r.cacheRR(rr, storedAt, storedAt.Add(rr.TTL))
			}
		}

		switch {
		case msg.m.Delegation != nil:
			r.mu.Lock()
			r.installDelegationLocked(*msg.m.Delegation)
			r.mu.Unlock()
			// Keep querying with the same QNAME; only the server set changes.
			if !zoneMatches(msg.m.Delegation.Zone, current) {
				return &Result{Name: n, Type: t, Servers: servers}, ErrNoDelegation
			}

		case msg.m.RCode == RCodeNXDomain:
			expiry, origin := r.negativeTTL(msg.m, r.now())
			r.cacheNegative(current, t, RCodeNXDomain, origin, expiry)
			res := &Result{
				Name:    n,
				Type:    t,
				Status:  StatusNXDOMAIN,
				CNAMEs:  append([]RR(nil), chain...),
				Servers: servers,
			}
			res.TTL = r.finalTTL(chain, chainExpiry, expiry, r.now())
			return res, nil

		case msg.m.RCode == RCodeOK:
			now2 := r.now()
			var cname *RR
			var terminal []RR
			for i := range msg.m.Answers {
				rr := msg.m.Answers[i]
				if rr.Name == current && rr.Type == TypeCNAME {
					c := rr
					cname = &c
				}
				// A terminal record matches either the current name (no alias in
				// this message) or the CNAME target carried alongside it.
				ownerMatch := rr.Name == current
				if cname != nil && rr.Name == Name(cname.Value) {
					ownerMatch = true
				}
				if ownerMatch && rr.Type == t && t != TypeCNAME {
					terminal = append(terminal, rr)
				}
			}

			// Every CNAME bounds the result TTL, including one in the same
			// message as the terminal answer.
			if cname != nil {
				if exp := now2.Add(cname.TTL); chainExpiry.IsZero() || exp.Before(chainExpiry) {
					chainExpiry = exp
				}
			}

			target := Name("")
			if cname != nil {
				target = Name(cname.Value)
			}
			isLoop := cname != nil && seen[target]

			switch {
			case isLoop:
				loop := make([]Name, 0, len(chain)+2)
				loop = append(loop, n)
				for _, c := range chain {
					loop = append(loop, Name(c.Value))
				}
				// The back-pointer's owner is already the chain's last name;
				// close the cycle by appending the repeated target.
				loop = append(loop, target)
				return &Result{
					Name:    n,
					Type:    t,
					Status:  StatusLoop,
					Loop:    loop,
					CNAMEs:  append([]RR(nil), chain...),
					Servers: servers,
				}, nil

			case len(terminal) > 0:
				if cname != nil {
					chain = append(chain, *cname)
					seen[target] = true
				}
				expiry := now2.Add(terminal[0].TTL)
				for _, rr := range terminal[1:] {
					if e := now2.Add(rr.TTL); e.Before(expiry) {
						expiry = e
					}
				}
				res := &Result{
					Name:    n,
					Type:    t,
					Status:  StatusOK,
					Answers: append([]RR(nil), terminal...),
					CNAMEs:  append([]RR(nil), chain...),
					Servers: servers,
				}
				res.TTL = r.finalTTL(chain, chainExpiry, expiry, now2)
				return res, nil

			case cname != nil:
				chain = append(chain, *cname)
				seen[target] = true
				current = target

			default:
				// NOERROR with no answer of the asked type: NODATA, scoped
				// exactly to (name, type) and distinct from NXDOMAIN.
				expiry, origin := r.negativeTTL(msg.m, now2)
				r.cacheNegative(current, t, RCodeOK, origin, expiry)
				res := &Result{
					Name:    n,
					Type:    t,
					Status:  StatusNODATA,
					CNAMEs:  append([]RR(nil), chain...),
					Servers: servers,
				}
				res.TTL = r.finalTTL(chain, chainExpiry, expiry, now2)
				return res, nil
			}

		default:
			return &Result{Name: n, Type: t, Servers: servers}, ErrNoResponse
		}
	}
}

// stepMsg bundles the chosen response with the full server list tried for the
// step (including failed retries).
type stepMsg struct {
	m       *Message
	servers []Server
}

// queryStep tries each authoritative server for the current zone until one
// answers authoritatively; transport/SERVFAIL failures retry across servers.
func (r *Resolver) queryStep(entry *delegEntry, q Query) (*stepMsg, error) {
	var tried []Server
	attempts := len(entry.d.Servers) + r.maxRetries
	if attempts < 1 {
		return nil, ErrNoDelegation
	}
	for i := 0; i < attempts; i++ {
		srv := entry.d.Servers[i%len(entry.d.Servers)]
		tried = append(tried, srv)
		msg, err := r.fx.Query(srv, q)
		if err != nil || msg == nil {
			continue
		}
		if msg.RCode == RCodeServFail {
			continue
		}
		msg.Server = srv
		return &stepMsg{m: msg, servers: tried}, nil
	}
	return &stepMsg{servers: tried}, ErrNoResponse
}

// longestDelegation walks upward from name until a live delegation is found.
// Expired zone cuts are skipped, naturally falling back to the parent cut.
func (r *Resolver) longestDelegation(name Name) (*delegEntry, Name) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	now := r.now()
	for z := name; ; z = parentZone(z) {
		if e, ok := r.delegs[z]; ok && now.Before(e.expires) {
			return e, z
		}
		if z == "." {
			return nil, "."
		}
	}
}

// finalTTL bounds the result lifetime by every CNAME on the chain.
func (r *Resolver) finalTTL(chain []RR, chainExpiry, expiry time.Time, now time.Time) time.Duration {
	ttl := expiry.Sub(now)
	if len(chain) > 0 {
		ttl = minTTL(ttl, chainExpiry.Sub(now))
	}
	if ttl < 0 {
		return 0
	}
	return ttl
}

func minTTL(a, b time.Duration) time.Duration {
	if b < a {
		return b
	}
	return a
}

// negativeTTL derives the negative-cache lifetime per RFC 2308 semantics:
// the SOA minimum, present on both NXDOMAIN and NODATA authority sections.
func (r *Resolver) negativeTTL(m *Message, now time.Time) (time.Time, Name) {
	if m.Authority != nil {
		return now.Add(m.Authority.MinTTL), m.Authority.Origin
	}
	return now, ""
}

func (r *Resolver) cacheRR(rr RR, stored, expires time.Time) {
	r.mu.Lock()
	defer r.mu.Unlock()
	k := rrKey{rr.Name, rr.Type}
	r.rrs[k] = append(r.rrs[k], rrEntry{rr: rr, storedAt: stored, expires: expires})
}

func (r *Resolver) cacheNegative(n Name, t Type, rcode RCode, origin Name, expires time.Time) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.negative[rrKey{n, t}] = &negEntry{
		rcode:    rcode,
		origin:   origin,
		storedAt: r.now(),
		expires:  expires,
	}
}

// cachedLookup reconstructs a result from cached state, including cached
// CNAME chains. It returns nil when nothing complete is available.
func (r *Resolver) cachedLookup(n Name, t Type, now time.Time) *Result {
	r.mu.RLock()
	defer r.mu.RUnlock()

	if neg, ok := r.negative[rrKey{n, t}]; ok && now.Before(neg.expires) {
		status := StatusNXDOMAIN
		if neg.rcode == RCodeOK {
			status = StatusNODATA
		}
		return &Result{
			Name:      n,
			Type:      t,
			Status:    status,
			TTL:       neg.expires.Sub(now),
			FromCache: true,
		}
	}

	var chain []RR
	var chainExpiry time.Time
	seen := map[Name]bool{n: true}
	current := n
	for {
		cn := r.liveRRLocked(current, TypeCNAME, now)
		if cn == nil {
			break
		}
		target := Name(cn.rr.Value)
		if seen[target] {
			return &Result{
				Name:      n,
				Type:      t,
				Status:    StatusLoop,
				CNAMEs:    append([]RR(nil), chain...),
				TTL:       minTTL(cn.expires.Sub(now), chainTTL(chain, chainExpiry, now)),
				FromCache: true,
			}
		}
		seen[target] = true
		chain = append(chain, cn.rr)
		if chainExpiry.IsZero() || cn.expires.Before(chainExpiry) {
			chainExpiry = cn.expires
		}
		current = target
	}

	// Negative answer at the chain's terminal name still completes the lookup.
	if neg, ok := r.negative[rrKey{current, t}]; ok && now.Before(neg.expires) {
		status := StatusNXDOMAIN
		if neg.rcode == RCodeOK {
			status = StatusNODATA
		}
		res := &Result{
			Name:      n,
			Type:      t,
			Status:    status,
			CNAMEs:    append([]RR(nil), chain...),
			TTL:       neg.expires.Sub(now),
			FromCache: true,
		}
		if len(chain) > 0 {
			res.TTL = minTTL(res.TTL, chainExpiry.Sub(now))
		}
		return res
	}

	if t != TypeCNAME {
		if live := r.liveRRListLocked(current, t, now); len(live) > 0 {
			answers := make([]RR, 0, len(live))
			expiry := live[0].expires
			for _, e := range live {
				answers = append(answers, e.rr)
				if e.expires.Before(expiry) {
					expiry = e.expires
				}
			}
			res := &Result{
				Name:      n,
				Type:      t,
				Status:    StatusOK,
				Answers:   answers,
				CNAMEs:    append([]RR(nil), chain...),
				TTL:       expiry.Sub(now),
				FromCache: true,
			}
			if len(chain) > 0 {
				res.TTL = minTTL(res.TTL, chainExpiry.Sub(now))
			}
			return res
		}
	}

	// A query of type CNAME is satisfied by the chain's first alias itself.
	if t == TypeCNAME && len(chain) > 0 {
		return &Result{
			Name:      n,
			Type:      t,
			Status:    StatusOK,
			Answers:   []RR{chain[0]},
			CNAMEs:    append([]RR(nil), chain[1:]...),
			TTL:       chainExpiry.Sub(now),
			FromCache: true,
		}
	}
	return nil
}

func chainTTL(chain []RR, chainExpiry time.Time, now time.Time) time.Duration {
	if len(chain) == 0 {
		return 1<<63 - 1 // effectively unbounded
	}
	return chainExpiry.Sub(now)
}

func (r *Resolver) liveRRLocked(n Name, t Type, now time.Time) *rrEntry {
	for _, e := range r.rrs[rrKey{n, t}] {
		if now.Before(e.expires) {
			cp := e
			return &cp
		}
	}
	return nil
}

func (r *Resolver) liveRRListLocked(n Name, t Type, now time.Time) []rrEntry {
	var out []rrEntry
	for _, e := range r.rrs[rrKey{n, t}] {
		if now.Before(e.expires) {
			out = append(out, e)
		}
	}
	return out
}
