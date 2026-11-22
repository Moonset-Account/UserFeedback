//! 发送端重传状态机的集成测试。
//!
//! 覆盖题面明确要求的验收场景：
//! - 两个空间同号、半段先确认后的剩余字节与在途增减；
//! - 旧包判丢后确认迟到（重传未发 / 已发两种交错）；
//! - 同一区间出现在多个承载包中，字节只完成一次；
//! - 按空间独立的丢失时限与下次计时时刻；
//! - 关闭包号空间只撤销本空间，并按调用方清单转移仍需完成的数据；
//! - 确认区间归并、未知包号忽略、重复确认幂等等边界。

use retransmit_state::{
    AckRanges, Event, PktStatus, Sender, SenderError, SentPacket, SpaceId, StreamRange,
};

const HS: SpaceId = SpaceId::Handshake;
const APP: SpaceId = SpaceId::AppData;

fn range(stream_id: u64, start: u64, end: u64) -> StreamRange {
    StreamRange::new(stream_id, start, end).unwrap()
}

fn pkt(
    space: SpaceId,
    packet_number: u64,
    time_sent: u64,
    size: u64,
    frames: Vec<StreamRange>,
) -> SentPacket {
    SentPacket {
        space,
        packet_number,
        time_sent,
        size,
        frames,
    }
}

fn ack_single(packet_number: u64) -> AckRanges {
    AckRanges::from_pairs(&[(packet_number, packet_number)]).unwrap()
}

/// 题面指定核对例：两个空间同号，半段先确认，核对剩余字节与在途增减。
#[test]
fn two_spaces_same_number_half_ack_remaining_and_inflight() {
    let mut sender = Sender::new(10, 10);

    // 握手空间与应用空间各有一个 0 号包，互不干扰。
    sender.on_sent(pkt(HS, 0, 0, 100, vec![])).unwrap();
    // 应用空间 0 号包承载流 0 的前半段，1 号包承载后半段。
    sender
        .on_sent(pkt(APP, 0, 0, 50, vec![range(0, 0, 50)]))
        .unwrap();
    sender
        .on_sent(pkt(APP, 1, 1, 50, vec![range(0, 50, 100)]))
        .unwrap();

    let accounts = sender.accounts();
    assert_eq!(accounts[0].in_flight_bytes, 100);
    assert_eq!(accounts[0].in_flight_packets, 1);
    assert_eq!(accounts[1].in_flight_bytes, 100);
    assert_eq!(accounts[1].in_flight_packets, 2);
    assert_eq!(sender.next_timeout(), Some(10));

    // 半段先确认：应用空间 0 号包 ACK，字节 [0,50) 完成。
    sender.on_ack(APP, &ack_single(0), 5).unwrap();

    let app = &sender.accounts()[1];
    assert_eq!(app.in_flight_bytes, 50);
    assert_eq!(app.in_flight_packets, 1);
    assert_eq!(app.acked_bytes, 50);
    assert_eq!(sender.completed_ranges(), vec![range(0, 0, 50)]);
    // 握手空间完全不受应用空间确认影响。
    let handshake = &sender.accounts()[0];
    assert_eq!(handshake.in_flight_bytes, 100);
    assert_eq!(handshake.in_flight_packets, 1);
    // 同号包在两个空间中的状态彼此独立。
    assert_eq!(
        sender.packet_status(HS, 0).unwrap().status,
        PktStatus::InFlight
    );
    assert_eq!(
        sender.packet_status(APP, 0).unwrap().status,
        PktStatus::Acked
    );
    assert_eq!(
        sender.packet_status(APP, 1).unwrap().status,
        PktStatus::InFlight
    );

    // 最早计时点仍是握手包的 t=10。
    assert_eq!(sender.next_timeout(), Some(10));
    // 推进时钟：握手 0 号包（deadline 10）与应用 1 号包（deadline 11）先后判丢。
    sender.on_timeout(11).unwrap();

    let handshake = &sender.accounts()[0];
    assert_eq!(handshake.in_flight_bytes, 0);
    assert_eq!(handshake.in_flight_packets, 0);
    assert_eq!(handshake.lost_packets, 1);
    let app = &sender.accounts()[1];
    assert_eq!(app.in_flight_bytes, 0);
    assert_eq!(app.in_flight_packets, 0);
    assert_eq!(app.lost_packets, 1);
    // 剩余待发字节只有后半段 [50,100)，前半段已完成不会重传。
    assert_eq!(sender.all_pending_ranges(), vec![range(0, 50, 100)]);
    assert_eq!(sender.pending_ranges(APP), vec![range(0, 50, 100)]);
    assert_eq!(sender.pending_ranges(HS), vec![]);

    // 重传剩余半段并确认，全部字节完成。
    sender
        .on_sent(pkt(APP, 2, 12, 50, vec![range(0, 50, 100)]))
        .unwrap();
    assert_eq!(sender.pending_ranges(APP), vec![]);
    assert_eq!(sender.accounts()[1].in_flight_bytes, 50);
    sender.on_ack(APP, &ack_single(2), 13).unwrap();
    assert_eq!(sender.completed_ranges(), vec![range(0, 0, 100)]);
    assert_eq!(sender.accounts()[1].in_flight_bytes, 0);
    assert_eq!(sender.accounts()[1].acked_bytes, 100);
    assert_eq!(sender.next_timeout(), None);
}

