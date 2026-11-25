package sourceselect

import (
	"fmt"
	"net"
	"time"
)

// EventKind enumerates state changes that can be replayed.
type EventKind string

const (
	EventUpsertInterface EventKind = "upsert-interface"
	EventInterfaceState  EventKind = "interface-state"
	EventRemoveInterface EventKind = "remove-interface"

	EventUpsertAddress EventKind = "upsert-address"
	EventRemoveAddress EventKind = "remove-address"

	EventUpsertRoute EventKind = "upsert-route"
	EventRouteState  EventKind = "route-state"
	EventRemoveRoute EventKind = "remove-route"

	EventOpenConnection    EventKind = "open-connection"
	EventRebuildConnection EventKind = "rebuild-connection"
	EventCloseConnection   EventKind = "close-connection"
	EventEvaluate          EventKind = "evaluate"
)

// Event is one ordered replay record. Only fields relevant to Kind need to be
// set. Constructors below make the common cases less error-prone.
type Event struct {
	Time  time.Time
	Kind  EventKind
	Label string

	// Interface fields.
	Interface      Interface
	InterfaceID    string
	InterfaceState InterfaceState

	// Address fields. Address is used by upsert; IP may override the remove
	// target and otherwise Address.IP is used.
	Address Address
	IP      net.IP

	// Route fields.
	Route      Route
	RouteID    string
	RouteState RouteState

	// Connection fields.
	ConnectionID string
	Destination  net.IP
}

// EventResult is the observable effect of applying one event.
type EventResult struct {
	Index       int
	Time        time.Time
	Kind        EventKind
	Label       string
	Selection   Selection
	Connection  Connection
	Connections []Connection
	Rebuilds    []RebuildRecommendation
	NewRebuilds []RebuildRecommendation
	EgressDrift []EgressDrift
}

// Replayer applies ordered events and retains step results.
type Replayer struct {
	*Selector
	Steps []EventResult
}

// NewReplayer creates an empty selector-backed replayer.
func NewReplayer() *Replayer {
	return &Replayer{Selector: NewSelector()}
}

// Apply applies one event, evaluates retention, computes egress drift and
// appends the result to Steps.
func (r *Replayer) Apply(event Event) (EventResult, error) {
	result := EventResult{
		Index: len(r.Steps),
		Time:  event.Time,
		Kind:  event.Kind,
		Label: event.Label,
	}

	var err error
	switch event.Kind {
	case EventUpsertInterface:
		err = r.UpsertInterface(event.Interface)

	case EventInterfaceState:
		err = r.SetInterfaceState(event.InterfaceID, event.InterfaceState)

	case EventRemoveInterface:
		err = r.RemoveInterface(event.InterfaceID)

	case EventUpsertAddress:
		err = r.UpsertAddress(event.InterfaceID, event.Address)

	case EventRemoveAddress:
		ip := event.IP
		if ip == nil {
			ip = event.Address.IP
		}
		err = r.RemoveAddress(event.InterfaceID, ip)

	case EventUpsertRoute:
		err = r.UpsertRoute(event.Route)

	case EventRouteState:
		err = r.SetRouteState(event.RouteID, event.RouteState)

	case EventRemoveRoute:
		err = r.RemoveRoute(event.RouteID)

	case EventOpenConnection:
		result.Connection, err = r.OpenConnection(event.ConnectionID, event.Destination, event.Time)
		if err == nil {
			result.Selection, err = r.SelectSource(event.Destination, event.Time)
		}

	case EventRebuildConnection:
		result.Connection, err = r.RebuildConnection(event.ConnectionID, event.Time)
		if err == nil {
			result.Selection, err = r.SelectSource(result.Connection.Destination, event.Time)
		}

	case EventCloseConnection:
		err = r.CloseConnection(event.ConnectionID)

	case EventEvaluate:
		// Evaluation happens uniformly after the switch.

	default:
		err = fmt.Errorf("%w: unknown event kind %q", ErrInvalidArgument, event.Kind)
	}

	if err != nil {
		return result, fmt.Errorf("event %d (%s): %w", result.Index, event.Kind, err)
	}

	result.Rebuilds, result.NewRebuilds = r.EvaluateChanges(event.Time)
	result.EgressDrift = r.EgressDrift(event.Time)
	result.Connections = r.Connections()

	r.Steps = append(r.Steps, result)
	return result, nil
}

// Run applies all events in order and returns the final report.
func (r *Replayer) Run(events []Event) (*ReplayReport, error) {
	for _, event := range events {
		if _, err := r.Apply(event); err != nil {
			return r.report(), err
		}
	}
	return r.report(), nil
}

// Report returns a snapshot of all applied steps and current connections.
func (r *Replayer) Report() *ReplayReport {
	return r.report()
}

func (r *Replayer) report() *ReplayReport {
	connections := r.Connections()
	var rebuilds []RebuildRecommendation
	if len(connections) > 0 {
		// The last step already evaluated at its event time. For a report
		// after Run, reuse those recommendations rather than inventing a time.
		if len(r.Steps) > 0 {
			rebuilds = append([]RebuildRecommendation(nil), r.Steps[len(r.Steps)-1].Rebuilds...)
		}
	}
	return &ReplayReport{
		Steps:       append([]EventResult(nil), r.Steps...),
		Connections: connections,
		Rebuilds:    rebuilds,
	}
}

// Replay creates a fresh Replayer and runs events.
func Replay(events []Event) (*ReplayReport, error) {
	return NewReplayer().Run(events)
}

// ReplayReport contains step-by-step results and final state.
type ReplayReport struct {
	Steps       []EventResult
	Connections []Connection
	Rebuilds    []RebuildRecommendation
}

// Constructor helpers keep replay scenarios readable.

func UpsertInterfaceEvent(at time.Time, iface Interface) Event {
	return Event{Time: at, Kind: EventUpsertInterface, Interface: iface, InterfaceID: iface.ID}
}

func RemoveInterfaceEvent(at time.Time, id string) Event {
	return Event{Time: at, Kind: EventRemoveInterface, InterfaceID: id}
}

func UpsertAddressEvent(at time.Time, interfaceID string, addr Address) Event {
	return Event{Time: at, Kind: EventUpsertAddress, InterfaceID: interfaceID, Address: addr}
}

func RemoveAddressEvent(at time.Time, interfaceID string, ip net.IP) Event {
	return Event{Time: at, Kind: EventRemoveAddress, InterfaceID: interfaceID, IP: ip}
}

func UpsertRouteEvent(at time.Time, route Route) Event {
	return Event{Time: at, Kind: EventUpsertRoute, Route: route, RouteID: route.ID}
}

func RouteStateEvent(at time.Time, routeID string, state RouteState) Event {
	return Event{Time: at, Kind: EventRouteState, RouteID: routeID, RouteState: state}
}

func OpenConnectionEvent(at time.Time, id string, destination net.IP) Event {
	return Event{Time: at, Kind: EventOpenConnection, ConnectionID: id, Destination: destination}
}

func RebuildConnectionEvent(at time.Time, id string) Event {
	return Event{Time: at, Kind: EventRebuildConnection, ConnectionID: id}
}

func EvaluateEvent(at time.Time) Event {
	return Event{Time: at, Kind: EventEvaluate}
}
