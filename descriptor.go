// Package dmaring implements a DMA descriptor queue for device drivers.
//
// The package has two layers:
//
//   - BuildDescriptors packs logically ordered physical segments into the
//     minimum number of legal hardware descriptors, merging physically adjacent
//     segments while honoring the per-descriptor byte limit and the power-of-two
//     address boundary constraint.
//   - Ring is a fixed-size circular queue of descriptor slots. A whole request
//     is reserved atomically; the production pointer only moves when enough free
//     slots exist. Requests may complete out of order, but only the oldest
//     continuously completed requests release their slots.
package dmaring

import "errors"

// Package-level sentinel errors.
var (
	// ErrInvalidLimits reports an illegal capacity or descriptor limit.
	ErrInvalidLimits = errors.New("dmaring: invalid descriptor limits or ring capacity")
	// ErrEmptyRequest reports a request without any transfer segment.
	ErrEmptyRequest = errors.New("dmaring: request contains no segments")
	// ErrZeroLength reports a segment whose Length is zero.
	ErrZeroLength = errors.New("dmaring: segment has zero length")
	// ErrOverflow reports that address plus length wraps the uint64 address space.
	ErrOverflow = errors.New("dmaring: address/length overflow")
	// ErrNoSpace reports that the ring cannot atomically reserve all slots the
	// request needs. The production pointer is left untouched in that case.
	ErrNoSpace = errors.New("dmaring: not enough free descriptor slots")
	// ErrDuplicateRequest reports a Submit whose request ID is already outstanding.
	ErrDuplicateRequest = errors.New("dmaring: duplicate request id")
	// ErrUnknownRequest reports completion or lookup of a request that is not
	// outstanding (never submitted or already reaped).
	ErrUnknownRequest = errors.New("dmaring: unknown request id")
)

// Limits describes the hardware constraints every descriptor must satisfy.
type Limits struct {
	// MaxBytes is the maximum number of bytes a single descriptor may cover.
	// It must be non-zero.
	MaxBytes uint64
	// Boundary must be zero or a power of two. When non-zero, no descriptor may
	// cross an address that is an integer multiple of Boundary. Zero disables
	// the boundary constraint.
	Boundary uint64
}

func (l Limits) validate() error {
	if l.MaxBytes == 0 {
		return ErrInvalidLimits
	}
	if l.Boundary != 0 && l.Boundary&(l.Boundary-1) != 0 {
		return ErrInvalidLimits
	}
	return nil
}

// Segment is one physically contiguous transfer segment. Segments passed to the
// queue must already be in logical (byte stream) order.
type Segment struct {
	PhysAddr uint64
	Length   uint64
}

// Descriptor is one hardware DMA descriptor: a physical start address and the
// number of bytes covered.
type Descriptor struct {
	PhysAddr uint64
	Length   uint64
}

// add64 returns a+b and false when the addition overflows uint64.
func add64(a, b uint64) (uint64, bool) {
	sum := a + b
	if sum < a {
		return 0, false
	}
	return sum, true
}

// boundaryRoom returns how many bytes starting at addr may be covered before the
// next power-of-two boundary. A zero boundary means "no boundary".
func boundaryRoom(addr, boundary uint64) uint64 {
	if boundary == 0 {
		return ^uint64(0)
	}
	return boundary - (addr & (boundary - 1))
}

// extendable returns how many more bytes the descriptor starting at startAddr
// with current length used may legally absorb, honoring both MaxBytes and the
// boundary measured from the descriptor's start.
func extendable(startAddr, used, maxBytes, boundary uint64) uint64 {
	room := maxBytes - used
	if b := boundaryRoom(startAddr, boundary); b-used < room {
		room = b - used
	}
	return room
}

// BuildDescriptors packs logically ordered physical segments into the minimum
// number of legal descriptors.
//
// Rules applied, in input order:
//
//   - adjacent physical segments (the next segment starts exactly where the
//     previous byte ended) are merged into the current descriptor while it can
//     still be legally extended;
//   - every descriptor covers at most Limits.MaxBytes bytes;
//   - when Limits.Boundary is non-zero, no descriptor crosses an address that is
//     a multiple of Boundary;
//   - a segment that does not fit the current descriptor is split across as few
//     descriptors as possible, each filled greedily.
//
// The returned descriptors preserve the original byte order and cover exactly
// the bytes of the input segments.
func BuildDescriptors(segs []Segment, l Limits) ([]Descriptor, error) {
	if err := l.validate(); err != nil {
		return nil, err
	}
	if len(segs) == 0 {
		return nil, ErrEmptyRequest
	}

	out := make([]Descriptor, 0)
	for si := range segs {
		seg := segs[si]
		if seg.Length == 0 {
			return nil, ErrZeroLength
		}
		// Reject segments whose inclusive byte range wraps the address space.
		if _, ok := add64(seg.PhysAddr, seg.Length); !ok {
			return nil, ErrOverflow
		}

		addr := seg.PhysAddr
		remaining := seg.Length

		for remaining > 0 {
			// Try to extend the previous descriptor when physically adjacent.
			if n := len(out); n > 0 {
				prev := &out[n-1]
				end, ok := add64(prev.PhysAddr, prev.Length)
				if !ok {
					return nil, ErrOverflow
				}
				if end == addr {
					avail := extendable(prev.PhysAddr, prev.Length, l.MaxBytes, l.Boundary)
					if avail > 0 {
						ext := avail
						if ext > remaining {
							ext = remaining
						}
						prev.Length += ext
						addr += ext
						remaining -= ext
						continue
					}
				}
			}

			// Start a fresh descriptor and fill it as far as legally possible.
			size := l.MaxBytes
			if room := boundaryRoom(addr, l.Boundary); room < size {
				size = room
			}
			if size > remaining {
				size = remaining
			}
			out = append(out, Descriptor{PhysAddr: addr, Length: size})
			addr += size
			remaining -= size
		}
	}

	return out, nil
}
