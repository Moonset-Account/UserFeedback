//! Exact, partitionable, serializable binary64 summation state.
//!
//! Every finite IEEE 754 binary64 value is an integer multiple of
//! `2^-1074`.  A [`SumState`] therefore keeps the exact mathematical sum as a
//! signed integer in units of `2^-1074`, with no intermediate rounding.  The
//! single nearest-even rounding happens in [`SumState::finish_bits`].
//!
//! The total number of ingested rows is capped at [`MAX_ROWS`] (`2^31`).  Under
//! that cap the exact magnitude is below `2^2129`, so 34 little-endian `u64`
//! limbs (2176 bits) hold it with room to spare.  A serialized state is always
//! [`SERIALIZED_LEN`] (288) bytes, comfortably under the 1 KiB limit.
//!
//! States can be built for arbitrary partitions and combined in any binary
//! tree with [`SumState::merge`]; addition of the exact integers is associative
//! and commutative, so the final bits never depend on the partition shape.
//!
//! # Example
//!
//! ```
//! use partitioned_sum::SumState;
//!
//! let a = 2.0f64.powi(53);
//!
//! // Partition the column as {2^53, -2^53} and {1}.
//! let mut left = SumState::new();
//! left.add_f64(a).unwrap();
//! left.add_f64(-a).unwrap();
//!
//! let mut right = SumState::new();
//! right.add_f64(1.0).unwrap();
//!
//! left.merge(&right).unwrap();
//! assert_eq!(left.rows(), 3);
//! assert_eq!(left.finish_bits(), 1.0f64.to_bits());
//!
//! // A left-to-right f64 sum of the same values loses the 1.
//! assert_eq!((a + 1.0 - a).to_bits(), 0);
//! ```

#![forbid(unsafe_code)]

use std::fmt;

/// Maximum number of rows (input values) a state may represent, inclusive.
///
/// The bound is `2^31` = 2_147_483_648 rows.
pub const MAX_ROWS: u64 = 1 << 31;

/// Exact byte length of one serialized state.
pub const SERIALIZED_LEN: usize = 288;

/// Maximum accepted serialized state size (1 KiB).
///
/// [`SumState::to_bytes`] always emits [`SERIALIZED_LEN`] bytes; the larger
/// bound exists so [`SumState::from_bytes`] can report oversized inputs.
pub const MAX_SERIALIZED_LEN: usize = 1024;

/// Fixed result bits for a state containing NaN or both infinities.
pub const CANONICAL_NAN_BITS: u64 = 0x7ff8_0000_0000_0000;

/// Bits of positive zero, which is the canonical representation of exact zero.
pub const POSITIVE_ZERO_BITS: u64 = 0x0000_0000_0000_0000;

/// Bits of positive infinity.
pub const POSITIVE_INFINITY_BITS: u64 = 0x7ff0_0000_0000_0000;

/// Bits of negative infinity.
pub const NEGATIVE_INFINITY_BITS: u64 = 0xfff0_0000_0000_0000;

/// Number of magnitude limbs.
const LIMBS: usize = 34;

/// Fraction field mask of a binary64 value.
const FRAC_MASK: u64 = (1 << 52) - 1;

/// 2^53 - 1, the largest significand of a finite binary64 value.
const MAX_SIGNIFICAND: u64 = (1 << 53) - 1;

/// `(2^53 - 1) << 2045`, the magnitude of the largest finite binary64 in units
/// of `2^-1074`.
const fn max_finite_units() -> [u64; LIMBS] {
    let mut limbs = [0u64; LIMBS];
    // (2^53 - 1) << 2045: bits 0..=2 of the significand land in limb 31 at
    // offsets 61..=63, and bits 3..=52 land in limb 32 at offsets 0..=49.
    let sig = MAX_SIGNIFICAND;
    limbs[31] = (sig & 0x7) << 61;
    limbs[32] = sig >> 3;
    limbs
}

const MAX_FINITE_UNITS: [u64; LIMBS] = max_finite_units();

/// Error reported by fallible operations on a [`SumState`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SumError {
    /// Adding the rows would exceed [`MAX_ROWS`].  The state is unchanged.
    RowLimitExceeded,
    /// The byte slice is not a valid canonical serialized state.
    MalformedState,
    /// The byte slice is longer than [`MAX_SERIALIZED_LEN`].
    StateTooLarge,
}

