//! Integration tests for the partitionable binary64 sum state.
//!
//! Coverage:
//! * the required 2^53 + 1 + (-2^53) partition invariance over every set
//!   partition and several merge trees, plus a naive-sum contrast;
//! * hand-derived nearest-even boundary cases (ties, subnormals, overflow);
//! * NaN / infinity policy;
//! * serialization round-trips, fixed size, malformed and oversized input;
//! * the 2^31 row cap, checked atomically via crafted canonical states;
//! * randomized differential tests against an independent oracle written in
//!   this file (dynamic limbs, different rounding derivation) over three
//!   magnitude bands: subnormal, fractional, and near-overflow.

use partitioned_sum::{
    SumError, SumState, CANONICAL_NAN_BITS, MAX_ROWS, MAX_SERIALIZED_LEN, NEGATIVE_INFINITY_BITS,
    POSITIVE_INFINITY_BITS, POSITIVE_ZERO_BITS, SERIALIZED_LEN,
};

const ONE_BITS: u64 = 0x3FF0_0000_0000_0000;
const TWO53_BITS: u64 = 0x4340_0000_0000_0000;
const NEG_TWO53_BITS: u64 = 0xC340_0000_0000_0000;

fn f(bits: u64) -> f64 {
    f64::from_bits(bits)
}

fn bits(x: f64) -> u64 {
    x.to_bits()
}

// ---------------------------------------------------------------------------
// Required property: 2^53, 1, -2^53 sum to 1 for any partition.
// ---------------------------------------------------------------------------

fn sum_of(values: &[u64]) -> u64 {
    SumState::sum_bits(values.iter().copied()).expect("within row budget")
}

/// Merge a list of per-partition states in the given parenthesization order,
/// here expressed as a simple left fold and a balanced fold.
fn left_fold(parts: &[SumState]) -> SumState {
    let mut acc = SumState::new();
    for part in parts {
        acc.merge(part).unwrap();
    }
    acc
}

fn balanced_fold(parts: &[SumState]) -> SumState {
    if parts.len() <= 1 {
        return parts.first().cloned().unwrap_or_default();
    }
    let mid = parts.len() / 2;
    let mut left = balanced_fold(&parts[..mid]);
    let right = balanced_fold(&parts[mid..]);
    left.merge(&right).unwrap();
    left
}

/// All set partitions of `items`, each as a list of groups.
fn set_partitions(items: &[u64]) -> Vec<Vec<Vec<u64>>> {
    fn go(
        remaining: &[u64],
        groups: Vec<Vec<u64>>,
        out: &mut Vec<Vec<Vec<u64>>>,
    ) {
        if remaining.is_empty() {
            out.push(groups);
            return;
        }
        let item = remaining[0];
        let rest = &remaining[1..];
        for i in 0..groups.len() {
            let mut next = groups.clone();
            next[i].push(item);
            go(rest, next, out);
        }
        let mut next = groups.clone();
        next.push(vec![item]);
        go(rest, next, out);
    }
    let mut out = Vec::new();
    go(items, Vec::new(), &mut out);
    out
}

#[test]
fn required_three_values_sum_to_one_for_every_partition() {
    let values = [TWO53_BITS, ONE_BITS, NEG_TWO53_BITS];

    for partition in set_partitions(&values) {
        let parts: Vec<SumState> = partition
            .iter()
            .map(|group| {
                let mut s = SumState::new();
                for &b in group {
                    s.add_bits(b).unwrap();
                }
                s
            })
            .collect();

        for order in [0usize, 1] {
            let merged = if order == 0 {
                left_fold(&parts)
            } else {
                balanced_fold(&parts)
            };
            assert_eq!(
                merged.finish_bits(),
                ONE_BITS,
                "partition {partition:?} order {order} must be exactly 1"
            );
            assert_eq!(merged.rows(), 3);
        }
    }

    // A non-contiguous arbitrary partition, built value by value.
    let mut a = SumState::new();
    let mut b = SumState::new();
    a.add_bits(TWO53_BITS).unwrap();
    a.add_bits(NEG_TWO53_BITS).unwrap();
    b.add_bits(ONE_BITS).unwrap();
    a.merge(&b).unwrap();
    assert_eq!(a.finish_bits(), ONE_BITS);

    // Direct ingestion in the given order agrees.
    assert_eq!(sum_of(&values), ONE_BITS);
}

