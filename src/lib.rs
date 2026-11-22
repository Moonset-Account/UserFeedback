//! 用户态共享内存等待队列模型。
//!
//! 语义要点：
//! - `wait` 接受一个或多个（地址， 期望值）位置；**全部**比较成功后才原子登记，
//!   任一不匹配则不登记任何队列。
//! - 不同进程的虚拟地址可映射到同一底层对象位置（跨进程别名）；位置身份为
//!   `(对象, 偏移, 世代)`，对象销毁后 id 复用时世代递增，旧世代上的等待不会被
//!   新世代的唤醒触及。
//! - 唤醒按等待登记顺序（FIFO）处理；某等待在任一登记处完成后，其在其他位置
//!   队列中的登记被同时解除。
//! - `requeue`（移交）先核对源位置的值，可唤醒前 `wake_count` 项，再把随后
//!   `move_count` 项移到目标队尾；已在目标登记的同一等待者不重复排队；等待者
//!   原绝对截止时间在移交后继续生效。
//! - 取消、超时、唤醒发生在同一时间时，结果由输入事件顺序（方法调用顺序）
//!   唯一决定；超时只在 `advance_time` 时按（截止时间， 登记序号）顺序触发。

use std::collections::{BTreeMap, HashMap, VecDeque};

/// 进程标识。
pub type Pid = u32;
/// 线程标识。
pub type Tid = u32;
/// 进程虚拟地址。
pub type VAddr = u64;
/// 底层共享对象标识。
pub type ObjectId = u64;
/// 对象世代：对象销毁后 id 被复用时递增。
pub type Generation = u64;
/// 等待者标识。
pub type WaiterId = u64;
/// 逻辑时间（绝对截止时间所用的时间轴）。
pub type Time = u64;

/// 等待位置的规范身份：底层对象 + 对象内偏移 + 对象世代。
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Debug)]
pub struct LocKey {
    pub object: ObjectId,
    pub offset: u64,
    pub generation: Generation,
}

/// `wait` 的失败原因（此时不会登记任何队列）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WaitError {
    /// 地址未映射。
    Unmapped { addr: VAddr },
    /// 某位置当前值与期望值不符（EAGAIN 语义）。
    ValueMismatch { addr: VAddr, expected: u64, actual: u64 },
}

/// 等待的最终结果，唯一确定。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WaitOutcome {
    /// 被 `wake` 或 `requeue` 的唤醒部分唤醒。
    Woken,
    /// 绝对截止时间到达。
    TimedOut,
    /// 被显式取消。
    Cancelled,
}

/// `requeue` 的失败原因（此时源、目标队列均不变）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RequeueError {
    /// 源或目标地址未映射。
    Unmapped { addr: VAddr },
    /// 源位置当前值与期望值不符。
    SourceMismatch { expected: u64, actual: u64 },
}

/// `requeue` 的结果报告。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RequeueReport {
    /// 被唤醒的等待者，按等待顺序。
    pub woken: Vec<WaiterId>,
    /// 被移动到目标队尾的等待者，按等待顺序（不含已在目标登记者）。
    pub moved: Vec<WaiterId>,
}

/// 一条地址映射：虚拟地址 -> 对象内位置。
#[derive(Clone, Copy, Debug)]
struct Mapping {
    object: ObjectId,
    offset: u64,
}

/// 等待者内部状态。
#[derive(Debug)]
struct Waiter {
    tid: Tid,
    /// 当前仍生效的登记位置（requeue 会把源位置替换为目标位置）。
    locations: Vec<LocKey>,
    /// 绝对截止时间（逻辑时间轴）。
    deadline: Option<Time>,
    /// 登记序号，用于同时截止时的确定性排序。
    seq: u64,
    outcome: Option<WaitOutcome>,
}

struct ObjectState {
    generation: Generation,
    /// 对象内偏移 -> 机器字值。
    words: HashMap<u64, u64>,
}

