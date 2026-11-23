//! 测试辅助：构造镜像/原生事件的简写。
#![allow(dead_code)]

use mirror_recovery::{MessageId, MirrorEvent, Offset};

/// 构造一条 A 消息镜像事件，负载编码来源位点便于断言。
pub fn mirrored(b_offset: Offset, source_offset: Offset, id: &str) -> MirrorEvent {
    MirrorEvent::Mirrored {
        b_offset,
        source_offset,
        id: id.to_string() as MessageId,
        payload: format!("msg-{source_offset}").into_bytes(),
    }
}

/// 构造一条 B 侧原生事件。
pub fn native(b_offset: Offset, label: &str) -> MirrorEvent {
    MirrorEvent::Native {
        b_offset,
        payload: format!("native-{label}").into_bytes(),
    }
}

/// 从投递报告中抽出（来源位点，B 位点）序列，只含镜像消息。
pub fn mirrored_offsets(
    report: &mirror_recovery::DrainReport,
) -> Vec<(Offset, Offset)> {
    report
        .delivered
        .iter()
        .filter(|d| d.kind == mirror_recovery::DeliveryKind::Mirrored)
        .map(|d| (d.source_offset.expect("mirrored delivery has source offset"), d.b_offset))
        .collect()
}

/// 从投递报告中抽出原生消息的 B 位点序列。
pub fn native_offsets(report: &mirror_recovery::DrainReport) -> Vec<Offset> {
    report
        .delivered
        .iter()
        .filter(|d| d.kind == mirror_recovery::DeliveryKind::Native)
        .map(|d| d.b_offset)
        .collect()
}
