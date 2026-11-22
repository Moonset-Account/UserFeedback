//! 可恢复的预写日志（write-ahead journal）。
//!
//! 日志由若干**事务**组成，每个事务以 `Begin` 记录开头、`Commit` 记录结尾，
//! 中间是任意数量的变更记录。日志只追加，从不就地修改。
//!
//! 崩溃注入点：
//! - [`CrashPoint::BeforeCommit`]：事务的变更记录已经全部追加，但 `Commit`
//!   标记尚未写入时“断电”。恢复时该事务被整体丢弃。
//! - [`CrashPoint::AfterCommit`]：`Commit` 标记已追加后“断电”。恢复时事务
//!   完整生效。
//!
//! “磁盘”由一个 `Vec<u8>` 模拟，所有编码均为定长头 + 长度前缀负载，
//! 不依赖任何第三方库。

use crate::fs::NodeKind;
use crate::FsError;

/// 单条日志记录。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Record {
    /// 事务开始（负载：单调递增的事务序号）。
    Begin(u64),
    /// 事务提交（负载：事务序号）。
    Commit(u64),
    /// 分配一个新 inode（inode 号，类型，初始 nlink）。
    InodeAlloc(u64, NodeKind, u64),
    /// 释放 inode，其号可被后续分配复用。
    InodeFree(u64),
    /// 目录中新增目录项（父目录 inode，名称，目标 inode）。
    EntryAdd(u64, String, u64),
    /// 目录中删除目录项（父目录 inode，名称）。
    EntryRemove(u64, String),
    /// 原子交换两个目录项引用的 inode（两个父目录 inode 与名称）。
    EntrySwap(u64, String, u64, String),
    /// inode 硬链接计数增量（可负）。
    Nlink(u64, i64),
    /// 覆盖普通文件的内容。
    SetData(u64, Vec<u8>),
}

const TAG_BEGIN: u8 = 1;
const TAG_COMMIT: u8 = 2;
const TAG_INODE_ALLOC: u8 = 3;
const TAG_INODE_FREE: u8 = 4;
const TAG_ENTRY_ADD: u8 = 5;
const TAG_ENTRY_REMOVE: u8 = 6;
const TAG_ENTRY_SWAP: u8 = 7;
const TAG_NLINK: u8 = 8;
const TAG_SET_DATA: u8 = 9;

const KIND_FILE: u8 = 1;
const KIND_DIR: u8 = 2;

/// 记录在介质尾部被截断（只读到一半字节）。与“真损坏”区分：
/// 截断只可能发生在最后一个未提交事务中，恢复时静默丢弃即可。
struct Torn;

/// 单记录解码的两种失败：半截字节（可忽略的尾部）或真损坏。
enum DecodeFail {
    Torn,
    Corrupt(String),
}

impl From<Torn> for DecodeFail {
    fn from(Torn: Torn) -> Self {
        DecodeFail::Torn
    }
}

fn put_u64(buf: &mut Vec<u8>, v: u64) {
    buf.extend_from_slice(&v.to_le_bytes());
}

fn put_i64(buf: &mut Vec<u8>, v: i64) {
    buf.extend_from_slice(&v.to_le_bytes());
}

fn put_str(buf: &mut Vec<u8>, s: &str) {
    put_u64(buf, s.len() as u64);
    buf.extend_from_slice(s.as_bytes());
}

fn put_bytes(buf: &mut Vec<u8>, b: &[u8]) {
    put_u64(buf, b.len() as u64);
    buf.extend_from_slice(b);
}

fn read_u64_opt(buf: &[u8], pos: &mut usize) -> Result<u64, Torn> {
    let end = pos.checked_add(8).ok_or(Torn)?;
    let slice = buf.get(*pos..end).ok_or(Torn)?;
    let mut bytes = [0u8; 8];
    bytes.copy_from_slice(slice);
    *pos = end;
    Ok(u64::from_le_bytes(bytes))
}

