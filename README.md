# partitioned-sum

Exact, partitionable, serializable summation state for IEEE 754 binary64
values, in Rust 1.95.0 with only the standard library.

A column of floating-point numbers changes its total when the partition count
changes, because each partial sum rounds. `SumState` removes that variability:
it keeps the **exact mathematical sum** as an integer, merges states in any
binary tree, and performs **one** nearest-even rounding only at output time.
The final bits therefore depend only on the multiset of inputs, never on how
they were partitioned or combined.

## Design

Every finite binary64 value is an integer multiple of `2^-1074`. A `SumState`
stores the signed sum in those units as a 34 × `u64` sign-magnitude integer
(2176 bits), with no intermediate rounding.

* **Why 34 limbs suffice.** With at most `MAX_ROWS = 2^31` rows, the exact
  magnitude is at most `2^31 × (2^1024 − 2^971) < 2^2129`, which fits in 2176
  bits. Deserialization re-checks this bound against the stored row count, so a
  parsed state is always physically reachable.
* **Single rounding.** `finish_bits` rounds once, nearest-even. The rounding
  decision uses the guard bit plus a sticky bit over all discarded limbs.
  Overflow under that rule yields the infinity with the sign of the exact sum.
* **Special values.** NaN, or both infinities, produce the fixed quiet NaN
  `0x7ff8000000000000`. A single infinity keeps its sign. Exact zero is always
  `+0.0` (`0x0000000000000000`), regardless of input order or signed zeros.
* **Row cap.** At most `2^31` rows. `add_*` and `merge` fail with
  `RowLimitExceeded` and leave the state **unchanged** (checked before mutating).
* **Serialization.** Exactly 288 bytes (magic `PSUM`, version, kind, sign,
  row count, 34 magnitude limbs), well under the 1 KiB budget. `from_bytes`
  rejects oversized input (`StateTooLarge`, > 1 KiB), wrong length, bad magic
  or version, unknown kind, non-canonical zero/special encodings, row counts
  above `2^31`, and magnitudes larger than the row count could produce.

## Public API

| Item | Purpose |
| --- | --- |
| `SumState::new()` | Empty state (`+0`, 0 rows). |
| `SumState::add_bits(u64)` | Ingest one raw binary64 bit pattern. |
| `SumState::add_f64(f64)` | Ingest one `f64` (`value.to_bits()`). |
| `SumState::merge(&SumState)` | Combine a partition; any tree shape works. |
| `SumState::finish_bits() -> u64` | Final result as a binary64 bit pattern. |
| `SumState::to_bytes() -> [u8; 288]` | Canonical serialization. |
| `SumState::from_bytes(&[u8])` | Parse and validate. |
| `SumState::sum_bits(iter)` / `sum_f64(iter)` | One-shot conveniences. |
| `SumState::rows()`, `is_empty()` | Row count introspection. |
| `MAX_ROWS`, `SERIALIZED_LEN`, `MAX_SERIALIZED_LEN` | Limits. |
| `CANONICAL_NAN_BITS`, `POSITIVE_ZERO_BITS`, `POSITIVE_INFINITY_BITS`, `NEGATIVE_INFINITY_BITS` | Result constants. |
| `SumError::{RowLimitExceeded, MalformedState, StateTooLarge}` | Errors (`Display` + `std::error::Error`). |

All results are reported as `u64` bit patterns so signed zeros, NaN payloads,
and infinities are compared exactly; use `f64::from_bits` on the caller side.

### Example

```rust
use partitioned_sum::SumState;

let a = 2.0f64.powi(53);
let mut left = SumState::new();
left.add_f64(a).unwrap();
left.add_f64(-a).unwrap();

let mut right = SumState::new();
right.add_f64(1.0).unwrap();

left.merge(&right).unwrap();
assert_eq!(left.finish_bits(), 1.0f64.to_bits());
```

