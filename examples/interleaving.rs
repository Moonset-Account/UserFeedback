//! 交错场景的可运行示例：旧包判丢后确认迟到。
//!
//! 运行：`cargo run --offline --example interleaving`
//! 所有输出均由状态机实时计算，没有预置结果。

use retransmit_state::{
    AckRanges, PktStatus, Sender, SentPacket, SpaceId, StreamRange,
};

fn main() {
    let app = SpaceId::AppData;
    let loss_time = 10u64;
    let mut sender = Sender::new(loss_time, loss_time);

    let stream_range = StreamRange::new(0, 0, 100).unwrap();
    sender
        .on_sent(SentPacket {
            space: app,
            packet_number: 0,
            time_sent: 0,
            size: 100,
            frames: vec![stream_range],
        })
        .expect("send packet 0");

    println!("发送后        : 下次计时={:?}, 在途字节={}",
        sender.next_timeout(),
        sender.accounts()[1].in_flight_bytes);

    // 时限到达，0 号包判丢，字节进入重传计划。
    sender.on_timeout(10).expect("timeout");
    println!("判丢后        : 状态={:?}, 待发区间={:?}, 在途字节={}",
        sender.packet_status(app, 0).unwrap().status,
        sender.pending_ranges(app),
        sender.accounts()[1].in_flight_bytes);

    // 重传还没发出，旧包的确认迟到。
    let ack = AckRanges::from_pairs(&[(0, 0)]).unwrap();
    sender.on_ack(app, &ack, 12).expect("late ack");
    println!("迟到确认后    : 状态={:?}, 待发区间={:?}, 迟到确认包数={}",
        sender.packet_status(app, 0).unwrap().status,
        sender.pending_ranges(app),
        sender.accounts()[1].spurious_ack_packets);
    println!("已完成字节    : {:?}", sender.completed_ranges());
    assert_eq!(
        sender.packet_status(app, 0).unwrap().status,
        PktStatus::AckedAfterLoss
    );
    assert!(sender.pending_ranges(app).is_empty());
}