/// 等待队列模型内核。所有变更操作按调用顺序构成输入事件序列。
pub struct Kernel {
    /// 进程地址空间：虚拟地址 -> 映射。
    spaces: HashMap<Pid, HashMap<VAddr, Mapping>>,
    /// 存活对象。
    objects: HashMap<ObjectId, ObjectState>,
    /// 已销毁对象 id -> 其最后世代（复用时 +1）。
    retired: HashMap<ObjectId, Generation>,
    next_object: ObjectId,
    /// 等待队列：位置 -> FIFO 等待者。
    queues: BTreeMap<LocKey, VecDeque<WaiterId>>,
    waiters: HashMap<WaiterId, Waiter>,
    next_waiter: WaiterId,
    seq: u64,
    now: Time,
}

impl Default for Kernel {
    fn default() -> Self {
        Self::new()
    }
}

impl Kernel {
    pub fn new() -> Self {
        Kernel {
            spaces: HashMap::new(),
            objects: HashMap::new(),
            retired: HashMap::new(),
            next_object: 0,
            queues: BTreeMap::new(),
            waiters: HashMap::new(),
            next_waiter: 0,
            seq: 0,
            now: 0,
        }
    }

    // ---------- 对象与地址空间 ----------

    /// 创建共享对象，返回其 id。优先复用已销毁的 id（世代递增）。
    pub fn create_object(&mut self) -> ObjectId {
        // 取最小可复用 id，保证确定性。
        let reusable = self.retired.keys().min().copied();
        if let Some(id) = reusable {
            let generation = self.retired.remove(&id).unwrap() + 1;
            self.objects.insert(
                id,
                ObjectState { generation, words: HashMap::new() },
            );
            id
        } else {
            let id = self.next_object;
            self.next_object += 1;
            self.objects.insert(
                id,
                ObjectState { generation: 0, words: HashMap::new() },
            );
            id
        }
    }

    /// 销毁对象：其 id 之后可被 `create_object` 复用（世代 +1）。
    /// 已登记在旧世代位置上的等待者保留，只能被超时或取消终结。
    pub fn destroy_object(&mut self, object: ObjectId) -> bool {
        if let Some(state) = self.objects.remove(&object) {
            self.retired.insert(object, state.generation);
            true
        } else {
            false
        }
    }

    /// 把进程 `pid` 的虚拟地址 `addr` 映射到 `object` 内偏移 `offset`。
    /// 重复映射同一地址等价于解除旧映射后重新映射。
    pub fn map(&mut self, pid: Pid, addr: VAddr, object: ObjectId, offset: u64) {
        assert!(self.objects.contains_key(&object), "map: object not alive");
        self.spaces
            .entry(pid)
            .or_default()
            .insert(addr, Mapping { object, offset });
    }

    /// 解除映射。
    pub fn unmap(&mut self, pid: Pid, addr: VAddr) -> bool {
        self.spaces
            .get_mut(&pid)
            .is_some_and(|space| space.remove(&addr).is_some())
    }

    /// 把虚拟地址解析为规范位置身份；未映射返回 `None`。
    pub fn resolve(&self, pid: Pid, addr: VAddr) -> Option<LocKey> {
        let mapping = self.spaces.get(&pid)?.get(&addr)?;
        let state = self.objects.get(&mapping.object)?;
        Some(LocKey {
            object: mapping.object,
            offset: mapping.offset,
            generation: state.generation,
        })
    }

    // ---------- 共享内存读写 ----------

    /// 写一个机器字。
    pub fn write_word(&mut self, pid: Pid, addr: VAddr, value: u64) -> Result<(), WaitError> {
        let key = self
            .resolve(pid, addr)
            .ok_or(WaitError::Unmapped { addr })?;
        self.objects
            .get_mut(&key.object)
            .expect("resolved object must be alive")
            .words
            .insert(key.offset, value);
        Ok(())
    }

    /// 读一个机器字（未写入过为 0）。
    pub fn read_word(&self, pid: Pid, addr: VAddr) -> Result<u64, WaitError> {
        let key = self
            .resolve(pid, addr)
            .ok_or(WaitError::Unmapped { addr })?;
        Ok(self
            .objects
            .get(&key.object)
            .expect("resolved object must be alive")
            .words
            .get(&key.offset)
            .copied()
            .unwrap_or(0))
    }

