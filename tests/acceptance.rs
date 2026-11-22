//! 端到端验收测试。
//!
//! 这些测试把题目要求的故障恢复、边界与并发交错场景当作验收条件：
//! 1. 同一逻辑调用的重算 / 重试 / 换 rank / 分段执行，逐元素核对两份从同一确认点
//!    派生的掩码；
//! 2. 提前取消部分批次、回包乱序不影响其他位置接受的值；
//! 3. 多线程并发重算（真实线程交错）结果一致；
//! 4. 不同用途 / 调用序号 / 样本的独立分支之间抽样边界互不侵犯。

use std::collections::{BTreeMap, BTreeSet};

use rngflow::rng::{Purpose, StreamKey};
use rngflow::{
    candidate_word, recompute_mask_bit, Checkpoint, StreamCursor,
};

const SEED: u64 = 2026_1123;
const STEP: u64 = 8;
const PATH: &str = "blocks/2/attn/dropout";
const CALL: u64 = 1;
const KEEP_PROB: f64 = 0.3;
const MASK_LEN: u64 = 40;
const MAX_ATTEMPTS: u64 = 64;

/// 确定性拒绝规则：拒绝高 4 位为 `0xF` 的候选。两次独立执行用同一谓词，
/// 因而在同一位置必然拒绝相同数量的候选。
fn acceptable(word: u64) -> bool {
    (word >> 60) != 0xF
}

fn dropout_key(sample_id: u64, call: u64) -> StreamKey {
    StreamKey::new(SEED, STEP, sample_id, PATH, call, Purpose::DROPOUT)
}

/// 一份参考结果：每个位置的（接受随机字、掩码位、接受时候选序号）。
#[derive(Debug, Clone)]
struct ReferenceMask {
    words: Vec<u64>,
    bits: Vec<bool>,
    attempts: Vec<u64>,
}

fn run_reference(sample_id: u64) -> ReferenceMask {
    let key = dropout_key(sample_id, CALL);
    let mut cursor = StreamCursor::new(key);
    let mut words = Vec::new();
    let mut bits = Vec::new();
    for _ in 0..MASK_LEN {
        let word = cursor
            .accept_if(MAX_ATTEMPTS, acceptable)
            .expect("64 个候选内必须出现可接受字");
        words.push(word);
        let threshold = (KEEP_PROB * u64::MAX as f64) as u128;
        bits.push(u128::from(word) <= threshold);
    }
    // 接受时候选序号直接取自游标持久化的拒绝历史。
    let (next_position, attempts) = cursor.progress();
    assert_eq!(next_position, MASK_LEN);
    assert_eq!(attempts.len(), MASK_LEN as usize);
    ReferenceMask {
        words,
        bits,
        attempts,
    }
}

/// 用“重算”方式（无游标、仅显式逻辑位置 + 确认点里的候选序号）生成掩码。
fn recompute_mask(sample_id: u64, attempts: &[u64]) -> Vec<bool> {
    let key = dropout_key(sample_id, CALL);
    attempts
        .iter()
        .enumerate()
        .map(|(position, &attempt)| {
            recompute_mask_bit(&key, position as u64, attempt, KEEP_PROB)
        })
        .collect()
}

