// Package migrationplanner implements a protocol-migration planner for a
// release controller.
//
// The planner searches for the shortest forward plan that moves every service
// to its target version under four classes of constraints:
//
//   - per-service deployable versions, simultaneously enabled codec profiles
//     and bounded old/new coexistence;
//   - per-call-edge request/response compatibility declared by direction;
//   - sessions that started before the migration and stay pinned to the exact
//     request/response codec pair they selected until a named drain event is
//     observed;
//   - a rollback budget: every accepted prefix must keep a config/replica-only
//     sequence that returns to the starting layout within the budget. Drain is
//     never treated as reversible.
//
// In-flight bounded bidirectional stream protocols (see stream.go) add a fifth
// constraint: an upgrade touching a live pinned session is only legal when the
// old and new finite-state machines are equivalent for that session.
package migrationplanner

import (
	"errors"
	"fmt"
	"sort"
	"strings"
)

// Codec identifies a wire encoding for one message direction.
type Codec string

// Profile is a request/response codec pair that a single replica may enable
// simultaneously. A profile is selected per replica.
type Profile struct {
	Request  Codec
	Response Codec
}

func (p Profile) key() string { return string(p.Request) + "\x00" + string(p.Response) }

// Version describes one deployable version of a service.
type Version struct {
	Name     string
	Profiles []Profile // codec pairs this version is able to enable
}

// Service describes the deployment envelope of one service.
type Service struct {
	// Name is the unique service identifier.
	Name string
	// Versions are the deployable versions, keyed by Version.Name.
	Versions map[string]Version
	// MaxReplicas bounds the number of live replicas. A +1 transitional
	// replica is only possible when MaxReplicas exceeds the initial count.
	MaxReplicas int
	// CoexistSteps bounds how long two distinct versions may be live at the
	// same time. The counter is the number of forward plan steps during which
	// the mixed layout has existed.
	CoexistSteps int
	// RollbackBudget bounds, per service, how many config/replica actions a
	// reserved rollback sequence may spend touching this service.
	RollbackBudget int
	// Target is the version name every live replica must run at the goal.
	Target string
}

// VPair identifies the ordered version pair of one call edge. It is the map
// key under which an edge stores the attached stream FSM.
type VPair struct {
	CallerVersion string
	CalleeVersion string
}

// Edge is one directed call relationship between two services.
type Edge struct {
	Caller string
	Callee string
	// RequestOK[callerCodec][calleeCodec] reports whether the callee accepts
	// the caller's request encoding.
	RequestOK map[Codec]map[Codec]bool
	// ResponseOK[calleeCodec][callerCodec] reports whether the caller accepts
	// the callee's response encoding.
	ResponseOK map[Codec]map[Codec]bool
	// Streams optionally attaches a named FSM to each ordered version pair.
	Streams map[VPair]*StreamProtocol
}

// Spec is the static, immutable version table handed to the planner.
type Spec struct {
	Services map[string]*Service
	Edges    []*Edge
}

// Session is a conversation that already had work in flight when planning
// started. Its selected codec pair is pinned until DrainEvent is observed.
type Session struct {
	ID            string
	Edge          string // Edge.Caller + "->" + Edge.Callee
	CallerRep     string // replica id holding the caller-side endpoint at start
	CalleeRep     string // replica id holding the callee-side endpoint at start
	CallerVersion string // versions selected when the session started
	CalleeVersion string
	ReqCodec      Codec
	RespCodec     Codec
	DrainEvent    string
}

// Replica is one live deployment unit.
type Replica struct {
	ID      string
	Service string
	Version string
	Profile Profile
}

// State is one full configuration of the fleet.
type State struct {
	spec *Spec

	// Replicas is keyed by replica id.
	Replicas map[string]*Replica
	// coexistAge[s] counts forward steps spent in a mixed-version layout.
	coexistAge map[string]int
	// pins is keyed by session id. Pinned sessions disappear on drain.
	pins map[string]*Session
	// drainedEvents are the events already observed, including events that
	// became available during the plan.
	drainedEvents map[string]bool
}

func (s *Spec) edgeByKey(key string) *Edge {
	for _, e := range s.Edges {
		if e.Key() == key {
			return e
		}
	}
	return nil
}

// Key returns the stable identifier of an edge.
func (e *Edge) Key() string { return e.Caller + "->" + e.Callee }

func profileAllowed(v Version, p Profile) bool {
	for _, q := range v.Profiles {
		if q == p {
			return true
		}
	}
	return false
}

// supportsRequest reports whether one caller/callee replica pair can exchange
// a request in the given encodings under this edge.
func (e *Edge) supportsRequest(req, callee Codec) bool {
	return e.RequestOK[req][callee]
}

// supportsResponse reports whether one callee/caller replica pair can exchange
// a response in the given encodings under this edge.
func (e *Edge) supportsResponse(resp, caller Codec) bool {
	return e.ResponseOK[resp][caller]
}

// profileConnects reports whether every profile on the caller side can talk
// to every profile on the callee side of this edge.
func (e *Edge) profileConnects(caller, callee Profile) bool {
	if !e.supportsRequest(caller.Request, callee.Request) {
		return false
	}
	if !e.supportsResponse(callee.Response, caller.Response) {
		return false
	}
	return true
}