#[test]
fn naive_left_to_right_f64_sum_loses_the_unit_but_this_does_not() {
    let values = [f(TWO53_BITS), 1.0, f(NEG_TWO53_BITS)];
    let naive = values.iter().fold(0.0f64, |acc, v| acc + v);
    // 2^53 + 1 rounds back to 2^53, so the naive total cancels to zero.
    assert_eq!(bits(naive), POSITIVE_ZERO_BITS);
    assert_eq!(bits(1.0), ONE_BITS);
    // The exact state keeps the same result regardless of ingestion order.
    assert_eq!(sum_of(&[TWO53_BITS, ONE_BITS, NEG_TWO53_BITS]), ONE_BITS);
    assert_eq!(sum_of(&[ONE_BITS, NEG_TWO53_BITS, TWO53_BITS]), ONE_BITS);
    assert_eq!(sum_of(&[NEG_TWO53_BITS, TWO53_BITS, ONE_BITS]), ONE_BITS);
}

// ---------------------------------------------------------------------------
// Hand-derived nearest-even boundaries.
//
// These bit patterns were derived from the IEEE 754 definition, not produced
// by the implementation: each is the uniquely correct nearest-even result of
// the stated exact sum.
// ---------------------------------------------------------------------------

#[test]
fn exact_ties_round_to_even() {
    let two53 = 2.0f64.powi(53);
    let cases: &[(&[f64], u64, &str)] = &[
        // 2^53 + 1 is the midpoint of 2^53 and 2^53+2; 2^53 has even
        // significand, so the tie rounds down.
        (&[two53, 1.0], TWO53_BITS, "2^53 + 1 -> 2^53"),
        // 2^53 + 2 is representable exactly.
        (&[two53, 2.0], 0x4340_0000_0000_0001, "2^53 + 2 exact"),
        // 2^53 + 3 is the midpoint of +2 and +4; +4 has even significand.
        (&[two53, 3.0], 0x4340_0000_0000_0002, "2^53 + 3 -> 2^53+4"),
        // 1 + 2^-53 is the midpoint between 1 and 1+2^-52; 1 is even.
        (&[1.0, 2.0f64.powi(-53)], ONE_BITS, "1 + 2^-53 -> 1"),
        // 1 + 3*2^-53 is the midpoint of 1+ulp and 1+2ulp; 1+2ulp is even.
        (
            &[1.0, 3.0 * 2.0f64.powi(-53)],
            0x3FF0_0000_0000_0002,
            "1 + 3*2^-53 -> 1+2ulp",
        ),
        // Same tie in the negative direction.
        (
            &[-1.0, -(2.0f64.powi(-53))],
            0xBFF0_0000_0000_0000,
            "-(1 + 2^-53) -> -1",
        ),
    ];
    for (values, expected, label) in cases {
        assert_eq!(SumState::sum_f64(values.iter().copied()).unwrap(), *expected, "{label}");
    }
}

#[test]
fn tiny_terms_survive_cancellation() {
    // Naive summation would drop the subnormal entirely.
    assert_eq!(
        SumState::sum_f64([1.0, f64::from_bits(1), -1.0]).unwrap(),
        0x0000_0000_0000_0001
    );
    // 1 + min_subnormal rounds back to 1 at output time.
    assert_eq!(SumState::sum_f64([1.0, f64::from_bits(1)]).unwrap(), ONE_BITS);
    // 1 + 2^-53 - 1 is exactly 2^-53.
    assert_eq!(
        SumState::sum_f64([1.0, 2.0f64.powi(-53), -1.0]).unwrap(),
        0x3CA0_0000_0000_0000
    );
    // 2^53 + 1 - 2^53 is exactly 1.
    assert_eq!(
        SumState::sum_f64([2.0f64.powi(53), 1.0, -2.0f64.powi(53)]).unwrap(),
        ONE_BITS
    );
}

#[test]
fn subnormal_and_smallest_normal_arithmetic_is_exact_until_output() {
    let min_sub = f64::from_bits(1);
    let max_sub = f64::from_bits(0x000F_FFFF_FFFF_FFFF);
    let min_normal = f64::from_bits(0x0010_0000_0000_0000); // 2^-1022
    let cases: &[(&[f64], u64, &str)] = &[
        (&[min_sub, min_sub, min_sub], 0x0000_0000_0000_0003, "3*min_sub"),
        (
            &[max_sub, max_sub, min_sub],
            0x001F_FFFF_FFFF_FFFF,
            "2*max_sub + min_sub = 2^-1021 - 2^-1074",
        ),
        (
            &[min_normal, -max_sub],
            0x0000_0000_0000_0001,
            "smallest normal - max_sub = min_sub",
        ),
        (
            &[min_normal, -min_sub],
            0x000F_FFFF_FFFF_FFFF,
            "smallest normal - min_sub = max_sub",
        ),
    ];
    for (values, expected, label) in cases {
        assert_eq!(SumState::sum_f64(values.iter().copied()).unwrap(), *expected, "{label}");
    }
}