/// 核心交错：旧包判丢、重传尚未发出时确认迟到。
#[test]
fn spurious_ack_cancels_unsent_retransmit_and_keeps_other_inflight() {
    let mut sender = Sender::new(10, 10);
    sender
        .on_sent(pkt(APP, 0, 0, 100, vec![range(0, 0, 100)]))
        .unwrap();
    // 另一个包保持在途，用于核对它的计数不被迟到确认影响。
    sender.on_sent(pkt(APP, 1, 3, 40, vec![])).unwrap();
    assert_eq!(sender.accounts()[1].in_flight_bytes, 140);

    // t=10：0 号包超时判丢并离开在途，进入重传计划；1 号包 deadline 13 仍在途。
    sender.on_timeout(10).unwrap();
    let app = sender.accounts()[1];
    assert_eq!(app.in_flight_bytes, 40);
    assert_eq!(app.in_flight_packets, 1);
    assert_eq!(app.lost_packets, 1);
    assert_eq!(sender.pending_ranges(APP), vec![range(0, 0, 100)]);

    // 旧包的确认在重传发出前迟到。
    sender.on_ack(APP, &ack_single(0), 12).unwrap();
    assert_eq!(
        sender.packet_status(APP, 0).unwrap().status,
        PktStatus::AckedAfterLoss
    );
    // 尚未发送的同区间重传计划被撤去。
    assert_eq!(sender.pending_ranges(APP), vec![]);
    assert_eq!(sender.all_pending_ranges(), vec![]);
    assert_eq!(sender.completed_ranges(), vec![range(0, 0, 100)]);
    // 在途计数不变：1 号包继续在途，0 号包不会被二次扣减。
    let app = sender.accounts()[1];
    assert_eq!(app.in_flight_bytes, 40);
    assert_eq!(app.in_flight_packets, 1);
    assert_eq!(app.acked_bytes, 0);
    assert_eq!(app.spurious_ack_packets, 1);

    // 重复确认幂等：计数不再变化（同一时刻重放，时钟不回退）。
    sender.on_ack(APP, &ack_single(0), 12).unwrap();
    let app = sender.accounts()[1];
    assert_eq!(app.spurious_ack_packets, 1);
    assert_eq!(app.in_flight_bytes, 40);
    assert_eq!(app.in_flight_packets, 1);

    // 1 号包随后正常确认，在途只剩 0。
    sender.on_ack(APP, &ack_single(1), 13).unwrap();
    let app = sender.accounts()[1];
    assert_eq!(app.in_flight_bytes, 0);
    assert_eq!(app.acked_bytes, 40);
}

