package egress

import (
	"errors"
	"fmt"
	"sort"
)

// Class identifies one of the two FIFO queues.
type Class int

const (
	Fast Class = iota + 1
	Normal
)

func (c Class) String() string {
	switch c {
	case Fast:
		return "fast"
	case Normal:
		return "normal"
	default:
		return "unknown"
	}
}

// GateMask says which class may start/transmit in a gate entry.
type GateMask uint8

const (
	GateClosed GateMask = 0
	GateFast   GateMask = 1 << iota
	GateNormal
	GateBoth = GateFast | GateNormal
)

// GateEntry is one duration-indexed row of the periodic gate table.
type GateEntry struct {
	Duration int      `json:"duration"`
	Gates    GateMask `json:"gates"`
}

// GateTable is a periodic gate control list. Origin is the absolute tick at
// which entry zero starts; it is normally zero for a known phase.
type GateTable struct {
	Entries []GateEntry `json:"entries"`
	// Period, when non-zero, must equal the sum of entry durations. Zero means
	// the sum is used.
	Period int `json:"period,omitempty"`
	// Guard reserves the last Guard integer-byte ticks before a class gate
	// closes. A fragment is legal only when End+Guard <= gateClose.
	Guard  int `json:"guard"`
	Origin int `json:"origin,omitempty"`
}

// Frame is one arrival reservation. TieOrder is normally assigned from input
// order so equal arrivals do not make results map-iteration dependent.
type Frame struct {
	ID        string `json:"id"`
	Class     Class  `json:"class"`
	Arrival   int    `json:"arrival"`
	Length    int    `json:"length"`
	Deadline  int    `json:"deadline"`
	TieOrder  int    `json:"-"`
	Committed bool   `json:"committed,omitempty"`
}

// Config is the input to the exact offline scheduler.
type Config struct {
	Table         *GateTable `json:"table"`
	Frames        []Frame    `json:"frames"`
	MinNonLast    int        `json:"min_non_last"`
	Overhead      int        `json:"overhead"`
	StartAt       int        `json:"start_at,omitempty"`
	MaxStateNodes int        `json:"-"`
}

// Fragment is one concrete on-chain transmission. End is exclusive: the
// fragment occupies integer ticks [Start,End) and End=Start+Overhead+Data.
type Fragment struct {
	FrameID  string `json:"frame_id"`
	Class    Class  `json:"class"`
	Index    int    `json:"index"`
	Start    int    `json:"start"`
	Overhead int    `json:"overhead"`
	Data     int    `json:"data"`
	End      int    `json:"end"`
	Bytes    int    `json:"bytes"`
	Final    bool   `json:"final"`
}

// FrameResult gives the completion tick and accounting for one frame.
type FrameResult struct {
	ID            string `json:"id"`
	Class         Class  `json:"class"`
	Arrival       int    `json:"arrival"`
	Length        int    `json:"length"`
	Deadline      int    `json:"deadline"`
	Completion    int    `json:"completion"`
	OnTime        bool   `json:"on_time"`
	FragmentCount int    `json:"fragment_count"`
	DataBytes     int    `json:"data_bytes"`
	OverheadBytes int    `json:"overhead_bytes"`
	OccupiedBytes int    `json:"occupied_bytes"`
}

// Plan is a complete schedule.
type Plan struct {
	Fragments     []Fragment             `json:"fragments"`
	Frames        []FrameResult          `json:"frames"`
	FrameByID     map[string]FrameResult `json:"frame_by_id"`
	FastOnTime    int                    `json:"fast_on_time"`
	NormalOnTime  int                    `json:"normal_on_time"`
	OverheadBytes int                    `json:"overhead_bytes"`
	DataBytes     int                    `json:"data_bytes"`
	OccupiedBytes int                    `json:"occupied_bytes"`
	Makespan      int                    `json:"makespan"`
}

// UnsendableError reports a frame that can never fit even one legal fragment.
type UnsendableError struct {
	FrameID string
	Class   Class
	Reason  string
}

func (e *UnsendableError) Error() string {
	return fmt.Sprintf("frame %s (%s) is unsendable: %s", e.FrameID, e.Class, e.Reason)
}

var ErrUnsendable = errors.New("unsendable frame")

func (e *UnsendableError) Unwrap() error { return ErrUnsendable }

// StateLimitError keeps the exhaustive solver from growing without bound.
type StateLimitError struct {
	Limit int
}

func (e *StateLimitError) Error() string {
	return fmt.Sprintf("scheduler state limit %d reached", e.Limit)
}

type orderedFrame struct {
	Frame
}

func sortFrames(frames []Frame) []Frame {
	out := make([]Frame, len(frames))
	copy(out, frames)
	for i := range out {
		if out[i].TieOrder == 0 && i > 0 {
			// Keep caller order unless the caller explicitly set TieOrder.
			out[i].TieOrder = i + 1
		} else if out[i].TieOrder == 0 {
			out[i].TieOrder = 1
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Arrival != out[j].Arrival {
			return out[i].Arrival < out[j].Arrival
		}
		return out[i].TieOrder < out[j].TieOrder
	})
	return out
}

func validateConfig(cfg Config) ([]Frame, error) {
	if cfg.Table == nil {
		return nil, errors.New("nil gate table")
	}
	if err := cfg.Table.Validate(); err != nil {
		return nil, err
	}
	if cfg.Overhead < 0 {
		return nil, errors.New("overhead must be non-negative")
	}
	if cfg.MinNonLast <= 0 {
		return nil, errors.New("minimum non-last fragment data must be positive")
	}
	if cfg.StartAt < 0 {
		return nil, errors.New("start time must be non-negative")
	}
	if len(cfg.Frames) == 0 {
		return nil, errors.New("at least one frame is required")
	}
	seen := map[string]bool{}
	for i, f := range cfg.Frames {
		if f.ID == "" {
			return nil, fmt.Errorf("frame %d has empty ID", i)
		}
		if seen[f.ID] {
			return nil, fmt.Errorf("duplicate frame ID %q", f.ID)
		}
		seen[f.ID] = true
		if f.Class != Fast && f.Class != Normal {
			return nil, fmt.Errorf("frame %q has invalid class %d", f.ID, f.Class)
		}
		if f.Arrival < 0 || f.Length <= 0 || f.Deadline < 0 {
			return nil, fmt.Errorf("frame %q has invalid arrival/length/deadline", f.ID)
		}
	}
	return sortFrames(cfg.Frames), nil
}
