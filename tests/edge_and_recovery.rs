//! 边界与故障恢复场景：空键、单侧键、空输入、失败后重试、过期成功回调。

use std::collections::BTreeSet;

use hotkey_full_outer_join::{
    CallbackStatus, FullOuterJoin, JoinOut, Row, SimulatedNode,
};

fn row(id: u64, key: Option<i64>, value: i64) -> Row<i64, i64> {
    Row { id, key, value }
}

fn equal_value(left: &Row<i64, i64>, right: &Row<i64, i64>) -> bool {
    left.value == right.value
}

fn collect<K: Ord + Clone, V: Clone>(
    join: &mut FullOuterJoin<K, V>,
) -> BTreeSet<JoinOut<K, V>>
where
    K: Ord,
    V: Ord,
{
    join.drain_output().into_iter().collect()
}

#[test]
fn null_keys_skip_block_pairs_and_go_straight_to_unmatched() {
    let left = vec![
        row(1, Some(7), 1),
        row(2, None, 100), // 空键：即使载荷相等也不允许匹配
    ];
    let right = vec![
        row(11, Some(7), 1),
        row(12, None, 100),
    ];

    let mut join = FullOuterJoin::<i64, i64>::new(4);
    join.load(&left, &right);

    // 空键不产生块对，只有非空键的一个块对。
    assert_eq!(join.pair_count(), 1);
    let immediate = join.drain_output();
    assert_eq!(immediate.len(), 2, "两个空键行应在 load 时立即补空");
    assert!(immediate
        .iter()
        .any(|o| matches!(o, JoinOut::LeftOnly { left_id: 2, .. })));
    assert!(immediate
        .iter()
        .any(|o| matches!(o, JoinOut::RightOnly { right_id: 12, .. })));

    let tasks = join.schedule(10);
    assert_eq!(tasks.len(), 1);
    let node = SimulatedNode::new(equal_value);
    assert_eq!(join.on_success(node.run(&tasks[0])), CallbackStatus::Accepted);
    assert!(join.is_done());

    let all: BTreeSet<_> = immediate.into_iter().chain(join.drain_output()).collect();
    assert_eq!(all.len(), 3);
    assert!(all.contains(&JoinOut::Matched {
        key: 7,
        left_id: 1,
        left_value: 1,
        right_id: 11,
        right_value: 1,
    }));
    assert_eq!(join.metrics().null_key_rows, 2);
    assert_eq!(join.metrics().left_only_outputs, 1);
    assert_eq!(join.metrics().right_only_outputs, 1);
}

#[test]
fn one_sided_key_emits_every_row_as_unmatched_without_tasks() {
    let left = vec![row(1, Some(1), 5), row(2, Some(1), 6), row(3, Some(2), 7)];
    let right = vec![row(10, Some(9), 5)];

    let mut join = FullOuterJoin::<i64, i64>::new(2);
    join.load(&left, &right);

    assert_eq!(join.pair_count(), 0, "没有共同键，不应存在任何块对");
    assert!(join.schedule(10).is_empty());
    assert!(join.is_done());

    let all = collect(&mut join);
    assert_eq!(all.len(), 4);
    assert_eq!(
        all.iter()
            .filter(|o| matches!(o, JoinOut::LeftOnly { .. }))
            .count(),
        3
    );
    assert_eq!(
        all.iter()
            .filter(|o| matches!(o, JoinOut::RightOnly { .. }))
            .count(),
        1
    );
    // 即使载荷相等（左行 1 与右行 10 都是 5），键不同也绝不匹配。
    assert!(all.iter().all(|o| !matches!(o, JoinOut::Matched { .. })));
}

#[test]
fn empty_inputs_complete_without_outputs_or_tasks() {
    let mut join = FullOuterJoin::<i64, i64>::new(4);
    join.load(&[], &[]);
    assert!(join.schedule(10).is_empty());
    assert!(join.is_done());
    assert!(join.drain_output().is_empty());
    assert_eq!(join.metrics().peak_task_input_rows, 0);
}

