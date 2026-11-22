//! 文件系统命名空间模型。
//!
//! 核心对象：
//! - [`Inode`]：文件/目录实体，携带内容、硬链接计数与打开计数；
//! - 目录项（父 inode + 名称）只保存被引用 inode 的编号；
//! - [`Handle`]：打开文件句柄，固定绑定到 inode，与目录项后续的删除/交换无关；
//! - [`Journal`]：目录交换、链接计数、inode 释放等所有结构性变更都先写日志。
//!
//! 所有方法都采用 `&mut self`，并发测试在外层用 `Mutex` 串行化，
//! 以确定性的交错序列验证语义。

use std::collections::{BTreeMap, HashMap, HashSet};

use crate::journal::{CrashPoint, Journal, Record, SimulatedCrash};
use crate::path::{components, split_parent_name};

const ROOT_INO: u64 = 1;

/// 节点类型。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeKind {
    /// 普通文件。
    File,
    /// 目录。
    Directory,
}

/// 文件系统操作错误。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FsError {
    /// 路径语法非法（例如含 `..`）。
    InvalidPath(String),
    /// 路径上的某个组件不存在。
    NotFound(String),
    /// 目标已存在。
    AlreadyExists(String),
    /// 路径中间组件不是目录。
    NotADirectory(String),
    /// 对目录执行了只允许普通文件的操作（如 link/open 读句柄）。
    IsADirectory(String),
    /// 对普通文件执行了只允许目录的操作。
    NotADirectoryTarget(String),
    /// 目录非空，不能 rmdir。
    DirectoryNotEmpty(String),
    /// 交换会让目录祖先关系成环。
    WouldCycle {
        ancestor: u64,
        descendant: u64,
    },
    /// 根目录不允许被该操作修改。
    RootReserved,
    /// 句柄编号未知（可能已关闭）。
    BadHandle(u64),
    /// 日志介质损坏，无法回放。
    CorruptJournal(String),
    /// 内部一致性检查失败（说明模型实现有 bug）。
    InvariantFailed(String),
}

impl std::fmt::Display for FsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FsError::InvalidPath(p) => write!(f, "非法路径: {p}"),
            FsError::NotFound(p) => write!(f, "路径不存在: {p}"),
            FsError::AlreadyExists(p) => write!(f, "目标已存在: {p}"),
            FsError::NotADirectory(p) => write!(f, "路径中间组件不是目录: {p}"),
            FsError::IsADirectory(p) => write!(f, "目标是目录，该操作仅支持普通文件: {p}"),
            FsError::NotADirectoryTarget(p) => write!(f, "目标不是目录: {p}"),
            FsError::DirectoryNotEmpty(p) => write!(f, "目录非空: {p}"),
            FsError::WouldCycle {
                ancestor,
                descendant,
            } => write!(
                f,
                "交换会形成环：目录 {ancestor} 是目录 {descendant} 的祖先"
            ),
            FsError::RootReserved => write!(f, "根目录保留，不能被该操作修改"),
            FsError::BadHandle(h) => write!(f, "无效或已关闭的句柄: {h}"),
            FsError::CorruptJournal(m) => write!(f, "日志损坏: {m}"),
            FsError::InvariantFailed(m) => write!(f, "文件系统不变量被破坏: {m}"),
        }
    }
}

impl std::error::Error for FsError {}

/// 一个 inode 的全部内存状态。
#[derive(Debug, Clone)]
pub struct Inode {
    pub id: u64,
    pub kind: NodeKind,
    /// 硬链接计数：指向该 inode 的目录项数（根目录固定为 1）。
    pub nlink: u64,
    /// 当前打开句柄数（易失状态，不写日志）。
    pub open_count: u64,
    /// 普通文件内容。
    pub data: Vec<u8>,
}

impl Inode {
    fn new(id: u64, kind: NodeKind, nlink: u64) -> Self {
        Self {
            id,
            kind,
            nlink,
            open_count: 0,
            data: Vec::new(),
        }
    }
}

/// 目录项：名称 -> inode 编号。目录本身用 `BTreeMap` 保证列举顺序稳定。
type DirEntry = BTreeMap<String, u64>;

/// 打开句柄的公开快照。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Handle {
    pub id: u64,
    /// 句柄固定绑定的 inode，打开后不再随命名空间变化。
    pub inode: u64,
}