#[test]
fn overflow_follows_round_to_nearest_even() {
    let max = f64::from_bits(0x7FEF_FFFF_FFFF_FFFF); // 2^1024 - 2^971
    let two969 = f64::from_bits(((969 + 1023) as u64) << 52);
    let two970 = f64::from_bits(((970 + 1023) as u64) << 52);

    // max + 2^969 is strictly below the overflow midpoint: stays finite.
    assert_eq!(
        SumState::sum_f64([max, two969]).unwrap(),
        0x7FEF_FFFF_FFFF_FFFF
    );
    // max + 2^970 is exactly the midpoint; ties-to-even selects 2^1024
    // (even significand), which overflows to +infinity.
    assert_eq!(
        SumState::sum_f64([max, two970]).unwrap(),
        POSITIVE_INFINITY_BITS
    );
    // Twice the maximum finite value overflows.
    assert_eq!(
        SumState::sum_f64([max, max]).unwrap(),
        POSITIVE_INFINITY_BITS
    );
    // Negative overflow keeps the sign.
    assert_eq!(
        SumState::sum_f64([-max, -max]).unwrap(),
        NEGATIVE_INFINITY_BITS
    );
    // Large cancellation that stays finite must not report overflow.
    assert_eq!(SumState::sum_f64([max, max, -max]).unwrap(), 0x7FEF_FFFF_FFFF_FFFF);
}

#[test]
fn exact_zero_is_always_positive() {
    assert_eq!(SumState::new().finish_bits(), POSITIVE_ZERO_BITS);
    assert_eq!(SumState::sum_f64([0.0, -0.0]).unwrap(), POSITIVE_ZERO_BITS);
    assert_eq!(SumState::sum_f64([-0.0, -0.0]).unwrap(), POSITIVE_ZERO_BITS);
    assert_eq!(SumState::sum_bits([0x8000_0000_0000_0000]).unwrap(), POSITIVE_ZERO_BITS);
    // Nonzero values that cancel exactly yield +0, not -0.
    assert_eq!(SumState::sum_f64([1.0, -1.0]).unwrap(), POSITIVE_ZERO_BITS);
    assert_eq!(
        SumState::sum_f64([-2.0f64.powi(53), 2.0f64.powi(53)]).unwrap(),
        POSITIVE_ZERO_BITS
    );
}

// ---------------------------------------------------------------------------
// NaN and infinity policy.
// ---------------------------------------------------------------------------

#[test]
fn nan_and_infinity_policy() {
    let nan_payload = f64::from_bits(0x7FF0_0000_0000_0001);
    let neg_nan = f64::from_bits(0xFFF8_0000_0000_0000);
    let inf = f64::from_bits(POSITIVE_INFINITY_BITS);
    let ninf = f64::from_bits(NEGATIVE_INFINITY_BITS);

    // A single infinity keeps its sign.
    assert_eq!(SumState::sum_f64([inf]).unwrap(), POSITIVE_INFINITY_BITS);
    assert_eq!(SumState::sum_f64([ninf]).unwrap(), NEGATIVE_INFINITY_BITS);
    assert_eq!(
        SumState::sum_f64([inf, 1.0, -2.0]).unwrap(),
        POSITIVE_INFINITY_BITS
    );
    assert_eq!(
        SumState::sum_f64([ninf, 1.0, 2.0]).unwrap(),
        NEGATIVE_INFINITY_BITS
    );
    // Both infinities give the fixed quiet NaN.
    assert_eq!(SumState::sum_f64([inf, ninf]).unwrap(), CANONICAL_NAN_BITS);
    assert_eq!(SumState::sum_f64([ninf, inf]).unwrap(), CANONICAL_NAN_BITS);
    // Any NaN, whatever its payload or sign, gives the same fixed NaN.
    assert_eq!(SumState::sum_f64([nan_payload]).unwrap(), CANONICAL_NAN_BITS);
    assert_eq!(SumState::sum_f64([neg_nan]).unwrap(), CANONICAL_NAN_BITS);
    assert_eq!(SumState::sum_f64([nan_payload, inf]).unwrap(), CANONICAL_NAN_BITS);
    assert_eq!(SumState::sum_f64([neg_nan, ninf, 1.0]).unwrap(), CANONICAL_NAN_BITS);
    // NaN is sticky even after a later cancelling pair.
    assert_eq!(
        SumState::sum_f64([nan_payload, 1.0, -1.0]).unwrap(),
        CANONICAL_NAN_BITS
    );
}

