//! 确认点：持久化各逻辑流的取值进度并支持恢复。
//!
//! 确认点记录的内容只有三类纯逻辑信息：流坐标（[`StreamKey`]）、下一个逻辑
//! 位置、每个已接受位置的拒绝候选数。物理 rank、分段、回包顺序一律不入盘。
//! 编解码是确定性的小端二进制格式，带魔数、版本号与自洽性校验，不依赖任何
//! 第三方依赖。

use std::collections::BTreeMap;

use crate::cursor::StreamCursor;
use crate::rng::StreamKey;

/// 确认点格式魔数：`"RNGF"` 的 ASCII。
pub const MAGIC: [u8; 4] = *b"RNGF";
/// 确认点格式版本（头部统一按 u64 小端编码）。
pub const FORMAT_VERSION: u64 = 1;

/// 确认点解析 / 校验错误。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CheckpointError {
    /// 字节数不足，或尾部有多余字节。
    BadLength { expected: usize, actual: usize },
    /// 魔数不匹配。
    BadMagic([u8; 4]),
    /// 格式版本不受支持。
    UnsupportedVersion(u64),
    /// 流记录数量字段非法。
    BadStreamCount,
    /// 同一 `stream_id` 出现重复记录。
    DuplicateStream(u64),
    /// 进度不自洽：拒绝记录条数与 `next_position` 不符。
    InconsistentProgress { stream_id: u64, next_position: u64, records: u64 },
}

impl std::fmt::Display for CheckpointError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CheckpointError::BadLength { expected, actual } => write!(
                f,
                "确认点长度不合法：期望 {expected} 字节，实际 {actual} 字节"
            ),
            CheckpointError::BadMagic(m) => write!(f, "确认点魔数不匹配：{m:02X?}"),
            CheckpointError::UnsupportedVersion(v) => {
                write!(f, "不支持的确认点格式版本：{v}")
            }
            CheckpointError::BadStreamCount => write!(f, "确认点流记录数量字段非法"),
            CheckpointError::DuplicateStream(id) => {
                write!(f, "确认点中存在重复的流记录：stream_id={id}")
            }
            CheckpointError::InconsistentProgress {
                stream_id,
                next_position,
                records,
            } => write!(
                f,
                "流 {stream_id} 进度不自洽：next_position={next_position}，拒绝记录 {records} 条"
            ),
        }
    }
}

impl std::error::Error for CheckpointError {}

/// 一条流的持久化进度。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StreamRecord {
    /// 完整流坐标。
    pub key: StreamKey,
    /// 下一个待接受的逻辑位置。
    pub next_position: u64,
    /// 每个已接受位置在接受前拒绝的候选数。
    pub rejections: Vec<u64>,
}

impl StreamRecord {
    /// 在该记录上重建内存游标（从确认点恢复取值进度）。
    #[must_use]
    pub fn restore_cursor(&self) -> StreamCursor {
        StreamCursor::restore(self.key, self.next_position, self.rejections.clone())
    }
}

/// 确认点：按 `stream_id` 索引的若干流进度。
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Checkpoint {
    streams: BTreeMap<u64, StreamRecord>,
}

impl Checkpoint {
    /// 空确认点。
    #[must_use]
    pub fn new() -> Self {
        Checkpoint {
            streams: BTreeMap::new(),
        }
    }

    /// 已记录的流数量。
    #[must_use]
    pub fn len(&self) -> usize {
        self.streams.len()
    }

