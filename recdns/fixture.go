package recdns

import (
	"sync"
	"time"
)

// HandlerFunc answers a single query directed at one server. It may return
// (nil, err) to model a transport failure / timeout.
type HandlerFunc func(Server, Query) (*Message, error)

// gate lets a test hold one in-flight server exchange open, synchronising
// interleavings such as a delegation cutover happening mid-resolution.
type gate struct {
	arrived chan struct{} // closed once the exchange reaches the gate
	release chan struct{} // closed by the test to let the exchange complete
	once    sync.Once
}

func newGate() *gate {
	return &gate{arrived: make(chan struct{}), release: make(chan struct{})}
}

func (g *gate) wait() {
	g.once.Do(func() { close(g.arrived) })
	<-g.release
}

// Fixture is the local message harness standing in for the network. It is
// safe for concurrent use.
type Fixture struct {
	mu      sync.Mutex
	handler HandlerFunc
	log     []QueryLog
	gates   map[string]*gate
	clock   func() time.Time
}

// NewFixture builds a fixture. handler answers every non-gated exchange; clock
// stamps the query log (defaults to time.Now).
func NewFixture(handler HandlerFunc, clock func() time.Time) *Fixture {
	if clock == nil {
		clock = time.Now
	}
	return &Fixture{
		handler: handler,
		gates:   make(map[string]*gate),
		clock:   clock,
	}
}

// Gate installs a one-shot blocking point for one (server, name, type)
// triple. The first matching exchange blocks until the gate is released; the
// gate is consumed on arrival, so retries and restarts never block on it.
func (f *Fixture) Gate(s Server, n Name, t Type) *gate {
	f.mu.Lock()
	defer f.mu.Unlock()
	g := newGate()
	f.gates[gateKey(s, n, t)] = g
	return g
}

func gateKey(s Server, n Name, t Type) string {
	return string(s) + "|" + string(n) + "|" + string(t)
}

// Query executes one server exchange through the fixture.
func (f *Fixture) Query(s Server, q Query) (*Message, error) {
	k := gateKey(s, q.Name, q.Type)
	f.mu.Lock()
	g := f.gates[k]
	// Consume the gate on arrival so later exchanges pass straight through.
	if g != nil {
		delete(f.gates, k)
	}
	// Record the attempt before parking so tests can assert on the exact
	// exchange currently blocked.
	f.log = append(f.log, QueryLog{
		Server: s,
		Name:   q.Name,
		Type:   q.Type,
		At:     f.clock(),
	})
	f.mu.Unlock()
	if g != nil {
		g.wait()
	}

	msg, err := f.handler(s, q)
	if err != nil {
		f.mu.Lock()
		f.log[len(f.log)-1].Error = err
		f.mu.Unlock()
	}
	return msg, err
}

// Log returns a copy of all exchanges in arrival order.
func (f *Fixture) Log() []QueryLog {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]QueryLog, len(f.log))
	copy(out, f.log)
	return out
}

// ServerSequence returns just the contacted servers, in order (including
// failed/retried exchanges).
func (f *Fixture) ServerSequence() []Server {
	log := f.Log()
	out := make([]Server, len(log))
	for i, e := range log {
		out[i] = e.Server
	}
	return out
}

// ResetLog clears the recorded exchanges.
func (f *Fixture) ResetLog() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.log = nil
}