#[test]
fn accepted_failure_resets_pair_and_retry_eventually_confirms() {
    let left = vec![row(1, Some(1), 9), row(2, Some(1), 8)];
    let right = vec![row(10, Some(1), 9), row(11, Some(1), 7)];

    let mut join = FullOuterJoin::<i64, i64>::new(2);
    join.load(&left, &right);
    assert_eq!(join.pair_count(), 1);

    let first = join.schedule(1).pop().expect("one task");
    assert_eq!(first.attempt, hotkey_full_outer_join::AttemptId(1));

    // 第一次尝试失败且回调及时到达：采纳，块对回到待派发。
    assert_eq!(
        join.on_failure(first.pair, first.attempt),
        CallbackStatus::Accepted
    );
    assert!(!join.is_done());

    // 重新调度拿到新尝试号。
    let second = join.retry_pair(first.pair);
    assert_eq!(second.attempt, hotkey_full_outer_join::AttemptId(2));

    // 第一次尝试的迟到成功回调必须被丢弃，不能借尸还魂。
    let node = SimulatedNode::new(equal_value);
    let stale_result = {
        let mut r = node.run(&first);
        r.attempt = first.attempt; // 保留旧尝试号
        r
    };
    assert_eq!(join.on_success(stale_result), CallbackStatus::Stale);
    assert!(!join.is_done());

    // 第二次尝试成功确认。
    assert_eq!(
        join.on_success(node.run(&second)),
        CallbackStatus::Accepted
    );
    assert!(join.is_done());

    let all = collect(&mut join);
    assert!(all.contains(&JoinOut::Matched {
        key: 1,
        left_id: 1,
        left_value: 9,
        right_id: 10,
        right_value: 9,
    }));
    assert_eq!(all.len(), 3, "一个匹配 + 左补空(行2) + 右补空(行11)");

    let m = join.metrics();
    assert_eq!(m.dispatched_attempts, 2);
    assert_eq!(m.accepted_failures, 1);
    assert_eq!(m.accepted_successes, 1);
    assert_eq!(m.stale_callbacks, 1);
    assert_eq!(m.confirmed_pairs, 1);
}

#[test]
fn double_success_on_same_pair_confirms_match_exactly_once() {
    let left = vec![row(1, Some(1), 9)];
    let right = vec![row(10, Some(1), 9)];

    let mut join = FullOuterJoin::<i64, i64>::new(4);
    join.load(&left, &right);
    let task = join.schedule(1).pop().unwrap();
    let node = SimulatedNode::new(equal_value);
    let result = node.run(&task);

    assert_eq!(join.on_success(result.clone()), CallbackStatus::Accepted);
    // 同一份成功回报再投递一次：块对已确认，按过期处理，匹配不得重复输出。
    assert_eq!(join.on_success(result), CallbackStatus::Stale);

    let all = collect(&mut join);
    assert_eq!(all.len(), 1);
    assert_eq!(join.metrics().matched_outputs, 1);
    assert_eq!(join.metrics().stale_callbacks, 1);
}

#[test]
fn block_capacity_boundary_keeps_every_task_within_limit() {
    // 7 行对 5 行，cap=3：左 3 块（3/3/1），右 2 块（3/2），共 6 个块对。
    let left: Vec<_> = (0..7).map(|i| row(i, Some(1), i as i64)).collect();
    let right: Vec<_> = (100..105).map(|i| row(i, Some(1), i as i64)).collect();

    let cap = 3usize;
    let mut join = FullOuterJoin::<i64, i64>::new(cap);
    join.load(&left, &right);
    assert_eq!(join.left_block_count(), 3);
    assert_eq!(join.right_block_count(), 2);
    assert_eq!(join.pair_count(), 6);

    let tasks = join.schedule(6);
    for task in &tasks {
        assert!(task.left.len() <= cap);
        assert!(task.right.len() <= cap);
        assert!(task.input_rows() <= 2 * cap);
    }
    assert_eq!(join.metrics().peak_task_input_rows, 6);
}
