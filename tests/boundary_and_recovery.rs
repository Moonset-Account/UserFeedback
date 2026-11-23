//! 边界条件与故障恢复场景测试。

mod common;

use mirror_recovery::{
    AckOutcome, DeliveryKind, DrainStatus, IngestOutcome, RecoveryError, RecoveryManager,
    TruncatedRange,
};

use common::{mirrored, native, native_offsets};

const P: u32 = 0;

fn manager_with_frontier(frontier: u64) -> RecoveryManager {
    let mut mgr = RecoveryManager::new();
    mgr.ensure_partition(P, frontier).unwrap();
    mgr
}

#[test]
fn unknown_partition_is_rejected_for_every_operation() {
    let mut mgr = RecoveryManager::new();
    assert!(matches!(
        mgr.ingest(7, mirrored(1, 1, "x")),
        Err(RecoveryError::UnknownPartition(7))
    ));
    assert!(matches!(
        mgr.drain(7, 10),
        Err(RecoveryError::UnknownPartition(7))
    ));
    assert!(matches!(
        mgr.ack_mirrored(7, 1),
        Err(RecoveryError::UnknownPartition(7))
    ));
    assert!(matches!(
        mgr.frontier(7),
        Err(RecoveryError::UnknownPartition(7))
    ));
    assert!(matches!(
        mgr.report_truncation(7, TruncatedRange::new(1, 2).unwrap()),
        Err(RecoveryError::UnknownPartition(7))
    ));
}

#[test]
fn invalid_truncation_ranges_are_rejected() {
    // 非法区间在构造时即被拒绝。
    assert_eq!(
        TruncatedRange::new(9, 1),
        Err(RecoveryError::InvalidTruncation { from: 9, to: 1 })
    );
    // 非法区间即便经由管理器登记也不会被静默接受。
    let mut mgr = RecoveryManager::new();
    mgr.ensure_partition(P, 0).unwrap();
    assert!(matches!(
        mgr.report_truncation(P, TruncatedRange { from: 9, to: 1 }),
        Err(RecoveryError::InvalidTruncation { from: 9, to: 1 })
    ));
    // 单点位点区间合法。
    assert_eq!(
        TruncatedRange::new(4, 4).unwrap(),
        TruncatedRange { from: 4, to: 4 }
    );
}

#[test]
fn empty_mirror_reports_open_gap_and_delivers_nothing() {
    let mut mgr = manager_with_frontier(0);
    let report = mgr.drain(P, 16).unwrap();
    assert!(report.delivered.is_empty());
    assert_eq!(report.status, DrainStatus::Gap);
    assert_eq!(report.gap, Some(mirror_recovery::Gap { from: 1, to: None }));
    assert_eq!(report.frontier, 0);
}

#[test]
fn truncation_directly_ahead_is_folded_without_a_gap() {
    let mut mgr = manager_with_frontier(0);
    mgr.report_truncation(P, TruncatedRange::new(1, 4).unwrap())
        .unwrap();
    mgr.ingest(P, mirrored(10, 5, "id-5")).unwrap();

    let report = mgr.drain(P, 16).unwrap();
    assert_eq!(
        report.skipped_truncations,
        vec![TruncatedRange::new(1, 4).unwrap()]
    );
    assert_eq!(report.frontier, 4);
    assert_eq!(
        report
            .delivered
            .iter()
            .filter(|d| d.kind == DeliveryKind::Mirrored)
            .map(|d| d.source_offset.unwrap())
            .collect::<Vec<_>>(),
        vec![5]
    );
    assert_eq!(report.gap, Some(mirror_recovery::Gap { from: 6, to: None }));
}

#[test]
fn adjacent_and_overlapping_truncations_merge() {
    let mut mgr = manager_with_frontier(0);
    mgr.report_truncation(P, TruncatedRange::new(2, 3).unwrap())
        .unwrap();
    mgr.report_truncation(P, TruncatedRange::new(4, 5).unwrap())
        .unwrap(); // 相邻
    mgr.report_truncation(P, TruncatedRange::new(1, 2).unwrap())
        .unwrap(); // 重叠、且起点更早
    assert_eq!(
        mgr.truncations(P).unwrap(),
        &[TruncatedRange::new(1, 5).unwrap()]
    );
}