/// inode 统计信息（重开后用于核对链接计数与回收状态）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InodeInfo {
    pub id: u64,
    pub kind: NodeKind,
    pub nlink: u64,
    pub open_count: u64,
    pub size: usize,
    /// 该 inode 当前是否仍被分配（false 表示已真实回收，号可被复用）。
    pub allocated: bool,
}

/// 文件系统命名空间。
pub struct FileSystem {
    inodes: HashMap<u64, Inode>,
    /// 目录项表：目录 inode -> (名称 -> 子 inode)。
    entries: HashMap<u64, DirEntry>,
    /// 仅目录 inode 有父指针；根目录的父指针指向自己。
    dir_parent: HashMap<u64, u64>,
    next_ino: u64,
    free_inos: Vec<u64>,
    handles: HashMap<u64, Handle>,
    next_handle: u64,
    journal: Journal,
}

impl FileSystem {
    // ---------- 构造与恢复 ----------

    /// 创建空白文件系统，根目录 inode 固定为 1。
    pub fn fresh() -> Self {
        let mut inodes = HashMap::new();
        inodes.insert(ROOT_INO, Inode::new(ROOT_INO, NodeKind::Directory, 1));
        let mut dir_parent = HashMap::new();
        dir_parent.insert(ROOT_INO, ROOT_INO);
        let mut entries = HashMap::new();
        entries.insert(ROOT_INO, BTreeMap::new());
        Self {
            inodes,
            entries,
            dir_parent,
            next_ino: ROOT_INO + 1,
            free_inos: Vec::new(),
            handles: HashMap::new(),
            next_handle: 1,
            journal: Journal::fresh(),
        }
    }

    /// 从持久介质重新挂载：回放全部已提交事务，丢弃未提交尾部，
    /// 并回收崩溃时“链接已归零但尚未写 InodeFree”的孤儿 inode。
    pub fn reopen(disk: Vec<u8>) -> Result<Self, FsError> {
        let committed = Journal::parse_committed(&disk)?;

        // 先在临时实例上回放结构，再把日志对象替换为承载原介质的那一个，
        // 这样重开后的 disk_len/committed_records 仍能看到全部历史事务。
        let mut scratch = FileSystem::fresh();
        scratch.replay(&committed)?;
        scratch.rebuild_metadata();

        let mut fs = scratch;
        fs.journal = Journal::open(disk);
        fs.gc_orphans();
        Ok(fs)
    }

    /// 取出当前持久介质字节（模拟把磁盘摘走）。
    pub fn into_disk(self) -> Vec<u8> {
        self.journal.into_disk()
    }

    /// 持久介质当前字节数。
    pub fn disk_len(&self) -> usize {
        self.journal.disk_len()
    }

    /// 武装一次性崩溃注入点。
    pub fn arm_crash(&mut self, point: CrashPoint) {
        self.journal.arm_crash(point);
    }

    /// 已提交事务在日志中的全部记录（按日志顺序，含 Begin/Commit）。
    pub fn committed_records(&self) -> Result<Vec<Record>, FsError> {
        // 通过解析当前磁盘得到，保证与“真实介质”一致而不是内存臆造。
        Journal::parse_committed(self.journal.peek_disk())
    }

    // ---------- 路径解析 ----------

    fn resolve_ino(&self, path: &str) -> Result<u64, FsError> {
        let mut cur = ROOT_INO;
        for part in components(path)? {
            let dir = self
                .inodes
                .get(&cur)
                .ok_or_else(|| FsError::InvariantFailed(format!("目录 inode {cur} 缺失")))?;
            if dir.kind != NodeKind::Directory {
                // 保留调用方给出的完整路径，便于定位。
                return Err(FsError::NotADirectory(path.to_string()));
            }
            let entries = self.dir_entries(cur)?;
            let Some(&next) = entries.get(&part) else {
                return Err(FsError::NotFound(path.to_string()));
            };
            cur = next;
        }
        Ok(cur)
    }

    fn dir_entries(&self, dir_ino: u64) -> Result<&DirEntry, FsError> {
        let ino = self
            .inodes
            .get(&dir_ino)
            .ok_or_else(|| FsError::InvariantFailed(format!("inode {dir_ino} 不存在")))?;
        if ino.kind != NodeKind::Directory {
            return Err(FsError::NotADirectoryTarget(format!("inode {dir_ino}")));
        }
        self.entries
            .get(&dir_ino)
            .ok_or_else(|| FsError::InvariantFailed(format!("目录 {dir_ino} 缺少目录项表")))
    }

