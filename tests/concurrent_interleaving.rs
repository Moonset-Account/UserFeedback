//! 并发交错场景：多线程同时 ingest / drain / ack / 报告裁剪，
//! 验证互斥保护下前沿单调、投递不重不漏。

mod common;

use std::collections::{BTreeSet, HashSet};
use std::sync::{Arc, Barrier};
use std::thread;

use mirror_recovery::{DeliveryKind, DrainStatus, ThreadSafeRecovery, TruncatedRange};

use common::mirrored;

const P: u32 = 0;
const LAST: u64 = 200;
const TRUNC_FROM: u64 = 50;
const TRUNC_TO: u64 = 59;

/// 测试覆盖的来源位点：1..=200，去掉裁剪区间 50..=59。
fn expected_sources() -> BTreeSet<u64> {
    (1..=LAST)
        .filter(|off| !(TRUNC_FROM..=TRUNC_TO).contains(off))
        .collect()
}

#[test]
fn concurrent_interleaving_preserves_ordering_and_no_duplicates() {
    let recovery = Arc::new(ThreadSafeRecovery::new());
    recovery.ensure_partition(P, 0).unwrap();

    // 先放入全部镜像（打乱到达顺序，B 位点按到达顺序分配），
    // 并重复镜像一部分消息、混入裁剪报告。
    let mut sources: Vec<u64> = expected_sources().iter().copied().collect();
    // 简单确定性洗牌：按 (offset * 7 % 199) 排序。
    sources.sort_by_key(|off| (off * 7) % 199);

    let mut b_offset = 1000u64;
    for source in &sources {
        b_offset += 1;
        recovery
            .ingest(P, mirrored(b_offset, *source, &format!("id-{source}")))
            .unwrap();
        if source % 17 == 0 {
            b_offset += 1;
            // 重复镜像：必须被识别，不能产生第二条缓冲。
            let outcome = recovery
                .ingest(P, mirrored(b_offset, *source, &format!("id-{source}")))
                .unwrap();
            assert!(matches!(
                outcome,
                mirror_recovery::IngestOutcome::Duplicate { .. }
            ));
        }
    }
    recovery
        .report_truncation(P, TruncatedRange::new(TRUNC_FROM, TRUNC_TO).unwrap())
        .unwrap();

    let barrier = Arc::new(Barrier::new(4));

    // 消费者 A：循环 drain + 确认拿到的镜像消息。
    let handle_a = {
        let recovery = Arc::clone(&recovery);
        let barrier = Arc::clone(&barrier);
        thread::spawn(move || {
            let mut delivered = BTreeSet::new();
            barrier.wait();
            for _ in 0..500 {
                let report = match recovery.drain(P, 8) {
                    Ok(r) => r,
                    Err(_) => continue,
                };
                for d in &report.delivered {
                    if d.kind == DeliveryKind::Mirrored {
                        let source = d.source_offset.unwrap();
                        assert!(
                            delivered.insert(source),
                            "consumer A saw duplicate delivery of {source}"
                        );
                        recovery.ack_mirrored(P, source).unwrap();
                    }
                }
                if report.status == DrainStatus::Gap && report.delivered.is_empty() {
                    // 数据尚未被其它线程补齐时短暂让出，避免空转。
                    thread::yield_now();
                }
            }
            delivered
        })
    };

    // 消费者 B：同样 drain + 确认，与 A 竞争；两者合并不应出现重复投递。
    let handle_b = {
        let recovery = Arc::clone(&recovery);
        let barrier = Arc::clone(&barrier);
        thread::spawn(move || {
            let mut delivered = BTreeSet::new();
            barrier.wait();
            for _ in 0..500 {
                let report = match recovery.drain(P, 8) {
                    Ok(r) => r,
                    Err(_) => continue,
                };
                for d in &report.delivered {
                    if d.kind == DeliveryKind::Mirrored {
                        let source = d.source_offset.unwrap();
                        assert!(
                            delivered.insert(source),
                            "consumer B saw duplicate delivery of {source}"
                        );
                        // 确认可能已被另一个消费者先行完成：幂等结果同样接受。
                        let _ = recovery.ack_mirrored(P, source);
                    }
                }
                if report.status == DrainStatus::Gap && report.delivered.is_empty() {
                    thread::yield_now();
                }
            }
            delivered
        })
    };

    // 重放线程：只重放已处于前沿之内的位点（动态读取前沿），
    // 验证并发重放全部走 Replayed，不会制造重复投递。
    let handle_replay = {
        let recovery = Arc::clone(&recovery);
        let barrier = Arc::clone(&barrier);
        thread::spawn(move || {
            barrier.wait();
            let mut replayed = 0usize;
            for round in 0..50u64 {
                let frontier = recovery.frontier(P).unwrap();
                for source in 1..=frontier {
                    if (TRUNC_FROM..=TRUNC_TO).contains(&source) {
                        continue; // 裁剪位点没有镜像实体，重放无意义
                    }
                    let b = 5000 + round * 1000 + source;
                    if let mirror_recovery::IngestOutcome::Replayed { .. } = recovery
                        .ingest(P, mirrored(b, source, &format!("id-{source}")))
                        .unwrap()
                    {
                        replayed += 1;
                    }
                }
                thread::yield_now();
            }
            replayed
        })
    };

    // 裁剪报告线程：并发重复登记同一区间，验证合并逻辑在锁保护下不 panic。
    let handle_truncate = {
        let recovery = Arc::clone(&recovery);
        let barrier = Arc::clone(&barrier);
        thread::spawn(move || {
            barrier.wait();
            for _ in 0..50u64 {
                recovery
                    .report_truncation(P, TruncatedRange::new(TRUNC_FROM, TRUNC_TO).unwrap())
                    .unwrap();
            }
        })
    };

    let consumer_a = handle_a.join().unwrap();
    let consumer_b = handle_b.join().unwrap();
    let total_replayed = handle_replay.join().unwrap();
    handle_truncate.join().unwrap();

    // 两个消费者看到的投递集合必须互斥。
    let intersection: HashSet<&u64> = consumer_a.intersection(&consumer_b).collect();
    assert!(intersection.is_empty(), "both consumers delivered {intersection:?}");

    // 合并后恰好覆盖全部非裁剪位点。
    let merged: BTreeSet<u64> = consumer_a.union(&consumer_b).copied().collect();
    assert_eq!(merged, expected_sources());

    // 前沿最终推进到 LAST（裁剪区间已折叠）。
    assert_eq!(recovery.frontier(P).unwrap(), LAST);

    // 重放线程确实观察到了 Replayed 路径。
    assert!(total_replayed > 0);

    // 最终再 drain 不再投递任何消息。
    let final_report = recovery.drain(P, 16).unwrap();
    assert!(final_report.delivered.is_empty());
    assert_eq!(final_report.frontier, LAST);
}
