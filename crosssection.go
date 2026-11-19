// Package crosssection reconstructs the maximal consistent cut (截面) across
// several services that each sealed their own ordered event prefix.
//
// Each service stores events ordered by a 1-based local sequence number. A cut
// is described by Cut[s], the largest event sequence retained for service s
// (0 means every event of that service is cut away). A valid cut must satisfy:
//
//   - Prefix rule: retaining an event requires retaining every earlier event of
//     the same service. This is inherent in representing the cut as a single
//     sequence number.
//   - Message rule: retaining a receive requires retaining the matching send.
//     If the send falls outside the cut, the receiving service is forced back
//     to just before the receive. One service falling back can therefore force
//     others to fall back again; a fixed-point iteration handles the cascade.
//   - Missing source: a receive with no send evidence anywhere is reported as
//     missing-source and the cut is forced to just before it.
//
// Iteration starts from the highest permitted cut (the configured cap) and only
// ever moves cuts backwards, so it terminates at the component-wise maximal
// consistent prefix that never exceeds any cap.
package crosssection

import (
	"errors"
	"fmt"
	"sort"
)

// EventKind classifies an event.
type EventKind int

const (
	// Local is an event that is neither a send nor a receive.
	Local EventKind = iota
	// Send is a message-send event; MessageID must be non-empty and unique.
	Send
	// Receive is a message-receive event; MessageID must be non-empty.
	Receive
)

// Event is one entry in a service's local log.
type Event struct {
	// Seq is the 1-based local sequence number; sequences must be strictly
	// increasing within a service.
	Seq int
	// Kind is the event type. Local events ignore MessageID.
	Kind EventKind
	// MessageID is the unique identifier carried by send/receive events.
	// Exactly one send with a given ID may exist across all services.
	MessageID string
}

// ServiceLog is one service's sealed, locally ordered event stream.
type ServiceLog struct {
	// Name uniquely identifies the service.
	Name string
	// Events are ordered by Seq; each Seq must be strictly greater than the
	// preceding one.
	Events []Event
	// MaxSeq is the largest local sequence the service is allowed to use.
	// The initial cut never exceeds it (negative values are treated as 0).
	MaxSeq int
}

// MissingReceive reports a receive whose message has no send evidence.
type MissingReceive struct {
	// MessageID is the identifier referenced by the orphan receive.
	MessageID string
	// Service is the service holding the receive event.
	Service string
	// Seq is the local sequence number of the receive event.
	Seq int
}

// Result is the reconstructed maximal consistent cut.
type Result struct {
	// Cut maps each service name to the largest retained event sequence
	// number (0 means the service retained no events).
	Cut map[string]int
	// InFlight lists messages whose send is inside the cut while their
	// receive is outside the cut (or no receive exists at all): messages
	// sent but not yet received on the reconstructed section.
	InFlight []string
	// MissingSource lists receives with no matching send anywhere, in the
	// order the receive events are encountered.
	MissingSource []MissingReceive
}

type eventRef struct {
	service string
	seq     int
}