/// 交错变体：重传包已经发出后旧包确认才迟到。
#[test]
fn spurious_ack_after_retransmit_sent_keeps_retransmit_inflight() {
    let mut sender = Sender::new(10, 10);
    sender
        .on_sent(pkt(APP, 0, 0, 100, vec![range(0, 0, 100)]))
        .unwrap();
    sender.on_timeout(10).unwrap();
    assert_eq!(sender.pending_ranges(APP), vec![range(0, 0, 100)]);

    // 重传包 1 承载同一区间并发出，待发计划清空。
    sender
        .on_sent(pkt(APP, 1, 11, 100, vec![range(0, 0, 100)]))
        .unwrap();
    assert_eq!(sender.pending_ranges(APP), vec![]);
    assert_eq!(sender.accounts()[1].in_flight_bytes, 100);

    // 旧包迟到确认：字节完成，重传包仍在途且占用不变。
    sender.on_ack(APP, &ack_single(0), 12).unwrap();
    assert_eq!(
        sender.packet_status(APP, 0).unwrap().status,
        PktStatus::AckedAfterLoss
    );
    assert_eq!(
        sender.packet_status(APP, 1).unwrap().status,
        PktStatus::InFlight
    );
    assert_eq!(sender.completed_ranges(), vec![range(0, 0, 100)]);
    assert_eq!(sender.pending_ranges(APP), vec![]);
    assert_eq!(sender.accounts()[1].in_flight_bytes, 100);
    assert_eq!(sender.accounts()[1].spurious_ack_packets, 1);

    // 重传包随后确认，只扣减一次在途。
    sender.on_ack(APP, &ack_single(1), 13).unwrap();
    assert_eq!(
        sender.packet_status(APP, 1).unwrap().status,
        PktStatus::Acked
    );
    let app = sender.accounts()[1];
    assert_eq!(app.in_flight_bytes, 0);
    assert_eq!(app.acked_bytes, 100);
    assert_eq!(sender.next_timeout(), None);
}

/// 同一区间出现在多个承载包中：字节完成只记一次，待发计划也只有一份。
#[test]
fn same_range_in_multiple_carriers_completes_once() {
    let mut sender = Sender::new(10, 10);
    sender
        .on_sent(pkt(APP, 0, 0, 60, vec![range(0, 0, 60)]))
        .unwrap();
    sender
        .on_sent(pkt(APP, 1, 1, 60, vec![range(0, 0, 60)]))
        .unwrap();
    assert_eq!(sender.accounts()[1].in_flight_bytes, 120);

    // 任一承载包确认，该区间即完成；另一包继续在途。
    sender.on_ack(APP, &ack_single(0), 5).unwrap();
    assert_eq!(sender.completed_ranges(), vec![range(0, 0, 60)]);
    assert_eq!(sender.accounts()[1].in_flight_bytes, 60);

    // 另一承载包确认不改变完成集合，在途正常扣减。
    sender.on_ack(APP, &ack_single(1), 6).unwrap();
    assert_eq!(sender.completed_ranges(), vec![range(0, 0, 60)]);
    assert_eq!(sender.accounts()[1].in_flight_bytes, 0);
    assert_eq!(sender.accounts()[1].acked_bytes, 120);

    // 两个承载包都判丢时，待发计划中同一区间只计费一次。
    let mut sender2 = Sender::new(10, 10);
    sender2
        .on_sent(pkt(APP, 0, 0, 60, vec![range(0, 0, 60)]))
        .unwrap();
    sender2
        .on_sent(pkt(APP, 1, 1, 60, vec![range(0, 0, 60)]))
        .unwrap();
    sender2.on_timeout(11).unwrap();
    let app = sender2.accounts()[1];
    assert_eq!(app.lost_packets, 2);
    assert_eq!(app.in_flight_bytes, 0);
    assert_eq!(sender2.pending_ranges(APP), vec![range(0, 0, 60)]);
    assert_eq!(app.pending_bytes, 60);

    // 仅一个承载包的确认迟到：字节即完成，整段重传计划撤去；另一包仍判丢。
    sender2.on_ack(APP, &ack_single(0), 12).unwrap();
    assert_eq!(sender2.pending_ranges(APP), vec![]);
    assert_eq!(
        sender2.packet_status(APP, 0).unwrap().status,
        PktStatus::AckedAfterLoss
    );
    assert_eq!(
        sender2.packet_status(APP, 1).unwrap().status,
        PktStatus::Lost
    );

    // 另一个承载包的确认随后迟到：同样标记为迟到确认，不产生任何在途变化。
    sender2.on_ack(APP, &ack_single(1), 13).unwrap();
    assert_eq!(
        sender2.packet_status(APP, 1).unwrap().status,
        PktStatus::AckedAfterLoss
    );
    assert_eq!(sender2.accounts()[1].spurious_ack_packets, 2);
}

