//! 可回放小盘面的命令行演示。
//!
//! 所有输出都来自真实执行的压实与读取，不预置任何结果。
//! 运行：`cargo run --offline --example replay_fixture`

use local_compaction::fixture::Tabletop;
use local_compaction::read::SymbolicValue;

fn format_sym(v: &SymbolicValue) -> String {
    match v {
        SymbolicValue::Absent => "<absent>".to_string(),
        SymbolicValue::Concrete(list) => {
            let elems: Vec<String> =
                list.iter().map(|e| String::from_utf8_lossy(e).to_string()).collect();
            format!("Present({elems:?})")
        }
        SymbolicValue::UnknownBase { depth, key, suffix } => {
            let key = String::from_utf8_lossy(key);
            let suffix: Vec<String> =
                suffix.iter().map(|e| String::from_utf8_lossy(e).to_string()).collect();
            format!("UnknownBase(depth={depth}, key={key}) + {suffix:?}")
        }
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let table = Tabletop::build();
    println!("== tabletop ==");
    println!("snapshot A = {}, snapshot B = {}, read_seq = {}", table.snapshot_a, table.snapshot_b, table.read_seq);
    println!("output boundary = {:?}", String::from_utf8_lossy(&table.boundaries[0]));

    let output = table.compact()?;
    let report = table.verify(&output)?;

    println!("\n== verification ==");
    println!("read points:      {:?}", report.read_points);
    println!("keys checked:     {}", report.keys_checked);
    println!("deep assignments: {}", report.assignments_exhausted);
    println!("evidence closure: {}", report.evidence_closure_ok);
    println!("verification ok:  {}", report.is_ok());
    assert!(report.is_ok(), "compaction must preserve every allowed read");

    println!("\n== replay rows (before compaction) ==");
    for (seq, key, value) in table.replay_before()? {
        println!("seq={seq:>3} key={key:<12} -> {}", format_sym(&value));
    }

    println!("\n== replay rows (after compaction) ==");
    let after = table.replay_after(&output)?;
    for (seq, key, value) in &after {
        println!("seq={seq:>3} key={key:<12} -> {}", format_sym(value));
    }
    assert_eq!(table.replay_before()?, after);

    println!("\n== output segments ==");
    for seg in &output.segments {
        println!(
            "segment depth={} range=[{}, {})",
            seg.depth,
            seg.range.start.as_ref().map(|b| String::from_utf8_lossy(b).to_string()).unwrap_or_else(|| "-inf".into()),
            seg.range.end.as_ref().map(|b| String::from_utf8_lossy(b).to_string()).unwrap_or_else(|| "+inf".into()),
        );
        for o in &seg.records {
            let evidence: Vec<u64> = o.evidence.iter().map(|r| r.0).collect();
            println!(
                "  point seq={:>3} op={:?} evidence={evidence:?}",
                o.record.seq, o.record.op
            );
        }
        for o in &seg.tombstones {
            let evidence: Vec<u64> = o.evidence.iter().map(|r| r.0).collect();
            println!(
                "  range seq={:>3} range={:?} evidence={evidence:?}",
                o.tombstone.seq, o.tombstone.range
            );
        }
    }

    println!("\n== reclaim summary ==");
    for (name, count) in Tabletop::reclaim_reason_counts(&output) {
        println!("{name:<24} {count}");
    }
    for entry in &output.reclaimed {
        println!("record {:>3} reclaimed: {:?}", entry.source.0, entry.reason);
    }

    Ok(())
}