// Reconstruct computes the maximal consistent cut across the given services.
// It returns an error only for malformed input; a cut that must retreat to zero
// is a valid result, not an error.
func Reconstruct(services []ServiceLog) (*Result, error) {
	if len(services) == 0 {
		return &Result{Cut: map[string]int{}}, nil
	}

	// Per-service events indexed by sequence, plus the highest sequence.
	index := make(map[string]map[int]Event, len(services))
	cut := make(map[string]int, len(services))
	lastSeq := make(map[string]int, len(services))
	seenService := make(map[string]bool, len(services))

	for _, svc := range services {
		if svc.Name == "" {
			return nil, errors.New("crosssection: service with empty name")
		}
		if seenService[svc.Name] {
			return nil, fmt.Errorf("crosssection: duplicate service %q", svc.Name)
		}
		seenService[svc.Name] = true

		bySeq := make(map[int]Event, len(svc.Events))
		prev := 0
		for _, ev := range svc.Events {
			if ev.Seq <= prev {
				return nil, fmt.Errorf(
					"crosssection: service %q event sequences must be strictly increasing (got %d after %d)",
					svc.Name, ev.Seq, prev)
			}
			prev = ev.Seq
			switch ev.Kind {
			case Send, Receive:
				if ev.MessageID == "" {
					return nil, fmt.Errorf(
						"crosssection: service %q event %d has empty MessageID",
						svc.Name, ev.Seq)
				}
			}
			bySeq[ev.Seq] = ev
		}
		index[svc.Name] = bySeq
		last := 0
		if len(svc.Events) > 0 {
			last = svc.Events[len(svc.Events)-1].Seq
		}
		lastSeq[svc.Name] = last

		// Initial cut: the highest prefix the cap permits, never beyond
		// the events that actually exist.
		initial := svc.MaxSeq
		if initial > last {
			initial = last
		}
		if initial < 0 {
			initial = 0
		}
		cut[svc.Name] = initial
	}

	// Locate the unique send and every receive for each message ID.
	sends := make(map[string]eventRef)
	receives := make(map[string][]eventRef)
	for _, svc := range services {
		for _, ev := range svc.Events {
			ref := eventRef{service: svc.Name, seq: ev.Seq}
			switch ev.Kind {
			case Send:
				if existing, ok := sends[ev.MessageID]; ok {
					return nil, fmt.Errorf(
						"crosssection: message %q has two sends (%s#%d and %s#%d)",
						ev.MessageID, existing.service, existing.seq, svc.Name, ev.Seq)
				}
				sends[ev.MessageID] = ref
			case Receive:
				receives[ev.MessageID] = append(receives[ev.MessageID], ref)
			}
		}
	}

	// Receives without any send evidence: mark them and force the cut of
	// their owning service to just before the receive.
	missing := make([]MissingReceive, 0)
	missingIDs := make(map[string]bool)
	for id, refs := range receives {
		if _, ok := sends[id]; ok {
			continue
		}
		missingIDs[id] = true
		for _, ref := range refs {
			missing = append(missing, MissingReceive{
				MessageID: id,
				Service:   ref.service,
				Seq:       ref.seq,
			})
			if before := ref.seq - 1; cut[ref.service] > before {
				cut[ref.service] = before
			}
		}
	}

	// Fixed-point cascade. Every adjustment only decreases a cut, so the
	// loop terminates; starting from the cap yields the maximal solution.
	changed := true
	for changed {
		changed = false
		for _, svc := range services {
			name := svc.Name
			for _, ev := range svc.Events {
				if ev.Seq > cut[name] {
					break // later events are outside the cut as well
				}
				if ev.Kind != Receive {
					continue
				}
				if missingIDs[ev.MessageID] {
					continue // already enforced above
				}
				send, ok := sends[ev.MessageID]
				if !ok {
					continue // defensively covered by missingIDs
				}
				// Retaining this receive requires the matching send to be
				// retained as well.
				if cut[send.service] < send.seq {
					if before := ev.Seq - 1; cut[name] > before {
						cut[name] = before
						changed = true
						break // restart the scan for this service
					}
				}
			}
		}
	}

	// Messages sent but not received on the section: the send is inside
	// the cut while the receive is either absent or outside the cut.
	inFlightSet := make(map[string]bool)
	for id, send := range sends {
		if cut[send.service] < send.seq {
			continue // send itself is cut away
		}
		retained := false
		for _, recv := range receives[id] {
			if cut[recv.service] >= recv.seq {
				retained = true
				break
			}
		}
		if !retained {
			inFlightSet[id] = true
		}
	}

	inFlight := make([]string, 0, len(inFlightSet))
	for id := range inFlightSet {
		inFlight = append(inFlight, id)
	}
	sort.Strings(inFlight)

	return &Result{
		Cut:           cut,
		InFlight:      inFlight,
		MissingSource: missing,
	}, nil
}
