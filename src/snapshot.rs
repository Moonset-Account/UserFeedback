//! 快照的纯文本编解码。
//!
//! 快照只保存权威状态（前沿、裁剪区间、B 侧原生消息的在途/确认集合）；
//! 来源镜像缓冲不持久化，重开后通过 B 日志重放回填，重放事件会被
//! [`crate::RecoveryManager::ingest`] 依据前沿判定为
//! [`crate::IngestOutcome::Replayed`] 而不会重复投递。
//!
//! 文本格式（每条记录一行，空格分隔）：
//!
//! ```text
//! MIRROR-RECOVERY-SNAPSHOT-V1
//! P <partition> <frontier>
//! T <partition> <from> <to>
//! M <partition> <b_offset> <source_offset> <hex-id> <hex-payload>  # 在途镜像
//! N <partition> <b_offset> <hex-payload>   # 已接收未投递的 B 侧原生消息
//! I <partition> <b_offset> <hex-payload>   # 已投递未确认的 B 侧原生消息
//! K <partition> <b_offset>                 # 已确认的 B 侧原生消息位点
//! END
//! ```
//!
//! 身份与负载使用小写十六进制编码（空字节串编码为空串），保证任意字节
//! （含空格、换行）都可安全落盘。

use std::collections::{BTreeMap, BTreeSet};

use crate::{
    Offset, Partition, PartitionSnapshot, PendingMirrored, RecoveryError, RecoverySnapshot,
    TruncatedRange,
};

const HEADER: &str = "MIRROR-RECOVERY-SNAPSHOT-V1";
const TERMINATOR: &str = "END";

fn to_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

fn from_hex(text: &str) -> Result<Vec<u8>, RecoveryError> {
    let bytes = text.as_bytes();
    if bytes.len() % 2 != 0 {
        return Err(RecoveryError::Snapshot(format!(
            "hex payload has odd length: {text}"
        )));
    }
    let mut out = Vec::with_capacity(bytes.len() / 2);
    for pair in bytes.chunks(2) {
        let nibble = |c: u8| match c {
            b'0'..=b'9' => Ok(c - b'0'),
            b'a'..=b'f' => Ok(c - b'a' + 10),
            _ => Err(RecoveryError::Snapshot(format!(
                "invalid hex digit in payload: {text}"
            ))),
        };
        let high = nibble(pair[0])?;
        let low = nibble(pair[1])?;
        out.push((high << 4) | low);
    }
    Ok(out)
}

/// 十六进制 -> UTF-8 字符串，用于解码稳定身份。
fn from_hex_string(text: &str) -> Result<String, RecoveryError> {
    let bytes = from_hex(text)?;
    String::from_utf8(bytes)
        .map_err(|e| RecoveryError::Snapshot(format!("id is not valid UTF-8: {e}")))
}

/// 将快照编码为可落盘的文本。
pub fn encode(snapshot: &RecoverySnapshot) -> String {
    let mut out = String::new();
    out.push_str(HEADER);
    out.push('\n');

    for snap in snapshot.partitions.values() {
        out.push_str(&format!("P {} {}\n", snap.partition, snap.frontier));
        for range in &snap.truncations {
            out.push_str(&format!(
                "T {} {} {}\n",
                snap.partition, range.from, range.to
            ));
        }
        for pending in &snap.mirrored_pending {
            out.push_str(&format!(
                "M {} {} {} {} {}\n",
                snap.partition,
                pending.b_offset,
                pending.source_offset,
                to_hex(pending.id.as_bytes()),
                to_hex(&pending.payload)
            ));
        }
        for (b_offset, payload) in &snap.native_pending {
            out.push_str(&format!(
                "N {} {} {}\n",
                snap.partition,
                b_offset,
                to_hex(payload)
            ));
        }
        for (b_offset, payload) in &snap.native_inflight {
            out.push_str(&format!(
                "I {} {} {}\n",
                snap.partition,
                b_offset,
                to_hex(payload)
            ));
        }
        for b_offset in &snap.native_acked {
            out.push_str(&format!("K {} {}\n", snap.partition, b_offset));
        }
    }

    out.push_str(TERMINATOR);
    out.push('\n');
    out
}