#[test]
fn mirrored_entity_takes_precedence_over_truncation() {
    let mut mgr = manager_with_frontier(0);
    mgr.report_truncation(P, TruncatedRange::new(1, 3).unwrap())
        .unwrap();
    mgr.ingest(P, mirrored(10, 2, "id-2")).unwrap(); // 裁剪区内仍有实体

    let report = mgr.drain(P, 16).unwrap();
    let sources: Vec<u64> = report
        .delivered
        .iter()
        .filter(|d| d.kind == DeliveryKind::Mirrored)
        .map(|d| d.source_offset.unwrap())
        .collect();
    // 实体是更强的证据：位点 2 必须被投递，绝不能因为裁剪报告而静默丢弃。
    assert_eq!(sources, vec![2]);

    // 实体 2 在途未确认，drain 被阻断；但位点 1（无实体、已裁剪）的信息
    // 会在确认 2 之后的下一次 drain 中折叠，不会丢失。
    mgr.ack_mirrored(P, 2).unwrap();
    let next = mgr.drain(P, 16).unwrap();
    // 确认后前沿先到 2；位点 1 仍在前沿之后且已裁剪，按顺序折叠 1，
    // 再折叠 3，最终前沿推进到 3。
    assert_eq!(next.frontier, 3);
    assert!(next
        .skipped_truncations
        .iter()
        .all(|r| r.from >= 1 && r.to <= 3));
    assert!(next
        .delivered
        .iter()
        .all(|d| d.kind != DeliveryKind::Mirrored));
}

#[test]
fn conflicting_evidence_is_quarantined() {
    let mut mgr = manager_with_frontier(0);
    mgr.ingest(P, mirrored(10, 1, "id-1")).unwrap();

    // 同身份映射到不同来源位点。
    assert!(matches!(
        mgr.ingest(P, mirrored(11, 2, "id-1")).unwrap(),
        IngestOutcome::Conflict {
            existing_source_offset: 1,
            rejected_b_offset: 11,
            ..
        }
    ));
    // 同来源位点出现不同身份。
    assert!(matches!(
        mgr.ingest(P, mirrored(12, 1, "other-id")).unwrap(),
        IngestOutcome::Conflict {
            existing_source_offset: 1,
            rejected_b_offset: 12,
            ..
        }
    ));

    let report = mgr.drain(P, 16).unwrap();
    assert_eq!(report.delivered.len(), 1); // 只有首条合法事件被投递
}

#[test]
fn acking_undelivered_or_buffered_message_is_rejected() {
    let mut mgr = manager_with_frontier(0);
    mgr.ingest(P, mirrored(10, 2, "id-2")).unwrap(); // 1 缺失，2 只在缓冲里
    assert!(matches!(
        mgr.ack_mirrored(P, 1),
        Err(RecoveryError::NotDelivered {
            source_offset: 1,
            ..
        })
    ));
    assert!(matches!(
        mgr.ack_mirrored(P, 2),
        Err(RecoveryError::NotDelivered {
            source_offset: 2,
            ..
        })
    ));

    // B 侧原生消息未投递同样不能确认。
    mgr.ingest(P, native(20, "x")).unwrap();
    assert!(matches!(
        mgr.ack_native(P, 20),
        Err(RecoveryError::NotDelivered {
            source_offset: 20,
            ..
        })
    ));
}

#[test]
fn native_messages_are_redelivered_when_unacked_after_reopen() {
    let mut mgr = manager_with_frontier(0);
    mgr.ingest(P, native(30, "a")).unwrap();
    mgr.ingest(P, native(31, "b")).unwrap();

    let first = mgr.drain(P, 16).unwrap();
    assert_eq!(native_offsets(&first), vec![30, 31]);
    mgr.ack_native(P, 30).unwrap(); // 只确认第一条

    // 模拟重开：快照中保留未确认的原生消息（含负载），已确认位点不再补发。
    let snap = mgr.snapshot();
    let text = mirror_recovery::snapshot::encode(&snap);
    let mut reopened = RecoveryManager::restore(mirror_recovery::snapshot::decode(&text).unwrap());

    let second = reopened.drain(P, 16).unwrap();
    // 31 在途重投；32 已在首次恢复后的 drain 中投递并确认，不再出现。
    assert_eq!(native_offsets(&second), vec![31]);
    assert_eq!(second.delivered[0].payload, b"native-b".to_vec(),
        "payload must survive snapshot round-trip");
    reopened.ack_native(P, 31).unwrap();

    // 已确认原生消息重放会被丢弃；未知 B 位点仍可正常接收。
    assert!(matches!(
        reopened.ingest(P, native(30, "a")).unwrap(),
        IngestOutcome::Replayed { b_offset: 30, .. }
    ));
    reopened.ingest(P, native(32, "c")).unwrap();
    let third = reopened.drain(P, 16).unwrap();
    assert_eq!(native_offsets(&third), vec![32]);
}