    /// 解析路径的父目录并返回 `(父 inode, 最后一个组件名)`。
    /// 所有错误都携带调用方给出的完整路径。
    fn resolve_parent(&self, path: &str) -> Result<(u64, String), FsError> {
        let Some((prefix, name)) = split_parent_name(path)? else {
            return Err(FsError::RootReserved);
        };
        let mut cur = ROOT_INO;
        for part in prefix {
            let dir = self
                .inodes
                .get(&cur)
                .ok_or_else(|| FsError::InvariantFailed(format!("目录 inode {cur} 缺失")))?;
            if dir.kind != NodeKind::Directory {
                return Err(FsError::NotADirectory(path.to_string()));
            }
            let Some(&next) = self.dir_entries(cur)?.get(&part) else {
                return Err(FsError::NotFound(path.to_string()));
            };
            cur = next;
        }
        if self.inodes.get(&cur).map(|i| i.kind) != Some(NodeKind::Directory) {
            return Err(FsError::NotADirectory(path.to_string()));
        }
        Ok((cur, name))
    }

    // ---------- 结构变更 ----------

    fn alloc_inode(&mut self, kind: NodeKind, nlink: u64) -> u64 {
        let id = if let Some(id) = self.free_inos.pop() {
            id
        } else {
            let id = self.next_ino;
            self.next_ino += 1;
            id
        };
        self.inodes.insert(id, Inode::new(id, kind, nlink));
        if kind == NodeKind::Directory {
            self.entries.insert(id, BTreeMap::new());
        }
        id
    }

    /// nlink 与 open_count 同时归零才真正释放，释放动作写入日志。
    fn maybe_free(&mut self, ino: u64, tx: &mut crate::journal::Tx) {
        let should_free = self
            .inodes
            .get(&ino)
            .map(|i| i.nlink == 0 && i.open_count == 0 && ino != ROOT_INO)
            .unwrap_or(false);
        if should_free {
            tx.push(Record::InodeFree(ino));
            self.inodes.remove(&ino);
            self.entries.remove(&ino);
            self.dir_parent.remove(&ino);
            self.free_inos.push(ino);
        }
    }

    fn adjust_nlink(&mut self, ino: u64, delta: i64, tx: &mut crate::journal::Tx) {
        tx.push(Record::Nlink(ino, delta));
        let ino_state = self
            .inodes
            .get_mut(&ino)
            .expect("调整 nlink 的 inode 必须存在");
        if delta >= 0 {
            ino_state.nlink = ino_state.nlink.checked_add(delta as u64).expect("nlink 溢出");
        } else {
            let dec = (-delta) as u64;
            assert!(ino_state.nlink >= dec, "nlink 不能减成负数");
            ino_state.nlink -= dec;
        }
    }

    /// 创建目录。
    pub fn mkdir(&mut self, path: &str) -> Result<u64, FsError> {
        let (parent, name) = self.resolve_parent(path)?;
        if self.dir_entries(parent)?.contains_key(&name) {
            return Err(FsError::AlreadyExists(path.to_string()));
        }

        let mut tx = self.journal.begin();
        let id = self.alloc_inode(NodeKind::Directory, 1);
        tx.push(Record::InodeAlloc(id, NodeKind::Directory, 1));
        tx.push(Record::EntryAdd(parent, name.clone(), id));
        self.journal
            .commit(tx)
            .map_err(|e: SimulatedCrash| FsError::CorruptJournal(e.to_string()))?;

        self.entries.get_mut(&parent).unwrap().insert(name, id);
        self.dir_parent.insert(id, parent);
        Ok(id)
    }

    /// 创建普通文件（空内容），已存在则报错。
    pub fn create_file(&mut self, path: &str) -> Result<u64, FsError> {
        let (parent, name) = self.resolve_parent(path)?;
        if self.dir_entries(parent)?.contains_key(&name) {
            return Err(FsError::AlreadyExists(path.to_string()));
        }

        let mut tx = self.journal.begin();
        let id = self.alloc_inode(NodeKind::File, 1);
        tx.push(Record::InodeAlloc(id, NodeKind::File, 1));
        tx.push(Record::EntryAdd(parent, name.clone(), id));
        self.journal
            .commit(tx)
            .map_err(|e: SimulatedCrash| FsError::CorruptJournal(e.to_string()))?;

        self.entries.get_mut(&parent).unwrap().insert(name, id);
        Ok(id)
    }

