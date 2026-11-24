//! # persistent-miniheap
//!
//! 持久内存上的小型对象堆：
//! - [`medium`]：可注入断电的持久介质后端，字对齐原子写、显式刷行、
//!   屏障语义、合法断电像枚举与落盘顺序反推；
//! - [`heap`]：根指针 / 变长对象 / 空闲表，带代次句柄的对象替换
//!   与回收协议，以及崩溃恢复与独立遍历审计。

pub mod heap;
pub mod medium;

pub use heap::{Audit, Handle, HeapError, ObjectView, PersistentHeap, RecoveryReport};
pub use medium::{
    CrashCut, CrashImage, CrashRegion, Event, FlushEvent, LineImage, OrderStrategy,
    PersistentMedia, ReconstructError, Reconstruction, LINE_BYTES, LINE_WORDS, WORD_BYTES,
};