#[test]
fn unacked_mirrored_message_is_redelivered_after_reopen() {
    // at-least-once：重开后未确认的在途消息由快照携带，需要重新投递；
    // 已确认进入前沿的消息即使 B 日志重放也不会再发。
    let mut mgr = manager_with_frontier(0);
    mgr.ingest(P, mirrored(10, 1, "id-1")).unwrap();
    mgr.ingest(P, mirrored(11, 2, "id-2")).unwrap();

    // drain 只投递连续可证明的第一个位点 1；位点 2 被在途的 1 挡住，
    // 但 peek 仍会报出位点 3 起的开放缺口。
    let first = mgr.drain(P, 16).unwrap();
    assert_eq!(first.delivered.len(), 1);
    assert_eq!(first.delivered[0].source_offset, Some(1));
    assert_eq!(first.status, DrainStatus::Gap);
    assert_eq!(first.gap, Some(mirror_recovery::Gap { from: 3, to: None }));

    // 进程崩溃前位点 1 也未确认：快照应携带位点 1 这一条在途消息。
    let snap = mgr.snapshot();
    assert_eq!(snap.partitions[&P].mirrored_pending.len(), 1);
    assert_eq!(snap.partitions[&P].mirrored_pending[0].source_offset, 1);
    let mut reopened = RecoveryManager::restore(
        mirror_recovery::snapshot::decode(&mirror_recovery::snapshot::encode(&snap)).unwrap(),
    );

    // B 日志重放：位点 1 已在快照在途集合中（重复重放走 Duplicate/Replayed 防护），
    // 位点 2 的缓冲条目需要由重放回填。
    assert!(matches!(
        reopened.ingest(P, mirrored(11, 2, "id-2")).unwrap(),
        IngestOutcome::Accepted { source_offset: 2, .. }
    ));

    // 重开后首次 drain：在途消息 1 重投（at-least-once），位点 2 仍被挡在后面。
    let second = reopened.drain(P, 16).unwrap();
    let sources: Vec<u64> = second
        .delivered
        .iter()
        .map(|d| d.source_offset.unwrap())
        .collect();
    assert_eq!(sources, vec![1]);
    assert_eq!(reopened.frontier(P).unwrap(), 0);

    // 同进程内再次 drain 不能重复投递（恢复标记已清除）；
    // 位点 1 在途，位点 2 有实体可证明，位点 3 起是开放缺口。
    let third = reopened.drain(P, 16).unwrap();
    assert!(third.delivered.is_empty());
    assert_eq!(third.status, DrainStatus::Gap);
    assert_eq!(third.gap, Some(mirror_recovery::Gap { from: 3, to: None }));

    // 确认位点 1 后前沿推进，位点 2 在下一次 drain 中正常投递。
    reopened.ack_mirrored(P, 1).unwrap();
    assert_eq!(reopened.frontier(P).unwrap(), 1);
    let fourth = reopened.drain(P, 16).unwrap();
    assert_eq!(
        fourth
            .delivered
            .iter()
            .map(|d| d.source_offset.unwrap())
            .collect::<Vec<_>>(),
        vec![2]
    );
    reopened.ack_mirrored(P, 2).unwrap();
    assert_eq!(reopened.frontier(P).unwrap(), 2);
}

#[test]
fn ensure_partition_is_idempotent_but_never_moves_frontier_backwards() {
    let mut mgr = RecoveryManager::new();
    mgr.ensure_partition(P, 5).unwrap();
    mgr.ensure_partition(P, 5).unwrap(); // 同前沿幂等
    mgr.ensure_partition(P, 3).unwrap_err(); // 试图回退
    mgr.ingest(P, mirrored(10, 6, "id-6")).unwrap();
    mgr.drain(P, 16).unwrap();
    mgr.ack_mirrored(P, 6).unwrap();
    assert_eq!(mgr.frontier(P).unwrap(), 6);
    // 更大的初始前沿也不允许覆盖已经推进的状态（防止重入切换导致回退）。
    mgr.ensure_partition(P, 100).unwrap();
    assert_eq!(mgr.frontier(P).unwrap(), 6);
}

