//! 合成栈回溯模块。
//!
//! 读取合成寄存器、分段栈内存与紧凑展开表，按指令半开范围 `[start, end)`
//! 选择展开规则，逐帧恢复调用链：
//!
//! - **普通帧**：返回地址指向调用指令的下一条指令，因此查表定位“前一条
//!   调用指令”时使用 `ra - 1`。
//! - **异步事件帧**：事件可能切换到备用栈并保存一整份被打断的上下文。
//!   跨过这一帧时必须精确恢复被打断的指令指针与栈指针（以及所在栈段），
//!   **不**套用普通调用的 `ra - 1` 规则。
//! - 每次取数都校验对应栈段边界；重复的 `(ip, sp)` 组合判定为回路并停止。
//! - 支持最多两级嵌套异步帧，超出即停止并报告。

use std::collections::HashSet;
use std::fmt;

/// 地址类型（合成 64 位地址空间）。
pub type Addr = u64;

/// 栈段标识。
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub enum StackSeg {
    /// 主栈。
    Main,
    /// 异步事件切换到的备用栈。
    Alt,
}

impl StackSeg {
    fn from_raw(raw: u64) -> Option<StackSeg> {
        match raw {
            0 => Some(StackSeg::Main),
            1 => Some(StackSeg::Alt),
            _ => None,
        }
    }

    fn to_raw(self) -> u64 {
        match self {
            StackSeg::Main => 0,
            StackSeg::Alt => 1,
        }
    }
}

/// 合成寄存器组：指令指针、栈指针及其所在栈段。
#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
pub struct Registers {
    pub ip: Addr,
    pub sp: Addr,
    pub seg: StackSeg,
}

/// 取数越界错误。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct MemFault {
    pub seg: StackSeg,
    pub addr: Addr,
    pub len: u64,
}

impl fmt::Display for MemFault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "read of {} bytes at {:#x} is outside segment {:?}",
            self.len, self.addr, self.seg
        )
    }
}

/// 一段命名的栈内存，`[base, base + bytes.len())` 半开区间为合法范围。
pub struct StackSegment {
    pub seg: StackSeg,
    pub base: Addr,
    pub bytes: Vec<u8>,
}

impl StackSegment {
    pub fn new(seg: StackSeg, base: Addr, size: usize) -> Self {
        StackSegment {
            seg,
            base,
            bytes: vec![0u8; size],
        }
    }

    /// 在段内偏移处写入一个 u64（小端），越界返回错误。
    pub fn write_u64(&mut self, addr: Addr, value: u64) -> Result<(), MemFault> {
        self.check(addr, 8)?;
        let off = (addr - self.base) as usize;
        self.bytes[off..off + 8].copy_from_slice(&value.to_le_bytes());
        Ok(())
    }

    fn check(&self, addr: Addr, len: u64) -> Result<(), MemFault> {
        let end = addr.checked_add(len).ok_or(MemFault {
            seg: self.seg,
            addr,
            len,
        })?;
        if addr < self.base || end > self.base + self.bytes.len() as u64 {
            return Err(MemFault {
                seg: self.seg,
                addr,
                len,
            });
        }
        Ok(())
    }
}

/// 分段栈内存：每次取数都必须落在对应栈段界内。
pub struct Memory {
    segments: Vec<StackSegment>,
}

impl Memory {
    pub fn new(segments: Vec<StackSegment>) -> Self {
        Memory { segments }
    }

    /// 在指定栈段内读取一个 u64；越界返回 `MemFault`。
    pub fn read_u64(&self, seg: StackSeg, addr: Addr) -> Result<u64, MemFault> {
        let segment = self
            .segments
            .iter()
            .find(|s| s.seg == seg)
            .ok_or(MemFault { seg, addr, len: 8 })?;
        segment.check(addr, 8)?;
        let off = (addr - segment.base) as usize;
        let mut buf = [0u8; 8];
        buf.copy_from_slice(&segment.bytes[off..off + 8]);
        Ok(u64::from_le_bytes(buf))
    }
}

/// 展开规则：如何定位上一帧及返回地址。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum UnwindRule {
    /// 普通调用帧。
    ///
    /// 调用者栈指针 `caller_sp = sp + cfa_offset`，返回地址保存在当前栈段
    /// 的 `[caller_sp - 8]`。返回地址指向调用指令的下一条指令，查表定位
    /// 前一条调用指令时使用 `ra - 1`。
    Normal { cfa_offset: u64 },
    /// 异步事件帧。
    ///
    /// 事件切换到备用栈后，把一整份被打断的上下文保存在当前栈段中：
    /// `sp + ip_off` 处为被打断的指令指针，`sp + sp_off` 处为被打断的栈
    /// 指针，`sp + seg_off` 处为其所在栈段（0 = Main, 1 = Alt）。
    /// 跨过这一帧时精确恢复这三个值，**不**套用 `ra - 1`。
    Async {
        ip_off: u64,
        sp_off: u64,
        seg_off: u64,
    },
}

/// 紧凑展开表条目：指令半开范围 `[start, end)` 与对应规则。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct UnwindEntry {
    pub start: Addr,
    pub end: Addr,
    pub rule: UnwindRule,
}

/// 已确认的一帧。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Frame {
    pub index: usize,
    pub ip: Addr,
    pub sp: Addr,
    pub seg: StackSeg,
    /// 该帧命中的是普通帧规则还是异步事件帧规则。
    pub rule: UnwindRule,
}