impl fmt::Display for SumError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SumError::RowLimitExceeded => {
                write!(f, "row limit of {MAX_ROWS} rows would be exceeded")
            }
            SumError::MalformedState => write!(f, "malformed serialized sum state"),
            SumError::StateTooLarge => {
                write!(f, "serialized state exceeds {MAX_SERIALIZED_LEN} bytes")
            }
        }
    }
}

impl std::error::Error for SumError {}

/// Classification of the special values seen so far.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Finite,
    Nan,
    PosInf,
    NegInf,
}

impl Kind {
    fn code(self) -> u8 {
        match self {
            Kind::Finite => 0,
            Kind::Nan => 1,
            Kind::PosInf => 2,
            Kind::NegInf => 3,
        }
    }

    fn from_code(code: u8) -> Option<Self> {
        match code {
            0 => Some(Kind::Finite),
            1 => Some(Kind::Nan),
            2 => Some(Kind::PosInf),
            3 => Some(Kind::NegInf),
            _ => None,
        }
    }
}

/// Exact, partitionable summation state for IEEE 754 binary64 values.
///
/// Values are supplied as raw binary64 bit patterns.  Finite values are
/// accumulated exactly; NaN and infinities set a sticky classification so the
/// final result follows the policy documented on [`SumState::finish_bits`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SumState {
    kind: Kind,
    negative: bool,
    rows: u64,
    mag: [u64; LIMBS],
}

impl Default for SumState {
    fn default() -> Self {
        Self::new()
    }
}

impl SumState {
    /// Creates an empty state: exact zero with zero rows.
    pub fn new() -> Self {
        SumState {
            kind: Kind::Finite,
            negative: false,
            rows: 0,
            mag: [0u64; LIMBS],
        }
    }

    /// Number of rows ingested so far (including NaN and infinity rows).
    pub fn rows(&self) -> u64 {
        self.rows
    }

    /// Returns `true` when no rows have been ingested.
    pub fn is_empty(&self) -> bool {
        self.rows == 0
    }

    /// Ingests one value given as its binary64 bit pattern.
    ///
    /// Fails with [`SumError::RowLimitExceeded`] if the state already holds
    /// [`MAX_ROWS`] rows; in that case the state is unchanged.
    pub fn add_bits(&mut self, bits: u64) -> Result<(), SumError> {
        if self.rows == MAX_ROWS {
            return Err(SumError::RowLimitExceeded);
        }
        self.rows += 1;
        self.absorb(bits);
        Ok(())
    }

    /// Ingests one `f64` value.
    ///
    /// Equivalent to [`SumState::add_bits`] with `value.to_bits()`, so NaN
    /// payloads and signed zeros are preserved as bit patterns.
    pub fn add_f64(&mut self, value: f64) -> Result<(), SumError> {
        self.add_bits(value.to_bits())
    }

    /// Merges `other` into this state.
    ///
    /// This is the partition-combination primitive: any binary tree of merges
    /// over a partition of the input yields the same canonical state (and thus
    /// the same final bits).  On [`SumError::RowLimitExceeded`] this state is
    /// left unchanged.
    pub fn merge(&mut self, other: &SumState) -> Result<(), SumError> {
        if other.rows > MAX_ROWS - self.rows {
            return Err(SumError::RowLimitExceeded);
        }
        self.rows += other.rows;

        let a = self.kind;
        let b = other.kind;
        match (a, b) {
            (Kind::Nan, _) | (_, Kind::Nan) => self.become_nan(),
            (Kind::PosInf, Kind::NegInf) | (Kind::NegInf, Kind::PosInf) => self.become_nan(),
            (Kind::PosInf, _) | (_, Kind::PosInf) => self.become_inf(false),
            (Kind::NegInf, _) | (_, Kind::NegInf) => self.become_inf(true),
            (Kind::Finite, Kind::Finite) => self.merge_finite(other),
        }
        Ok(())
    }

    /// Returns the final result as a binary64 bit pattern.
    ///
    /// * Finite inputs are summed exactly and rounded once, to nearest with
    ///   ties to even.  Overflow follows that rule and produces the infinity
    ///   with the sign of the exact sum.
    /// * Exact zero is always positive zero ([`POSITIVE_ZERO_BITS`]).
    /// * If any input was NaN, or both positive and negative infinity were
    ///   seen, the result is the fixed quiet NaN [`CANONICAL_NAN_BITS`].
    /// * Otherwise, if infinity was seen, its sign is preserved.
    pub fn finish_bits(&self) -> u64 {
        match self.kind {
            Kind::Nan => CANONICAL_NAN_BITS,
            Kind::PosInf => POSITIVE_INFINITY_BITS,
            Kind::NegInf => NEGATIVE_INFINITY_BITS,
            Kind::Finite => round_units(&self.mag, self.negative),
        }
    }

