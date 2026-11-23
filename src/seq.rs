//! 32-bit wrapping sequence numbers mapped onto a monotonic flat `u64` space.
//!
//! Wire sequence numbers are modular `u32` values. Internally every byte
//! position is represented by a flat `u64` number that never wraps, which lets
//! ordinary interval arithmetic express ranges that straddle the 2^32 line.
//! The mapping uses the standard half-circle rule: a wire value is the unique
//! representative within `2^31` of a reference point. Any active window is
//! required to be smaller than half the circle, so the mapping is unambiguous
//! for every legal input.

/// Modulus of the wire sequence-number space.
pub const WRAP: u64 = 1u64 << 32;
/// Half the circle: the largest legal window span.
pub const HALF: u64 = 1u64 << 31;

/// A sequence number exactly as it appears on the wire.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Wire(pub u32);

/// A half-open byte range expressed in wire values; `end` may wrap relative to
/// `start`, and must span strictly less than `2^31` bytes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WireRange {
    /// First byte of the range.
    pub start: u32,
    /// One past the last byte; interpreted modulo 2^32.
    pub end: u32,
}

impl WireRange {
    /// Construct a wire range.
    pub fn new(start: u32, end: u32) -> Self {
        Self { start, end }
    }

    /// Length of the range in bytes, never zero for a non-empty range.
    pub fn len(&self) -> u64 {
        (u64::from(self.end).wrapping_sub(u64::from(self.start))) & (WRAP - 1)
    }

    /// Whether the range covers zero bytes.
    pub fn is_empty(&self) -> bool {
        self.start == self.end
    }
}

/// Flat value assigned to wire sequence number zero on the first wrap; keeps
/// flat numbers positive so subtraction cannot underflow.
const BASE: u64 = WRAP;

/// Flat number of the initial (pre-wrap) sequence number `isn`.
pub fn initial_flat(isn: u32) -> u64 {
    BASE + u64::from(isn)
}

/// Map a wire value to its unique flat representative within half a circle of
/// `ref_flat`.
pub fn unwrap_value(value: u32, ref_flat: u64) -> u64 {
    let forward =
        (u64::from(value).wrapping_sub(ref_flat & (WRAP - 1))) & (WRAP - 1);
    if forward >= HALF {
        // The representative is one circle behind the reference's cycle.
        ref_flat - (WRAP - forward)
    } else {
        // The representative is in the reference's cycle or a later one.
        ref_flat + forward
    }
}

/// Unwrap a wire range relative to `ref_flat`; returns the flat half-open
/// range. Errors if the range spans at least half the circle.
pub fn unwrap_range(range: WireRange, ref_flat: u64) -> Result<(u64, u64), String> {
    let len = range.len();
    if len >= HALF {
        return Err(format!(
            "wire range [{}, {}) spans {len} bytes, half-circle limit is {HALF}",
            range.start, range.end
        ));
    }
    let start = unwrap_value(range.start, ref_flat);
    Ok((start, start + len))
}

/// Convert a flat value back to its wire representation.
pub fn wrap_value(flat: u64) -> u32 {
    (flat & (WRAP - 1)) as u32
}

/// Split a flat `[start, end)` range into wire ranges that individually do not
/// cross the 2^32 boundary, so every returned `WireRange` is a simple
/// non-wrapping segment on the wire.
pub fn split_to_wire(start: u64, end: u64) -> Vec<WireRange> {
    let mut out = Vec::new();
    let mut cursor = start;
    while cursor < end {
        let cycle_end = (cursor & !(WRAP - 1)) + WRAP;
        let piece_end = end.min(cycle_end);
        out.push(WireRange::new(
            wrap_value(cursor),
            wrap_value(piece_end),
        ));
        cursor = piece_end;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unwrap_follows_half_circle_rule() {
        let r = initial_flat(1000);
        assert_eq!(unwrap_value(1005, r), r + 5);
        // 100 bytes "behind" the reference must map backwards.
        assert_eq!(unwrap_value(900, r), r - 100);
    }

    #[test]
    fn unwrap_range_handles_wraparound() {
        // Reference sits near the end of the circle.
        let r = initial_flat(u32::MAX - 10);
        let (s, e) = unwrap_range(WireRange::new(u32::MAX - 4, 6), r).unwrap();
        assert_eq!(e - s, 11);
        assert_eq!(wrap_value(s), u32::MAX - 4);
        assert_eq!(wrap_value(e), 6);
        assert!(s < e);
    }

    #[test]
    fn half_circle_violation_is_rejected() {
        let r = initial_flat(0);
        assert!(unwrap_range(WireRange::new(0, HALF as u32), r).is_err());
    }

    #[test]
    fn split_at_cycle_boundary() {
        let start = initial_flat(u32::MAX - 4);
        let pieces = split_to_wire(start, start + 10);
        assert_eq!(
            pieces,
            vec![
                WireRange::new(u32::MAX - 4, 0),
                WireRange::new(0, 5),
            ]
        );
    }

    #[test]
    fn empty_wire_range_has_zero_length() {
        assert_eq!(WireRange::new(42, 42).len(), 0);
    }
}