/// 关闭空间只撤销该空间；still_needed 明确转入存续空间，已完成部分自动剔除。
#[test]
fn discard_space_clears_only_that_space_and_transfers_still_needed() {
    let mut sender = Sender::new(10, 10);
    sender
        .on_sent(pkt(HS, 0, 0, 100, vec![range(7, 0, 20)]))
        .unwrap();
    sender
        .on_sent(pkt(APP, 0, 0, 80, vec![range(0, 0, 80)]))
        .unwrap();
    sender.on_ack(APP, &ack_single(0), 5).unwrap();

    sender
        .discard_space(HS, 6, &[range(7, 0, 40)])
        .unwrap();

    let accounts = sender.accounts();
    assert!(accounts[0].closed);
    assert_eq!(accounts[0].in_flight_bytes, 0);
    assert_eq!(accounts[0].sent_bytes, 0);
    assert_eq!(accounts[0].pending_bytes, 0);
    assert!(!accounts[1].closed);
    assert_eq!(accounts[1].acked_bytes, 80);
    // 调用方声明仍需完成的数据进入存续空间的待发计划。
    assert_eq!(sender.pending_ranges(APP), vec![range(7, 0, 40)]);
    assert_eq!(sender.pending_ranges(HS), vec![]);

    // 关闭空间拒绝新包，确认事件静默忽略，且不再产生计时。
    assert!(matches!(
        sender.on_sent(pkt(HS, 9, 7, 10, vec![])),
        Err(SenderError::SpaceClosed { .. })
    ));
    sender.on_ack(HS, &ack_single(0), 8).unwrap();
    assert_eq!(sender.next_timeout(), None);
}

/// still_needed 中已经完成的字节不转移，只转移未完成部分。
#[test]
fn discard_transfers_only_uncompleted_piece() {
    let mut sender = Sender::new(10, 10);
    sender
        .on_sent(pkt(APP, 0, 0, 100, vec![range(0, 0, 100)]))
        .unwrap();
    sender.on_ack(APP, &ack_single(0), 5).unwrap();

    sender
        .discard_space(APP, 6, &[range(0, 0, 100), range(1, 0, 10)])
        .unwrap();
    assert!(sender.accounts()[1].closed);
    // [0,100) 已完成被剔除；流 1 的 [0,10) 转入握手空间。
    assert_eq!(sender.pending_ranges(HS), vec![range(1, 0, 10)]);
    assert_eq!(sender.all_pending_ranges(), vec![range(1, 0, 10)]);
}

/// 按空间独立的丢失时限；运行中调整时限后下次计时随之变化。
#[test]
fn per_space_loss_times_and_timer_updates() {
    let mut sender = Sender::new(5, 20);
    sender.on_sent(pkt(HS, 0, 0, 10, vec![])).unwrap();
    sender.on_sent(pkt(APP, 0, 0, 10, vec![])).unwrap();
    assert_eq!(sender.next_timeout(), Some(5));

    sender.on_timeout(5).unwrap();
    assert_eq!(sender.accounts()[0].lost_packets, 1);
    assert_eq!(sender.accounts()[1].in_flight_packets, 1);
    assert_eq!(sender.next_timeout(), Some(20));

    sender.on_timeout(19).unwrap();
    assert_eq!(sender.accounts()[1].lost_packets, 0);

    // 动态延长应用空间时限：deadline 从 20 推到 30。
    sender.set_loss_time(APP, 30);
    assert_eq!(sender.next_timeout(), Some(30));
    sender.on_timeout(25).unwrap();
    assert_eq!(sender.accounts()[1].lost_packets, 0);
    sender.on_timeout(30).unwrap();
    assert_eq!(sender.accounts()[1].lost_packets, 1);
    assert_eq!(sender.next_timeout(), None);
}