Naive `f64` addition of `2^53 + 1` drops the 1; this state keeps it exactly.

## Reproduce

Requires Rust 1.95.0. No dependencies, no network.

```sh
cargo test --offline
cargo test --offline --release
```

Optional runnable demo (computes everything at run time):

```sh
cargo run --offline --example partitions -- 0x4340000000000000 1 0xc340000000000000
```

It enumerates several partitions of the given values, prints each final bit
pattern, and exits non-zero if they disagree.

## Verification scope

`tests/sum_state.rs` contains 21 integration tests plus 1 doc test, all
executed and passing (`cargo test --offline`: 21 passed in the integration
target, 1 passed in doc-tests, 0 failed; release build likewise).

* **Required property.** All 5 set partitions of `{2^53, 1, −2^53}`, each
  merged by left fold and balanced fold, return exactly `1.0`. Also covered:
  arbitrary ingestion orders, a split that groups `2^53` with `−2^53`, and a
  contrast test showing naive `f64` summation returns `+0` for the same input.
* **Normal path.** Exact ties at several binades, representable sums,
  cancellation that recovers a tiny term, exact zero from `−0.0`/cancel pairs.
* **Boundary path.** Subnormal arithmetic (`3·min_sub`,
  `2·max_sub + min_sub`, smallest-normal ± min_sub), the subnormal/normal seam,
  and overflow: `max + 2^969` stays finite, `max + 2^970` (exact midpoint,
  ties-to-even) overflows to `+inf`, negative overflow keeps its sign, and large
  cancellation past `max` stays finite.
* **Interleaved / special.** NaN with various payloads and signs, one infinity,
  both infinities, NaN stickiness, and mixed finite/infinite sequences.
* **Serialization.** Fixed 288-byte size and ≤ 1 KiB budget, round-trip
  equality, merging serialized replicas, malformed/oversized/non-canonical
  rejection, and the row-count/magnitude consistency bound.
* **Row cap.** Atomic rejection at exactly `2^31` rows for both `add` and
  `merge` (states at the cap are crafted via canonical bytes rather than built
  row by row), plus the maximum reachable magnitude `rows × max_finite` at
  `2^31` rows parsing and rounding to `+inf`.
* **Differential.** Four randomized bands (subnormal, fractional, integer,
  near-overflow), 300 trials each, compared against an independent in-test
  oracle that uses a different representation (dynamic limbs) and a different
  rounding derivation. Each trial is also re-checked through a random 2-way and
  a 3-way merge. Bands assert that exact ties and (for the overflow band) both
  finite and infinite outcomes actually occurred, so no path runs empty.
* **Partition invariance (mixed corpus).** A 9-value corpus including
  `−0.0`, `max`, `min_sub`, and `2^-1022` is summed over all 21147 set
  partitions (Bell(9), asserted in the test) with two fold shapes and a
  serialize/merge relay; extreme sequences (overflow-then-cancel, 64
  subnormals, `+inf`/`−inf`) are checked over sampled partitions.

### Mutation check

Two deliberate faults were introduced into the rounding logic and the suite
caught both, confirming the tests are not vacuous: always-rounding-up on the
guard bit failed 4 tests; removing the sticky-bit contribution failed 3.

### Not executed / limitations

* `cargo clippy` and `cargo fmt --check` were **not run**: the sandbox did not
  permit those commands. No claim is made about lints or formatting.
* The 1 KiB serialize budget and the `2^31` row cap are enforced structurally
  (fixed 288-byte encoding; pre-mutation limit checks). Building a state with
  `2^31` rows one row at a time is not exercised in tests (it would take
  billions of iterations); the cap is instead tested with canonical crafted
  states and by merging states whose row counts sum across the limit.
* The differential oracle is independent of the library's arithmetic but lives
  in the same test file; it is not a third-party reference implementation.
* Only synthetic inputs generated inside the workspace are used. No network,
  no credentials, no real targets.
