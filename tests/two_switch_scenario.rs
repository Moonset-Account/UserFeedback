//! 题面核心场景：B 先收到高位点再收到低位点，中间夹一个明确裁剪区间，
//! 经历 A→B、重开重放、B→A、再 A→B 两次完整切换，核对：
//!
//! 1. 后到的低位点不会漏过；
//! 2. 已确认消息不会重复投递；
//! 3. “尚未镜像”与“明确裁剪”分别处理（后者折叠跳过，前者给出待补区间）。

mod common;

use mirror_recovery::{
    AckOutcome, DrainStatus, IngestOutcome, MirrorEvent, RecoveryError, RecoveryManager,
    TruncatedRange,
};

use common::{mirrored, mirrored_offsets, native, native_offsets};

const P: u32 = 0;

/// 追踪所有实际投递过的来源位点，用于全局“不重不漏”断言。
fn collect_mirrored(
    seen: &mut std::collections::BTreeSet<u64>,
    report: &mirror_recovery::DrainReport,
) {
    for (source, _) in mirrored_offsets(report) {
        assert!(
            seen.insert(source),
            "source offset {source} delivered twice"
        );
    }
}

/// 真实消费循环：drain -> 确认全部镜像投递 -> 再 drain，直到出现缺口或没有新投递。
/// 返回最后一次报告。
fn drain_and_ack_until_blocked(
    mgr: &mut RecoveryManager,
    seen: &mut std::collections::BTreeSet<u64>,
) -> mirror_recovery::DrainReport {
    let mut last = mgr.drain(P, 64).unwrap();
    loop {
        let sources: Vec<u64> = mirrored_offsets(&last).into_iter().map(|(s, _)| s).collect();
        if sources.is_empty() {
            return last;
        }
        collect_mirrored(seen, &last);
        for source in sources {
            mgr.ack_mirrored(P, source).unwrap();
        }
        last = mgr.drain(P, 64).unwrap();
    }
}