    /// Serializes the state into its canonical 288-byte form.
    ///
    /// Layout (all integers little-endian):
    ///
    /// | offset | size | field                              |
    /// |--------|------|------------------------------------|
    /// | 0      | 4    | magic `PSUM`                       |
    /// | 4      | 1    | format version (`1`)               |
    /// | 5      | 1    | kind (`0` finite, `1` NaN, `2` +inf, `3` -inf) |
    /// | 6      | 1    | sign of magnitude (`0`/`1`)        |
    /// | 7      | 1    | reserved (`0`)                     |
    /// | 8      | 8    | row count                          |
    /// | 16     | 272  | 34 magnitude limbs in 2^-1074 units |
    pub fn to_bytes(&self) -> [u8; SERIALIZED_LEN] {
        let mut out = [0u8; SERIALIZED_LEN];
        out[0..4].copy_from_slice(b"PSUM");
        out[4] = 1;
        out[5] = self.kind.code();
        out[6] = u8::from(self.negative);
        out[7] = 0;
        out[8..16].copy_from_slice(&self.rows.to_le_bytes());
        for (i, limb) in self.mag.iter().enumerate() {
            let start = 16 + i * 8;
            out[start..start + 8].copy_from_slice(&limb.to_le_bytes());
        }
        out
    }

    /// Parses a canonical serialized state.
    ///
    /// Rejects inputs longer than [`MAX_SERIALIZED_LEN`] with
    /// [`SumError::StateTooLarge`], and any non-canonical or inconsistent
    /// encoding with [`SumError::MalformedState`].  In particular the row
    /// count must not exceed [`MAX_ROWS`], a magnitude must be representable by
    /// the rows that supposedly produced it, and zero/special states must use
    /// their unique canonical encoding.
    pub fn from_bytes(bytes: &[u8]) -> Result<Self, SumError> {
        if bytes.len() > MAX_SERIALIZED_LEN {
            return Err(SumError::StateTooLarge);
        }
        if bytes.len() != SERIALIZED_LEN || &bytes[0..4] != b"PSUM" {
            return Err(SumError::MalformedState);
        }
        if bytes[4] != 1 || bytes[7] != 0 {
            return Err(SumError::MalformedState);
        }
        let kind = Kind::from_code(bytes[5]).ok_or(SumError::MalformedState)?;
        let sign_byte = bytes[6];
        if sign_byte > 1 {
            return Err(SumError::MalformedState);
        }
        let negative = sign_byte == 1;

        let mut count_bytes = [0u8; 8];
        count_bytes.copy_from_slice(&bytes[8..16]);
        let rows = u64::from_le_bytes(count_bytes);
        if rows > MAX_ROWS {
            return Err(SumError::MalformedState);
        }

        let mut mag = [0u64; LIMBS];
        for (i, limb) in mag.iter_mut().enumerate() {
            let start = 16 + i * 8;
            let mut limb_bytes = [0u8; 8];
            limb_bytes.copy_from_slice(&bytes[start..start + 8]);
            *limb = u64::from_le_bytes(limb_bytes);
        }

        match kind {
            Kind::Finite => {
                if is_zero_mag(&mag) {
                    // The canonical exact-zero state is +0 with no sign flag.
                    if negative {
                        return Err(SumError::MalformedState);
                    }
                } else {
                    let bound =
                        scaled_max_units(rows).ok_or(SumError::MalformedState)?;
                    if !mag_le(&mag, &bound) {
                        return Err(SumError::MalformedState);
                    }
                }
            }
            Kind::Nan | Kind::PosInf | Kind::NegInf => {
                // Special states never retain a magnitude or sign, and can
                // only arise after ingesting at least one row.
                if rows == 0 || negative || !is_zero_mag(&mag) {
                    return Err(SumError::MalformedState);
                }
            }
        }

        Ok(SumState {
            kind,
            negative,
            rows,
            mag,
        })
    }

    /// Consumes an iterator of binary64 bit patterns and returns the final bits.
    pub fn sum_bits<I: IntoIterator<Item = u64>>(values: I) -> Result<u64, SumError> {
        let mut state = SumState::new();
        for bits in values {
            state.add_bits(bits)?;
        }
        Ok(state.finish_bits())
    }

    /// Consumes an iterator of `f64` values and returns the final bits.
    pub fn sum_f64<I: IntoIterator<Item = f64>>(values: I) -> Result<u64, SumError> {
        Self::sum_bits(values.into_iter().map(f64::to_bits))
    }

