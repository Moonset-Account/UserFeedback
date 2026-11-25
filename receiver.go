package pppframe

import "fmt"

// DropReason identifies why a frame was not delivered to the upper layer.
type DropReason uint8

const (
	DropNone DropReason = iota
	// DropShortFrame means the unstuffed frame had fewer than two FCS bytes.
	DropShortFrame
	// DropFCS means the 16-bit PPP FCS did not verify.
	DropFCS
	// DropInvalidEscape means 0x7d was followed by a flag or otherwise did
	// not form a valid byte-stuffing sequence at a boundary.
	DropInvalidEscape
	// DropOversized means the configured payload limit was exceeded.
	DropOversized
	// DropReset means a local/link reset occurred before a frame ended.
	DropReset
	// DropBareControl is used only with BareControlRejectFrame.
	DropBareControl

	dropReasonCount
)

// String returns a stable short diagnostic name.
func (r DropReason) String() string {
	switch r {
	case DropNone:
		return "none"
	case DropShortFrame:
		return "short_frame"
	case DropFCS:
		return "fcs_error"
	case DropInvalidEscape:
		return "invalid_escape"
	case DropOversized:
		return "oversized_frame"
	case DropReset:
		return "reset"
	case DropBareControl:
		return "bare_mapped_control"
	default:
		return fmt.Sprintf("unknown_drop_reason_%d", uint8(r))
	}
}

// DroppedFrame is diagnostic evidence for one undelivered frame. Byte slices
// are owned by the value and do not alias Receiver scratch storage.
type DroppedFrame struct {
	Reason DropReason

	// Sequence is the 1-based number among all drops from this receiver.
	Sequence uint64

	// Data contains decoded bytes accumulated before the drop. For FCS and
	// short-frame drops this includes the trailing candidate FCS bytes.
	Data []byte

	// DiscardedControls lists unescaped receive-mapped bytes omitted from
	// Data under the default byte-discard policy.
	DiscardedControls []byte
}

// Stats is a point-in-time copy of receiver counters.
type Stats struct {
	BytesReceived     uint64
	FramesAccepted    uint64
	EmptyDelimiters   uint64
	ControlsDiscarded uint64
	Resyncs           uint64
	Drops             uint64

	dropCounts [dropReasonCount]uint64
}

// DropCount returns cumulative drops for one reason.
func (s Stats) DropCount(reason DropReason) uint64 {
	if reason <= DropNone || int(reason) >= len(s.dropCounts) {
		return 0
	}
	return s.dropCounts[reason]
}

// Report contains all frames and diagnostics completed by one Feed call.
type Report struct {
	// Frames contains copied upper-layer payloads.
	Frames [][]byte

	// Drops contains drops completed during this Feed call. Reset drops are
	// returned by Receiver.Reset and are also available from Drops.
	Drops []DroppedFrame

	// EmptyDelimiters is the number of zero-content flag boundaries in this
	// call. Consecutive flags normally contribute to this count.
	EmptyDelimiters int

	// DiscardedControls lists bare mapped-control bytes in this call.
	DiscardedControls []byte

	// Resyncs is the number of reset/oversize/reject recovery flags seen in
	// this call.
	Resyncs int
}

// Receiver is a chunk-tolerant, stateful PPP frame receiver. It is not safe
// for concurrent use; Adapter provides serialized access.
type Receiver struct {
	initialized bool

	rxMap      ControlMap
	pendingMap *ControlMap
	maxPayload int
	policy     BareControlPolicy

	buf      []byte
	controls []byte
	fcs      uint16

	escape   bool
	dirty    bool
	seenFlag bool
	resync   bool

	drops []DroppedFrame
	stats Stats
}

// NewReceiver creates a receiver using receiveMap. Options may override
// size and bare-control policy.
func NewReceiver(receiveMap ControlMap, opts ...Option) *Receiver {
	s := defaultSettings()
	for _, opt := range opts {
		opt(&s)
	}
	r := &Receiver{
		initialized: true,
		rxMap:       receiveMap,
		maxPayload:  s.maxPayload,
		policy:      s.bare,
		fcs:         fcsInit,
	}
	if r.maxPayload == 0 {
		r.maxPayload = DefaultMaxPayloadSize
	}
	return r
}

func (r *Receiver) ensureInit() {
	if r.initialized {
		return
	}
	r.initialized = true
	r.fcs = fcsInit
	if r.maxPayload == 0 {
		r.maxPayload = DefaultMaxPayloadSize
	}
}

// ReceiveMap returns the active confirmed receive map.
func (r *Receiver) ReceiveMap() ControlMap {
	r.ensureInit()
	return r.rxMap
}

// PendingReceiveMap returns a map waiting for the next frame boundary.
func (r *Receiver) PendingReceiveMap() (ControlMap, bool) {
	r.ensureInit()
	if r.pendingMap == nil {
		return 0, false
	}
	return *r.pendingMap, true
}

// UpdateReceiveMap installs a newly confirmed receive map. If a frame is
// currently being assembled or reset resynchronization is pending, the map
// is staged and applied at the next flag boundary. It returns true when the
// map became active immediately.
func (r *Receiver) UpdateReceiveMap(m ControlMap) bool {
	r.ensureInit()
	if r.dirty || r.resync {
		pending := m
		r.pendingMap = &pending
		return false
	}
	r.rxMap = m
	r.pendingMap = nil
	return true
}