#[test]
fn partitions_advance_independently() {
    let mut mgr = RecoveryManager::new();
    mgr.ensure_partition(0, 0).unwrap();
    mgr.ensure_partition(1, 10).unwrap();

    mgr.ingest(0, mirrored(1, 1, "p0-id-1")).unwrap();
    mgr.ingest(1, mirrored(2, 11, "p1-id-11")).unwrap();
    mgr.drain(0, 16).unwrap();
    mgr.drain(1, 16).unwrap();
    mgr.ack_mirrored(0, 1).unwrap();
    mgr.ack_mirrored(1, 11).unwrap();

    assert_eq!(mgr.frontier(0).unwrap(), 1);
    assert_eq!(mgr.frontier(1).unwrap(), 11);
    assert_eq!(mgr.partitions(), [0, 1].into_iter().collect());

    let snap = mgr.snapshot();
    let restored = RecoveryManager::restore(
        mirror_recovery::snapshot::decode(&mirror_recovery::snapshot::encode(&snap)).unwrap(),
    );
    assert_eq!(restored.frontier(0).unwrap(), 1);
    assert_eq!(restored.frontier(1).unwrap(), 11);
}

#[test]
fn max_offset_boundary_does_not_loop_forever() {
    let mut mgr = manager_with_frontier(u64::MAX - 1);
    // 最后一个位点也被裁剪：折叠到 u64::MAX 后必须正常终止。
    mgr.report_truncation(P, TruncatedRange::new(u64::MAX, u64::MAX).unwrap())
        .unwrap();
    let report = mgr.drain(P, 16).unwrap();
    assert_eq!(report.frontier, u64::MAX);
    assert!(report.gap.is_none());
    // 再次 drain 安全返回空报告。
    let again = mgr.drain(P, 16).unwrap();
    assert!(again.delivered.is_empty());
    assert_eq!(again.frontier, u64::MAX);
}

#[test]
fn switch_back_ack_folds_truncations_behind_confirmed_message() {
    let mut mgr = manager_with_frontier(0);
    mgr.report_truncation(P, TruncatedRange::new(2, 3).unwrap())
        .unwrap();
    // A 侧确认位点 1，随后裁剪区间 2..=3 应自动并入前沿。
    let outcome = mgr.switch_back_ack(P, 1).unwrap();
    assert_eq!(outcome, AckOutcome::Advanced { frontier: 3 });
    assert_eq!(mgr.frontier(P).unwrap(), 3);
}

#[test]
fn pending_message_blocks_redelivery_and_gap_detection_waits_for_ack() {
    let mut mgr = manager_with_frontier(0);
    mgr.ingest(P, mirrored(10, 1, "id-1")).unwrap();
    mgr.ingest(P, mirrored(11, 3, "id-3")).unwrap();
    let first = mgr.drain(P, 16).unwrap();
    assert_eq!(first.delivered.len(), 1); // 位点 2 缺失 -> gap，3 留在缓冲
    assert_eq!(first.status, DrainStatus::Gap);
    assert_eq!(
        first.gap,
        Some(mirror_recovery::Gap {
            from: 2,
            to: Some(3)
        })
    );

    // 位点 1 在途未确认：再次 drain 不能重发，但系统会越过在途消息向前看，
    // 仍然把位点 2 的缺口报告出来，方便消费者并行准备补数。
    let second = mgr.drain(P, 16).unwrap();
    assert!(second.delivered.is_empty());
    assert_eq!(second.status, DrainStatus::Gap);
    assert_eq!(
        second.gap,
        Some(mirror_recovery::Gap {
            from: 2,
            to: Some(3)
        })
    );

    mgr.ack_mirrored(P, 1).unwrap();
    // 确认后前沿推进到 1，再次 drain 暴露同样的位点 2 缺口。
    let third = mgr.drain(P, 16).unwrap();
    assert_eq!(third.status, DrainStatus::Gap);
    assert_eq!(
        third.gap,
        Some(mirror_recovery::Gap {
            from: 2,
            to: Some(3)
        })
    );
}