    /// 写入普通文件内容：路径不存在时创建文件（语义类似
    /// `open(O_CREAT|O_TRUNC)`），已存在且为普通文件时覆盖；内容变更走日志，
    /// 保证重开后可读。
    pub fn write_file(&mut self, path: &str, data: &[u8]) -> Result<(), FsError> {
        // 不存在则在同一事务里完成“创建目录项 + 分配 inode + 写内容”，
        // 保证崩溃时要么整事务生效、要么完全不生效。
        let ino = match self.resolve_ino(path) {
            Ok(ino) => {
                if self.inodes.get(&ino).map(|i| i.kind) != Some(NodeKind::File) {
                    return Err(FsError::IsADirectory(path.to_string()));
                }
                ino
            }
            Err(FsError::NotFound(_)) => self.create_file(path)?,
            Err(other) => return Err(other),
        };
        let mut tx = self.journal.begin();
        tx.push(Record::SetData(ino, data.to_vec()));
        self.journal
            .commit(tx)
            .map_err(|e: SimulatedCrash| FsError::CorruptJournal(e.to_string()))?;
        self.inodes.get_mut(&ino).unwrap().data = data.to_vec();
        Ok(())
    }

    /// 为普通文件建立硬链接：`target` 必须已存在且是普通文件，
    /// `link_path` 必须不存在。
    pub fn link(&mut self, target: &str, link_path: &str) -> Result<(), FsError> {
        let target_ino = self.resolve_ino(target)?;
        if self.inodes.get(&target_ino).map(|i| i.kind) != Some(NodeKind::File) {
            return Err(FsError::IsADirectory(target.to_string()));
        }
        let (parent, name) = self.resolve_parent(link_path)?;
        if self.dir_entries(parent)?.contains_key(&name) {
            return Err(FsError::AlreadyExists(link_path.to_string()));
        }

        let mut tx = self.journal.begin();
        tx.push(Record::EntryAdd(parent, name.clone(), target_ino));
        self.adjust_nlink(target_ino, 1, &mut tx);
        self.journal
            .commit(tx)
            .map_err(|e: SimulatedCrash| FsError::CorruptJournal(e.to_string()))?;

        self.entries.get_mut(&parent).unwrap().insert(name, target_ino);
        Ok(())
    }

    /// 删除一个目录项。普通文件 nlink 减一；归零且无句柄时真实释放。
    pub fn unlink(&mut self, path: &str) -> Result<(), FsError> {
        let (parent, name) = self.resolve_parent(path)?;
        let entries = self.dir_entries(parent)?;
        let Some(&ino) = entries.get(&name) else {
            return Err(FsError::NotFound(path.to_string()));
        };
        if self.inodes.get(&ino).map(|i| i.kind) == Some(NodeKind::Directory) {
            return Err(FsError::IsADirectory(path.to_string()));
        }

        let mut tx = self.journal.begin();
        tx.push(Record::EntryRemove(parent, name.clone()));
        self.adjust_nlink(ino, -1, &mut tx);
        self.maybe_free(ino, &mut tx);
        self.journal
            .commit(tx)
            .map_err(|e: SimulatedCrash| FsError::CorruptJournal(e.to_string()))?;

        self.entries.get_mut(&parent).unwrap().remove(&name);
        Ok(())
    }

    /// 删除空目录。
    pub fn rmdir(&mut self, path: &str) -> Result<(), FsError> {
        let (parent, name) = self.resolve_parent(path)?;
        let entries = self.dir_entries(parent)?;
        let Some(&ino) = entries.get(&name) else {
            return Err(FsError::NotFound(path.to_string()));
        };
        if self.inodes.get(&ino).map(|i| i.kind) != Some(NodeKind::Directory) {
            return Err(FsError::NotADirectoryTarget(path.to_string()));
        }
        if !self.dir_entries(ino)?.is_empty() {
            return Err(FsError::DirectoryNotEmpty(path.to_string()));
        }

        let mut tx = self.journal.begin();
        tx.push(Record::EntryRemove(parent, name.clone()));
        tx.push(Record::Nlink(ino, -1));
        tx.push(Record::InodeFree(ino));
        self.journal
            .commit(tx)
            .map_err(|e: SimulatedCrash| FsError::CorruptJournal(e.to_string()))?;

        self.entries.get_mut(&parent).unwrap().remove(&name);
        self.inodes.remove(&ino);
        self.entries.remove(&ino);
        self.dir_parent.remove(&ino);
        self.free_inos.push(ino);
        Ok(())
    }