/// 首次停止原因。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum StopReason {
    /// 返回地址为 0：到达调用链顶端，正常结束。
    EndOfChain,
    /// 当前指令指针不在任何展开范围内。
    NoUnwindInfo { ip: Addr },
    /// 取数越过对应栈段边界。
    MemoryFault(MemFault),
    /// 指令指针与栈指针组合重复，判定为回路。
    Cycle { ip: Addr, sp: Addr },
    /// 嵌套异步帧超过支持的最大级数。
    AsyncDepthExceeded { depth: usize },
    /// 异步帧保存的栈段编号非法。
    InvalidSavedSegment { raw: u64 },
    /// 达到帧数上限（防御性停止）。
    MaxFrames,
}

/// 一次回溯的结果：可确认的帧与首次停止原因。
#[derive(Clone, Debug)]
pub struct WalkResult {
    pub frames: Vec<Frame>,
    pub stop: StopReason,
}

/// 回溯器。
pub struct Unwinder<'a> {
    mem: &'a Memory,
    /// 按 `start` 升序排列的紧凑展开表。
    table: &'a [UnwindEntry],
    /// 支持的最大嵌套异步帧级数（题目要求两级）。
    max_async_depth: usize,
    /// 防御性帧数上限。
    max_frames: usize,
}

impl<'a> Unwinder<'a> {
    pub fn new(mem: &'a Memory, table: &'a [UnwindEntry]) -> Self {
        Unwinder {
            mem,
            table,
            max_async_depth: 2,
            max_frames: 1024,
        }
    }

    /// 在展开表中查找包含 `ip` 的半开范围条目。
    fn lookup(&self, ip: Addr) -> Option<&UnwindEntry> {
        self.table
            .iter()
            .find(|e| e.start <= ip && ip < e.end)
    }

    /// 从 `start` 寄存器状态开始回溯，返回可确认的帧与首次停止原因。
    pub fn walk(&self, start: &Registers) -> WalkResult {
        let mut frames = Vec::new();
        let mut seen: HashSet<(Addr, Addr)> = HashSet::new();
        let mut cur = *start;
        let mut async_depth = 0usize;

        let stop = loop {
            if frames.len() >= self.max_frames {
                break StopReason::MaxFrames;
            }
            // 重复的 (ip, sp) 组合：回路。
            if !seen.insert((cur.ip, cur.sp)) {
                break StopReason::Cycle {
                    ip: cur.ip,
                    sp: cur.sp,
                };
            }
            let entry = match self.lookup(cur.ip) {
                Some(e) => e,
                None => break StopReason::NoUnwindInfo { ip: cur.ip },
            };
            frames.push(Frame {
                index: frames.len(),
                ip: cur.ip,
                sp: cur.sp,
                seg: cur.seg,
                rule: entry.rule,
            });
            match entry.rule {
                UnwindRule::Normal { cfa_offset } => {
                    let caller_sp = cur.sp + cfa_offset;
                    let ra = match self.mem.read_u64(cur.seg, caller_sp - 8) {
                        Ok(v) => v,
                        Err(f) => break StopReason::MemoryFault(f),
                    };
                    if ra == 0 {
                        break StopReason::EndOfChain;
                    }
                    // 普通调用：返回地址指向调用指令的下一条，定位前一条
                    // 调用指令用 ra - 1；栈段不变。
                    cur = Registers {
                        ip: ra - 1,
                        sp: caller_sp,
                        seg: cur.seg,
                    };
                }
                UnwindRule::Async {
                    ip_off,
                    sp_off,
                    seg_off,
                } => {
                    async_depth += 1;
                    if async_depth > self.max_async_depth {
                        break StopReason::AsyncDepthExceeded {
                            depth: async_depth,
                        };
                    }
                    // 异步事件帧：精确恢复被打断的 ip / sp / 栈段，
                    // 不做 ra - 1。
                    let ip = match self.mem.read_u64(cur.seg, cur.sp + ip_off) {
                        Ok(v) => v,
                        Err(f) => break StopReason::MemoryFault(f),
                    };
                    let sp = match self.mem.read_u64(cur.seg, cur.sp + sp_off) {
                        Ok(v) => v,
                        Err(f) => break StopReason::MemoryFault(f),
                    };
                    let raw_seg = match self.mem.read_u64(cur.seg, cur.sp + seg_off) {
                        Ok(v) => v,
                        Err(f) => break StopReason::MemoryFault(f),
                    };
                    let seg = match StackSeg::from_raw(raw_seg) {
                        Some(s) => s,
                        None => break StopReason::InvalidSavedSegment { raw: raw_seg },
                    };
                    cur = Registers { ip, sp, seg };
                }
            }
        };

        WalkResult { frames, stop }
    }
}

// 供测试与外部调用方构造合成上下文使用的辅助函数。
impl StackSegment {
    /// 写入一份异步事件保存的被打断上下文。
    pub fn write_saved_context(
        &mut self,
        at: Addr,
        ip: Addr,
        sp: Addr,
        seg: StackSeg,
    ) -> Result<(), MemFault> {
        self.write_u64(at, ip)?;
        self.write_u64(at + 8, sp)?;
        self.write_u64(at + 16, seg.to_raw())?;
        Ok(())
    }
}