#[test]
fn two_switch_cycle_never_skips_low_offsets_nor_redelivers_acked() {
    let mut seen = std::collections::BTreeSet::new();
    let mut mgr = RecoveryManager::new();

    // ---- 切换前：消费者在 A 上已确认到来源位点 2，最后确认位点即初始前沿 ----
    mgr.ensure_partition(P, 2).unwrap();

    // ---- A→B：B 的实际位点按到达顺序决定，先到高位点，后到低位点 ----
    // 来源 5 尚未镜像；来源 6..=8 被 A 明确裁剪；B 中还混入原生消息与重复镜像。
    let b_log: Vec<MirrorEvent> = vec![
        mirrored(100, 9, "id-9"), // 高位点先到
        native(101, "b-only-1"),  // B 侧原生消息
        mirrored(102, 3, "id-3"), // 低位点后到
        mirrored(103, 4, "id-4"),
        mirrored(104, 9, "id-9"), // 同一 A 消息重复镜像（首次副本获胜）
        mirrored(105, 10, "id-10"),
    ];
    for event in b_log {
        mgr.ingest(P, event).unwrap();
    }
    mgr.report_truncation(P, TruncatedRange::new(6, 8).unwrap())
        .unwrap();

    // 重复镜像应被识别为 Duplicate，不新增缓冲。
    assert_eq!(
        mgr.ingest(P, mirrored(106, 9, "id-9")).unwrap(),
        IngestOutcome::Duplicate {
            b_offset: 106,
            source_offset: 9,
            id: "id-9".to_string(),
        }
    );

    // ---- 第一次 drain：低位点 3 按来源位点顺序先投（高位点 9 仍在缓冲）----
    let r1 = mgr.drain(P, 64).unwrap();
    assert_eq!(mirrored_offsets(&r1), vec![(3, 102)]);
    assert_eq!(native_offsets(&r1), vec![101]); // 原生消息同批投递
    // 位点 4 有实体但被在途的 3 挡住，peek 跳过连续证据后发现真正缺口在 5。
    assert_eq!(r1.status, DrainStatus::Gap);
    assert_eq!(
        r1.gap,
        Some(mirror_recovery::Gap {
            from: 5,
            to: Some(6)
        })
    );
    assert!(r1.skipped_truncations.is_empty()); // 缺口在裁剪区间之前，不能提前折叠
    collect_mirrored(&mut seen, &r1);

    // 再次 drain：镜像在途消息 3 不会重复发（原生消息在途时按 at-least-once 重投）。
    let r1b = mgr.drain(P, 64).unwrap();
    assert!(
        mirrored_offsets(&r1b).is_empty(),
        "in-flight mirrored message must not be redelivered in same session"
    );
    assert_eq!(
        r1b.gap,
        Some(mirror_recovery::Gap {
            from: 5,
            to: Some(6)
        })
    );

    // B 侧确认：只能确认已投递消息，未投递的 9 必须被拒绝。
    assert!(matches!(
        mgr.ack_mirrored(P, 9),
        Err(RecoveryError::NotDelivered {
            source_offset: 9,
            ..
        })
    ));
    mgr.ack_mirrored(P, 3).unwrap();
    assert_eq!(mgr.frontier(P).unwrap(), 3);
    // 幂等重复确认。
    assert!(matches!(
        mgr.ack_mirrored(P, 3).unwrap(),
        AckOutcome::AlreadyConfirmed {
            source_offset: 3,
            ..
        }
    ));
    assert_eq!(
        mgr.ack_native(P, 101).unwrap(),
        AckOutcome::Advanced { frontier: 3 }
    );

    // 确认 3 后，drain 投递 4；4 在途期间仍只能看到缺口 5。
    let r1c = mgr.drain(P, 64).unwrap();
    assert_eq!(mirrored_offsets(&r1c), vec![(4, 103)]);
    assert_eq!(
        r1c.gap,
        Some(mirror_recovery::Gap {
            from: 5,
            to: Some(6)
        })
    );
    collect_mirrored(&mut seen, &r1c);
    mgr.ack_mirrored(P, 4).unwrap();
    assert_eq!(mgr.frontier(P).unwrap(), 4);

    // ---- 补齐“尚未镜像”的位点 5（与裁剪区分处理的关键）----
    mgr.ingest(P, mirrored(107, 5, "id-5")).unwrap();

    // ---- 消费循环：投递 5 -> 折叠裁剪 6..=8 -> 投递 9 -> 投递 10，11 起开放缺口 ----
    let r2 = drain_and_ack_until_blocked(&mut mgr, &mut seen);
    assert_eq!(r2.status, DrainStatus::Gap);
    assert_eq!(r2.gap, Some(mirror_recovery::Gap { from: 11, to: None }));
    assert_eq!(r2.frontier, 10);
    assert!(mgr
        .truncations(P)
        .unwrap()
        .iter()
        .any(|r| r.from == 6 && r.to == 8));
    assert_eq!(mgr.frontier(P).unwrap(), 10);

    // ---- 故障重开：持久化快照后用 B 原始日志重放回填 ----
    let snapshot = mgr.snapshot();
    let text = mirror_recovery::snapshot::encode(&snapshot);
    let decoded = mirror_recovery::snapshot::decode(&text).unwrap();
    assert_eq!(decoded, snapshot);
    let mut reopened = RecoveryManager::restore(decoded);

    // 重放 B 日志：所有来源位点 <= 前沿 10 的镜像一律 Replayed，不会再次投递。
    assert_eq!(
        reopened.ingest(P, mirrored(102, 3, "id-3")).unwrap(),
        IngestOutcome::Replayed {
            b_offset: 102,
            source_offset: 3
        }
    );
    assert_eq!(
        reopened.ingest(P, mirrored(100, 9, "id-9")).unwrap(),
        IngestOutcome::Replayed {
            b_offset: 100,
            source_offset: 9
        }
    );
    // 重放中的重复镜像同样按 Replayed 丢弃。
    assert_eq!(
        reopened.ingest(P, mirrored(104, 9, "id-9")).unwrap(),
        IngestOutcome::Replayed {
            b_offset: 104,
            source_offset: 9
        }
    );
    // 已确认的 B 侧原生消息重放也被丢弃。
    assert_eq!(
        reopened.ingest(P, native(101, "b-only-1")).unwrap(),
        IngestOutcome::Replayed {
            b_offset: 101,
            source_offset: 101
        }
    );

    // 重开后前沿保持，drain 只剩开放缺口，无任何重复投递。
    let rr = reopened.drain(P, 64).unwrap();
    assert!(rr.delivered.is_empty());
    assert_eq!(rr.frontier, 10);
    assert_eq!(rr.gap, Some(mirror_recovery::Gap { from: 11, to: None }));

    // 重开后新到的消息可以继续被消费。
    reopened.ingest(P, mirrored(108, 11, "id-11")).unwrap();
    let rr2 = reopened.drain(P, 64).unwrap();
    assert_eq!(mirrored_offsets(&rr2), vec![(11, 108)]);
    collect_mirrored(&mut seen, &rr2);
    reopened.ack_mirrored(P, 11).unwrap();
    assert_eq!(reopened.frontier(P).unwrap(), 11);

    // ---- 切回 A：A 侧确认必须严格连续，前沿与 B 阶段共用 ----
    assert!(matches!(
        reopened.switch_back_ack(P, 13),
        Err(RecoveryError::OutOfOrderAck {
            expected: 12,
            ..
        })
    ));
    assert_eq!(
        reopened.switch_back_ack(P, 12).unwrap(),
        AckOutcome::Advanced { frontier: 12 }
    );
    assert_eq!(
        reopened.switch_back_ack(P, 13).unwrap(),
        AckOutcome::Advanced { frontier: 13 }
    );
    assert!(matches!(
        reopened.switch_back_ack(P, 12).unwrap(),
        AckOutcome::AlreadyConfirmed {
            source_offset: 12,
            ..
        }
    ));

    // ---- 再次 A→B：建立分区幂等，已确认的 3..=13 绝不重发 ----
    reopened.ensure_partition(P, 13).unwrap();
    assert_eq!(
        reopened.ingest(P, mirrored(109, 12, "id-12")).unwrap(),
        IngestOutcome::Replayed {
            b_offset: 109,
            source_offset: 12
        }
    );
    reopened.ingest(P, mirrored(110, 14, "id-14")).unwrap();
    reopened.ingest(P, mirrored(111, 15, "id-15")).unwrap();
    reopened
        .report_truncation(P, TruncatedRange::new(16, 16).unwrap())
        .unwrap();
    reopened.ingest(P, mirrored(112, 17, "id-17")).unwrap();
    // 来源 18 缺失，制造新的“尚未镜像”缺口。
    reopened.ingest(P, mirrored(113, 19, "id-19")).unwrap();

    // 投递预算限制为 1：每次只投一条，验证预算与在途阻断；
    // 即便预算耗尽，peek 仍会越过证据链报出远处缺口 18。
    let r3 = reopened.drain(P, 1).unwrap();
    assert_eq!(mirrored_offsets(&r3), vec![(14, 110)]);
    assert_eq!(r3.status, DrainStatus::Gap);
    assert_eq!(
        r3.gap,
        Some(mirror_recovery::Gap {
            from: 18,
            to: Some(19)
        })
    );
    collect_mirrored(&mut seen, &r3);
    reopened.ack_mirrored(P, 14).unwrap();

    let r3b = reopened.drain(P, 1).unwrap();
    assert_eq!(mirrored_offsets(&r3b), vec![(15, 111)]);
    collect_mirrored(&mut seen, &r3b);
    reopened.ack_mirrored(P, 15).unwrap();

    // 16 被裁剪自动折叠（由确认 15 后的 fold 完成）；17 投递后进入在途。
    let r4 = reopened.drain(P, 64).unwrap();
    assert_eq!(mirrored_offsets(&r4), vec![(17, 112)]);
    assert_eq!(
        r4.skipped_truncations,
        vec![TruncatedRange::new(16, 16).unwrap()]
    );
    assert_eq!(r4.frontier, 16); // 17 在途未确认，前沿停在裁剪折叠后的 16
    assert_eq!(
        r4.gap,
        Some(mirror_recovery::Gap {
            from: 18,
            to: Some(19)
        })
    );
    collect_mirrored(&mut seen, &r4);
    reopened.ack_mirrored(P, 17).unwrap();
    assert_eq!(reopened.frontier(P).unwrap(), 17);

    // 17 确认后，18 仍是缺口（19 有实体但被缺口挡住）。
    let r4b = reopened.drain(P, 64).unwrap();
    assert!(mirrored_offsets(&r4b).is_empty());
    assert_eq!(
        r4b.gap,
        Some(mirror_recovery::Gap {
            from: 18,
            to: Some(19)
        })
    );

    // 补齐缺口后走完整消费循环：投递 18、19，之后 20 起开放。
    reopened.ingest(P, mirrored(114, 18, "id-18")).unwrap();
    let r5 = drain_and_ack_until_blocked(&mut reopened, &mut seen);
    assert_eq!(r5.gap, Some(mirror_recovery::Gap { from: 20, to: None }));
    assert_eq!(reopened.frontier(P).unwrap(), 19);

    // ---- 全局核对 ----
    // B 侧实际投递过的来源位点：11 在重开阶段投递；12、13 切回 A 后由 A 直接
    // 确认（从未经过 B），14、15 在第二次 A→B 后投递；裁剪位点 6..=8、16 从不投递。
    let expected_b_deliveries: std::collections::BTreeSet<u64> =
        (3..=5).chain(9..=11).chain(14..=15).chain(17..=19).collect();
    assert_eq!(seen, expected_b_deliveries);
    // 最终前沿覆盖所有已确认与已裁剪位点。
    assert_eq!(reopened.frontier(P).unwrap(), 19);
    // 裁剪位点从未作为消息投递。
    for gone in [6, 7, 8, 16] {
        assert!(
            !seen.contains(&gone),
            "truncated offset {gone} was delivered"
        );
    }
    // 12、13 虽未经过 B，但通过切回 A 的连续确认并入了前沿，证明前沿在 A/B 间共用。
    assert!(reopened.frontier(P).unwrap() >= 13);
}