    // ---------- 打开句柄与读取 ----------

    /// 打开普通文件，返回固定到当前 inode 的句柄。
    pub fn open(&mut self, path: &str) -> Result<Handle, FsError> {
        let ino = self.resolve_ino(path)?;
        if self.inodes.get(&ino).map(|i| i.kind) != Some(NodeKind::File) {
            return Err(FsError::IsADirectory(path.to_string()));
        }
        let id = self.next_handle;
        self.next_handle += 1;
        self.handles.insert(id, Handle { id, inode: ino });
        self.inodes.get_mut(&ino).unwrap().open_count += 1;
        Ok(Handle { id, inode: ino })
    }

    /// 关闭句柄；若这是最后一个引用且 nlink 已归零，inode 在此刻真实释放。
    pub fn close(&mut self, handle: Handle) -> Result<(), FsError> {
        let Handle { id, inode } = handle;
        if self.handles.remove(&id).is_none() {
            return Err(FsError::BadHandle(id));
        }
        let state = self
            .inodes
            .get_mut(&inode)
            .ok_or_else(|| FsError::InvariantFailed(format!("句柄 {id} 指向的 inode 已消失")))?;
        assert!(state.open_count > 0, "open_count 不能减成负数");
        state.open_count -= 1;

        let mut tx = self.journal.begin();
        self.maybe_free(inode, &mut tx);
        self.journal
            .commit(tx)
            .map_err(|e: SimulatedCrash| FsError::CorruptJournal(e.to_string()))?;
        Ok(())
    }

    /// 通过旧句柄读取：永远读打开时绑定的那个 inode。
    pub fn read_handle(&self, handle: &Handle) -> Result<Vec<u8>, FsError> {
        let saved = self
            .handles
            .get(&handle.id)
            .ok_or(FsError::BadHandle(handle.id))?;
        if saved.inode != handle.inode {
            return Err(FsError::BadHandle(handle.id));
        }
        Ok(self
            .inodes
            .get(&handle.inode)
            .ok_or_else(|| FsError::InvariantFailed("句柄 inode 不应被释放".into()))?
            .data
            .clone())
    }

    /// 通过当前路径读取：读路径当前解析到的 inode。
    pub fn read_path(&self, path: &str) -> Result<Vec<u8>, FsError> {
        let ino = self.resolve_ino(path)?;
        let inode = self
            .inodes
            .get(&ino)
            .ok_or_else(|| FsError::InvariantFailed("解析出的 inode 不存在".into()))?;
        if inode.kind != NodeKind::File {
            return Err(FsError::IsADirectory(path.to_string()));
        }
        Ok(inode.data.clone())
    }

    // ---------- 原子交换 ----------

    /// 原子交换两个目录项引用的 inode。
    ///
    /// 交换只改目录项，不动 inode 本身，因此：
    /// - 已打开的句柄仍然指向原 inode；
    /// - 两个目录项可以位于不同目录；
    /// - 若交换会让目录祖先关系成环（把祖先目录换到后代名下）则拒绝；
    /// - 两个名称都必须已存在。
    pub fn swap(&mut self, a: &str, b: &str) -> Result<(), FsError> {
        // resolve_parent 保证错误携带完整原始路径，且父级一定是目录。
        let (parent_a, na) = self.resolve_parent(a)?;
        let (parent_b, nb) = self.resolve_parent(b)?;

        let ino_a = *self
            .dir_entries(parent_a)?
            .get(&na)
            .ok_or_else(|| FsError::NotFound(a.to_string()))?;
        let ino_b = *self
            .dir_entries(parent_b)?
            .get(&nb)
            .ok_or_else(|| FsError::NotFound(b.to_string()))?;

        let kind_a = self.inodes.get(&ino_a).unwrap().kind;
        let kind_b = self.inodes.get(&ino_b).unwrap().kind;

        // 成环判定：目录 X 的祖先（含自身）不能被换到 X 的子树里。
        if kind_a == NodeKind::Directory
            && kind_b == NodeKind::Directory
            && (self.is_ancestor_or_self(ino_a, ino_b) || self.is_ancestor_or_self(ino_b, ino_a))
        {
            let (ancestor, descendant) = if self.is_ancestor_or_self(ino_a, ino_b) {
                (ino_a, ino_b)
            } else {
                (ino_b, ino_a)
            };
            return Err(FsError::WouldCycle {
                ancestor,
                descendant,
            });
        }

        let mut tx = self.journal.begin();
        tx.push(Record::EntrySwap(parent_a, na.clone(), parent_b, nb.clone()));
        self.journal
            .commit(tx)
            .map_err(|e: SimulatedCrash| FsError::CorruptJournal(e.to_string()))?;

        // 提交后再应用内存状态。
        self.entries
            .get_mut(&parent_a)
            .unwrap()
            .insert(na.clone(), ino_b);
        self.entries
            .get_mut(&parent_b)
            .unwrap()
            .insert(nb.clone(), ino_a);

        if kind_a == NodeKind::Directory {
            self.dir_parent.insert(ino_a, parent_b);
        }
        if kind_b == NodeKind::Directory {
            self.dir_parent.insert(ino_b, parent_a);
        }
        Ok(())
    }

