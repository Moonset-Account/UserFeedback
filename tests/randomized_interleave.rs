//! 随机交错压力测试：固定种子下随机生成数据与切块容量，乱序确认块对，
//! 随机注入失败、取代性重试与过期回调，最终输出与朴素参考全外连接比对。

use std::collections::{BTreeMap, BTreeSet, HashSet};

use hotkey_full_outer_join::{
    CallbackStatus, FullOuterJoin, JoinOut, Row, SimulatedNode,
};

/// 确定性 LCG，避免引入任何第三方依赖。
struct Lcg(u64);

impl Lcg {
    fn next_u64(&mut self) -> u64 {
        // Numerical Recipes 常数。
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        self.0
    }

    fn below(&mut self, n: usize) -> usize {
        (self.next_u64() % n as u64) as usize
    }
}

fn make_row(id: u64, key: Option<i64>, value: i64) -> Row<i64, i64> {
    Row { id, key, value }
}

/// 朴素参考实现：全量嵌套循环 + 全外连接补空。
fn reference(
    left: &[Row<i64, i64>],
    right: &[Row<i64, i64>],
) -> BTreeSet<JoinOut<i64, i64>> {
    let mut out = BTreeSet::new();
    let mut left_hit = HashSet::new();
    let mut right_hit = HashSet::new();

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
                        // 与执行节点一致：等值键相同且附加条件（载荷相等）成立。
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

fn run_seed(seed: u64) -> (hotkey_full_outer_join::Metrics, u64, u64) {
    let mut rng = Lcg(seed);
    let cap = 1 + rng.below(4); // 1..=4，制造大量小块与边界块
    let n_left = rng.below(12);
    let n_right = rng.below(12);

    // 键空间较小以制造热键；约 1/6 概率为空键；载荷在小范围内重复以制造多重命中与未命中。
    let gen = |rng: &mut Lcg, id: u64| -> Row<i64, i64> {
        let key = if rng.below(6) == 0 {
            None
        } else {
            Some(rng.below(3) as i64)
        };
        let value = rng.below(5) as i64;
        make_row(id, key, value)
    };

    let left: Vec<_> = (0..n_left as u64).map(|i| gen(&mut rng, i + 1)).collect();
    let right: Vec<_> = (1000..1000 + n_right as u64)
        .map(|i| gen(&mut rng, i))
        .collect();

    let mut join = FullOuterJoin::<i64, i64>::new(cap);
    join.load(&left, &right);
    let node = SimulatedNode::new(
        |l: &Row<i64, i64>, r: &Row<i64, i64>| l.value == r.value,
    );

    // pair 索引 -> 最新在途任务；可能挂起的旧尝试结果另存为「僵尸」。
    let mut inflight: BTreeMap<usize, hotkey_full_outer_join::Task<i64, i64>> = BTreeMap::new();
    let mut zombies: Vec<(hotkey_full_outer_join::PairResult, bool)> = Vec::new();
    let mut outputs: Vec<JoinOut<i64, i64>> = Vec::new();

    let mut guard = 0usize;
    while !(join.is_done() && inflight.is_empty() && zombies.is_empty()) {
        guard += 1;
        assert!(guard < 10_000, "seed {seed} 驱动未收敛");

        // 1) 随机派发新块对（含失败后回到待派发的块对）；至少派发一个，保证每轮有进展。
        let fresh = join.schedule(1 + rng.below(3));
        for task in fresh {
            assert!(task.left.len() <= cap && task.right.len() <= cap);
            assert!(task.input_rows() <= 2 * cap);
            inflight.insert(task.pair.0, task);
        }

        // 2) 在途任务逐个推进，保证没有任务被长期饿死。
        let pair_keys: Vec<usize> = inflight.keys().copied().collect();
        for pair_idx in pair_keys {
            match rng.below(10) {
                // 20%：取代性重试，旧尝试变僵尸，之后随机成功或失败回来。
                0..=1 => {
                    let pair = hotkey_full_outer_join::PairId(pair_idx);
                    let old = inflight.remove(&pair_idx).unwrap();
                    let new_task = join.supersede_pair(pair);
                    assert_eq!(new_task.attempt.0, old.attempt.0 + 1);
                    let old_result = node.run(&old);
                    zombies.push((old_result, rng.below(2) == 0));
                    inflight.insert(pair_idx, new_task);
                }
                // 20%：当前尝试失败。
                2..=3 => {
                    let task = inflight.remove(&pair_idx).unwrap();
                    let status = join.on_failure(task.pair, task.attempt);
                    assert_eq!(status, CallbackStatus::Accepted);
                }
                // 60%：当前尝试真实执行并成功回报。
                _ => {
                    let task = inflight.remove(&pair_idx).unwrap();
                    let result = node.run(&task);
                    let status = join.on_success(result);
                    assert_eq!(status, CallbackStatus::Accepted);
                    outputs.extend(join.drain_output());
                }
            }
        }

        // 3) 投递一个僵尸（过期）回调，必须全部被识别为 Stale；无条件投递，保证排空。
        if !zombies.is_empty() {
            let (result, as_failure) = zombies.remove(rng.below(zombies.len()));
            let status = if as_failure {
                join.on_failure(result.pair, result.attempt)
            } else {
                join.on_success(result)
            };
            assert_eq!(status, CallbackStatus::Stale);
            outputs.extend(join.drain_output());
        }
    }

    outputs.extend(join.drain_output());
    let actual: BTreeSet<_> = outputs.into_iter().collect();
    let expected = reference(&left, &right);
    assert_eq!(
        actual, expected,
        "seed {seed} 输出与参考实现不一致; cap={cap}, left={left:?}, right={right:?}"
    );

    // 指标一致性检查。
    let m = join.metrics();
    assert_eq!(
        m.confirmed_pairs as usize,
        join.pair_count(),
        "seed {seed} 每个块对应确认一次"
    );
    assert!(m.peak_task_input_rows <= 2 * cap, "seed {seed} 内存上界被突破");
    assert_eq!(
        m.matched_outputs as usize,
        actual
            .iter()
            .filter(|o| matches!(o, JoinOut::Matched { .. }))
            .count()
    );
    assert_eq!(
        m.left_only_outputs as usize,
        actual
            .iter()
            .filter(|o| matches!(o, JoinOut::LeftOnly { .. }))
            .count()
    );
    assert_eq!(
        m.right_only_outputs as usize,
        actual
            .iter()
            .filter(|o| matches!(o, JoinOut::RightOnly { .. }))
            .count()
    );
    let null_rows = left.iter().filter(|r| r.key.is_none()).count()
        + right.iter().filter(|r| r.key.is_none()).count();
    assert_eq!(m.null_key_rows as usize, null_rows);

    let snapshot = join.metrics();
    (snapshot.clone(), snapshot.dispatched_attempts, snapshot.confirmed_pairs)
}

#[test]
fn randomized_interleaves_match_reference_across_seeds() {
    let mut any_failure = false;
    let mut any_stale = false;
    let mut any_retry = false;
    for seed in 1..=200u64 {
        let (m, attempts, confirmed) =
            run_seed(seed.wrapping_mul(0x9e3779b97f4a7c15) ^ 0xabcdef);
        any_failure |= m.accepted_failures > 0;
        any_stale |= m.stale_callbacks > 0;
        any_retry |= attempts > confirmed as u64;
    }
    assert!(any_failure, "200 个种子中应至少出现一次被采纳的失败回调");
    assert!(any_stale, "200 个种子中应至少出现一次过期回调被丢弃");
    assert!(any_retry, "200 个种子中应至少出现一次重试/取代派发");
}
