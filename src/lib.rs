//! # 热键切块的全外连接执行算子
//!
//! 全外连接中少数大键（热键）可能占满单个执行节点的内存。本 crate 把热键
//! 两侧的行分别切成不超过给定行数的小块，再以「左块 × 右块」的块对为单位
//! 调度任务：
//!
//! - 每个任务只持有一个左块和一个右块，单任务输入行数有明确上界；
//! - 块对之间没有顺序依赖，可以乱序完成；
//! - 同一块对的失败尝试可以被新尝试取代，迟到/过期回调一律丢弃；
//! - 只有已确认尝试产出的匹配对和逐行命中记录会被采纳，且每对匹配只输出一次；
//! - 一个块中未命中的行，必须等该块参与的所有块对都确认后，才能走补空路径；
//! - 空键（`key == None`）不参与块对调度，各自直接走未匹配路径。
//!
//! 模块划分：
//!
//! - [`row`]：输入行 [`Row`] 与全外连接输出行 [`JoinOut`]；
//! - [`block`]：行块切分函数 [`partition`] 与块结构 [`Block`]；
//! - [`operator`]：调度算子 [`FullOuterJoin`]，负责切块、块对状态机、回调确认与终态输出；
//! - [`executor`]：模拟执行节点 [`SimulatedNode`]，对块对做真实的嵌套循环连接并应用附加条件。

pub mod block;
pub mod executor;
pub mod operator;
pub mod row;

pub use block::{partition, Block};
pub use executor::SimulatedNode;
pub use operator::{
    AttemptId, CallbackStatus, FullOuterJoin, LeftBlockId, Metrics, PairId, PairResult,
    RightBlockId, Task,
};
pub use row::{JoinOut, Row};