// Stats returns a copy of cumulative receiver statistics.
func (r *Receiver) Stats() Stats {
	r.ensureInit()
	return r.stats
}

// Drops returns a copy of all cumulative drop records.
func (r *Receiver) Drops() []DroppedFrame {
	r.ensureInit()
	return append([]DroppedFrame(nil), r.drops...)
}

// Reset reports an unfinished-frame reset and puts the receiver in resync
// mode. Bytes are ignored until the next Flag delimiter; that flag is
// consumed as the resynchronization point. If dirty reports false, no
// partial-frame Drop is returned, but the receiver still waits for a flag.
func (r *Receiver) Reset() (DroppedFrame, bool) {
	r.ensureInit()
	var d DroppedFrame
	hasDrop := false
	if r.dirty {
		d = r.recordDrop(DropReset)
		hasDrop = true
	}
	r.resync = true
	r.resetFrame()
	return d, hasDrop
}

// Feed accepts an arbitrarily sized batch of serial bytes and returns all
// frames and diagnostics completed by this batch. An escape prefix and its
// successor may be in different Feed calls.
func (r *Receiver) Feed(chunk []byte) Report {
	r.ensureInit()
	rep := Report{}

	for _, b := range chunk {
		r.stats.BytesReceived++

		if r.resync {
			if b == Flag {
				r.stats.Resyncs++
				rep.Resyncs++
				r.boundary()
			}
			continue
		}

		if b == Flag {
			r.handleFlag(&rep)
			continue
		}

		// A pending escape must consume the next octet even if that octet
		// is 0x7d. Flag was handled above because a literal flag after
		// escape does not form the standard escaped-flag sequence.
		if r.escape {
			r.escape = false
			r.dirty = true
			r.appendDecoded(b^EscapeXOR, &rep)
			continue
		}

		if b == Escape {
			r.escape = true
			r.dirty = true
			continue
		}

		if r.rxMap.Has(b) {
			switch r.policy {
			case BareControlAcceptByte:
				r.dirty = true
				r.appendDecoded(b, &rep)
			case BareControlRejectFrame:
				d := r.recordDrop(DropBareControl)
				rep.Drops = append(rep.Drops, d)
				r.resync = true
				r.resetFrame()
			default:
				r.dirty = true
				r.stats.ControlsDiscarded++
				r.controls = append(r.controls, b)
				rep.DiscardedControls = append(rep.DiscardedControls, b)
			}
			continue
		}

		r.appendDecoded(b, &rep)
	}

	return rep
}

func (r *Receiver) appendDecoded(b byte, rep *Report) {
	r.dirty = true
	if r.maxPayload > 0 && len(r.buf) >= r.maxPayload+2 {
		d := r.recordDrop(DropOversized)
		rep.Drops = append(rep.Drops, d)
		r.resync = true
		r.resetFrame()
		return
	}

	r.buf = append(r.buf, b)
	r.fcs = fcsUpdateByte(r.fcs, b)
}

func (r *Receiver) handleFlag(rep *Report) {
	if r.escape {
		d := r.recordDrop(DropInvalidEscape)
		rep.Drops = append(rep.Drops, d)
		r.boundary()
		return
	}

	if !r.dirty {
		// The first flag after construction is a hunting/sync flag, not a
		// complete empty frame. Every later flag with no intervening octets
		// completes an empty delimiter.
		if r.seenFlag {
			r.stats.EmptyDelimiters++
			rep.EmptyDelimiters++
		}
		r.boundary()
		return
	}

	if len(r.buf) < 2 {
		d := r.recordDrop(DropShortFrame)
		rep.Drops = append(rep.Drops, d)
	} else if r.fcs != fcsGood {
		d := r.recordDrop(DropFCS)
		rep.Drops = append(rep.Drops, d)
	} else {
		payloadLen := len(r.buf) - 2
		payload := make([]byte, payloadLen)
		copy(payload, r.buf[:payloadLen])
		rep.Frames = append(rep.Frames, payload)
		r.stats.FramesAccepted++
	}

	r.boundary()
}

func (r *Receiver) recordDrop(reason DropReason) DroppedFrame {
	r.stats.Drops++
	r.stats.dropCounts[reason]++

	d := DroppedFrame{
		Reason:            reason,
		Sequence:          r.stats.Drops,
		Data:              append([]byte(nil), r.buf...),
		DiscardedControls: append([]byte(nil), r.controls...),
	}
	r.drops = append(r.drops, d)
	return d
}

func (r *Receiver) resetFrame() {
	r.buf = r.buf[:0]
	r.controls = r.controls[:0]
	r.fcs = fcsInit
	r.escape = false
	r.dirty = false
}

// boundary ends the current frame and applies a map confirmed for this
// direction at this frame boundary.
func (r *Receiver) boundary() {
	r.resync = false
	r.resetFrame()
	r.seenFlag = true
	if r.pendingMap != nil {
		r.rxMap = *r.pendingMap
		r.pendingMap = nil
	}
}