fn read_i64_opt(buf: &[u8], pos: &mut usize) -> Result<i64, Torn> {
    let end = pos.checked_add(8).ok_or(Torn)?;
    let slice = buf.get(*pos..end).ok_or(Torn)?;
    let mut bytes = [0u8; 8];
    bytes.copy_from_slice(slice);
    *pos = end;
    Ok(i64::from_le_bytes(bytes))
}

fn read_str_opt(buf: &[u8], pos: &mut usize) -> Result<String, Torn> {
    let len = read_u64_opt(buf, pos)? as usize;
    let end = pos.checked_add(len).ok_or(Torn)?;
    let slice = buf.get(*pos..end).ok_or(Torn)?;
    let s = std::str::from_utf8(slice).map_err(|_| Torn)?.to_string();
    *pos = end;
    Ok(s)
}

fn read_bytes_opt(buf: &[u8], pos: &mut usize) -> Result<Vec<u8>, Torn> {
    let len = read_u64_opt(buf, pos)? as usize;
    let end = pos.checked_add(len).ok_or(Torn)?;
    let slice = buf.get(*pos..end).ok_or(Torn)?;
    let out = slice.to_vec();
    *pos = end;
    Ok(out)
}

impl Record {
    fn encode(&self, buf: &mut Vec<u8>) {
        match self {
            Record::Begin(tx) => {
                buf.push(TAG_BEGIN);
                put_u64(buf, *tx);
            }
            Record::Commit(tx) => {
                buf.push(TAG_COMMIT);
                put_u64(buf, *tx);
            }
            Record::InodeAlloc(id, kind, nlink) => {
                buf.push(TAG_INODE_ALLOC);
                put_u64(buf, *id);
                buf.push(match kind {
                    NodeKind::File => KIND_FILE,
                    NodeKind::Directory => KIND_DIR,
                });
                put_u64(buf, *nlink);
            }
            Record::InodeFree(id) => {
                buf.push(TAG_INODE_FREE);
                put_u64(buf, *id);
            }
            Record::EntryAdd(parent, name, child) => {
                buf.push(TAG_ENTRY_ADD);
                put_u64(buf, *parent);
                put_str(buf, name);
                put_u64(buf, *child);
            }
            Record::EntryRemove(parent, name) => {
                buf.push(TAG_ENTRY_REMOVE);
                put_u64(buf, *parent);
                put_str(buf, name);
            }
            Record::EntrySwap(p1, n1, p2, n2) => {
                buf.push(TAG_ENTRY_SWAP);
                put_u64(buf, *p1);
                put_str(buf, n1);
                put_u64(buf, *p2);
                put_str(buf, n2);
            }
            Record::Nlink(id, delta) => {
                buf.push(TAG_NLINK);
                put_u64(buf, *id);
                put_i64(buf, *delta);
            }
            Record::SetData(id, data) => {
                buf.push(TAG_SET_DATA);
                put_u64(buf, *id);
                put_bytes(buf, data);
            }
        }
    }