/// 确认区间自动归并；未知包号忽略；缺口包保留在途。
#[test]
fn ack_ranges_merge_unknown_numbers_ignored() {
    let ack = AckRanges::from_pairs(&[(5, 7), (1, 2), (3, 4)]).unwrap();
    assert_eq!(ack.ranges, vec![(1, 7)]);
    for pn in 1..=7 {
        assert!(ack.ranges.iter().any(|&(a, b)| a <= pn && pn <= b));
    }

    let mut sender = Sender::new(10, 10);
    for pn in 0..4 {
        sender.on_sent(pkt(APP, pn, 0, 10, vec![])).unwrap();
    }
    let ack = AckRanges::from_pairs(&[(0, 0), (2, 3), (99, 99)]).unwrap();
    sender.on_ack(APP, &ack, 5).unwrap();

    let app = sender.accounts()[1];
    assert_eq!(app.acked_packets, 3);
    assert_eq!(app.in_flight_packets, 1);
    assert_eq!(
        sender.packet_status(APP, 1).unwrap().status,
        PktStatus::InFlight
    );
}

/// 输入校验：非法区间、非法 ACK、重复包号、时钟回退。
#[test]
fn validation_and_clock_monotonicity() {
    let mut sender = Sender::new(10, 10);
    assert!(StreamRange::new(0, 5, 4).is_err());
    assert!(matches!(
        AckRanges::from_pairs(&[(3, 2)]),
        Err(SenderError::InvalidAckRange { .. })
    ));

    sender.on_sent(pkt(APP, 0, 10, 10, vec![])).unwrap();
    assert!(matches!(
        sender.on_sent(pkt(APP, 0, 11, 10, vec![])),
        Err(SenderError::DuplicatePacket { .. })
    ));
    assert!(matches!(
        sender.on_timeout(9),
        Err(SenderError::TimeWentBackwards { .. })
    ));
    // 不同空间使用相同包号是合法的。
    sender.on_sent(pkt(HS, 0, 10, 10, vec![])).unwrap();
    assert_eq!(sender.accounts()[0].in_flight_packets, 1);
    assert_eq!(sender.accounts()[1].in_flight_packets, 1);
}

/// 用事件流驱动的完整交错叙事：判丢、迟到确认、空间关闭与重传收尾。
#[test]
fn event_driven_full_interleaving() {
    let mut sender = Sender::new(10, 10);
    let events = vec![
        Event::Sent(pkt(HS, 0, 0, 30, vec![range(5, 0, 10)])),
        Event::Sent(pkt(APP, 0, 0, 100, vec![range(0, 0, 100)])),
        Event::Sent(pkt(APP, 1, 1, 100, vec![range(0, 100, 200)])),
        // 应用空间前半段先确认。
        Event::Ack(APP, ack_single(0), 5),
        // t=10：握手包判丢，其流区间进入握手空间待发计划。
        Event::Timeout(10),
        // t=11：应用 1 号包判丢，后半段进入应用空间待发计划。
        Event::Timeout(11),
        // 握手旧包迟到确认：撤去握手空间待发计划并完成其字节。
        Event::Ack(HS, ack_single(0), 12),
        // 关闭握手空间；still_needed 实际已全部完成，不产生转移。
        Event::DiscardSpace {
            space: HS,
            at_time: 13,
            still_needed: vec![range(5, 0, 10)],
        },
        // 重传应用空间后半段并确认。
        Event::Sent(pkt(APP, 2, 14, 100, vec![range(0, 100, 200)])),
        Event::Ack(APP, ack_single(2), 15),
    ];
    for event in events {
        sender.handle(event).unwrap();
    }

    assert!(sender.accounts()[0].closed);
    assert_eq!(sender.pending_ranges(HS), vec![]);
    assert_eq!(sender.pending_ranges(APP), vec![]);
    assert_eq!(
        sender.completed_ranges(),
        vec![range(0, 0, 200), range(5, 0, 10)]
    );
    assert_eq!(sender.accounts()[1].in_flight_bytes, 0);
    assert_eq!(sender.accounts()[1].acked_bytes, 200);
    assert_eq!(sender.accounts()[1].lost_packets, 1);
    assert_eq!(sender.next_timeout(), None);
}