    // ---------- 等待 / 唤醒 / 取消 / 超时 ----------

    /// 等待调用：`locations` 为（虚拟地址， 期望值）列表。
    ///
    /// 先解析全部地址并逐一比较当前值，全部相等才把等待者**原子地**登记到
    /// 每个位置队列的尾部；任一不匹配则不产生任何登记。
    /// `deadline` 为绝对截止时间（逻辑时间轴）。
    pub fn wait(
        &mut self,
        tid: Tid,
        pid: Pid,
        locations: &[(VAddr, u64)],
        deadline: Option<Time>,
    ) -> Result<WaiterId, WaitError> {
        assert!(!locations.is_empty(), "wait: need at least one location");
        // 第一阶段：解析 + 比较，不产生副作用。
        let mut keys = Vec::with_capacity(locations.len());
        for &(addr, expected) in locations {
            let key = self
                .resolve(pid, addr)
                .ok_or(WaitError::Unmapped { addr })?;
            let actual = self
                .objects
                .get(&key.object)
                .expect("resolved object must be alive")
                .words
                .get(&key.offset)
                .copied()
                .unwrap_or(0);
            if actual != expected {
                return Err(WaitError::ValueMismatch { addr, expected, actual });
            }
            if !keys.contains(&key) {
                keys.push(key);
            }
        }
        // 第二阶段：原子登记到所有位置队尾。
        let id = self.next_waiter;
        self.next_waiter += 1;
        self.seq += 1;
        let waiter = Waiter {
            tid,
            locations: keys.clone(),
            deadline,
            seq: self.seq,
            outcome: None,
        };
        self.waiters.insert(id, waiter);
        for key in keys {
            self.queues.entry(key).or_default().push_back(id);
        }
        Ok(id)
    }

    /// 唤醒 `pid` 视角下 `addr` 处队列的前 `count` 个等待者（FIFO）。
    /// 返回被唤醒者 id（按等待顺序）。地址未映射时返回空。
    pub fn wake(&mut self, pid: Pid, addr: VAddr, count: usize) -> Vec<WaiterId> {
        let Some(key) = self.resolve(pid, addr) else {
            return Vec::new();
        };
        let mut woken = Vec::new();
        for _ in 0..count {
            let Some(id) = self.queues.get_mut(&key).and_then(VecDeque::pop_front)
            else {
                break;
            };
            // 队列里可能残留已完成等待者的条目已被同步清除，这里一定是挂起的。
            self.complete(id, WaitOutcome::Woken);
            woken.push(id);
        }
        woken
    }

    /// 取消一个等待者。已完成（唤醒/超时/取消）时返回 `false`。
    pub fn cancel(&mut self, id: WaiterId) -> bool {
        if self.is_pending(id) {
            self.complete(id, WaitOutcome::Cancelled);
            true
        } else {
            false
        }
    }

    /// 推进逻辑时间到 `now`，按（截止时间， 登记序号）顺序触发超时。
    /// 返回本次超时的等待者 id。时间不允许回退。
    pub fn advance_time(&mut self, now: Time) -> Vec<WaiterId> {
        assert!(now >= self.now, "time must not go backwards");
        self.now = now;
        let mut expired: Vec<(Time, u64, WaiterId)> = self
            .waiters
            .iter()
            .filter(|(_, w)| w.outcome.is_none())
            .filter_map(|(&id, w)| w.deadline.map(|d| (d, w.seq, id)))
            .filter(|&(d, _, _)| d <= now)
            .collect();
        expired.sort_unstable();
        let mut out = Vec::new();
        for (_, _, id) in expired {
            self.complete(id, WaitOutcome::TimedOut);
            out.push(id);
        }
        out
    }

