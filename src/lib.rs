//! # rngflow —— 可重算的随机流分配器
//!
//! 训练中的一次前向可能因为激活重算而再次执行，同一批样本也可能被调度到不同
//! rank 上处理。本 crate 用**计数器模式（counter mode）**分配随机流：
//!
//! * 每个随机字都是纯函数
//!   `word(实验种子, 全局步, 样本ID, 算子路径, 逻辑调用序号, 抽样用途, 抽样位置, 候选序号)`
//!   的输出，不存在任何全局可变 RNG 状态；
//! * 实际 rank、回包顺序、一次执行被拆成几段都不参与取值，因此无法影响结果；
//! * 同一逻辑调用的重试 / 重算从相同坐标出发，必然得到相同序列；
//! * 每个抽样位置可以拒绝任意多个候选随机字（拒绝采样），被接受的值只由该位置
//!   自己的候选序号决定，跳过一行或取消部分批次不会改变其他位置接受的值；
//! * 确认点只持久化“各位置接受时已拒绝的候选数”，运行时可据此恢复取值进度，
//!   也可以从明确给出的逻辑位置直接重新计算掩码。
//!
//! 模块划分：
//! * [`rng`]：确定性混合函数、流坐标 [`rng::StreamKey`] 与纯函数随机字；
//! * [`cursor`]：抽样游标、逻辑调用进度 [`cursor::CallProgress`] 与重算入口；
//! * [`checkpoint`]：确认点 [`checkpoint::Checkpoint`] 及其确定性二进制编解码。

#![deny(unsafe_code)]

pub mod checkpoint;
pub mod cursor;
pub mod rng;

pub use checkpoint::{Checkpoint, CheckpointError};
pub use cursor::{
    recompute_mask_bit, recompute_word, CallProgress, ProgressCursor, StreamCursor,
};
pub use rng::{candidate_word, fnv1a_64, mix64, word_at, Purpose, StreamKey};
