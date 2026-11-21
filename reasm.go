// Package tcprx implements the receive-side reassembly logic of an
// established TCP connection. The caller supplies the first expected
// sequence number and a fixed receive window that must be smaller than
// half of the 32-bit sequence space, so all sequence comparisons can be
// done safely with modular (serial number) arithmetic.
package tcprx

// Segment is one incoming TCP segment.
type Segment struct {
	Seq  uint32 // 32-bit sequence number of the first data byte
	Data []byte // payload bytes
	FIN  bool   // whether the segment carries FIN (consumes one sequence number)
}

// Output is the result of feeding one segment into the reassembler.
type Output struct {
	Data []byte // bytes newly delivered to the upper layer, in order
	Ack  uint32 // cumulative acknowledgment number (next expected sequence)
	Fin  bool   // true only on the single call that consumes the FIN
}

// buffered is an out-of-order payload held until the gap before it closes.
type buffered struct {
	start uint32
	data  []byte
}

// Reassembler holds the receive state of one direction of a connection.
type Reassembler struct {
	next   uint32 // next expected sequence number (cumulative ACK)
	win    uint32 // fixed receive window, must be < 2^31
	segs   []buffered
	finSeq uint32 // sequence number occupied by the pending FIN
	hasFin bool   // a FIN was accepted but its preceding bytes are incomplete
	ended  bool   // FIN has been consumed; the stream is closed
}

// New creates a Reassembler. firstSeq is the first sequence number the
// receiver expects; window is the fixed receive window and must be in
// (0, 2^31).
func New(firstSeq uint32, window uint32) *Reassembler {
	if window == 0 || window >= 1<<31 {
		panic("tcprx: window must be in (0, 2^31)")
	}
	return &Reassembler{next: firstSeq, win: window}
}

// Ended reports whether the FIN has been consumed.
func (r *Reassembler) Ended() bool { return r.ended }

// Ack returns the current cumulative acknowledgment number.
func (r *Reassembler) Ack() uint32 { return r.next }

// Accept feeds one segment and reports what became deliverable.
// Retransmitted overlapping bytes are assumed to carry identical content.
func (r *Reassembler) Accept(seg Segment) Output {
	out := Output{Ack: r.next}
	if r.ended {
		return out // stream already closed; ignore further input
	}

	data := seg.Data
	fin := seg.FIN
	start := seg.Seq

	total := uint32(len(data))
	if fin {
		total++
	}
	if total == 0 {
		return out
	}

	// Trim the part that lies before the window [next, next+win).
	if d := int32(r.next - start); d > 0 {
		if uint32(d) >= total {
			return out // entirely old/duplicate
		}
		if uint32(d) <= uint32(len(data)) {
			data = data[d:]
		} else {
			data = nil
		}
		start += uint32(d)
		total -= uint32(d)
	}

	// Trim the part that lies beyond the window.
	hi := r.next + r.win
	if ex := int32((start + total) - hi); ex > 0 {
		if uint32(ex) >= total {
			return out // entirely beyond the window
		}
		total -= uint32(ex)
		if total <= uint32(len(data)) {
			data = data[:total]
			fin = false // FIN fell outside the window
		}
	}

	if fin {
		r.finSeq = start + uint32(len(data))
		r.hasFin = true
	}
	if len(data) > 0 {
		cp := make([]byte, len(data))
		copy(cp, data)
		r.segs = append(r.segs, buffered{start: start, data: cp})
	}

	// Deliver the contiguous prefix starting at next. Overlapping
	// segments are reconciled byte by byte; retransmitted bytes agree.
	var delivered []byte
	for {
		progressed := false
		for _, s := range r.segs {
			off := int32(r.next - s.start)
			if off >= 0 && uint32(off) < uint32(len(s.data)) {
				delivered = append(delivered, s.data[off])
				r.next++
				progressed = true
				break
			}
		}
		if !progressed {
			break
		}
	}

	// Drop fully consumed buffered segments.
	kept := r.segs[:0]
	for _, s := range r.segs {
		if int32((s.start + uint32(len(s.data))) - r.next) <= 0 {
			continue
		}
		kept = append(kept, s)
	}
	r.segs = kept

	// FIN takes effect only once every byte ahead of it is delivered.
	if r.hasFin && r.finSeq == r.next {
		r.next++ // FIN consumes one sequence number
		r.hasFin = false
		r.ended = true
		out.Fin = true
	}

	out.Data = delivered
	out.Ack = r.next
	return out
}