// ---------------------------------------------------------------------------
// Serialization.
// ---------------------------------------------------------------------------

#[test]
fn serialized_state_has_fixed_size_within_budget() {
    let mut state = SumState::new();
    state.add_f64(f64::from_bits(TWO53_BITS)).unwrap();
    state.add_f64(1.0).unwrap();
    let bytes = state.to_bytes();
    assert_eq!(bytes.len(), SERIALIZED_LEN);
    assert_eq!(SERIALIZED_LEN, 16 + 34 * 8);
    assert!(bytes.len() <= MAX_SERIALIZED_LEN);
    assert!(MAX_SERIALIZED_LEN <= 1024);
}

#[test]
fn serialization_round_trip_preserves_result_and_merges() {
    let mut original = SumState::new();
    for v in [TWO53_BITS, ONE_BITS, NEG_TWO53_BITS, 0x8000_0000_0000_0000] {
        original.add_bits(v).unwrap();
    }
    let restored = SumState::from_bytes(&original.to_bytes()).unwrap();
    assert_eq!(restored, original);
    assert_eq!(restored.rows(), original.rows());
    assert_eq!(restored.finish_bits(), ONE_BITS);

    // Serialized replicas can be merged and still agree.
    let mut merged = SumState::from_bytes(&original.to_bytes()).unwrap();
    merged.merge(&restored).unwrap();
    assert_eq!(merged.rows(), 8);
    // Each replica sums to exactly 1, so the merged state is exactly 2.
    assert_eq!(merged.finish_bits(), 0x4000_0000_0000_0000);
}

#[test]
fn special_states_round_trip() {
    for bits_in in [CANONICAL_NAN_BITS, POSITIVE_INFINITY_BITS, NEGATIVE_INFINITY_BITS] {
        let mut state = SumState::new();
        state.add_bits(bits_in).unwrap();
        let restored = SumState::from_bytes(&state.to_bytes()).unwrap();
        assert_eq!(restored, state);
        assert_eq!(restored.finish_bits(), bits_in);
    }
}

#[test]
fn malformed_and_oversized_states_are_rejected() {
    let mut good = SumState::new();
    good.add_f64(2.0f64.powi(53)).unwrap();
    good.add_f64(1.0).unwrap();
    let bytes = good.to_bytes();

    // Wrong length and wrong magic.
    assert_eq!(SumState::from_bytes(&[]), Err(SumError::MalformedState));
    assert_eq!(SumState::from_bytes(&bytes[..287]), Err(SumError::MalformedState));
    let mut bad = bytes;
    bad[0] = b'X';
    assert_eq!(SumState::from_bytes(&bad), Err(SumError::MalformedState));

    // Wrong version, unknown kind, bad sign byte, nonzero reserved byte.
    for (index, value) in [(4usize, 2u8), (5, 9), (6, 2), (7, 1)] {
        let mut bad = bytes;
        bad[index] = value;
        assert_eq!(SumState::from_bytes(&bad), Err(SumError::MalformedState), "byte {index}");
    }

    // Oversized input is reported separately.
    let oversized = vec![0u8; MAX_SERIALIZED_LEN + 1];
    assert_eq!(SumState::from_bytes(&oversized), Err(SumError::StateTooLarge));
    // Exactly the budget but not the canonical length is malformed.
    let budget = vec![0u8; MAX_SERIALIZED_LEN];
    assert_eq!(SumState::from_bytes(&budget), Err(SumError::MalformedState));
}

