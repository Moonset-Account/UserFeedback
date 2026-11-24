//! 端到端测试：走一遍与 `examples/replay_fixture.rs` 相同的真实流程，
//! 确保示例所声称的一切都由实际执行支撑，而非预置输出。

use local_compaction::fixture::Tabletop;

#[test]
fn end_to_end_replay_flow_matches_before_and_after_compaction() {
    let table = Tabletop::build();

    // 1. 真实执行压实。
    let output = table.compact().expect("compaction succeeds");

    // 2. 真实执行等价性验证（符号 + 深层旧值穷举 + 证据闭环）。
    let report = table.verify(&output).expect("verification succeeds");
    assert!(report.is_ok());
    assert!(!report.read_points.is_empty());
    assert!(report.keys_checked > 0);
    assert!(report.assignments_exhausted > 0);
    assert!(report.evidence_closure_ok);

    // 3. 压实前后的完整回放表逐格相等。
    let before = table.replay_before().expect("before replay");
    let after = table.replay_after(&output).expect("after replay");
    assert_eq!(before.len(), after.len());
    assert_eq!(before, after);

    // 4. 回收统计的各项计数之和等于回收清单长度。
    let counts = Tabletop::reclaim_reason_counts(&output);
    let total: usize = counts.iter().map(|(_, n)| n).sum();
    assert_eq!(total, output.reclaimed.len());

    // 5. 输出段数量与切点一致。
    assert_eq!(output.segments.len(), table.boundaries.len() + 1);
}

#[test]
fn end_to_end_verification_is_deterministic_across_runs() {
    // 同一盘面重复构建/压实必须得到完全一致的结果（可回放性）。
    let first = Tabletop::build().compact().unwrap();
    let second = Tabletop::build().compact().unwrap();
    assert_eq!(first, second);
}