    /// 解码一条记录；字节在记录中途耗尽返回 [`DecodeFail::Torn`]，
    /// 标签/类型非法等返回 [`DecodeFail::Corrupt`]。
    fn decode_opt(buf: &[u8], pos: &mut usize) -> Result<Record, DecodeFail> {
        let tag = *buf.get(*pos).ok_or(DecodeFail::Torn)?;
        *pos += 1;
        let rec = match tag {
            TAG_BEGIN => Record::Begin(read_u64_opt(buf, pos)?),
            TAG_COMMIT => Record::Commit(read_u64_opt(buf, pos)?),
            TAG_INODE_ALLOC => {
                let id = read_u64_opt(buf, pos)?;
                let kind_byte = *buf.get(*pos).ok_or(DecodeFail::Torn)?;
                *pos += 1;
                let kind = match kind_byte {
                    KIND_FILE => NodeKind::File,
                    KIND_DIR => NodeKind::Directory,
                    _ => {
                        return Err(DecodeFail::Corrupt(format!(
                            "未知 inode 类型字节 {kind_byte}"
                        )))
                    }
                };
                let nlink = read_u64_opt(buf, pos)?;
                Record::InodeAlloc(id, kind, nlink)
            }
            TAG_INODE_FREE => Record::InodeFree(read_u64_opt(buf, pos)?),
            TAG_ENTRY_ADD => {
                let parent = read_u64_opt(buf, pos)?;
                let name = read_str_opt(buf, pos)?;
                let child = read_u64_opt(buf, pos)?;
                Record::EntryAdd(parent, name, child)
            }
            TAG_ENTRY_REMOVE => {
                let parent = read_u64_opt(buf, pos)?;
                let name = read_str_opt(buf, pos)?;
                Record::EntryRemove(parent, name)
            }
            TAG_ENTRY_SWAP => {
                let p1 = read_u64_opt(buf, pos)?;
                let n1 = read_str_opt(buf, pos)?;
                let p2 = read_u64_opt(buf, pos)?;
                let n2 = read_str_opt(buf, pos)?;
                Record::EntrySwap(p1, n1, p2, n2)
            }
            TAG_NLINK => {
                let id = read_u64_opt(buf, pos)?;
                let delta = read_i64_opt(buf, pos)?;
                Record::Nlink(id, delta)
            }
            TAG_SET_DATA => {
                let id = read_u64_opt(buf, pos)?;
                let data = read_bytes_opt(buf, pos)?;
                Record::SetData(id, data)
            }
            _ => {
                return Err(DecodeFail::Corrupt(format!("未知记录标签 {tag}")));
            }
        };
        Ok(rec)
    }
}

/// 崩溃注入点。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CrashPoint {
    /// 事务体已落盘、提交标记写入前。
    BeforeCommit,
    /// 提交标记写入后。
    AfterCommit,
}

/// 事务构建器：收集一个事务内的全部记录，提交时一次性追加。
pub struct Tx {
    id: u64,
    records: Vec<Record>,
}

impl Tx {
    fn new(id: u64) -> Self {
        Self {
            id,
            records: Vec::new(),
        }
    }

    /// 向事务追加一条记录。
    pub fn push(&mut self, record: Record) {
        self.records.push(record);
    }
}

/// 预写日志本身。`disk` 即模拟的持久介质。
pub struct Journal {
    disk: Vec<u8>,
    next_tx: u64,
    armed: Option<CrashPoint>,
}

/// 模拟断电的错误：调用方应把它当作“进程已死亡”，文件系统句柄不再可用。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SimulatedCrash {
    pub point: CrashPoint,
    pub tx_id: u64,
}

impl std::fmt::Display for SimulatedCrash {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "模拟断电（事务 {}，注入点 {:?}）",
            self.tx_id, self.point
        )
    }
}

impl std::error::Error for SimulatedCrash {}

impl Journal {
    /// 在空白介质上创建日志。
    pub fn fresh() -> Self {
        Self {
            disk: Vec::new(),
            next_tx: 1,
            armed: None,
        }
    }

    /// 在已有介质上打开日志（不做回放，回放由文件系统层完成）。
    /// 事务序号从历史已提交事务的最大序号之后继续，避免编号冲突。
    pub fn open(disk: Vec<u8>) -> Self {
        let max_tx = Journal::parse_committed(&disk)
            .unwrap_or_default()
            .into_iter()
            .filter_map(|r| match r {
                Record::Begin(id) | Record::Commit(id) => Some(id),
                _ => None,
            })
            .max()
            .unwrap_or(0);
        Self {
            disk,
            next_tx: max_tx + 1,
            armed: None,
        }
    }

    /// 取出持久介质的完整字节（测试中用于模拟重新挂载）。
    pub fn into_disk(self) -> Vec<u8> {
        self.disk
    }

    /// 只读查看持久介质字节。
    pub fn peek_disk(&self) -> &[u8] {
        &self.disk
    }

    /// 持久介质当前的字节数。
    pub fn disk_len(&self) -> usize {
        self.disk.len()
    }