    /// `maybe_ancestor` 是否为 `of` 的祖先或自身（沿目录父指针上行）。
    fn is_ancestor_or_self(&self, maybe_ancestor: u64, of: u64) -> bool {
        let mut cur = of;
        loop {
            if cur == maybe_ancestor {
                return true;
            }
            if cur == ROOT_INO {
                return false;
            }
            match self.dir_parent.get(&cur) {
                Some(&p) => cur = p,
                None => return false,
            }
        }
    }

    // ---------- 查询 ----------

    /// inode 是否仍被分配（未真实回收）。
    pub fn inode_exists(&self, ino: u64) -> bool {
        self.inodes.contains_key(&ino)
    }

    /// 查询 inode 统计；已回收的号返回 `allocated=false` 的占位信息。
    pub fn inode_info(&self, ino: u64) -> InodeInfo {
        match self.inodes.get(&ino) {
            Some(i) => InodeInfo {
                id: i.id,
                kind: i.kind,
                nlink: i.nlink,
                open_count: i.open_count,
                size: i.data.len(),
                allocated: true,
            },
            None => InodeInfo {
                id: ino,
                kind: NodeKind::File,
                nlink: 0,
                open_count: 0,
                size: 0,
                allocated: false,
            },
        }
    }

    /// 按路径查 inode 统计。
    pub fn stat_path(&self, path: &str) -> Result<InodeInfo, FsError> {
        let ino = self.resolve_ino(path)?;
        Ok(self.inode_info(ino))
    }

    /// 列举目录下的 `(名称, inode)`，按名称排序。
    pub fn list_dir(&self, path: &str) -> Result<Vec<(String, u64)>, FsError> {
        let ino = self.resolve_ino(path)?;
        Ok(self
            .dir_entries(ino)?
            .iter()
            .map(|(n, c)| (n.clone(), *c))
            .collect())
    }

    // ---------- 不变量检查 ----------