    /// 是否没有任何流记录。
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.streams.is_empty()
    }

    /// 用游标当前进度写入（或覆盖）一条流记录。
    pub fn record_cursor(&mut self, cursor: &StreamCursor) {
        let key = cursor.key();
        let (next_position, rejections) = cursor.progress();
        self.streams.insert(
            key.stream_id(),
            StreamRecord {
                key,
                next_position,
                rejections,
            },
        );
    }

    /// 直接写入一条流的进度，`rejections.len()` 必须等于 `next_position`。
    ///
    /// # Panics
    /// 进度不自洽时 panic。
    pub fn record_progress(
        &mut self,
        key: StreamKey,
        next_position: u64,
        rejections: Vec<u64>,
    ) {
        assert_eq!(
            rejections.len(),
            next_position as usize,
            "进度记录不自洽：next_position={next_position}，拒绝记录 {} 条",
            rejections.len()
        );
        self.streams.insert(
            key.stream_id(),
            StreamRecord {
                key,
                next_position,
                rejections,
            },
        );
    }

    /// 按 `stream_id` 取流记录。
    #[must_use]
    pub fn get(&self, stream_id: u64) -> Option<&StreamRecord> {
        self.streams.get(&stream_id)
    }

    /// 按流坐标取流记录。
    #[must_use]
    pub fn get_key(&self, key: &StreamKey) -> Option<&StreamRecord> {
        self.streams.get(&key.stream_id())
    }

    /// 迭代全部流记录（按 `stream_id` 排序，保证编码确定性）。
    pub fn iter(&self) -> impl Iterator<Item = (&u64, &StreamRecord)> {
        self.streams.iter()
    }

    /// 编码为确定性小端二进制。
    #[must_use]
    pub fn encode_to_vec(&self) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(&MAGIC);
        out.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
        out.extend_from_slice(&(self.streams.len() as u64).to_le_bytes());
        for (_, record) in &self.streams {
            let k = record.key;
            out.extend_from_slice(&k.seed.to_le_bytes());
            out.extend_from_slice(&k.global_step.to_le_bytes());
            out.extend_from_slice(&k.sample_id.to_le_bytes());
            out.extend_from_slice(&k.operator_path.to_le_bytes());
            out.extend_from_slice(&k.logical_call.to_le_bytes());
            out.extend_from_slice(&k.purpose.0.to_le_bytes());
            out.extend_from_slice(&record.next_position.to_le_bytes());
            out.extend_from_slice(&(record.rejections.len() as u64).to_le_bytes());
            for r in &record.rejections {
                out.extend_from_slice(&r.to_le_bytes());
            }
        }
        out
    }

    /// 从字节解码并做完整校验。
    ///
    /// # Errors
    /// 魔数 / 版本 / 长度 / 重复流 / 进度自洽性任一不满足时返回
    /// [`CheckpointError`]。
    pub fn decode(bytes: &[u8]) -> Result<Self, CheckpointError> {
        let mut cursor = ByteCursor::new(bytes);
        let magic = cursor.take_array::<4>()?;
        if magic != MAGIC {
            return Err(CheckpointError::BadMagic(magic));
        }
        let version = cursor.take_u64()?;
        if version != FORMAT_VERSION {
            return Err(CheckpointError::UnsupportedVersion(version));
        }
        let count = cursor.take_u64()?;
        let mut streams = BTreeMap::new();
        for _ in 0..count {
            let seed = cursor.take_u64()?;
            let global_step = cursor.take_u64()?;
            let sample_id = cursor.take_u64()?;
            let operator_path = cursor.take_u64()?;
            let logical_call = cursor.take_u64()?;
            let purpose = cursor.take_u64()?;
            let next_position = cursor.take_u64()?;
            let rejection_count = cursor.take_u64()?;
            let mut rejections = Vec::with_capacity(rejection_count as usize);
            for _ in 0..rejection_count {
                rejections.push(cursor.take_u64()?);
            }
            if rejections.len() as u64 != next_position {
                return Err(CheckpointError::InconsistentProgress {
                    stream_id: 0,
                    next_position,
                    records: rejections.len() as u64,
                });
            }
            let key = StreamKey::with_path_hash(
                seed,
                global_step,
                sample_id,
                operator_path,
                logical_call,
                crate::rng::Purpose(purpose),
            );
            let id = key.stream_id();
            if streams
                .insert(
                    id,
                    StreamRecord {
                        key,
                        next_position,
                        rejections,
                    },
                )
                .is_some()
            {
                return Err(CheckpointError::DuplicateStream(id));
            }
        }
        if cursor.remaining() != 0 {
            return Err(CheckpointError::BadLength {
                expected: bytes.len() - cursor.remaining(),
                actual: bytes.len(),
            });
        }
        Ok(Checkpoint { streams })
    }
}

