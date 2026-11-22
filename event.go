package rcu

import "fmt"

// EventKind identifies one input to the grace-period coordinator.
type EventKind int

const (
	// KindEnterRead starts (or nests) a thread's RCU read-side section.
	KindEnterRead EventKind = iota + 1
	// KindExitRead ends one nesting level of a thread's read-side section.
	KindExitRead
	// KindMigrate records that a thread moved to another CPU. The thread
	// keeps the reader identity (and outermost generation) it already had.
	KindMigrate
	// KindCPUOnline marks a CPU online.
	KindCPUOnline
	// KindCPUOffline marks a CPU offline. It never ends a read section.
	KindCPUOffline
	// KindCPUQuiescent records that a CPU announced a quiescent state.
	// It never ends a thread's read section.
	KindCPUQuiescent
	// KindRegisterCallback queues a reclaim callback for a source.
	KindRegisterCallback
	// KindAckCallback records a reclaim receipt for one callback.
	KindAckCallback
)

var eventKindNames = map[EventKind]string{
	KindEnterRead:        "enter_read",
	KindExitRead:         "exit_read",
	KindMigrate:          "migrate",
	KindCPUOnline:        "cpu_online",
	KindCPUOffline:       "cpu_offline",
	KindCPUQuiescent:     "cpu_quiescent",
	KindRegisterCallback: "register_callback",
	KindAckCallback:      "ack_callback",
}

func (k EventKind) String() string {
	if name, ok := eventKindNames[k]; ok {
		return name
	}
	return fmt.Sprintf("EventKind(%d)", int(k))
}

// Event is one replayable input to the coordinator. It is a plain value so a
// caller can persist a slice of events and replay it later.
type Event struct {
	Kind     EventKind `json:"kind"`
	Thread   string    `json:"thread,omitempty"`
	CPU      int       `json:"cpu,omitempty"`
	Callback string    `json:"callback,omitempty"`
	Source   string    `json:"source,omitempty"`
}

// EnterRead reports that thread entered its read-side section. If the thread
// was already inside one, the section is nested and keeps its outermost
// generation.
func EnterRead(thread string) Event {
	return Event{Kind: KindEnterRead, Thread: thread}
}

// ExitRead reports that thread left one level of its read-side section.
func ExitRead(thread string) Event {
	return Event{Kind: KindExitRead, Thread: thread}
}

// Migrate reports that thread now runs on cpu. Migration does not change the
// reader identity of a thread that is inside a read-side section.
func Migrate(thread string, cpu int) Event {
	return Event{Kind: KindMigrate, Thread: thread, CPU: cpu}
}

// CPUOnline reports that cpu came online.
func CPUOnline(cpu int) Event {
	return Event{Kind: KindCPUOnline, CPU: cpu}
}

// CPUOffline reports that cpu went offline. This is not a quiescent state and
// does not end any read-side section.
func CPUOffline(cpu int) Event {
	return Event{Kind: KindCPUOffline, CPU: cpu}
}

// CPUQuiescent reports that cpu announced a quiescent state. The announcement
// is recorded but never substitutes for a thread leaving its read section.
func CPUQuiescent(cpu int) Event {
	return Event{Kind: KindCPUQuiescent, CPU: cpu}
}

// RegisterCallback queues callback id from source. Its wait set is the set of
// reader instances that are inside a read-side section at this moment.
func RegisterCallback(id, source string) Event {
	return Event{Kind: KindRegisterCallback, Callback: id, Source: source}
}

// AckCallback records the reclaimer's receipt for callback id.
func AckCallback(id string) Event {
	return Event{Kind: KindAckCallback, Callback: id}
}

func (e Event) String() string {
	switch e.Kind {
	case KindEnterRead, KindExitRead:
		return fmt.Sprintf("%s(%s)", e.Kind, e.Thread)
	case KindMigrate:
		return fmt.Sprintf("%s(%s,cpu=%d)", e.Kind, e.Thread, e.CPU)
	case KindCPUOnline, KindCPUOffline, KindCPUQuiescent:
		return fmt.Sprintf("%s(cpu=%d)", e.Kind, e.CPU)
	case KindRegisterCallback:
		return fmt.Sprintf("%s(%s,source=%s)", e.Kind, e.Callback, e.Source)
	case KindAckCallback:
		return fmt.Sprintf("%s(%s)", e.Kind, e.Callback)
	default:
		return e.Kind.String()
	}
}

// LoggedEvent is an accepted event together with the sequence number the
// coordinator assigned to it. Sequence numbers start at 1 and are contiguous.
type LoggedEvent struct {
	Seq   int64 `json:"seq"`
	Event Event `json:"event"`
}

// Outcome reports what an accepted event changed. BecameEffective lists the
// callbacks that became releasable because of this event, in delivery order.
// A callback whose registration has an empty wait set becomes effective on its
// own registration event.
type Outcome struct {
	Seq             int64
	Kind            EventKind
	BecameEffective []string
}