#[test]
fn inconsistent_row_and_magnitude_encodings_are_rejected() {
    // A zero state with the negative flag set is non-canonical.
    let mut bad = SumState::new().to_bytes();
    bad[6] = 1;
    assert_eq!(SumState::from_bytes(&bad), Err(SumError::MalformedState));

    // A special state must have rows > 0 and no magnitude/sign.
    let mut special = SumState::new();
    special.add_bits(POSITIVE_INFINITY_BITS).unwrap();
    let mut bad = special.to_bytes();
    bad[8..16].copy_from_slice(&0u64.to_le_bytes());
    assert_eq!(SumState::from_bytes(&bad), Err(SumError::MalformedState));
    let mut bad = special.to_bytes();
    bad[6] = 1;
    assert_eq!(SumState::from_bytes(&bad), Err(SumError::MalformedState));
    let mut bad = special.to_bytes();
    bad[16] = 1;
    assert_eq!(SumState::from_bytes(&bad), Err(SumError::MalformedState));

    // A magnitude larger than `rows` maximum-finite values is impossible.
    let mut two_max = SumState::new();
    let max = f64::from_bits(0x7FEF_FFFF_FFFF_FFFF);
    two_max.add_f64(max).unwrap();
    two_max.add_f64(max).unwrap();
    let mut bad = two_max.to_bytes();
    bad[8..16].copy_from_slice(&1u64.to_le_bytes()); // claim only one row
    assert_eq!(SumState::from_bytes(&bad), Err(SumError::MalformedState));
}

/// Constructs the canonical state whose magnitude is exactly the maximum
/// reachable after `rows` rows, i.e. `rows * MAX_FINITE`.  Returns `None` if
/// that magnitude overflows the 34-limb field.
fn max_reachable_state(rows: u64) -> Option<SumState> {
    let max_limbs = {
        // Magnitude of the largest finite value in 2^-1074 units:
        // (2^53 - 1) << 2045.
        let mut limbs = [0u64; 34];
        let sig = (1u64 << 53) - 1;
        limbs[31] = (sig & 0x7) << 61;
        limbs[32] = sig >> 3;
        limbs
    };
    let mut out = [0u64; 34];
    let mut carry = 0u64;
    for i in 0..34 {
        let p = u128::from(max_limbs[i]) * u128::from(rows) + u128::from(carry);
        out[i] = p as u64;
        carry = (p >> 64) as u64;
    }
    if carry != 0 {
        return None;
    }
    let mut bytes = [0u8; SERIALIZED_LEN];
    bytes[0..4].copy_from_slice(b"PSUM");
    bytes[4] = 1;
    bytes[8..16].copy_from_slice(&rows.to_le_bytes());
    for (i, limb) in out.iter().enumerate() {
        bytes[16 + i * 8..24 + i * 8].copy_from_slice(&limb.to_le_bytes());
    }
    SumState::from_bytes(&bytes).ok()
}

#[test]
fn extreme_magnitudes_at_the_row_cap_are_accepted_and_round() {
    // The magnitude ceiling grows linearly with rows; the parser must accept
    // exactly `rows * MAX_FINITE` and reject one unit more (in higher bits).
    let full = max_reachable_state(MAX_ROWS).expect("2^31 rows fit in 34 limbs");
    assert_eq!(full.rows(), MAX_ROWS);
    let bits = full.finish_bits();
    // 2^31 copies of the largest finite value overflow to +infinity.
    assert_eq!(bits, POSITIVE_INFINITY_BITS);
    assert_eq!(full.to_bytes().len(), SERIALIZED_LEN);

    let one = max_reachable_state(1).expect("one row fits");
    assert_eq!(
        one.finish_bits(),
        0x7FEF_FFFF_FFFF_FFFF,
        "one max-finite row must round back to max finite"
    );

    // Tiny row counts still bound the magnitude: 2 rows worth with a claim of
    // 1 row is rejected, while 2 rows is accepted.
    assert!(max_reachable_state(2).is_some());
    let mut over_one = max_reachable_state(2).unwrap().to_bytes();
    over_one[8..16].copy_from_slice(&1u64.to_le_bytes());
    assert_eq!(SumState::from_bytes(&over_one), Err(SumError::MalformedState));

    // Row counts far above the cap cannot be encoded regardless of magnitude.
    assert!(max_reachable_state(u64::MAX / 2).is_none());
}

// ---------------------------------------------------------------------------
// Row cap (2^31), exercised through crafted canonical states to avoid
// iterating two billion times.
// ---------------------------------------------------------------------------

fn state_with_rows(rows: u64) -> SumState {
    let mut bytes = SumState::new().to_bytes();
    bytes[8..16].copy_from_slice(&rows.to_le_bytes());
    SumState::from_bytes(&bytes).expect("zero magnitude is valid for any row count")
}