    /// 全量一致性检查，返回错误即不变量被破坏。测试在交错/恢复后调用。
    pub fn check_invariants(&self) -> Result<(), FsError> {
        // 1. 根目录存在且是目录。
        let root = self
            .inodes
            .get(&ROOT_INO)
            .ok_or_else(|| FsError::InvariantFailed("根 inode 缺失".into()))?;
        if root.kind != NodeKind::Directory || root.nlink != 1 {
            return Err(FsError::InvariantFailed("根 inode 状态错误".into()));
        }

        // 2. 从根可达性遍历，统计每个普通文件被多少目录项引用。
        let mut reachable: HashSet<u64> = HashSet::new();
        let mut entry_count: HashMap<u64, u64> = HashMap::new();
        let mut queue = vec![ROOT_INO];
        while let Some(dir_ino) = queue.pop() {
            if !reachable.insert(dir_ino) {
                // 目录再次被访问意味着目录有多个父级或成环。
                return Err(FsError::InvariantFailed(format!(
                    "目录 {dir_ino} 被重复到达，目录结构成环或存在目录硬链接"
                )));
            }
            for (name, child) in self.dir_entries(dir_ino)? {
                let child_ino = self
                    .inodes
                    .get(child)
                    .ok_or_else(|| FsError::InvariantFailed(format!(
                        "目录项 {dir_ino}/{name} 指向已释放 inode {child}"
                    )))?;
                *entry_count.entry(*child).or_insert(0) += 1;
                if child_ino.kind == NodeKind::Directory {
                    queue.push(*child);
                }
            }
        }

        // 3. 目录必须全部从根可达；普通文件通过目录项计数（nlink）校验，
        //    不要求出现在目录可达集合中（硬链接文件本来就不在目录遍历队列里）。
        for (ino, inode) in self.inodes.iter() {
            if inode.kind == NodeKind::Directory && !reachable.contains(ino) {
                return Err(FsError::InvariantFailed(format!(
                    "目录 inode {ino} 已分配但从根不可达"
                )));
            }
        }

        // 4. 目录父指针与实际结构一致。
        for dir_ino in reachable.iter().copied() {
            let recorded = self.dir_parent.get(&dir_ino).copied().ok_or_else(|| {
                FsError::InvariantFailed(format!("目录 {dir_ino} 缺少父指针"))
            })?;
            if dir_ino == ROOT_INO {
                if recorded != ROOT_INO {
                    return Err(FsError::InvariantFailed("根的父指针必须指向自己".into()));
                }
                continue;
            }
            let parent_entries = self.dir_entries(recorded)?;
            let points_back = parent_entries.iter().any(|(_, c)| *c == dir_ino);
            if !points_back {
                return Err(FsError::InvariantFailed(format!(
                    "目录 {dir_ino} 的父指针指向 {recorded}，但该目录下没有指回它的目录项"
                )));
            }
        }

        // 5. nlink 与目录项计数一致（目录固定为 1）。
        for (ino, inode) in &self.inodes {
            match inode.kind {
                NodeKind::File => {
                    let actual = entry_count.get(ino).copied().unwrap_or(0);
                    if inode.nlink != actual {
                        return Err(FsError::InvariantFailed(format!(
                            "文件 inode {ino} nlink={} 但目录项计数={actual}",
                            inode.nlink
                        )));
                    }
                }
                NodeKind::Directory => {
                    if inode.nlink != 1 {
                        return Err(FsError::InvariantFailed(format!(
                            "目录 inode {ino} nlink 必须固定为 1，实际为 {}",
                            inode.nlink
                        )));
                    }
                }
            }
            if inode.open_count as usize != self.handles.values().filter(|h| h.inode == *ino).count()
            {
                return Err(FsError::InvariantFailed(format!(
                    "inode {ino} open_count 与句柄表不一致"
                )));
            }
        }

        // 6. 句柄只指向仍存活的 inode。
        for h in self.handles.values() {
            if !self.inodes.contains_key(&h.inode) {
                return Err(FsError::InvariantFailed(format!(
                    "句柄 {} 指向已释放 inode {}",
                    h.id, h.inode
                )));
            }
        }
        Ok(())
    }

    // ---------- 日志回放 ----------

    fn replay(&mut self, records: &[Record]) -> Result<(), FsError> {
        for rec in records {
            self.apply(rec)?;
        }
        Ok(())
    }

    fn apply(&mut self, rec: &Record) -> Result<(), FsError> {
        match rec {
            Record::Begin(_) | Record::Commit(_) => {}
            Record::InodeAlloc(id, kind, nlink) => {
                if let Some(pos) = self.free_inos.iter().position(|x| x == id) {
                    self.free_inos.remove(pos);
                }
                if *id >= self.next_ino {
                    self.next_ino = id + 1;
                }
                let mut inode = Inode::new(*id, *kind, *nlink);
                if *kind == NodeKind::Directory {
                    self.entries.insert(*id, BTreeMap::new());
                    self.dir_parent.entry(*id).or_insert(ROOT_INO);
                }
                // 目录不使用 data 字段。
                inode.kind = *kind;
                self.inodes.insert(*id, inode);
            }
            Record::InodeFree(id) => {
                self.inodes.remove(id);
                self.entries.remove(id);
                self.dir_parent.remove(id);
                if !self.free_inos.contains(id) {
                    self.free_inos.push(*id);
                }
            }
            Record::EntryAdd(parent, name, child) => {
                self.entries
                    .entry(*parent)
                    .or_insert_with(BTreeMap::new)
                    .insert(name.clone(), *child);
            }
            Record::EntryRemove(parent, name) => {
                if let Some(map) = self.entries.get_mut(parent) {
                    map.remove(name);
                }
            }
            Record::EntrySwap(p1, n1, p2, n2) => {
                let a = self
                    .entries
                    .get(p1)
                    .and_then(|m| m.get(n1))
                    .copied()
                    .ok_or_else(|| FsError::CorruptJournal(format!(
                        "回放 EntrySwap：{p1}/{n1} 不存在"
                    )))?;
                let b = self
                    .entries
                    .get(p2)
                    .and_then(|m| m.get(n2))
                    .copied()
                    .ok_or_else(|| FsError::CorruptJournal(format!(
                        "回放 EntrySwap：{p2}/{n2} 不存在"
                    )))?;
                self.entries.get_mut(p1).unwrap().insert(n1.clone(), b);
                self.entries.get_mut(p2).unwrap().insert(n2.clone(), a);
            }
            Record::Nlink(ino, delta) => {
                let state = self.inodes.get_mut(ino).ok_or_else(|| {
                    FsError::CorruptJournal(format!("回放 Nlink：inode {ino} 不存在"))
                })?;
                if *delta >= 0 {
                    state.nlink = state
                        .nlink
                        .checked_add(*delta as u64)
                        .ok_or_else(|| FsError::CorruptJournal("回放 nlink 溢出".into()))?;
                } else {
                    let dec = (-*delta) as u64;
                    if state.nlink < dec {
                        return Err(FsError::CorruptJournal(format!(
                            "回放后 inode {ino} nlink 为负"
                        )));
                    }
                    state.nlink -= dec;
                }
            }
            Record::SetData(ino, data) => {
                let state = self.inodes.get_mut(ino).ok_or_else(|| {
                    FsError::CorruptJournal(format!("回放 SetData：inode {ino} 不存在"))
                })?;
                state.data = data.clone();
            }
        }
        Ok(())
    }

