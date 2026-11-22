//! # namespace-fs
//!
//! 一个纯内存、纯标准库实现的文件系统命名空间模型，支持：
//!
//! - 普通文件的硬链接（[`fs::FileSystem::link`]）与链接计数；
//! - 打开后通过固定 inode 的句柄读取（[`fs::FileSystem::open`] /
//!   [`fs::FileSystem::read_handle`]），链接删光后句柄仍可读；
//! - 两个目录项的原子交换（[`fs::FileSystem::swap`]），可跨目录，
//!   只改目录项引用的 inode，并拒绝会导致目录祖先成环的交换；
//! - inode 仅在硬链接与打开引用同时归零后才真实回收，号可复用；
//! - 所有结构性变更（含目录交换、链接计数、inode 释放/复用）写入
//!   预写日志（[`journal::Journal`]），提交标记前后中断都能重放为
//!   一致的命名空间。
//!
//! 模块入口与常用类型见 [`fs::FileSystem`]。

pub mod fs;
pub mod journal;
mod path;

pub use fs::{
    FileSystem, FsError, Handle, Inode, InodeInfo, NodeKind,
};
pub use journal::{CrashPoint, Journal, Record, SimulatedCrash};

/// 根目录固定使用的 inode 编号。
pub const ROOT_INO: u64 = 1;