#[test]
fn recompute_after_rank_swap_and_segmentation_matches_elementwise() {
    // 样本 0..6 的参考结果。
    let reference: BTreeMap<u64, ReferenceMask> =
        (0..6).map(|s| (s, run_reference(s))).collect();

    // ---- 执行 A：rank 0 上一次跑完，中途在位置 20 打确认点 ----
    let mut cp_a = Checkpoint::new();
    let mut cursor_a = StreamCursor::new(dropout_key(0, CALL));
    for _ in 0..20 {
        cursor_a.accept_if(MAX_ATTEMPTS, acceptable).unwrap();
    }
    cp_a.record_cursor(&cursor_a);

    // 从参考结果里选一个确认点之后、确实需要拒绝候选的位置，
    // 模拟“在该位置的拒绝采样中途崩溃”：确认点只含已接受位置，
    // 重试时从该位置候选序号 0 重新开始，必须再次拒绝同样数量的候选。
    let crash_position = reference[&0]
        .attempts
        .iter()
        .enumerate()
        .skip(20)
        .find(|(_, &attempts)| attempts > 0)
        .map(|(p, _)| p as u64)
        .expect("参考序列中应当存在需要拒绝候选的位置");
    let crash_attempts = reference[&0].attempts[crash_position as usize];
    assert!(crash_attempts > 0);

    // 先把确认点推进到崩溃位置（全部成功接受）。
    let record = loop {
        let rec = cp_a.get_key(&dropout_key(0, CALL)).unwrap().clone();
        if rec.next_position == crash_position {
            break rec;
        }
        let mut cur = rec.restore_cursor();
        cur.accept_if(MAX_ATTEMPTS, acceptable).unwrap();
        cp_a.record_cursor(&cur);
    };

    // 崩溃前执行：按谓词拒绝 crash_attempts 个候选后进程死亡（游标丢弃）。
    let mut dying = record.restore_cursor();
    for _ in 0..crash_attempts {
        assert!(!acceptable(dying.peek_word()), "被拒绝的候选必须不满足谓词");
        dying.reject();
    }
    let word_on_death = dying.peek_word();
    drop(dying);

    // 重试执行：从同一确认点恢复，重放拒绝路径，接受同一个候选。
    let mut retry_cursor = record.restore_cursor();
    for _ in 0..crash_attempts {
        retry_cursor.reject();
    }
    assert_eq!(retry_cursor.peek_word(), word_on_death);
    let retried_word = retry_cursor.accept();
    assert_eq!(retried_word, reference[&0].words[crash_position as usize]);
    while retry_cursor.next_position() < MASK_LEN {
        retry_cursor
            .accept_if(MAX_ATTEMPTS, acceptable)
            .unwrap();
    }
    let mask_a = recompute_mask(0, &retry_cursor.progress().1);
    assert_eq!(mask_a, reference[&0].bits);

    // ---- 执行 B：换 rank（rank 7）、拆成三段、样本乱序回包 ----
    // rank 不进入流坐标，这里仅用变量表达物理调度差异。
    let rank_b: u32 = 7;
    let _ = rank_b;
    let mut cp_b = Checkpoint::new();
    let segments = [0u64..10, 10..25, 25..MASK_LEN];
    let mut cursor_b = StreamCursor::new(dropout_key(0, CALL));
    for segment in segments {
        for _ in segment {
            cursor_b.accept_if(MAX_ATTEMPTS, acceptable).unwrap();
        }
        // 每段结束落确认点，下一段从确认点恢复。
        cp_b.record_cursor(&cursor_b);
        let rec = cp_b.get_key(&dropout_key(0, CALL)).unwrap().clone();
        cursor_b = rec.restore_cursor();
    }
    let mask_b = recompute_mask(0, &cursor_b.progress().1);

    // 逐元素核对：两份从同一确认点体系派生的掩码完全一致。
    assert_eq!(mask_a.len(), mask_b.len());
    for (i, (a, b)) in mask_a.iter().zip(mask_b.iter()).enumerate() {
        assert_eq!(a, b, "位置 {i} 的掩码位在重算/换rank/分段后不一致");
    }
    assert_eq!(mask_b, reference[&0].bits);

    // 乱序回包：样本按 [5,1,4,0,2] 处理，结果按样本 ID 归位后仍与参考一致。
    let mut out_of_order: BTreeMap<u64, Vec<bool>> = BTreeMap::new();
    for sample_id in [5u64, 1, 4, 0, 2] {
        let mut cur = StreamCursor::new(dropout_key(sample_id, CALL));
        for _ in 0..MASK_LEN {
            cur.accept_if(MAX_ATTEMPTS, acceptable).unwrap();
        }
        out_of_order.insert(sample_id, recompute_mask(sample_id, &cur.progress().1));
    }
    for (sample_id, mask) in out_of_order {
        assert_eq!(mask, reference[&sample_id].bits);
    }
}