#[test]
fn row_cap_is_enforced_atomically() {
    assert_eq!(MAX_ROWS, 1 << 31);

    let mut full = state_with_rows(MAX_ROWS);
    let before = full.clone();
    assert_eq!(full.add_bits(ONE_BITS), Err(SumError::RowLimitExceeded));
    assert_eq!(full, before, "failed add must not mutate the state");
    assert_eq!(full.add_f64(0.0), Err(SumError::RowLimitExceeded));

    let mut one = SumState::new();
    one.add_bits(ONE_BITS).unwrap();

    // MAX_ROWS rows plus one more is rejected atomically.
    let mut at_cap = state_with_rows(MAX_ROWS - 1);
    let snapshot = at_cap.clone();
    let mut two = SumState::new();
    two.add_bits(ONE_BITS).unwrap();
    two.add_bits(ONE_BITS).unwrap();
    assert_eq!(two.rows(), 2);
    assert_eq!(at_cap.merge(&two), Err(SumError::RowLimitExceeded));
    assert_eq!(at_cap, snapshot);
    // Exactly reaching the cap is allowed.
    at_cap.merge(&one).unwrap();
    assert_eq!(at_cap.rows(), MAX_ROWS);
    assert_eq!(at_cap.merge(&one), Err(SumError::RowLimitExceeded));

    // Row counts above the cap cannot be parsed.
    let mut too_many = SumState::new().to_bytes();
    too_many[8..16].copy_from_slice(&(MAX_ROWS + 1).to_le_bytes());
    assert_eq!(SumState::from_bytes(&too_many), Err(SumError::MalformedState));

    // A parseable state at the cap reports its count.
    assert_eq!(full.rows(), MAX_ROWS);
}

// ---------------------------------------------------------------------------
// Independent oracle and randomized differential tests.
//
// The oracle represents the exact sum as a signed i128 in units of
// 2^unit_exp and rounds to binary64 with a separate derivation (candidate
// significand plus explicit remainder/half comparison).  Test inputs are
// generated as exact multiples of 2^unit_exp so the oracle's integer sum is
// exact.  This shares no arithmetic with the library's limb accumulator.
// ---------------------------------------------------------------------------

fn pow2(exp: i32) -> f64 {
    assert!((-1074..=1023).contains(&exp));
    if exp >= -1022 {
        f64::from_bits(((exp + 1023) as u64) << 52)
    } else if exp == -1074 {
        f64::from_bits(1)
    } else {
        f64::from_bits(1u64 << (exp + 1074))
    }
}

fn oracle_bits(sum: i128, unit_exp: i32) -> u64 {
    if sum == 0 {
        return POSITIVE_ZERO_BITS;
    }
    let sign = if sum < 0 { 1u64 << 63 } else { 0 };
    let mag = sum.unsigned_abs();
    let highest = 127 - mag.leading_zeros() as i32;
    let e = highest + unit_exp; // value in [2^e, 2^(e+1))
    let q = if e < -1022 { -1074 } else { e - 52 };
    let drop = q - unit_exp; // bits to discard from mag

    let (mut n, round, half) = if drop <= 0 {
        assert!(-drop <= 127);
        (mag << ((-drop) as u32), 0u128, 0u128)
    } else {
        let sh = drop as u32;
        assert!(sh <= 127);
        (mag >> sh, mag & ((1u128 << sh) - 1), 1u128 << (sh - 1))
    };

    if drop > 0 && (round > half || (round == half && (n & 1) == 1)) {
        n += 1;
    }
    let mut e = e;
    if n == (1u128 << 53) {
        n >>= 1;
        e += 1;
    }
    if e < -1022 {
        return sign | n as u64;
    }
    if e > 1023 {
        return sign | POSITIVE_INFINITY_BITS;
    }
    sign | (((e + 1023) as u64) << 52) | (n as u64 & ((1u64 << 52) - 1))
}

struct Rng(u64);