/// 解析 [`encode`] 产生的文本；任何格式偏差都返回
/// [`RecoveryError::Snapshot`]，绝不静默吞掉。
pub fn decode(text: &str) -> Result<RecoverySnapshot, RecoveryError> {
    let mut lines = text.lines();
    match lines.next() {
        Some(HEADER) => {}
        Some(other) => {
            return Err(RecoveryError::Snapshot(format!(
                "bad snapshot header: {other}"
            )))
        }
        None => return Err(RecoveryError::Snapshot("empty snapshot".to_string())),
    }

    // 先收集原始记录，最后组装，确保同一分区只出现一条 P 记录。
    struct RawPartition {
        frontier: Offset,
        truncations: Vec<TruncatedRange>,
        mirrored_pending: Vec<PendingMirrored>,
        native_pending: BTreeMap<Offset, Vec<u8>>,
        native_inflight: BTreeMap<Offset, Vec<u8>>,
        native_acked: BTreeSet<Offset>,
    }

    let mut raw: BTreeMap<Partition, RawPartition> = BTreeMap::new();
    let mut saw_terminator = false;
    let mut line_no = 1usize;

    while let Some(line) = lines.next() {
        line_no += 1;
        if line == TERMINATOR {
            saw_terminator = true;
            // END 之后不允许还有内容。
            if lines.next().is_some() {
                return Err(RecoveryError::Snapshot(
                    "trailing records after END".to_string(),
                ));
            }
            break;
        }
        if line.is_empty() {
            return Err(RecoveryError::Snapshot(format!(
                "blank line at line {line_no}"
            )));
        }

        let mut parts = line.split(' ');
        let tag = parts
            .next()
            .ok_or_else(|| RecoveryError::Snapshot(format!("line {line_no}: missing tag")))?;

        let parse = |maybe: Option<&str>, what: &str| -> Result<u64, RecoveryError> {
            maybe
                .ok_or_else(|| {
                    RecoveryError::Snapshot(format!("line {line_no}: missing {what}"))
                })?
                .parse::<u64>()
                .map_err(|e| RecoveryError::Snapshot(format!("line {line_no}: bad {what}: {e}")))
        };

        match tag {
            "P" => {
                let partition = parse(parts.next(), "partition")? as Partition;
                let frontier = parse(parts.next(), "frontier")?;
                if parts.next().is_some() {
                    return Err(RecoveryError::Snapshot(format!(
                        "line {line_no}: extra fields in P record"
                    )));
                }
                if raw.contains_key(&partition) {
                    return Err(RecoveryError::Snapshot(format!(
                        "line {line_no}: duplicate P record for partition {partition}"
                    )));
                }
                raw.insert(
                    partition,
                    RawPartition {
                        frontier,
                        truncations: Vec::new(),
                        mirrored_pending: Vec::new(),
                        native_pending: BTreeMap::new(),
                        native_inflight: BTreeMap::new(),
                        native_acked: BTreeSet::new(),
                    },
                );
            }
            "T" => {
                let partition = parse(parts.next(), "partition")? as Partition;
                let from = parse(parts.next(), "truncation from")?;
                let to = parse(parts.next(), "truncation to")?;
                if parts.next().is_some() {
                    return Err(RecoveryError::Snapshot(format!(
                        "line {line_no}: extra fields in T record"
                    )));
                }
                let state = raw.get_mut(&partition).ok_or_else(|| {
                    RecoveryError::Snapshot(format!(
                        "line {line_no}: T record for unknown partition {partition}"
                    ))
                })?;
                state.truncations.push(TruncatedRange::new(from, to)?);
            }
            "M" => {
                let partition = parse(parts.next(), "partition")? as Partition;
                let b_offset = parse(parts.next(), "b_offset")?;
                let source_offset = parse(parts.next(), "source_offset")?;
                let id_hex = parts.next().ok_or_else(|| {
                    RecoveryError::Snapshot(format!("line {line_no}: missing id"))
                })?;
                let payload_hex = parts.next().ok_or_else(|| {
                    RecoveryError::Snapshot(format!("line {line_no}: missing payload"))
                })?;
                if parts.next().is_some() {
                    return Err(RecoveryError::Snapshot(format!(
                        "line {line_no}: extra fields in M record"
                    )));
                }
                let id = from_hex_string(id_hex)?;
                let payload = from_hex(payload_hex)?;
                let state = raw.get_mut(&partition).ok_or_else(|| {
                    RecoveryError::Snapshot(format!(
                        "line {line_no}: M record for unknown partition {partition}"
                    ))
                })?;
                if state
                    .mirrored_pending
                    .iter()
                    .any(|m| m.source_offset == source_offset)
                {
                    return Err(RecoveryError::Snapshot(format!(
                        "line {line_no}: duplicate pending source offset {source_offset}"
                    )));
                }
                state.mirrored_pending.push(PendingMirrored {
                    b_offset,
                    source_offset,
                    id,
                    payload,
                });
            }
            "N" | "I" => {
                let partition = parse(parts.next(), "partition")? as Partition;
                let b_offset = parse(parts.next(), "b_offset")?;
                let payload_hex = parts.next().ok_or_else(|| {
                    RecoveryError::Snapshot(format!("line {line_no}: missing payload"))
                })?;
                if parts.next().is_some() {
                    return Err(RecoveryError::Snapshot(format!(
                        "line {line_no}: extra fields in {tag} record"
                    )));
                }
                let payload = from_hex(payload_hex)?;
                let state = raw.get_mut(&partition).ok_or_else(|| {
                    RecoveryError::Snapshot(format!(
                        "line {line_no}: {tag} record for unknown partition {partition}"
                    ))
                })?;
                // 同一个 B 位点不允许同时出现在 pending/inflight 两个集合。
                if state.native_pending.contains_key(&b_offset)
                    || state.native_inflight.contains_key(&b_offset)
                {
                    return Err(RecoveryError::Snapshot(format!(
                        "line {line_no}: duplicate native b_offset {b_offset}"
                    )));
                }
                let target = if tag == "N" {
                    &mut state.native_pending
                } else {
                    &mut state.native_inflight
                };
                target.insert(b_offset, payload);
            }
            "K" => {
                let partition = parse(parts.next(), "partition")? as Partition;
                let b_offset = parse(parts.next(), "b_offset")?;
                if parts.next().is_some() {
                    return Err(RecoveryError::Snapshot(format!(
                        "line {line_no}: extra fields in K record"
                    )));
                }
                let state = raw.get_mut(&partition).ok_or_else(|| {
                    RecoveryError::Snapshot(format!(
                        "line {line_no}: K record for unknown partition {partition}"
                    ))
                })?;
                if !state.native_acked.insert(b_offset) {
                    return Err(RecoveryError::Snapshot(format!(
                        "line {line_no}: duplicate native ack b_offset {b_offset}"
                    )));
                }
            }
            other => {
                return Err(RecoveryError::Snapshot(format!(
                    "line {line_no}: unknown record tag {other}"
                )));
            }
        }
    }

    if !saw_terminator {
        return Err(RecoveryError::Snapshot(
            "snapshot missing END terminator".to_string(),
        ));
    }

    let mut partitions = BTreeMap::new();
    for (partition, mut state) in raw {
        // 重新走合并逻辑，保证恢复后的区间集合规范化。
        let mut truncations = Vec::new();
        state.truncations.sort();
        for range in state.truncations {
            if let Some(last) = truncations.last_mut().map(|r: &mut TruncatedRange| r) {
                let overlap_or_adjacent =
                    last.to.saturating_add(1) >= range.from && range.to.saturating_add(1) >= last.from;
                if overlap_or_adjacent {
                    last.from = last.from.min(range.from);
                    last.to = last.to.max(range.to);
                    continue;
                }
            }
            truncations.push(range);
        }
        // 在途镜像按来源位点排序，保证快照文本确定性。
        state.mirrored_pending.sort_by_key(|m| m.source_offset);
        partitions.insert(
            partition,
            PartitionSnapshot {
                partition,
                frontier: state.frontier,
                truncations,
                mirrored_pending: state.mirrored_pending,
                native_pending: state.native_pending,
                native_inflight: state.native_inflight,
                native_acked: state.native_acked,
            },
        );
    }

    Ok(RecoverySnapshot { partitions })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex_roundtrip_handles_all_bytes() {
        for b in 0u8..=255 {
            let data = vec![b, b.wrapping_add(1), 0x00, 0xff];
            assert_eq!(from_hex(&to_hex(&data)).unwrap(), data);
        }
        assert_eq!(from_hex("").unwrap(), Vec::<u8>::new());
    }

    #[test]
    fn decode_rejects_malformed_inputs() {
        assert!(decode("").is_err());
        assert!(decode("WRONG\nEND\n").is_err());
        assert!(decode(&format!("{HEADER}\n")).is_err());
        assert!(decode(&format!("{HEADER}\nEND\ntrailing\n")).is_err());
        assert!(decode(&format!("{HEADER}\nP 0\nEND\n")).is_err());
        assert!(decode(&format!("{HEADER}\nP 0 1\nP 0 2\nEND\n")).is_err());
        assert!(decode(&format!("{HEADER}\nT 0 1 2\nEND\n")).is_err());
        assert!(decode(&format!("{HEADER}\nP 0 0\nN 0 3 abc\nEND\n")).is_err());
        assert!(decode(&format!("{HEADER}\nP 0 0\nZ 0\nEND\n")).is_err());
        assert!(decode(&format!("{HEADER}\nP 0 0\nT 0 9 1\nEND\n")).is_err());
    }
}