#[test]
fn early_cancellation_of_batches_leaves_other_positions_untouched() {
    // 先完整跑样本 0..4 作为参考。
    let reference: BTreeMap<u64, ReferenceMask> =
        (0..5).map(|s| (s, run_reference(s))).collect();

    // 提前取消：样本 1、3 的批次被整体取消（游标从未建立/未落盘）。
    let cancelled: BTreeSet<u64> = BTreeSet::from([1u64, 3]);
    let mut cp = Checkpoint::new();
    for sample_id in 0..5u64 {
        if cancelled.contains(&sample_id) {
            continue;
        }
        let mut cur = StreamCursor::new(dropout_key(sample_id, CALL));
        // 再模拟“批内提前取消”：样本 2 只接受前 12 个位置就停止。
        let limit = if sample_id == 2 { 12 } else { MASK_LEN };
        for _ in 0..limit {
            cur.accept_if(MAX_ATTEMPTS, acceptable).unwrap();
        }
        cp.record_cursor(&cur);
    }

    // 取消不能改变其他位置接受的值：未取消样本逐元素重算核对。
    for sample_id in [0u64, 4] {
        let rec = cp.get_key(&dropout_key(sample_id, CALL)).unwrap();
        let mask = recompute_mask(sample_id, &rec.rejections);
        assert_eq!(mask, reference[&sample_id].bits);
    }
    // 样本 2 的前 12 个位置与参考一致，且确认点里恰好只有这 12 个位置。
    let rec2 = cp.get_key(&dropout_key(2, CALL)).unwrap();
    assert_eq!(rec2.next_position, 12);
    let partial = recompute_mask(2, &rec2.rejections);
    assert_eq!(partial, reference[&2].bits[..12]);

    // 被取消样本在确认点中完全缺席，恢复后补跑仍得到参考值。
    for sample_id in cancelled {
        assert!(cp.get_key(&dropout_key(sample_id, CALL)).is_none());
        let mut cur = StreamCursor::new(dropout_key(sample_id, CALL));
        for _ in 0..MASK_LEN {
            cur.accept_if(MAX_ATTEMPTS, acceptable).unwrap();
        }
        assert_eq!(
            recompute_mask(sample_id, &cur.progress().1),
            reference[&sample_id].bits
        );
    }
}

#[test]
fn concurrent_recompute_from_one_checkpoint_is_interleaving_safe() {
    let reference = run_reference(0);
    let key = dropout_key(0, CALL);

    // 确认点只保存逻辑坐标与每位置候选序号。
    let mut cp = Checkpoint::new();
    cp.record_progress(key, MASK_LEN, reference.attempts.clone());
    let record = cp.get_key(&key).unwrap().clone();

    // 8 个线程并发重算不同位置区间，真实发生线程调度交错；
    // 全程无共享可变状态，仅读确认点记录。
    let handles: Vec<_> = (0..8)
        .map(|worker| {
            let rec = record.clone();
            std::thread::spawn(move || {
                let mut local = Vec::new();
                let mut p = worker as u64;
                while p < MASK_LEN {
                    let attempt = rec.rejections[p as usize];
                    local.push((
                        p,
                        recompute_mask_bit(&rec.key, p, attempt, KEEP_PROB),
                        candidate_word(&rec.key, p, attempt),
                    ));
                    p += 8;
                }
                local
            })
        })
        .collect();

    let mut joined: BTreeMap<u64, (bool, u64)> = BTreeMap::new();
    for h in handles {
        for (p, bit, word) in h.join().expect("工作线程不应 panic") {
            assert!(joined.insert(p, (bit, word)).is_none(), "位置被重复计算");
        }
    }
    assert_eq!(joined.len() as u64, MASK_LEN);
    for (p, (bit, word)) in joined {
        assert_eq!(bit, reference.bits[p as usize], "并发重算掩码位 {p} 不一致");
        assert_eq!(word, reference.words[p as usize], "并发重算随机字 {p} 不一致");
    }
}