// clone returns a deep copy of the state suitable for applying an action to.
func (st *State) clone() *State {
	next := &State{
		spec:          st.spec,
		Replicas:      make(map[string]*Replica, len(st.Replicas)),
		coexistAge:    make(map[string]int, len(st.coexistAge)),
		pins:          make(map[string]*Session, len(st.pins)),
		drainedEvents: make(map[string]bool, len(st.drainedEvents)),
	}
	for id, r := range st.Replicas {
		cp := *r
		next.Replicas[id] = &cp
	}
	for k, v := range st.coexistAge {
		next.coexistAge[k] = v
	}
	for k, v := range st.pins {
		cp := *v
		next.pins[k] = &cp
	}
	for k, v := range st.drainedEvents {
		next.drainedEvents[k] = v
	}
	return next
}

// key is the canonical state key used by the BFS visited set.
func (st *State) key() string {
	ids := make([]string, 0, len(st.Replicas))
	for id := range st.Replicas {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	var b strings.Builder
	for _, id := range ids {
		r := st.Replicas[id]
		b.WriteString(id)
		b.WriteString("=")
		b.WriteString(r.Service)
		b.WriteString("/")
		b.WriteString(r.Version)
		b.WriteString("/")
		b.WriteString(r.Profile.key())
		b.WriteString(";")
	}
	b.WriteString("|pins:")
	pinIDs := make([]string, 0, len(st.pins))
	for id := range st.pins {
		pinIDs = append(pinIDs, id)
	}
	sort.Strings(pinIDs)
	for _, id := range pinIDs {
		p := st.pins[id]
		b.WriteString(id)
		b.WriteString("@")
		b.WriteString(string(p.ReqCodec))
		b.WriteString(",")
		b.WriteString(string(p.RespCodec))
		b.WriteString(";")
	}
	b.WriteString("|events:")
	ev := make([]string, 0, len(st.drainedEvents))
	for k := range st.drainedEvents {
		ev = append(ev, k)
	}
	sort.Strings(ev)
	for _, k := range ev {
		b.WriteString(k)
		b.WriteString(",")
	}
	b.WriteString("|age:")
	svc := make([]string, 0, len(st.coexistAge))
	for k := range st.coexistAge {
		svc = append(svc, k)
	}
	sort.Strings(svc)
	for _, k := range svc {
		b.WriteString(k)
		b.WriteString("=")
		b.WriteString(fmt.Sprintf("%d", st.coexistAge[k]))
		b.WriteString(";")
	}
	return b.String()
}

// configKey identifies only the replica/version/profile layout. It ignores
// coexistence ages, pins and drain events: a rollback returns to the starting
// layout without resurrecting a drained session, since drain is irreversible.
func (st *State) configKey() string {
	return st.layoutKey()
}

// layoutKey renders the canonical replica/version/profile layout.
func (st *State) layoutKey() string {
	ids := make([]string, 0, len(st.Replicas))
	for id := range st.Replicas {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	var b strings.Builder
	for _, id := range ids {
		r := st.Replicas[id]
		b.WriteString(id)
		b.WriteString("=")
		b.WriteString(r.Service)
		b.WriteString("/")
		b.WriteString(r.Version)
		b.WriteString("/")
		b.WriteString(r.Profile.key())
		b.WriteString(";")
	}
	return b.String()
}

// serviceReplicas returns live replicas of one service in stable id order.
func (st *State) serviceReplicas(service string) []*Replica {
	var out []*Replica
	for _, r := range st.Replicas {
		if r.Service == service {
			out = append(out, r)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// mixedVersions reports whether a service currently runs more than one version.
func (st *State) mixedVersions(service string) bool {
	seen := map[string]bool{}
	for _, r := range st.serviceReplicas(service) {
		seen[r.Version] = true
		if len(seen) > 1 {
			return true
		}
	}
	return false
}

// validateSpec performs static consistency checks on the input table.
func (s *Spec) validate() error {
	if len(s.Services) == 0 {
		return errors.New("spec has no services")
	}
	names := map[string]bool{}
	for name, svc := range s.Services {
		if name != svc.Name {
			return fmt.Errorf("service map key %q does not match Name %q", name, svc.Name)
		}
		if names[name] {
			return fmt.Errorf("duplicate service %q", name)
		}
		names[name] = true
		if svc.MaxReplicas <= 0 {
			return fmt.Errorf("service %q needs positive MaxReplicas", name)
		}
		if svc.CoexistSteps < 0 || svc.RollbackBudget < 0 {
			return fmt.Errorf("service %q has negative limit", name)
		}
		if _, ok := svc.Versions[svc.Target]; !ok {
			return fmt.Errorf("service %q target %q is not deployable", name, svc.Target)
		}
		for vn, v := range svc.Versions {
			if vn != v.Name {
				return fmt.Errorf("version map key %q does not match Name %q on %q", vn, v.Name, name)
			}
			if len(v.Profiles) == 0 {
				return fmt.Errorf("version %s/%s has no profiles", name, vn)
			}
		}
	}
	for _, e := range s.Edges {
		if !names[e.Caller] || !names[e.Callee] {
			return fmt.Errorf("edge %s references unknown service", e.Key())
		}
	}
	return nil
}