impl Rng {
    fn next_u64(&mut self) -> u64 {
        // xorshift64
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn range_i64(&mut self, lo: i64, hi: i64) -> i64 {
        let span = (hi - lo + 1) as u64;
        lo + (self.next_u64() % span) as i64
    }
}

/// Outcome counts from a differential band, used to prove the band actually
/// exercises the paths it claims to.
#[derive(Default, Debug)]
struct BandStats {
    finite: usize,
    infinite: usize,
    exact_tie: usize,
}

fn run_band(seed: u64, unit_exp: i32, k_hi: i64, trials: usize) -> BandStats {
    let scale = pow2(unit_exp);
    let mut rng = Rng(seed);
    let mut stats = BandStats::default();
    for trial in 0..trials {
        let count = (rng.next_u64() % 40 + 1) as usize;
        let mut values = Vec::with_capacity(count);
        let mut exact: i128 = 0;
        for _ in 0..count {
            let k = rng.range_i64(-k_hi, k_hi);
            let value = (k as f64) * scale;
            // Confirm the generated value is exactly k * 2^unit_exp.
            assert_eq!(
                exact_units(value, unit_exp),
                k as i128,
                "generator must be exact"
            );
            values.push(value);
            exact += k as i128;
        }
        let expected = oracle_bits(exact, unit_exp);
        if oracle_is_tie(exact, unit_exp) {
            stats.exact_tie += 1;
        }
        if expected & 0x7ff0_0000_0000_0000 == 0x7ff0_0000_0000_0000 {
            stats.infinite += 1;
        } else {
            stats.finite += 1;
        }
        let got = SumState::sum_f64(values.iter().copied()).unwrap();
        assert_eq!(
            got, expected,
            "band 2^{unit_exp} trial {trial} values {values:?}"
        );

        // Also verify a random partition and merge tree gives the same bits.
        let mut cut = 1 + (rng.next_u64() as usize % count);
        if cut >= count {
            cut = count / 2;
        }
        let mut left = SumState::new();
        for v in &values[..cut] {
            left.add_f64(*v).unwrap();
        }
        let mut right = SumState::new();
        for v in &values[cut..] {
            right.add_f64(*v).unwrap();
        }
        let mut merged = left.clone();
        merged.merge(&right).unwrap();
        assert_eq!(merged.finish_bits(), expected, "partitioned trial {trial}");
        assert_eq!(merged.rows() as usize, count);

        // Three-way merge with a middle replica must also agree.
        let third = values.len() / 3;
        let mut a = SumState::new();
        for v in &values[..third] {
            a.add_f64(*v).unwrap();
        }
        let mut b = SumState::new();
        for v in &values[third..2 * third] {
            b.add_f64(*v).unwrap();
        }
        let mut c = SumState::new();
        for v in &values[2 * third..] {
            c.add_f64(*v).unwrap();
        }
        b.merge(&c).unwrap();
        a.merge(&b).unwrap();
        assert_eq!(a.finish_bits(), expected, "three-way merge trial {trial}");
    }
    stats
}

/// The oracle's decision procedure, reported separately so a band can assert
/// that exact ties were really present.
fn oracle_is_tie(sum: i128, unit_exp: i32) -> bool {
    if sum == 0 {
        return false;
    }
    let mag = sum.unsigned_abs();
    let highest = 127 - mag.leading_zeros() as i32;
    let e = highest + unit_exp;
    let q = if e < -1022 { -1074 } else { e - 52 };
    let drop = q - unit_exp;
    if drop <= 0 || drop > 127 {
        return false;
    }
    let sh = drop as u32;
    let rem = mag & ((1u128 << sh) - 1);
    rem == 1u128 << (sh - 1)
}

/// Recovers `k` such that `value == k * 2^unit_exp`, asserting exactness.
fn exact_units(value: f64, unit_exp: i32) -> i128 {
    let b = value.to_bits();
    let sign = if b >> 63 == 1 { -1i128 } else { 1i128 };
    let exp_field = ((b >> 52) & 0x7ff) as i32;
    let frac = b & ((1u64 << 52) - 1);
    if exp_field == 0 && frac == 0 {
        return 0;
    }
    let (sig, e2) = if exp_field == 0 {
        (frac as i128, -1074)
    } else {
        (((1u64 << 52) | frac) as i128, exp_field - 1023 - 52)
    };
    let shift = e2 - unit_exp;
    if shift >= 0 {
        sign * (sig << shift)
    } else {
        let down = (-shift) as u32;
        assert!(down < 127, "value is not a multiple of 2^{unit_exp}");
        assert_eq!(
            sig & ((1i128 << down) - 1),
            0,
            "value is not a multiple of 2^{unit_exp}"
        );
        sign * (sig >> down)
    }
}

#[test]
fn differential_subnormal_band() {
    // Coefficients up to 2^53 keep generator values exact while letting sums
    // exceed 2^52, so rounding at the subnormal/normal boundary really occurs.
    let stats = run_band(0x1234_5678_9abc_def1, -1074, 1 << 53, 300);
    assert!(stats.finite > 0);
    assert!(stats.exact_tie > 0, "band should contain exact subnormal ties");
}

#[test]
fn differential_fractional_band() {
    let stats = run_band(0x0bad_f00d_dead_beef, -10, (1 << 53) - 1, 300);
    assert!(stats.finite > 0);
    assert!(stats.exact_tie > 0, "band should contain exact fraction ties");
}

#[test]
fn differential_near_overflow_band() {
    let stats = run_band(0xfeed_face_cafe_1234, 971, (1 << 53) - 1, 300);
    assert!(stats.finite > 0, "band must reach finite results");
    assert!(stats.infinite > 0, "band must reach the overflow path");
}

#[test]
fn differential_integer_band() {
    let stats = run_band(0x5eed_5eed_5eed_5eed, 0, 1 << 53, 300);
    assert!(stats.finite > 0);
    assert!(stats.exact_tie > 0, "band should contain exact integer ties");
}

// ---------------------------------------------------------------------------
// Partition-tree invariance on a mixed, hand-picked corpus.
// ---------------------------------------------------------------------------

#[test]
fn every_partition_and_merge_tree_agrees() {
    let values: Vec<u64> = vec![
        TWO53_BITS,
        ONE_BITS,
        NEG_TWO53_BITS,
        0x3CA0_0000_0000_0000, // 2^-53
        0x0000_0000_0000_0001, // min subnormal
        0x8000_0000_0000_0000, // -0.0
        0xC000_0000_0000_0000, // -2.0
        0x7FEF_FFFF_FFFF_FFFF, // max finite
        0x0010_0000_0000_0000, // 2^-1022
    ];
    let direct = SumState::sum_bits(values.iter().copied()).unwrap();

    let all = set_partitions(&values);
    // Bell(9) = 21147; assert the enumeration size rather than assume it.
    assert_eq!(all.len(), 21_147, "Bell number for 9 elements");

    for partition in all {
        let parts: Vec<SumState> = partition
            .iter()
            .map(|group| {
                let mut s = SumState::new();
                for &b in group {
                    s.add_bits(b).unwrap();
                }
                s
            })
            .collect();

        let folded = left_fold(&parts);
        let balanced = balanced_fold(&parts);
        // Merge a serialized round-trip of the left fold to model a worker
        // handoff between stages.
        let mut relayed = SumState::from_bytes(&folded.to_bytes()).unwrap();
        let mut reversed = parts.clone();
        reversed.reverse();
        let reversed = left_fold(&reversed);
        relayed.merge(&reversed).unwrap();

        for (label, state) in [("left", folded), ("balanced", balanced)] {
            assert_eq!(
                state.finish_bits(),
                direct,
                "partition {partition:?} folded {label} must match direct"
            );
            assert_eq!(state.rows(), values.len() as u64);
        }
        assert_eq!(
            relayed.finish_bits(),
            // relayed holds two copies of the corpus, so its exact total is
            // twice `direct`'s exact total; compare through a fresh state.
            {
                let mut twice = SumState::new();
                for v in values.iter().chain(values.iter()) {
                    twice.add_bits(*v).unwrap();
                }
                twice.finish_bits()
            }
        );
    }
}

#[test]
fn extreme_value_sequences_stay_partition_invariant() {
    let max = 0x7FEF_FFFF_FFFF_FFFFu64;
    let min_sub = 0x0000_0000_0000_0001u64;
    let corpus = [
        vec![max, max, 0xFFEF_FFFF_FFFF_FFFF], // max + max + (-max)
        vec![min_sub; 64],
        vec![min_sub, max, min_sub, 0xFFEF_FFFF_FFFF_FFFF], // max + (-max) + tiny
        vec![0x7FF0_0000_0000_0000, 0xFFF0_0000_0000_0000], // +inf and -inf
    ];
    for values in corpus {
        let direct = SumState::sum_bits(values.iter().copied()).unwrap();
        let partitions = if values.len() <= 6 {
            set_partitions(&values)
        } else {
            // Full enumeration is only feasible for tiny inputs; for longer
            // ones sample contiguous splits and an alternating split.
            let mid = values.len() / 2;
            let odds: Vec<u64> = values.iter().step_by(2).copied().collect();
            let evens: Vec<u64> = values.iter().skip(1).step_by(2).copied().collect();
            vec![
                vec![values.clone()],
                values.iter().map(|&b| vec![b]).collect(),
                vec![values[..mid].to_vec(), values[mid..].to_vec()],
                vec![odds, evens],
            ]
        };
        for partition in partitions {
            let parts: Vec<SumState> = partition
                .iter()
                .map(|group| {
                    let mut s = SumState::new();
                    for &b in group {
                        s.add_bits(b).unwrap();
                    }
                    s
                })
                .collect();
            assert_eq!(left_fold(&parts).finish_bits(), direct);
            assert_eq!(balanced_fold(&parts).finish_bits(), direct);
        }
    }
}