    /// 移交：先核对 `pid` 视角下源位置 `src` 的值等于 `expected`，
    /// 然后唤醒源队列前 `wake_count` 项，再把随后至多 `move_count` 项
    /// 移到目标 `dst` 队尾。已在目标登记的同一等待者不重复排队（仅从源
    /// 队列摘除其源登记）。等待者的绝对截止时间不变。
    pub fn requeue(
        &mut self,
        pid: Pid,
        src: VAddr,
        expected: u64,
        wake_count: usize,
        move_count: usize,
        dst: VAddr,
    ) -> Result<RequeueReport, RequeueError> {
        let src_key = self
            .resolve(pid, src)
            .ok_or(RequeueError::Unmapped { addr: src })?;
        let dst_key = self
            .resolve(pid, dst)
            .ok_or(RequeueError::Unmapped { addr: dst })?;
        let actual = self
            .objects
            .get(&src_key.object)
            .expect("resolved object must be alive")
            .words
            .get(&src_key.offset)
            .copied()
            .unwrap_or(0);
        if actual != expected {
            return Err(RequeueError::SourceMismatch { expected, actual });
        }

        // 唤醒前 wake_count 项（FIFO）。
        let mut woken = Vec::new();
        for _ in 0..wake_count {
            let Some(id) = self
                .queues
                .get_mut(&src_key)
                .and_then(VecDeque::pop_front)
            else {
                break;
            };
            self.complete(id, WaitOutcome::Woken);
            woken.push(id);
        }

        // 移动随后至多 move_count 项到目标队尾。
        let mut moved = Vec::new();
        for _ in 0..move_count {
            let Some(id) = self
                .queues
                .get_mut(&src_key)
                .and_then(VecDeque::pop_front)
            else {
                break;
            };
            let waiter = self.waiters.get_mut(&id).expect("queued waiter exists");
            // 从等待者的登记位置中移除源位置。
            waiter.locations.retain(|&k| k != src_key);
            if waiter.locations.contains(&dst_key) {
                // 同一等待者已在目标登记：不重复排队，仅解除源登记。
                continue;
            }
            waiter.locations.push(dst_key);
            self.queues.entry(dst_key).or_default().push_back(id);
            moved.push(id);
        }

        Ok(RequeueReport { woken, moved })
    }

    // ---------- 观测 ----------

    /// 等待者所属线程。
    pub fn tid_of(&self, id: WaiterId) -> Option<Tid> {
        self.waiters.get(&id).map(|w| w.tid)
    }

    /// 等待者是否仍挂起。
    pub fn is_pending(&self, id: WaiterId) -> bool {
        self.waiters
            .get(&id)
            .is_some_and(|w| w.outcome.is_none())
    }

    /// 等待者的唯一最终结果；仍挂起时为 `None`。
    pub fn outcome(&self, id: WaiterId) -> Option<WaitOutcome> {
        self.waiters.get(&id).and_then(|w| w.outcome)
    }

    /// 等待者当前仍生效的登记位置。
    pub fn registrations(&self, id: WaiterId) -> Vec<LocKey> {
        self.waiters
            .get(&id)
            .map(|w| w.locations.clone())
            .unwrap_or_default()
    }

    /// 某位置队列当前的等待者快照（按等待顺序）。
    pub fn queue_snapshot(&self, key: LocKey) -> Vec<WaiterId> {
        self.queues
            .get(&key)
            .map(|q| q.iter().copied().collect())
            .unwrap_or_default()
    }

    /// 当前逻辑时间。
    pub fn now(&self) -> Time {
        self.now
    }

    // ---------- 内部 ----------

    /// 终结等待者：记录唯一结果，并解除其在**所有**登记处的排队。
    fn complete(&mut self, id: WaiterId, outcome: WaitOutcome) {
        let locations = {
            let waiter = self.waiters.get_mut(&id).expect("complete: waiter exists");
            debug_assert!(waiter.outcome.is_none(), "waiter completed twice");
            waiter.outcome = Some(outcome);
            std::mem::take(&mut waiter.locations)
        };
        for key in locations {
            if let Some(queue) = self.queues.get_mut(&key) {
                queue.retain(|&queued| queued != id);
            }
        }
    }
}