    /// Ingests one bit pattern without checking or updating the row limit.
    fn absorb(&mut self, bits: u64) {
        if self.kind == Kind::Nan {
            return;
        }
        let exponent = (bits >> 52) & 0x7ff;
        let fraction = bits & FRAC_MASK;
        if exponent == 0x7ff {
            if fraction != 0 {
                self.become_nan();
            } else if bits >> 63 == 1 {
                self.absorb_infinity(true);
            } else {
                self.absorb_infinity(false);
            }
            return;
        }
        if self.kind != Kind::Finite {
            return;
        }
        let negative = bits >> 63 == 1;
        let (units, shift) = if exponent == 0 {
            (fraction, 0u32)
        } else {
            ((1 << 52) | fraction, (exponent - 1) as u32)
        };
        if units != 0 {
            self.add_finite(negative, units, shift);
        }
    }

    /// Records an infinity and resolves it against any previous infinity.
    fn absorb_infinity(&mut self, negative: bool) {
        match self.kind {
            Kind::Finite => self.become_inf(negative),
            Kind::PosInf if negative => self.become_nan(),
            Kind::NegInf if !negative => self.become_nan(),
            _ => {}
        }
    }

    /// Adds `units * 2^(shift - 1074)` to the exact finite accumulator.
    fn add_finite(&mut self, negative: bool, units: u64, shift: u32) {
        let mut addend = [0u64; LIMBS];
        let limb_index = (shift / 64) as usize;
        let offset = shift % 64;
        let wide = (units as u128) << offset;
        addend[limb_index] = wide as u64;
        if offset != 0 {
            addend[limb_index + 1] = (wide >> 64) as u64;
        }

        if is_zero_mag(&self.mag) {
            self.mag = addend;
            self.negative = negative;
            return;
        }
        if self.negative == negative {
            self.mag = add_mag(&self.mag, &addend);
            return;
        }
        match cmp_mag(&self.mag, &addend) {
            std::cmp::Ordering::Equal => {
                self.mag = [0u64; LIMBS];
                self.negative = false;
            }
            std::cmp::Ordering::Greater => {
                self.mag = sub_mag(&self.mag, &addend);
            }
            std::cmp::Ordering::Less => {
                self.mag = sub_mag(&addend, &self.mag);
                self.negative = negative;
            }
        }
    }

    /// Merges the exact finite accumulators of two finite states.
    fn merge_finite(&mut self, other: &SumState) {
        let other_zero = is_zero_mag(&other.mag);
        if other_zero {
            if is_zero_mag(&self.mag) {
                self.negative = false;
            }
            return;
        }
        if is_zero_mag(&self.mag) {
            self.mag = other.mag;
            self.negative = other.negative;
            return;
        }
        if self.negative == other.negative {
            self.mag = add_mag(&self.mag, &other.mag);
            return;
        }
        match cmp_mag(&self.mag, &other.mag) {
            std::cmp::Ordering::Equal => {
                self.mag = [0u64; LIMBS];
                self.negative = false;
            }
            std::cmp::Ordering::Greater => {
                self.mag = sub_mag(&self.mag, &other.mag);
            }
            std::cmp::Ordering::Less => {
                self.mag = sub_mag(&other.mag, &self.mag);
                self.negative = other.negative;
            }
        }
    }

    fn become_nan(&mut self) {
        self.kind = Kind::Nan;
        self.negative = false;
        self.mag = [0u64; LIMBS];
    }

    fn become_inf(&mut self, negative: bool) {
        self.kind = if negative { Kind::NegInf } else { Kind::PosInf };
        self.negative = false;
        self.mag = [0u64; LIMBS];
    }
}

/// Rounds an exact magnitude in units of `2^-1074` to binary64 bits.
fn round_units(mag: &[u64; LIMBS], negative: bool) -> u64 {
    let sign = if negative { 1u64 << 63 } else { 0 };
    let len = mag_bit_len(mag);
    if len == 0 {
        // Exact zero is canonically positive.
        return POSITIVE_ZERO_BITS;
    }
    if len <= 52 {
        // Subnormal (or tiny normal-free) values are exact multiples of
        // 2^-1074 below 2^-1022, hence exactly representable.
        return sign | mag[0];
    }
    if len == 53 {
        // Smallest normal binade: exponent field 1, 53-bit significand.
        return sign | (1u64 << 52) | (mag[0] & FRAC_MASK);
    }

    let shift = len - 53;
    let mut significand = mag_shr_to_u64(mag, shift);
    let round_bit = mag_get_bit(mag, shift - 1);
    let sticky = mag_any_below(mag, shift - 1);
    let mut exponent = i64::from(len) - 52;

    if round_bit && (sticky || (significand & 1) == 1) {
        significand += 1;
        if significand == (1u64 << 53) {
            significand >>= 1;
            exponent += 1;
        }
    }

    if exponent > 2046 {
        // Round-to-nearest-even overflow yields the infinity with this sign.
        return sign | POSITIVE_INFINITY_BITS;
    }
    debug_assert!((1..=2046).contains(&exponent));
    sign | ((exponent as u64) << 52) | (significand & FRAC_MASK)
}