    /// 武装一次性崩溃注入：下一次 `commit` 到达该注入点时抛出
    /// [`SimulatedCrash`]，注入随即解除。
    pub fn arm_crash(&mut self, point: CrashPoint) {
        self.armed = Some(point);
    }

    /// 开始一个新事务。
    pub fn begin(&mut self) -> Tx {
        let tx = Tx::new(self.next_tx);
        self.next_tx += 1;
        tx
    }

    /// 提交事务：先追加事务体，再追加提交标记。
    ///
    /// 若武装了 [`CrashPoint::BeforeCommit`]，事务体追加后立即返回断电错误，
    /// 提交标记永远不会写入；若武装了 [`CrashPoint::AfterCommit`]，
    /// 提交标记写入后返回断电错误。
    pub fn commit(&mut self, tx: Tx) -> Result<(), SimulatedCrash> {
        Record::Begin(tx.id).encode(&mut self.disk);
        for rec in &tx.records {
            rec.encode(&mut self.disk);
        }

        let armed = self.armed.take();
        if armed == Some(CrashPoint::BeforeCommit) {
            return Err(SimulatedCrash {
                point: CrashPoint::BeforeCommit,
                tx_id: tx.id,
            });
        }

        Record::Commit(tx.id).encode(&mut self.disk);

        if armed == Some(CrashPoint::AfterCommit) {
            return Err(SimulatedCrash {
                point: CrashPoint::AfterCommit,
                tx_id: tx.id,
            });
        }
        Ok(())
    }

    /// 不带注入地提交一个事务（主要供恢复后的孤儿回收使用）。
    pub fn commit_plain(&mut self, tx: Tx) {
        self.commit(tx)
            .expect("未武装崩溃注入时 commit 不会失败");
    }

