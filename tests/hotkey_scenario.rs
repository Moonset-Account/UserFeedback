//! 题面主场景：热键两侧各分三块，某行只在最后确认的块对中命中，
//! 并伴随一次迟到的失败回调；检查匹配对、补空行与每个任务的输入峰值。

use std::collections::BTreeSet;

use hotkey_full_outer_join::{
    AttemptId, CallbackStatus, FullOuterJoin, JoinOut, Row, SimulatedNode,
};

const HOT_KEY: i64 = 99;
const CAP: usize = 2;

fn row(id: u64, key: Option<i64>, value: i64) -> Row<i64, i64> {
    Row { id, key, value }
}

/// 附加条件：等值键之外，还要求两侧载荷相等。
fn equal_value(left: &Row<i64, i64>, right: &Row<i64, i64>) -> bool {
    left.value == right.value
}

/// 参考实现：朴素全量嵌套循环全外连接，用来和切块调度算子的最终输出逐行比对。
fn reference_full_outer(
    left: &[Row<i64, i64>],
    right: &[Row<i64, i64>],
) -> BTreeSet<JoinOut<i64, i64>> {
    let mut out = BTreeSet::new();
    let mut left_hit = BTreeSet::new();
    let mut right_hit = BTreeSet::new();

    for l in left {
        match &l.key {
            None => {
                out.insert(JoinOut::LeftOnly {
                    key: None,
                    left_id: l.id,
                    left_value: l.value,
                });
            }
            Some(lk) => {
                for r in right {
                    if let Some(rk) = &r.key {
                        if lk == rk && l.value == r.value {
                            left_hit.insert(l.id);
                            right_hit.insert(r.id);
                            out.insert(JoinOut::Matched {
                                key: *lk,
                                left_id: l.id,
                                left_value: l.value,
                                right_id: r.id,
                                right_value: r.value,
                            });
                        }
                    }
                }
            }
        }
    }
    for r in right {
        if r.key.is_none() {
            out.insert(JoinOut::RightOnly {
                key: None,
                right_id: r.id,
                right_value: r.value,
            });
        } else if !right_hit.contains(&r.id) {
            out.insert(JoinOut::RightOnly {
                key: r.key,
                right_id: r.id,
                right_value: r.value,
            });
        }
    }
    for l in left {
        if l.key.is_some() && !left_hit.contains(&l.id) {
            out.insert(JoinOut::LeftOnly {
                key: l.key,
                left_id: l.id,
                left_value: l.value,
            });
        }
    }
    out
}

