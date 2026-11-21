//! Command-line demonstration that runs the real library.
//!
//! Usage:
//!   cargo run --offline --example partitions -- <hex-or-decimal>...
//!
//! Each argument is parsed as a binary64 value: `0x...` is read as a raw bit
//! pattern, anything else as a decimal literal.  The example enumerates a few
//! partitions of the inputs, prints each partition's final bits, and checks
//! that they all agree.  Nothing here is hard-coded output: every number is
//! computed by the library at run time.

use partitioned_sum::SumState;

fn parse(arg: &str) -> u64 {
    if let Some(hex) = arg.strip_prefix("0x").or_else(|| arg.strip_prefix("0X")) {
        u64::from_str_radix(hex, 16).unwrap_or_else(|e| panic!("bad hex {arg:?}: {e}"))
    } else {
        arg.parse::<f64>()
            .unwrap_or_else(|e| panic!("bad number {arg:?}: {e}"))
            .to_bits()
    }
}

fn sum_partition(groups: &[Vec<u64>]) -> (u64, u64) {
    let mut parts = Vec::new();
    for group in groups {
        let mut state = SumState::new();
        for &bits in group {
            state.add_bits(bits).expect("input within row cap");
        }
        parts.push(state);
    }
    let mut acc = SumState::new();
    for part in &parts {
        acc.merge(part).expect("within row cap");
    }
    (acc.finish_bits(), acc.rows())
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() {
        eprintln!("usage: partitions <value>...   (0x... means raw bits)");
        std::process::exit(2);
    }
    let values: Vec<u64> = args.iter().map(|a| parse(a)).collect();
    println!("input ({} values):", values.len());
    for (i, &b) in values.iter().enumerate() {
        println!("  [{i}] bits=0x{b:016x} value={:?}", f64::from_bits(b));
    }

    // Partition A: everything in one state.
    let single = vec![values.clone()];
    // Partition B: one state per value, merged left to right.
    let per_value: Vec<Vec<u64>> = values.iter().map(|&b| vec![b]).collect();
    // Partition C: first half in one state, second half in another.
    let mid = values.len() / 2;
    let halves = vec![values[..mid].to_vec(), values[mid..].to_vec()];
    // Partition D: alternating values into two states.
    let odds: Vec<u64> = values.iter().step_by(2).copied().collect();
    let evens: Vec<u64> = values.iter().skip(1).step_by(2).copied().collect();
    let alternating = vec![odds, evens];

    let mut results = Vec::new();
    for (name, groups) in [
        ("single", &single),
        ("per-value", &per_value),
        ("halves", &halves),
        ("alternating", &alternating),
    ] {
        let (bits, rows) = sum_partition(groups);
        println!("{name:>12}: rows={rows} result=0x{bits:016x} value={:?}", f64::from_bits(bits));
        results.push((name, bits));
    }

    let first = results[0].1;
    let all_agree = results.iter().all(|(_, b)| *b == first);
    println!(
        "all partitions agree: {all_agree}  final=0x{first:016x} = {:?}",
        f64::from_bits(first)
    );
    if !all_agree {
        std::process::exit(1);
    }
}