    /// 解析日志，返回所有**已提交**事务的记录序列。
    ///
    /// 遇到第一个没有对应 `Commit` 的事务（含被截断的尾部、半截记录）即停止；
    /// 未提交事务的所有记录都不会出现在结果中。已提交事务内部若出现解码
    /// 错误才视为介质损坏并返回错误。
    pub fn parse_committed(disk: &[u8]) -> Result<Vec<Record>, FsError> {
        let mut pos = 0;
        let mut committed: Vec<Record> = Vec::new();
        while pos < disk.len() {
            // 必须以 Begin 开启一个事务。
            let record = match Record::decode_opt(disk, &mut pos) {
                Ok(rec) => rec,
                Err(DecodeFail::Torn) => return Ok(committed),
                Err(DecodeFail::Corrupt(m)) => return Err(FsError::CorruptJournal(m)),
            };
            let Record::Begin(tx_id) = record else {
                return Err(FsError::CorruptJournal(format!(
                    "位置 {pos} 期望 Begin，实际为 {record:?}"
                )));
            };
            let mut body = vec![Record::Begin(tx_id)];
            loop {
                if pos >= disk.len() {
                    // Commit 缺失：丢弃整个事务。
                    return Ok(committed);
                }
                let rec = match Record::decode_opt(disk, &mut pos) {
                    Ok(rec) => rec,
                    Err(DecodeFail::Torn) => {
                        // 事务体中的半截记录：按未提交处理，丢弃整个事务。
                        return Ok(committed);
                    }
                    Err(DecodeFail::Corrupt(m)) => return Err(FsError::CorruptJournal(m)),
                };
                match rec {
                    Record::Commit(id) if id == tx_id => {
                        body.push(Record::Commit(id));
                        break;
                    }
                    Record::Commit(other) => {
                        return Err(FsError::CorruptJournal(format!(
                            "事务 {tx_id} 中出现了不属于它的 Commit({other})"
                        )));
                    }
                    other => body.push(other),
                }
            }
            committed.extend(body);
        }
        Ok(committed)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encode_decode_roundtrip() {
        let records = vec![
            Record::Begin(7),
            Record::InodeAlloc(3, NodeKind::Directory, 1),
            Record::EntryAdd(1, "dir".into(), 3),
            Record::Nlink(9, -2),
            Record::SetData(9, vec![1, 2, 3, 255]),
            Record::EntrySwap(1, "a".into(), 3, "b".into()),
            Record::EntryRemove(1, "x".into()),
            Record::InodeFree(4),
            Record::Commit(7),
        ];
        let mut wrapped = Vec::new();
        for r in &records {
            r.encode(&mut wrapped);
        }
        let got = Journal::parse_committed(&wrapped).unwrap();
        assert_eq!(got, records);

        // 没有事务外壳的裸记录：第一个标签不是 Begin，属于真损坏，报错。
        let mut bare = Vec::new();
        Record::EntryAdd(1, "a".into(), 2).encode(&mut bare);
        assert!(Journal::parse_committed(&bare).is_err());

        // 已“提交”的事务里出现未知标签：同样按损坏处理而不是静默吞掉。
        let mut bad = Vec::new();
        Record::Begin(1).encode(&mut bad);
        bad.push(99); // 未知标签
        Record::Commit(1).encode(&mut bad);
        assert!(Journal::parse_committed(&bad).is_err());
    }

    #[test]
    fn truncated_tail_is_dropped() {
        let mut j = Journal::fresh();
        let mut tx = j.begin();
        tx.push(Record::EntryAdd(1, "a".into(), 2));
        j.commit_plain(tx);
        let good_len = j.disk_len();

        // 第二个事务完整提交后，手工把介质截断到该事务内部模拟撕裂。
        let mut tx2 = j.begin();
        tx2.push(Record::EntryRemove(1, "a".into()));
        j.commit_plain(tx2);
        let full = j.into_disk();
        assert!(full.len() > good_len + 3);
        let torn = &full[..good_len + 3]; // 落在第二个事务的 Begin 之后
        let got = Journal::parse_committed(torn).unwrap();
        assert!(got.iter().any(|r| matches!(
            r,
            Record::EntryAdd(p, n, _) if *p == 1 && n == "a"
        )));
        assert!(!got.iter().any(|r| matches!(
            r,
            Record::EntryRemove(p, n) if *p == 1 && n == "a"
        )));

        // 只截断掉 Commit 标记（事务体完整）也应丢弃整个第二事务。
        let no_commit = &full[..full.len().saturating_sub(2)];
        let got2 = Journal::parse_committed(no_commit).unwrap();
        assert!(!got2.iter().any(|r| matches!(
            r,
            Record::EntryRemove(p, n) if *p == 1 && n == "a"
        )));
    }

    #[test]
    fn before_commit_loses_transaction() {
        let mut j = Journal::fresh();
        let mut tx = j.begin();
        tx.push(Record::EntryAdd(1, "a".into(), 2));
        j.commit_plain(tx);

        j.arm_crash(CrashPoint::BeforeCommit);
        let mut tx2 = j.begin();
        tx2.push(Record::EntryRemove(1, "a".into()));
        let crash = j.commit(tx2).unwrap_err();
        assert_eq!(crash.point, CrashPoint::BeforeCommit);

        let got = Journal::parse_committed(&j.into_disk()).unwrap();
        assert!(got.iter().any(|r| matches!(
            r,
            Record::EntryAdd(p, n, _) if *p == 1 && n == "a"
        )));
        assert!(!got.iter().any(|r| matches!(
            r,
            Record::EntryRemove(p, n) if *p == 1 && n == "a"
        )));
    }

    #[test]
    fn after_commit_keeps_transaction() {
        let mut j = Journal::fresh();
        j.arm_crash(CrashPoint::AfterCommit);
        let mut tx = j.begin();
        tx.push(Record::EntryAdd(1, "a".into(), 2));
        let crash = j.commit(tx).unwrap_err();
        assert_eq!(crash.point, CrashPoint::AfterCommit);

        let got = Journal::parse_committed(&j.into_disk()).unwrap();
        assert!(got.iter().any(|r| matches!(
            r,
            Record::EntryAdd(p, n, _) if *p == 1 && n == "a"
        )));
    }
}