#[test]
fn hot_key_three_by_three_with_late_failure_and_final_pair_hit() {
    // 左侧 6 行（每块 2 行，共 3 块），右侧 6 行（3 块），全部挂在同一个热键上。
    let left = vec![
        row(1, Some(HOT_KEY), 10),
        row(2, Some(HOT_KEY), 20),
        row(3, Some(HOT_KEY), 30),
        row(4, Some(HOT_KEY), 40),
        row(5, Some(HOT_KEY), 50),
        row(6, Some(HOT_KEY), 60),
    ];
    // 右侧载荷故意打乱，使命中分散到不同块对；99 是无法匹配的载荷，30 在右侧缺失。
    let right = vec![
        row(101, Some(HOT_KEY), 20),
        row(102, Some(HOT_KEY), 99),
        row(103, Some(HOT_KEY), 10),
        row(104, Some(HOT_KEY), 50),
        row(105, Some(HOT_KEY), 60),
        row(106, Some(HOT_KEY), 40),
    ];

    let mut join = FullOuterJoin::<i64, i64>::new(CAP);
    join.load(&left, &right);

    assert_eq!(join.left_block_count(), 3, "左侧应切成三块");
    assert_eq!(join.right_block_count(), 3, "右侧应切成三块");
    assert_eq!(join.pair_count(), 9, "3x3 共九个块对");

    let node = SimulatedNode::new(equal_value);

    // 一次调度全部 9 个块对，并记录每个任务实际持有的输入行数。
    let tasks = join.schedule(9);
    assert_eq!(tasks.len(), 9);
    let mut task_input_peaks = Vec::new();
    for task in &tasks {
        assert!(task.left.len() <= CAP);
        assert!(task.right.len() <= CAP);
        task_input_peaks.push(task.input_rows());
    }
    assert!(
        task_input_peaks.iter().all(|&n| n <= 2 * CAP),
        "任何任务持有的输入行数不得超过两个块的容量: {:?}",
        task_input_peaks
    );
    assert_eq!(join.metrics().peak_task_input_rows, 2 * CAP);

    // 块对布局：P0..P2 = L0 x R0..R2，P3..P5 = L1 x ...，P6..P8 = L2 x ...
    // 行 6（载荷 60）唯一可能的命中在 P8 = (L2, R2)，且 P8 安排为全局最后确认。
    let p_star = hotkey_full_outer_join::PairId(8);
    let (star_left, star_right) = join.pair_blocks(p_star);
    assert_eq!(star_left, hotkey_full_outer_join::LeftBlockId(2));
    assert_eq!(star_right, hotkey_full_outer_join::RightBlockId(2));

    // 先乱序确认其余 8 个块对，P8 始终悬而未决。
    let delivery_order = [3usize, 0, 6, 4, 1, 7, 5, 2];
    let mut emitted_so_far = Vec::new();
    for &idx in &delivery_order {
        let result = node.run(&tasks[idx]);
        assert_eq!(join.on_success(result), CallbackStatus::Accepted);
        emitted_so_far.extend(join.drain_output());
    }

    // 最后块对确认前：行 6 与右行 105 不得出现在任何输出里（既不能提前补空，也还没匹配）。
    assert!(
        !emitted_so_far.iter().any(|o| o.left_id() == Some(6)),
        "行 6 的所有可能配对块尚未全部确认，不能提前输出"
    );
    assert!(
        !emitted_so_far.iter().any(|o| o.right_id() == Some(105)),
        "右行 105 的所有可能配对块尚未全部确认，不能提前输出"
    );
    assert!(!join.is_done());

    // 对悬而未决的 P8 发起取代性新尝试（旧尝试 attempt 1 立即作废）。
    let retry_task = join.supersede_pair(p_star);
    assert_eq!(retry_task.attempt, AttemptId(2));
    assert!(retry_task.input_rows() <= 2 * CAP);
    task_input_peaks.push(retry_task.input_rows());

    // 新尝试真实执行并成功，其中包含行 6 唯一的一次命中 (6, 105)。
    let retry_result = node.run(&retry_task);
    assert_eq!(retry_result.attempt, AttemptId(2));
    assert!(retry_result.matches.contains(&(6, 105)));

    // 旧尝试的失败回调迟到：尝试号已过期，必须丢弃且不得改变块对状态。
    let late_failure = join.on_failure(p_star, AttemptId(1));
    assert_eq!(late_failure, CallbackStatus::Stale);

    // 新尝试的成功回报被采纳，P8 确认，L2/R2 随之终态。
    assert_eq!(join.on_success(retry_result), CallbackStatus::Accepted);
    emitted_so_far.extend(join.drain_output());
    assert!(join.is_done());

    // 最终输出与朴素参考实现完全一致（匹配对、左右补空行、键与载荷）。
    let actual: BTreeSet<_> = emitted_so_far.into_iter().collect();
    let expected = reference_full_outer(&left, &right);
    assert_eq!(actual, expected);

    // 显式核对关键结果：5 个匹配对、行 3 左补空、右行 102 右补空。
    let matched: Vec<_> = actual
        .iter()
        .filter_map(|o| match o {
            JoinOut::Matched {
                left_id, right_id, ..
            } => Some((*left_id, *right_id)),
            _ => None,
        })
        .collect();
    assert_eq!(
        matched.iter().copied().collect::<BTreeSet<_>>(),
        [
            (1, 103),
            (2, 101),
            (4, 106),
            (5, 104),
            (6, 105),
        ]
        .into_iter()
        .collect()
    );
    assert!(actual.contains(&JoinOut::LeftOnly {
        key: Some(HOT_KEY),
        left_id: 3,
        left_value: 30,
    }));
    assert!(actual.contains(&JoinOut::RightOnly {
        key: Some(HOT_KEY),
        right_id: 102,
        right_value: 99,
    }));

    let metrics = join.metrics();
    assert_eq!(metrics.dispatched_attempts, 10, "9 个首次尝试 + 1 个取代尝试");
    assert_eq!(metrics.confirmed_pairs, 9);
    assert_eq!(metrics.accepted_successes, 9);
    assert_eq!(metrics.accepted_failures, 0, "失败回调是过期的，不应计入采纳失败");
    assert_eq!(metrics.stale_callbacks, 1, "恰好一次迟到的过期失败回调");
    assert_eq!(metrics.matched_outputs, 5);
    assert_eq!(metrics.left_only_outputs, 1);
    assert_eq!(metrics.right_only_outputs, 1);
    assert_eq!(metrics.null_key_rows, 0);
    assert!(
        task_input_peaks.iter().all(|&n| n <= 2 * CAP),
        "每个任务的输入峰值都不得超过 2*cap: {:?}",
        task_input_peaks
    );
    assert_eq!(metrics.peak_task_input_rows, 2 * CAP);
}