fn mag_bit_len(mag: &[u64; LIMBS]) -> u32 {
    for i in (0..LIMBS).rev() {
        if mag[i] != 0 {
            return (i as u32) * 64 + (64 - mag[i].leading_zeros());
        }
    }
    0
}

fn mag_get_bit(mag: &[u64; LIMBS], bit: u32) -> bool {
    let limb = (bit / 64) as usize;
    let offset = bit % 64;
    mag[limb] & (1u64 << offset) != 0
}

fn mag_any_below(mag: &[u64; LIMBS], limit: u32) -> bool {
    if limit == 0 {
        return false;
    }
    let limb = (limit / 64) as usize;
    let offset = limit % 64;
    for item in mag.iter().take(limb) {
        if *item != 0 {
            return true;
        }
    }
    if offset != 0 && mag[limb] & ((1u64 << offset) - 1) != 0 {
        return true;
    }
    false
}

fn mag_shr_to_u64(mag: &[u64; LIMBS], shift: u32) -> u64 {
    let limb = (shift / 64) as usize;
    let offset = shift % 64;
    if limb >= LIMBS {
        return 0;
    }
    let mut value = mag[limb] >> offset;
    if offset != 0 && limb + 1 < LIMBS {
        value |= mag[limb + 1] << (64 - offset);
    }
    value
}

fn is_zero_mag(mag: &[u64; LIMBS]) -> bool {
    mag.iter().all(|limb| *limb == 0)
}

fn add_mag(a: &[u64; LIMBS], b: &[u64; LIMBS]) -> [u64; LIMBS] {
    let mut out = [0u64; LIMBS];
    let mut carry = 0u64;
    for i in 0..LIMBS {
        let sum = u128::from(a[i]) + u128::from(b[i]) + u128::from(carry);
        out[i] = sum as u64;
        carry = (sum >> 64) as u64;
    }
    // The row cap keeps every reachable magnitude below 2^2129 < 2^2176.
    debug_assert_eq!(carry, 0, "magnitude overflowed {LIMBS} limbs");
    out
}

/// Subtracts `b` from `a`; the caller guarantees `a >= b`.
fn sub_mag(a: &[u64; LIMBS], b: &[u64; LIMBS]) -> [u64; LIMBS] {
    let mut out = [0u64; LIMBS];
    let mut borrow = 0u64;
    for i in 0..LIMBS {
        let (diff, borrow1) = a[i].overflowing_sub(b[i]);
        let (diff, borrow2) = diff.overflowing_sub(borrow);
        out[i] = diff;
        borrow = u64::from(borrow1) | u64::from(borrow2);
    }
    debug_assert_eq!(borrow, 0, "sub_mag called with a < b");
    out
}

fn cmp_mag(a: &[u64; LIMBS], b: &[u64; LIMBS]) -> std::cmp::Ordering {
    for i in (0..LIMBS).rev() {
        match a[i].cmp(&b[i]) {
            std::cmp::Ordering::Equal => {}
            other => return other,
        }
    }
    std::cmp::Ordering::Equal
}

fn mag_le(a: &[u64; LIMBS], b: &[u64; LIMBS]) -> bool {
    !matches!(cmp_mag(a, b), std::cmp::Ordering::Greater)
}

/// Computes `rows * MAX_FINITE_UNITS`, returning `None` if it does not fit in
/// [`LIMBS`] limbs.  This is the largest magnitude reachable after `rows` rows.
fn scaled_max_units(rows: u64) -> Option<[u64; LIMBS]> {
    let mut out = [0u64; LIMBS];
    let mut carry = 0u64;
    for i in 0..LIMBS {
        let product = u128::from(MAX_FINITE_UNITS[i]) * u128::from(rows) + u128::from(carry);
        out[i] = product as u64;
        carry = (product >> 64) as u64;
    }
    if carry == 0 {
        Some(out)
    } else {
        None
    }
}