/// 极简小端字节读取器，集中处理长度校验。
struct ByteCursor<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> ByteCursor<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        ByteCursor { bytes, pos: 0 }
    }

    fn remaining(&self) -> usize {
        self.bytes.len() - self.pos
    }

    fn take_array<const N: usize>(&mut self) -> Result<[u8; N], CheckpointError> {
        if self.remaining() < N {
            return Err(CheckpointError::BadLength {
                expected: self.pos + N,
                actual: self.bytes.len(),
            });
        }
        let mut buf = [0u8; N];
        buf.copy_from_slice(&self.bytes[self.pos..self.pos + N]);
        self.pos += N;
        Ok(buf)
    }

    fn take_u64(&mut self) -> Result<u64, CheckpointError> {
        Ok(u64::from_le_bytes(self.take_array::<8>()?))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rng::Purpose;

    fn sample_key(purpose: Purpose) -> StreamKey {
        StreamKey::new(9, 100, 55, "model/block7/dropout", 3, purpose)
    }

    #[test]
    fn round_trip_preserves_progress_and_is_deterministic() {
        let mut cp = Checkpoint::new();
        let mut dropout = StreamCursor::new(sample_key(Purpose::DROPOUT));
        dropout.reject();
        dropout.accept();
        dropout.accept();
        cp.record_cursor(&dropout);
        cp.record_progress(sample_key(Purpose::ROUTING), 1, vec![0]);

        let encoded = cp.encode_to_vec();
        // 确定性编码：同一状态两次编码字节完全相同。
        assert_eq!(encoded, cp.encode_to_vec());
        let decoded = Checkpoint::decode(&encoded).expect("合法确认点必须解码成功");
        assert_eq!(decoded, cp);

        let record = decoded
            .get_key(&sample_key(Purpose::DROPOUT))
            .expect("dropout 流必须存在");
        assert_eq!(record.next_position, 2);
        assert_eq!(record.rejections, vec![1, 0]);
        // 恢复出的游标与原游标在同一位置继续抽样，取值一致。
        let mut restored = record.restore_cursor();
        let mut original = dropout;
        assert_eq!(restored.accept(), original.accept());
    }

    #[test]
    fn decode_rejects_truncated_and_garbage() {
        let mut cp = Checkpoint::new();
        cp.record_progress(sample_key(Purpose::DROPOUT), 1, vec![0]);
        let good = cp.encode_to_vec();

        // 截断。
        assert!(matches!(
            Checkpoint::decode(&good[..good.len() - 1]),
            Err(CheckpointError::BadLength { .. })
        ));
        // 魔数错误。
        let mut bad_magic = good.clone();
        bad_magic[0] ^= 0xFF;
        assert!(matches!(
            Checkpoint::decode(&bad_magic),
            Err(CheckpointError::BadMagic(_))
        ));
        // 版本错误。
        let mut bad_version = good.clone();
        bad_version[4..12].copy_from_slice(&999u64.to_le_bytes());
        assert!(matches!(
            Checkpoint::decode(&bad_version),
            Err(CheckpointError::UnsupportedVersion(999))
        ));
        // 尾部多余字节。
        let mut trailing = good.clone();
        trailing.push(0);
        assert!(matches!(
            Checkpoint::decode(&trailing),
            Err(CheckpointError::BadLength { .. })
        ));
        // 空输入。
        assert!(matches!(
            Checkpoint::decode(&[]),
            Err(CheckpointError::BadLength { .. })
        ));
    }

    #[test]
    fn decode_rejects_inconsistent_progress() {
        // 手工构造 next_position=2 但只有 1 条拒绝记录的非法确认点。
        let mut out = Vec::new();
        out.extend_from_slice(&MAGIC);
        out.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
        out.extend_from_slice(&1u64.to_le_bytes());
        let k = sample_key(Purpose::DROPOUT);
        for field in [
            k.seed,
            k.global_step,
            k.sample_id,
            k.operator_path,
            k.logical_call,
            k.purpose.0,
        ] {
            out.extend_from_slice(&field.to_le_bytes());
        }
        out.extend_from_slice(&2u64.to_le_bytes()); // next_position
        out.extend_from_slice(&1u64.to_le_bytes()); // rejection_count
        out.extend_from_slice(&0u64.to_le_bytes());
        assert!(matches!(
            Checkpoint::decode(&out),
            Err(CheckpointError::InconsistentProgress { .. })
        ));
    }

    #[test]
    fn empty_checkpoint_round_trips() {
        let cp = Checkpoint::new();
        assert!(cp.is_empty());
        let bytes = cp.encode_to_vec();
        let decoded = Checkpoint::decode(&bytes).expect("空确认点应可解码");
        assert!(decoded.is_empty());
        assert_eq!(decoded, cp);
    }
}