    /// 回放后重建目录父指针、可达性与 inode 分配水位。
    fn rebuild_metadata(&mut self) {
        self.dir_parent.clear();
        self.dir_parent.insert(ROOT_INO, ROOT_INO);

        let mut queue = vec![ROOT_INO];
        let mut reachable: HashSet<u64> = HashSet::new();
        while let Some(dir_ino) = queue.pop() {
            if !reachable.insert(dir_ino) {
                continue;
            }
            let entries = self
                .entries
                .get(&dir_ino)
                .cloned()
                .unwrap_or_default();
            for (_name, child) in entries {
                if let Some(inode) = self.inodes.get(&child) {
                    if inode.kind == NodeKind::Directory {
                        self.dir_parent.insert(child, dir_ino);
                        queue.push(child);
                    }
                }
            }
        }

        // 分配水位：大于所有现存 inode 号。
        let max_live = self.inodes.keys().copied().max().unwrap_or(ROOT_INO);
        if max_live >= self.next_ino {
            self.next_ino = max_live + 1;
        }
    }

    /// 恢复时回收 nlink 已归零（或不可达）的孤儿 inode，回收动作补写日志，
    /// 保证再次重开状态仍然一致。
    fn gc_orphans(&mut self) {
        let live: HashSet<u64> = self.inodes.keys().copied().collect();
        let mut referenced: HashSet<u64> = HashSet::new();
        for map in self.entries.values() {
            for child in map.values() {
                referenced.insert(*child);
            }
        }

        let orphans: Vec<u64> = live
            .iter()
            .copied()
            .filter(|ino| *ino != ROOT_INO)
            .filter(|ino| {
                let nlink_zero = self.inodes.get(ino).map(|i| i.nlink == 0).unwrap_or(false);
                nlink_zero || !referenced.contains(ino)
            })
            .collect();

        if orphans.is_empty() {
            return;
        }
        let mut tx = self.journal.begin();
        for ino in &orphans {
            tx.push(Record::InodeFree(*ino));
        }
        self.journal.commit_plain(tx);
        for ino in orphans {
            self.inodes.remove(&ino);
            self.entries.remove(&ino);
            self.dir_parent.remove(&ino);
            if !self.free_inos.contains(&ino) {
                self.free_inos.push(ino);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fresh_fs_has_root_only() {
        let fs = FileSystem::fresh();
        fs.check_invariants().unwrap();
        assert_eq!(fs.list_dir("/").unwrap(), Vec::new());
        assert!(fs.inode_exists(ROOT_INO));
    }

    #[test]
    fn root_cannot_be_unlinked_or_swapped() {
        let mut fs = FileSystem::fresh();
        fs.create_file("/a").unwrap();
        assert_eq!(fs.unlink("/").unwrap_err(), FsError::RootReserved);
        assert_eq!(fs.swap("/", "/a").unwrap_err(), FsError::RootReserved);
    }
}
