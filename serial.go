// Package zonetransfer implements zone transfer planning (primary side) and
// transfer application (secondary side) for a model of DNS-style incremental
// (IXFR) and full (AXFR) zone transfer.
//
// The inputs are snapshots identified by an independent version ID plus a
// 32-bit SOA-style serial, and delta record sets with explicit from/to
// versions. Serial arithmetic follows RFC 1982: equal serial numbers with
// different contents cannot be chained, and a difference of exactly half the
// number space is incomparable.
package zonetransfer

const serialMod uint64 = 1 << 32
const halfCircle uint64 = serialMod / 2

// SerialOrder describes the RFC 1982 ordering relation of two 32-bit serials.
type SerialOrder int

const (
	// SerialOrderLess means a < b in serial-number space.
	SerialOrderLess SerialOrder = iota
	// SerialOrderEqual means a == b numerically.
	SerialOrderEqual
	// SerialOrderGreater means a > b in serial-number space.
	SerialOrderGreater
	// SerialOrderIncomparable means the unsigned difference is exactly half
	// the number space (2^31), so neither ordering is defined.
	SerialOrderIncomparable
)

// CompareSerial implements RFC 1982 section 3.2 comparison for 32-bit
// serial numbers. A difference of exactly 2^31 is reported as
// SerialIncomparable rather than ordered.
func CompareSerial(a, b uint32) SerialOrder {
	if a == b {
		return SerialOrderEqual
	}
	// The modular distance from a to b.
	d := (uint64(b) - uint64(a) + serialMod) % serialMod
	if d == halfCircle {
		return SerialOrderIncomparable
	}
	if d < halfCircle {
		return SerialOrderLess
	}
	return SerialOrderGreater
}

// SerialLess reports whether a precedes b. Incomparable serials and equal
// serials are not "less".
func SerialLess(a, b uint32) bool {
	return CompareSerial(a, b) == SerialOrderLess
}