#[test]
fn independent_branches_keep_separate_sampling_boundaries() {
    let base = StreamKey::new(SEED, STEP, 0, PATH, CALL, Purpose::DROPOUT);

    // 同一逻辑调用下两个用途各自推进：routing 抽 15 个、dropout 抽 3 个。
    let mut routing = StreamCursor::new(StreamKey {
        purpose: Purpose::ROUTING,
        ..base
    });
    let mut dropout = StreamCursor::new(base);
    for _ in 0..15 {
        routing.accept();
    }
    for _ in 0..3 {
        dropout.accept();
    }

    // 边界 1：dropout 的位置编号没有被 routing 消耗。
    assert_eq!(routing.next_position(), 15);
    assert_eq!(dropout.next_position(), 3);
    assert_eq!(
        dropout.accepted_word(2).unwrap(),
        candidate_word(&base, 2, 0),
        "dropout 位置 2 的值必须只由自己的位置计数决定"
    );

    // 边界 2：routing 中途取消（丢弃游标）不改变 dropout 已接受/将接受的值。
    let mut dropout2 = StreamCursor::new(base);
    let d0 = dropout2.accept();
    let _routed: Vec<u64> = {
        let mut r = StreamCursor::new(StreamKey {
            purpose: Purpose::ROUTING,
            ..base
        });
        (0..7).map(|_| r.accept()).collect()
        // r 在此 drop，模拟 routing 分支提前取消。
    };
    let d1 = dropout2.accept();
    assert_eq!(d0, candidate_word(&base, 0, 0));
    assert_eq!(d1, candidate_word(&base, 1, 0));

    // 边界 3：不同逻辑调用序号、不同样本是独立流，同位置同候选序号取值不同；
    // 且修改一个流的进度不会在确认点里覆盖另一个流。
    let other_call = StreamKey::new(SEED, STEP, 0, PATH, CALL + 1, Purpose::DROPOUT);
    let other_sample = StreamKey::new(SEED, STEP, 99, PATH, CALL, Purpose::DROPOUT);
    let mut cp = Checkpoint::new();
    cp.record_cursor(&dropout);
    cp.record_cursor(&routing);
    cp.record_progress(other_call, 1, vec![0]);
    cp.record_progress(other_sample, 1, vec![0]);
    assert_eq!(cp.len(), 4);
    assert_ne!(
        cp.get_key(&base).unwrap().rejections.len(),
        0
    );
    assert_ne!(
        candidate_word(&base, 0, 0),
        candidate_word(&other_call, 0, 0)
    );
    assert_ne!(
        candidate_word(&base, 0, 0),
        candidate_word(&other_sample, 0, 0)
    );
    assert_ne!(
        candidate_word(&base, 0, 0),
        candidate_word(
            &StreamKey {
                purpose: Purpose::ROUTING,
                ..base
            },
            0,
            0
        )
    );
}

#[test]
fn checkpoint_survives_encoding_round_trip_under_fault_injection() {
    // 构造带拒绝历史的确认点，编码 -> 解码 -> 恢复 -> 继续抽样，
    // 与从未中断的游标逐值比对（故障恢复主路径）。
    let key = dropout_key(4, CALL);
    let mut uninterrupted = StreamCursor::new(key);
    let mut before_fault = StreamCursor::new(key);
    for _ in 0..18 {
        before_fault.accept_if(MAX_ATTEMPTS, acceptable).unwrap();
    }
    for _ in 0..18 {
        uninterrupted.accept_if(MAX_ATTEMPTS, acceptable).unwrap();
    }

    let mut cp = Checkpoint::new();
    cp.record_cursor(&before_fault);
    let bytes = cp.encode_to_vec();
    let recovered_cp = Checkpoint::decode(&bytes).expect("确认点解码失败");
    let mut recovered = recovered_cp
        .get_key(&key)
        .expect("流记录丢失")
        .restore_cursor();

    for position in 18..MASK_LEN {
        let a = recovered.accept_if(MAX_ATTEMPTS, acceptable).unwrap();
        let b = uninterrupted.accept_if(MAX_ATTEMPTS, acceptable).unwrap();
        assert_eq!(a, b, "故障恢复后位置 {position} 的接受值漂移");
    }
    assert_eq!(
        recovered.progress().1,
        uninterrupted.progress().1,
        "完整拒绝历史必须可重放"
    );
}