/// 交错压力：多轮重传丢失、部分确认、迟到确认交织，逐步核对待发字节、
/// 完成字节与在途计数；同一区间反复出现也不重复计费。
#[test]
fn repeated_loss_partial_and_spurious_acks_keep_invariants() {
    let mut sender = Sender::new(10, 10);
    let total = 100u64;

    // 初始包 0 只承载前半段，包 1 承载后半段（两个承载包区间不同）。
    sender
        .on_sent(pkt(APP, 0, 0, 50, vec![range(0, 0, 50)]))
        .unwrap();
    sender
        .on_sent(pkt(APP, 1, 0, 50, vec![range(0, 50, total)]))
        .unwrap();
    assert_eq!(sender.accounts()[1].in_flight_bytes, total);

    // 第一轮判丢：两个区间都进入待发，合并为连续的一段。
    sender.on_timeout(10).unwrap();
    assert_eq!(sender.accounts()[1].in_flight_bytes, 0);
    assert_eq!(sender.pending_ranges(APP), vec![range(0, 0, total)]);

    // 重传拆成两个半包 2、3，待发被实际发出的区间清空。
    sender
        .on_sent(pkt(APP, 2, 11, 50, vec![range(0, 0, 50)]))
        .unwrap();
    sender
        .on_sent(pkt(APP, 3, 11, 50, vec![range(0, 50, 100)]))
        .unwrap();
    assert_eq!(sender.pending_ranges(APP), vec![]);
    assert_eq!(sender.accounts()[1].in_flight_bytes, 100);

    // 前半段先确认，后半段随后判丢。
    sender.on_ack(APP, &ack_single(2), 15).unwrap();
    assert_eq!(sender.completed_ranges(), vec![range(0, 0, 50)]);
    sender.on_timeout(21).unwrap();
    assert_eq!(
        sender.packet_status(APP, 3).unwrap().status,
        PktStatus::Lost
    );
    assert_eq!(sender.pending_ranges(APP), vec![range(0, 50, 100)]);
    assert_eq!(sender.accounts()[1].in_flight_bytes, 0);

    // 重传后半段的包 4 发出；同时最初的包 0 迟到确认（只覆盖前半段）。
    sender
        .on_sent(pkt(APP, 4, 22, 50, vec![range(0, 50, 100)]))
        .unwrap();
    sender.on_ack(APP, &ack_single(0), 23).unwrap();
    // 迟到确认不改变在途：包 4 仍占用 50 字节；完成集合不扩展。
    assert_eq!(sender.accounts()[1].in_flight_bytes, 50);
    assert_eq!(
        sender.packet_status(APP, 0).unwrap().status,
        PktStatus::AckedAfterLoss
    );
    assert_eq!(sender.completed_ranges(), vec![range(0, 0, 50)]);

    // 包 4 也判丢（第二轮），待发仍是同一段且只计费一次。
    sender.on_timeout(32).unwrap();
    assert_eq!(sender.accounts()[1].in_flight_bytes, 0);
    assert_eq!(sender.pending_ranges(APP), vec![range(0, 50, 100)]);
    assert_eq!(sender.accounts()[1].pending_bytes, 50);

    // 承载后半段的包 1 迟到确认：撤去尚未发送的重传计划，在途保持为 0。
    sender.on_ack(APP, &ack_single(1), 33).unwrap();
    assert_eq!(sender.pending_ranges(APP), vec![]);
    assert_eq!(sender.accounts()[1].in_flight_bytes, 0);
    assert_eq!(sender.completed_ranges(), vec![range(0, 0, 100)]);

    // 全流程在途字节从未为负；发送/确认/判丢/迟到确认的累计口径一致。
    let app = sender.accounts()[1];
    assert_eq!(app.sent_packets, 5);
    assert_eq!(app.sent_bytes, 250);
    assert_eq!(app.acked_packets, 1);
    assert_eq!(app.acked_bytes, 50);
    assert_eq!(app.lost_packets, 4);
    assert_eq!(app.spurious_ack_packets, 2);
    assert_eq!(app.in_flight_packets, 0);
    assert_eq!(sender.next_timeout(), None);
}
