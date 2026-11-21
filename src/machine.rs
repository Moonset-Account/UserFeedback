//! 小机器的顺序执行模型。
//!
//! 机器状态是位置到机器字的映射；执行器逐条搬移，并强制：
//! - 只允许寄存器->寄存器、寄存器->栈槽、栈槽->寄存器三种等宽复制；
//! - 读写的位置必须在状态中有定义；
//! - 每条指令的宽度必须与执行宽度一致。
//!
//! 这个模拟器是测试与验证的"真实机器"：展开结果不是靠预置输出比对，
//! 而是真正逐条执行后检查最终状态是否满足并行复制语义。

use crate::{Loc, LocKind, Move};
use std::collections::HashMap;

/// 一个机器字。内部值对测试透明，仅用于区分不同位置的初值。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Word(pub u64);

/// 机器状态：寄存器与栈槽统一编址在一张位置表中。
#[derive(Debug, Clone, Default)]
pub struct State {
    pub memory: HashMap<Loc, Word>,
}

impl State {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }
}

/// 执行一条搬移计划时可能出现的错误。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MachineError {
    /// 机器不支持的搬移形式（唯一被禁止的是栈槽->栈槽）。
    UnsupportedMove(Move),
    /// 读取了未在状态中定义的位置。
    UndefinedLocation(Loc),
    /// 指令宽度与执行宽度不一致。
    WidthMismatch { expected: u32, got: u32 },
}

impl std::fmt::Display for MachineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnsupportedMove(m) => write!(f, "machine cannot execute stack-to-stack move: {m:?}"),
            Self::UndefinedLocation(loc) => write!(f, "access to undefined location: {loc:?}"),
            Self::WidthMismatch { expected, got } => {
                write!(f, "move width {got} does not match machine width {expected}")
            }
        }
    }
}

impl std::error::Error for MachineError {}

/// 在给定机器状态上顺序执行 `moves`，原地更新状态。
///
/// # 错误
///
/// 任一指令非法、宽度不符或访问未定义位置时返回错误，
/// 此时状态保留失败指令执行前的样子（该指令不会生效）。
pub fn execute(
    state: &mut State,
    moves: &[Move],
    width: u32,
) -> Result<(), MachineError> {
    for m in moves {
        if m.width != width {
            return Err(MachineError::WidthMismatch {
                expected: width,
                got: m.width,
            });
        }
        if matches!((m.from.kind, m.to.kind), (LocKind::Stack, LocKind::Stack)) {
            return Err(MachineError::UnsupportedMove(*m));
        }
        let value = *state
            .memory
            .get(&m.from)
            .ok_or(MachineError::UndefinedLocation(m.from))?;
        let slot = state
            .memory
            .get_mut(&m.to)
            .ok_or(MachineError::UndefinedLocation(m.to))?;
        *slot = value;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Loc, Move};

    #[test]
    fn register_to_register_copies_value() {
        let mut st = State::new();
        st.memory.insert(Loc::reg(0), Word(42));
        st.memory.insert(Loc::reg(1), Word(7));
        execute(&mut st, &[Move::new(Loc::reg(0), Loc::reg(1), 8)], 8).unwrap();
        assert_eq!(st.memory[&Loc::reg(1)], Word(42));
        assert_eq!(st.memory[&Loc::reg(0)], Word(42));
    }

    #[test]
    fn stack_round_trip_through_register() {
        let mut st = State::new();
        st.memory.insert(Loc::stack(0), Word(9));
        st.memory.insert(Loc::reg(9), Word(0));
        st.memory.insert(Loc::stack(1), Word(0));
        execute(
            &mut st,
            &[
                Move::new(Loc::stack(0), Loc::reg(9), 8),
                Move::new(Loc::reg(9), Loc::stack(1), 8),
            ],
            8,
        )
        .unwrap();
        assert_eq!(st.memory[&Loc::stack(1)], Word(9));
    }

    #[test]
    fn stack_to_stack_is_rejected_by_machine() {
        let mut st = State::new();
        st.memory.insert(Loc::stack(0), Word(1));
        st.memory.insert(Loc::stack(1), Word(2));
        let err = execute(
            &mut st,
            &[Move::new(Loc::stack(0), Loc::stack(1), 8)],
            8,
        )
        .unwrap_err();
        assert!(matches!(err, MachineError::UnsupportedMove(_)));
        // 失败指令不得生效。
        assert_eq!(st.memory[&Loc::stack(1)], Word(2));
    }
}
