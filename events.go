package neighborqueue

import "time"

// Event is the common interface of all simulated inputs the queue accepts.
type Event interface{ isEvent() }

// SendEvent is a simulated outbound packet request.
type SendEvent struct {
	Target Target
	Packet Packet
}

// ResolveEvent signals that neighbor resolution or a confirmation probe has
// completed.
//
//   - For an Incomplete entry, Link must be non-empty on success.
//   - For a Confirm entry, Receipt must identify the exact probe packet.
//   - A non-nil Err fails the pending attempt and returns affected packets.
type ResolveEvent struct {
	IfIdx   IfIndex
	Target  Target
	Link    LinkAddr
	Receipt Receipt
	Err     error
}

// TickEvent advances the simulation clock and processes timeouts.
type TickEvent struct {
	Now time.Time
}

// InterfaceChangeEvent moves a target's route onto NewIf. The new interface
// gets its own independent neighbor entry; old-interface state is preserved.
type InterfaceChangeEvent struct {
	Target Target
	NewIf  IfIndex
}

func (SendEvent) isEvent()            {}
func (ResolveEvent) isEvent()         {}
func (TickEvent) isEvent()            {}
func (InterfaceChangeEvent) isEvent() {}
