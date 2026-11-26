// Package dnssecpreflight performs offline DNSSEC validation-gap analysis.
//
// It deliberately does not implement DNSSEC cryptography. Callers provide a
// Verifier callback, allowing production code to use real DNSKEY/RRSIG crypto
// and tests to use deterministic synthetic signatures.
package dnssecpreflight

import "fmt"

// SerialModulus is the size of the 32-bit RRSIG inception/expiration space.
const SerialModulus int64 = 1 << 32

// HalfCircle is one half of the serial space. Valid input windows are strictly
// shorter than this.
const HalfCircle int64 = 1 << 31

// Timestamp is a 32-bit DNSSEC signature timestamp.
type Timestamp uint32

// Algorithm is a DNSSEC algorithm number.
type Algorithm uint8

// KeyTag is a 16-bit DNSKEY/RRSIG key tag. A tag is only a candidate index.
type KeyTag uint16

// Common DNSSEC algorithm numbers used in tests and documentation.
const (
	AlgRSAMD5           Algorithm = 1
	AlgDSASHA1          Algorithm = 3
	AlgRSASHA1          Algorithm = 5
	AlgRSASHA1NSEC3SHA1 Algorithm = 7
	AlgRSASHA256        Algorithm = 8
	AlgRSASHA512        Algorithm = 10
	AlgECDSAP256SHA256  Algorithm = 13
	AlgECDSAP384SHA384  Algorithm = 14
	AlgED25519          Algorithm = 15
	AlgED448            Algorithm = 16
)

// Interval is an unwrapped, half-open integer-second interval [Start, End).
//
// DNSSEC expiration timestamps are inclusive. Therefore an RRSIG expiring at
// second e has End == unwrap(e) + 1. This makes adjacent validity periods
// contiguous when one signature starts at e+1.
type Interval struct {
	Start int64 `json:"start"`
	End   int64 `json:"end"`
}

// Length returns the number of integer seconds in the interval.
func (in Interval) Length() int64 {
	if in.End < in.Start {
		return 0
	}
	return in.End - in.Start
}

// Contains reports whether t is in [Start, End).
func (in Interval) Contains(t int64) bool {
	return t >= in.Start && t < in.End
}

// Intersects reports whether two half-open intervals overlap.
func (in Interval) Intersects(other Interval) bool {
	return in.Start < other.End && other.Start < in.End
}

// Clip returns the intersection with horizon.
func (in Interval) Clip(horizon Interval) (Interval, bool) {
	start := maxInt64(in.Start, horizon.Start)
	end := minInt64(in.End, horizon.End)
	if start >= end {
		return Interval{}, false
	}
	return Interval{Start: start, End: end}, true
}

// StartTimestamp converts the unwrapped start back to its 32-bit value.
func (in Interval) StartTimestamp() Timestamp { return Timestamp(uint32(in.Start)) }

// EndTimestamp converts the exclusive end back to a 32-bit value.
func (in Interval) EndTimestamp() Timestamp { return Timestamp(uint32(in.End)) }

// LastTimestamp returns the inclusive final second (End - 1) as uint32.
func (in Interval) LastTimestamp() Timestamp { return Timestamp(uint32(in.End - 1)) }

// CircularInterval is a 32-bit timestamp view of an Interval.
type CircularInterval struct {
	Start           Timestamp `json:"start"`
	EndInclusive    Timestamp `json:"end_inclusive"`
	WrapsZero       bool      `json:"wraps_zero"`
	DurationSeconds int64     `json:"duration_seconds"`
}

// Circular renders the interval using raw 32-bit boundary timestamps.
func (in Interval) Circular() CircularInterval {
	start := in.StartTimestamp()
	last := in.LastTimestamp()
	return CircularInterval{
		Start:           start,
		EndInclusive:    last,
		WrapsZero:       uint32(start) > uint32(last),
		DurationSeconds: in.Length(),
	}
}

// UnwrapTimestamp maps t to the occurrence of its modulo-2^32 timeline nearest
// to near. This is the standard serial-number interpretation needed around the
// 32-bit wrap.
func UnwrapTimestamp(t Timestamp, near int64) int64 {
	v := int64(uint32(t))
	k := (near - v) / SerialModulus
	best := v + k*SerialModulus
	for _, candidateK := range []int64{k - 1, k + 1} {
		candidate := v + candidateK*SerialModulus
		if absInt64(candidate-near) < absInt64(best-near) {
			best = candidate
		}
	}
	return best
}

// UnwrapWindow expands an RRSIG [Inception, Expiration] inclusive window into
// an unwrapped half-open interval. The forward serial distance must be strictly
// below 2^31 as required by the preflight input contract.
func UnwrapWindow(inception, expiration Timestamp, near int64) (Interval, error) {
	delta := int64(int32(uint32(expiration) - uint32(inception)))
	if delta < 0 || delta >= HalfCircle {
		return Interval{}, fmt.Errorf(
			"dnssecpreflight: RRSIG window %d..%d has serial length outside allowed (0,%d)",
			inception, expiration, HalfCircle)
	}
	start := UnwrapTimestamp(inception, near)
	return Interval{Start: start, End: start + delta + 1}, nil
}

// alignWindow shifts an already unwrapped periodic window by whole serial
// cycles so that it is the occurrence most relevant to horizon.
func alignWindow(window, horizon Interval) Interval {
	if horizon.End <= horizon.Start {
		return window
	}
	k := (horizon.Start - window.Start) / SerialModulus
	best := shiftInterval(window, k*SerialModulus)
	bestOverlap := overlapLength(best, horizon)
	for _, candidateK := range []int64{k - 1, k + 1, k - 2, k + 2} {
		candidate := shiftInterval(window, candidateK*SerialModulus)
		overlap := overlapLength(candidate, horizon)
		if overlap > bestOverlap ||
			(overlap == bestOverlap && overlap > 0 && candidate.Start < best.Start) ||
			(overlap == 0 && absInt64(candidate.Start-horizon.Start) < absInt64(best.Start-horizon.Start)) {
			best, bestOverlap = candidate, overlap
		}
	}
	return best
}

func shiftInterval(in Interval, delta int64) Interval {
	return Interval{Start: in.Start + delta, End: in.End + delta}
}

func overlapLength(a, b Interval) int64 {
	lo := maxInt64(a.Start, b.Start)
	hi := minInt64(a.End, b.End)
	if hi <= lo {
		return 0
	}
	return hi - lo
}

func uniqueSortedInt64(values []int64) []int64 {
	if len(values) == 0 {
		return nil
	}
	// Small input sizes make sort+compact straightforward.
	for i := 0; i < len(values); i++ {
		for j := i + 1; j < len(values); j++ {
			if values[j] < values[i] {
				values[i], values[j] = values[j], values[i]
			}
		}
	}
	out := values[:1]
	for _, v := range values[1:] {
		if v != out[len(out)-1] {
			out = append(out, v)
		}
	}
	return out
}

func minInt64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}

func maxInt64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

func absInt64(v int64) int64 {
	if v < 0 {
		return -v
	}
	return v
}
