//! 并行复制（parallel copy）展开器。
//!
//! 输入是一组在基本块衔接处**应当同时发生**的等宽赋值：
//! 每个目的位置至多写一次，同一源位置可被多个目的读取。
//! 本模块把它展开成小机器可以逐条执行的顺序 [`Move`] 指令。
//!
//! # 小机器约束
//!
//! 只允许三种等宽复制：
//! - 寄存器 -> 寄存器
//! - 寄存器 -> 栈槽
//! - 栈槽   -> 寄存器
//!
//! 因此栈槽到栈槽的搬移必须经额外寄存器拆成两条指令。
//!
//! # 展开策略
//!
//! 以"目的 -> 源"构造依赖图。一个目的在它的旧值不再被任何
//! 尚未处理的目的读取时（入度为 0）即可安全搬移，先剥离所有
//! 无环节点；剥离结束后剩余节点全部位于环上，再用额外寄存器
//! （必要时配合额外栈槽）破环：
//!
//! - 无环输入永远不会使用额外栈槽；
//! - 纯寄存器环也不会使用额外栈槽；
//! - 只有环内存在连续两个栈槽（需要 stack->stack 中转）时，
//!   才把暂存寄存器中的破环值溢出到额外栈槽。
//!
//! 不属于目的集合的普通位置（旁观者）的值始终被保留。

use std::collections::{HashMap, HashSet, VecDeque};

pub mod machine;

/// 位置种类：机器寄存器或栈槽。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum LocKind {
    /// 机器寄存器。
    Reg,
    /// 栈槽。
    Stack,
}

/// 一个可寻址的机器位置（寄存器编号或栈槽编号）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Loc {
    pub kind: LocKind,
    pub id: u32,
}

impl Loc {
    /// 构造一个寄存器位置。
    #[must_use]
    pub const fn reg(id: u32) -> Self {
        Self {
            kind: LocKind::Reg,
            id,
        }
    }

    /// 构造一个栈槽位置。
    #[must_use]
    pub const fn stack(id: u32) -> Self {
        Self {
            kind: LocKind::Stack,
            id,
        }
    }

    /// 是否为栈槽。
    #[must_use]
    pub const fn is_stack(self) -> bool {
        matches!(self.kind, LocKind::Stack)
    }
}

/// 一条并行赋值：`dest <- src`，宽度为 `width` 字节。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Assignment {
    pub dest: Loc,
    pub src: Loc,
    pub width: u32,
}

impl Assignment {
    #[must_use]
    pub const fn new(dest: Loc, src: Loc, width: u32) -> Self {
        Self { dest, src, width }
    }
}

/// 一组同时生效的等宽并行复制。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParallelCopy {
    pub width: u32,
    pub assignments: Vec<Assignment>,
}

impl ParallelCopy {
    /// 构造一组宽度统一为 `width` 的并行复制。
    #[must_use]
    pub fn new(width: u32, assignments: Vec<Assignment>) -> Self {
        Self { width, assignments }
    }
}

/// 一条顺序搬移指令。所有指令均为小机器合法的等宽复制。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Move {
    pub from: Loc,
    pub to: Loc,
    pub width: u32,
}

impl Move {
    #[must_use]
    pub const fn new(from: Loc, to: Loc, width: u32) -> Self {
        Self { from, to, width }
    }

    /// 该搬移是否为小机器允许的三种形式之一。
    #[must_use]
    pub const fn is_legal(&self) -> bool {
        // 栈 -> 栈 是唯一被禁止的形式；自赋值无意义但机器可执行，
        // 展开器本身会在输入阶段去掉自赋值。
        !matches!((self.from.kind, self.to.kind), (LocKind::Stack, LocKind::Stack))
    }
}

/// 代码生成器可支配的一个额外寄存器与一个额外栈槽。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Scratch {
    pub reg: Loc,
    pub stack: Loc,
}

impl Scratch {
    /// 以给定编号创建额外寄存器 `rX` 与额外栈槽 `sY`。
    #[must_use]
    pub const fn new(reg_id: u32, stack_id: u32) -> Self {
        Self {
            reg: Loc::reg(reg_id),
            stack: Loc::stack(stack_id),
        }
    }
}

/// 展开结果：可直接在小机器上顺序执行的指令计划。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Plan {
    /// 顺序指令；每条都是合法的等宽复制。
    pub moves: Vec<Move>,
    /// 执行过程中是否用到了额外寄存器。
    pub used_scratch_reg: bool,
    /// 执行过程中是否用到了额外栈槽。
    pub used_scratch_stack: bool,
    /// 输入中被去掉的自赋值数量。
    pub dropped_self_assignments: usize,
}

/// 展开失败的原因。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExpandError {
    /// 复制宽度为 0。
    InvalidWidth(u32),
    /// 某条赋值宽度与并行复制的统一宽度不一致。
    WidthMismatch { expected: u32, got: u32 },
    /// 额外寄存器位置被声明成了栈槽，或额外栈槽被声明成了寄存器。
    BadScratchLocation(Scratch),
    /// 同一个目的位置出现了多次（并行复制要求每个目的只写一次）。
    DuplicateDestination(Loc),
    /// 额外寄存器/栈槽与输入中的普通位置冲突。
    ScratchCollision(Loc),
    /// 展开器内部产生了非法指令（正常输入不会触发，属于防御性检查）。
    IllegalMove(Move),
}

impl std::fmt::Display for ExpandError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidWidth(w) => write!(f, "copy width must be non-zero, got {w}"),
            Self::WidthMismatch { expected, got } => write!(
                f,
                "assignment width {got} does not match parallel copy width {expected}"
            ),
            Self::BadScratchLocation(s) => {
                write!(f, "scratch locations have wrong kinds: {s:?}")
            }
            Self::DuplicateDestination(loc) => {
                write!(f, "destination written more than once: {loc:?}")
            }
            Self::ScratchCollision(loc) => {
                write!(f, "scratch location collides with an input location: {loc:?}")
            }
            Self::IllegalMove(m) => write!(f, "expander produced an illegal move: {m:?}"),
        }
    }
}

impl std::error::Error for ExpandError {}

/// 把并行复制展开为顺序搬移计划。
///
/// # 错误
///
/// 见 [`ExpandError`]：宽度非法、目的重复、暂存位置冲突等情况返回错误。
pub fn expand(copy: &ParallelCopy, scratch: &Scratch) -> Result<Plan, ExpandError> {
    if copy.width == 0 {
        return Err(ExpandError::InvalidWidth(0));
    }
    if scratch.reg.kind != LocKind::Reg || scratch.stack.kind != LocKind::Stack {
        return Err(ExpandError::BadScratchLocation(*scratch));
    }

    // 1. 校验：宽度一致；去掉自赋值；目的唯一；暂存位置不与普通位置冲突。
    let mut dropped_self_assignments = 0usize;
    let mut source_of: HashMap<Loc, Loc> = HashMap::new();
    let mut input_locs: HashSet<Loc> = HashSet::new();
    for a in &copy.assignments {
        if a.width != copy.width {
            return Err(ExpandError::WidthMismatch {
                expected: copy.width,
                got: a.width,
            });
        }
        input_locs.insert(a.dest);
        input_locs.insert(a.src);
        if a.dest == a.src {
            dropped_self_assignments += 1;
            continue;
        }
        if source_of.insert(a.dest, a.src).is_some() {
            return Err(ExpandError::DuplicateDestination(a.dest));
        }
    }
    if input_locs.contains(&scratch.reg) {
        return Err(ExpandError::ScratchCollision(scratch.reg));
    }
    if input_locs.contains(&scratch.stack) {
        return Err(ExpandError::ScratchCollision(scratch.stack));
    }

    let mut moves: Vec<Move> = Vec::new();
    let width = copy.width;

    // emit_move 负责把 stack->stack 拆成 stack->reg->stack 两条；
    // 注意这里传入的 reg 必须是当前空闲的寄存器。
    let mut emit = |from: Loc, to: Loc, free_reg: Loc, out: &mut Vec<Move>| {
        if from == to {
            return;
        }
        if from.is_stack() && to.is_stack() {
            out.push(Move::new(from, free_reg, width));
            out.push(Move::new(free_reg, to, width));
        } else {
            out.push(Move::new(from, to, width));
        }
    };

    // 2. 依赖图：dest -> src。remaining 中每个节点统计还有多少
    //    "未处理目的"把它当作源（入度）。入度为 0 的目的可以先搬。
    let mut remaining: HashSet<Loc> = source_of.keys().copied().collect();
    let mut pending_readers: HashMap<Loc, usize> = HashMap::new();
    for &src in source_of.values() {
        if source_of.contains_key(&src) {
            *pending_readers.entry(src).or_insert(0) += 1;
        }
    }
    let mut queue: VecDeque<Loc> = remaining
        .iter()
        .copied()
        .filter(|d| pending_readers.get(d).copied().unwrap_or(0) == 0)
        .collect();

    while let Some(dest) = queue.pop_front() {
        if !remaining.remove(&dest) {
            continue;
        }
        let src = source_of[&dest];
        // 无环阶段 scratch reg 从不持有存活值，可直接作为中转。
        emit(src, dest, scratch.reg, &mut moves);
        if let Some(count) = pending_readers.get_mut(&src) {
            *count -= 1;
            if *count == 0 && remaining.contains(&src) {
                queue.push_back(src);
            }
        }
    }

    // 3. 剩余节点全部在环上（每个节点入度恰为 1），逐环打破。
    while !remaining.is_empty() {
        let start = *remaining.iter().next().expect("non-empty remaining set");
        let cycle = extract_cycle(&start, &source_of, &remaining);
        for loc in &cycle {
            remaining.remove(loc);
        }
        break_cycle(
            &cycle,
            &source_of,
            *scratch,
            &mut moves,
            &mut emit,
        );
    }

    let plan = Plan {
        used_scratch_reg: moves.iter().any(|m| m.from == scratch.reg || m.to == scratch.reg),
        used_scratch_stack: moves
            .iter()
            .any(|m| m.from == scratch.stack || m.to == scratch.stack),
        moves,
        dropped_self_assignments,
    };
    for m in &plan.moves {
        if !m.is_legal() || m.width != width {
            return Err(ExpandError::IllegalMove(*m));
        }
    }
    Ok(plan)
}

/// 从 `start` 出发沿 src 边收集一个环。调用时 `start` 必须位于环上。
fn extract_cycle(
    start: &Loc,
    source_of: &HashMap<Loc, Loc>,
    remaining: &HashSet<Loc>,
) -> Vec<Loc> {
    let mut order: Vec<Loc> = Vec::new();
    let mut index: HashMap<Loc, usize> = HashMap::new();
    let mut cur = *start;
    while remaining.contains(&cur) && !index.contains_key(&cur) {
        index.insert(cur, order.len());
        order.push(cur);
        cur = source_of[&cur];
    }
    let at = index[&cur];
    order.split_off(at)
}

/// 用额外寄存器（必要时额外栈槽）打破一个环并发出全部搬移。
///
/// 环按 `cycle[i]` 的源是 `cycle[i-1]`（循环下标）排列，即
/// `cycle[0] <- cycle[last] <- ... <- cycle[0]`。
fn break_cycle(
    cycle: &[Loc],
    source_of: &HashMap<Loc, Loc>,
    scratch: Scratch,
    moves: &mut Vec<Move>,
    emit: &mut impl FnMut(Loc, Loc, Loc, &mut Vec<Move>),
) {
    debug_assert!(cycle.len() >= 2);
    // 最后一条边 cycle[0] <- cycle[last] 是否为栈到栈。
    let closing_stack_to_stack = *cycle.last().unwrap() != cycle[0]
        && cycle.last().unwrap().is_stack()
        && cycle[0].is_stack();
    let has_stack_to_stack = closing_stack_to_stack
        || cycle
            .windows(2)
            .any(|w| w[0].is_stack() && source_of[&w[0]].is_stack());

    let pivot = cycle[0];

    if !has_stack_to_stack {
        // 环内没有连续栈槽：暂存寄存器可以一直持有 pivot 的旧值。
        // scratch <- pivot
        emit(pivot, scratch.reg, scratch.reg, moves);
        // pivot <- src(pivot), ... 沿 src 边依次回填。
        let mut cur = pivot;
        for _ in 0..cycle.len() - 1 {
            let s = source_of[&cur];
            emit(s, cur, scratch.reg, moves);
            cur = s;
        }
        // 最后一个节点 <- 暂存的 pivot 旧值
        emit(scratch.reg, cur, scratch.reg, moves);
    } else {
        // 环内存在 stack->stack：中转栈槽会破坏暂存寄存器里的存活值，
        // 先把它溢出到额外栈槽，结束后再取回。
        emit(pivot, scratch.reg, scratch.reg, moves); // scratch_reg <- pivot
        emit(scratch.reg, scratch.stack, scratch.reg, moves); // scratch_stack <- pivot 旧值
        let mut cur = pivot;
        for _ in 0..cycle.len() - 1 {
            let s = source_of[&cur];
            emit(s, cur, scratch.reg, moves);
            cur = s;
        }
        emit(scratch.stack, scratch.reg, scratch.reg, moves); // 取回 pivot 旧值
        emit(scratch.reg, cur, scratch.reg, moves);
    }
}

#[cfg(test)]
mod tests {
    use super::machine::{execute, MachineError, State, Word};
    use super::*;

    const W: u32 = 8;
    const SCRATCH: Scratch = Scratch::new(100, 200);

    fn r(id: u32) -> Loc {
        Loc::reg(id)
    }
    fn s(id: u32) -> Loc {
        Loc::stack(id)
    }

    /// 每个位置一个互不相同的初值标记；暂存位置同样需要在机器状态中定义。
    fn initial_state(locs: &[Loc]) -> State {
        let mut st = State::new();
        for (i, loc) in locs.iter().enumerate() {
            st.memory.insert(
                *loc,
                Word(((i as u64) + 1).wrapping_mul(0x9E37_79B9_7F4A_7C15)),
            );
        }
        let base = locs.len() as u64 + 1;
        st.memory
            .insert(SCRATCH.reg, Word(base.wrapping_mul(0x9E37_79B9_7F4A_7C15)));
        st.memory.insert(
            SCRATCH.stack,
            Word((base + 1).wrapping_mul(0x9E37_79B9_7F4A_7C15)),
        );
        st
    }

    fn all_locs(copy: &ParallelCopy) -> Vec<Loc> {
        let mut locs: Vec<Loc> = Vec::new();
        for a in &copy.assignments {
            locs.push(a.dest);
            locs.push(a.src);
        }
        locs.sort_unstable();
        locs.dedup();
        locs
    }

    /// 执行计划并逐条校验并行语义：
    /// 目的得到其源的*初始*值；非目的位置保持原值；旁观者不变。
    fn assert_parallel_semantics(copy: &ParallelCopy, plan: &Plan) {
        let locs = all_locs(copy);
        let initial = initial_state(&locs);
        let mut final_state = initial.clone();
        execute(&mut final_state, &plan.moves, W).expect("plan must execute");

        for a in &copy.assignments {
            assert_eq!(
                final_state.memory.get(&a.dest).copied(),
                initial.memory.get(&a.src).copied(),
                "destination {:?} must receive source {:?} initial value",
                a.dest,
                a.src
            );
        }
        let dests: HashSet<Loc> = copy.assignments.iter().map(|a| a.dest).collect();
        for loc in &locs {
            if !dests.contains(loc) {
                assert_eq!(
                    final_state.memory.get(loc),
                    initial.memory.get(loc),
                    "bystander location {loc:?} must be preserved"
                );
            }
        }
        for m in &plan.moves {
            assert!(m.is_legal(), "every emitted move must be legal: {m:?}");
            assert_eq!(m.width, W);
        }
    }

    fn expand_ok(copy: &ParallelCopy) -> Plan {
        let plan = expand(copy, &SCRATCH).expect("expansion succeeds");
        assert_parallel_semantics(copy, &plan);
        plan
    }

    #[test]
    fn empty_copy_emits_nothing() {
        let plan = expand_ok(&ParallelCopy::new(W, vec![]));
        assert!(plan.moves.is_empty());
        assert!(!plan.used_scratch_reg);
        assert!(!plan.used_scratch_stack);
    }

    #[test]
    fn single_register_move() {
        let copy = ParallelCopy::new(W, vec![Assignment::new(r(1), r(0), W)]);
        let plan = expand_ok(&copy);
        assert_eq!(plan.moves, vec![Move::new(r(0), r(1), W)]);
        assert!(!plan.used_scratch_stack);
    }

    #[test]
    fn stack_read_and_write_go_through_register() {
        // s1 <- s0 必须拆成两条，且无环时不使用额外栈槽。
        let copy = ParallelCopy::new(W, vec![Assignment::new(s(1), s(0), W)]);
        let plan = expand_ok(&copy);
        assert_eq!(
            plan.moves,
            vec![
                Move::new(s(0), SCRATCH.reg, W),
                Move::new(SCRATCH.reg, s(1), W),
            ]
        );
        assert!(plan.used_scratch_reg);
        assert!(!plan.used_scratch_stack);
    }

    #[test]
    fn shared_source_fan_out() {
        // 同一个源 r0 同时写到 r1、r2、s1。
        let copy = ParallelCopy::new(
            W,
            vec![
                Assignment::new(r(1), r(0), W),
                Assignment::new(r(2), r(0), W),
                Assignment::new(s(1), r(0), W),
            ],
        );
        let plan = expand_ok(&copy);
        assert!(!plan.used_scratch_stack);
        assert_eq!(plan.moves.len(), 3);
    }

    #[test]
    fn self_assignments_are_dropped() {
        let copy = ParallelCopy::new(
            W,
            vec![
                Assignment::new(r(0), r(0), W),
                Assignment::new(r(1), r(0), W),
                Assignment::new(s(2), s(2), W),
            ],
        );
        let plan = expand_ok(&copy);
        assert_eq!(plan.dropped_self_assignments, 2);
        for m in &plan.moves {
            assert_ne!(m.from, m.to);
        }
    }

    #[test]
    fn register_three_cycle_uses_only_scratch_register() {
        // r0<-r2, r1<-r0, r2<-r1
        let copy = ParallelCopy::new(
            W,
            vec![
                Assignment::new(r(0), r(2), W),
                Assignment::new(r(1), r(0), W),
                Assignment::new(r(2), r(1), W),
            ],
        );
        let plan = expand_ok(&copy);
        assert!(plan.used_scratch_reg);
        assert!(!plan.used_scratch_stack, "register cycle must not touch scratch stack");
    }

    #[test]
    fn register_swap_plus_tree() {
        // r0<->r1 成环，r2<-r1、r3<-r2 是挂在环上的无环链。
        let copy = ParallelCopy::new(
            W,
            vec![
                Assignment::new(r(0), r(1), W),
                Assignment::new(r(1), r(0), W),
                Assignment::new(r(2), r(1), W),
                Assignment::new(r(3), r(2), W),
            ],
        );
        let plan = expand_ok(&copy);
        assert!(!plan.used_scratch_stack);
    }

    #[test]
    fn two_stack_slots_swap() {
        // s0 <-> s1：必须同时动用额外寄存器与额外栈槽。
        let copy = ParallelCopy::new(
            W,
            vec![
                Assignment::new(s(0), s(1), W),
                Assignment::new(s(1), s(0), W),
            ],
        );
        let plan = expand_ok(&copy);
        assert!(plan.used_scratch_reg);
        assert!(plan.used_scratch_stack);
    }

    #[test]
    fn stack_swap_with_old_first_slot_written_to_third_dest() {
        // 题目点名的交错场景：
        // s0<-s1, s1<-s0（两栈槽互换），且 s0 的旧值还要写到 r2。
        let copy = ParallelCopy::new(
            W,
            vec![
                Assignment::new(s(0), s(1), W),
                Assignment::new(s(1), s(0), W),
                Assignment::new(r(2), s(0), W),
            ],
        );
        let plan = expand_ok(&copy);
        assert!(plan.used_scratch_reg);
        assert!(plan.used_scratch_stack);

        // 显式核对三方初值都落到正确位置。
        let locs = vec![s(0), s(1), r(2)];
        let initial = initial_state(&locs);
        let mut st = initial.clone();
        execute(&mut st, &plan.moves, W).unwrap();
        assert_eq!(st.memory[&s(0)], initial.memory[&s(1)]);
        assert_eq!(st.memory[&s(1)], initial.memory[&s(0)]);
        assert_eq!(st.memory[&r(2)], initial.memory[&s(0)]);
    }

    #[test]
    fn mixed_register_stack_cycle_no_scratch_stack() {
        // 环上寄存器与栈槽交替，没有连续栈槽：无需额外栈槽。
        let copy = ParallelCopy::new(
            W,
            vec![
                Assignment::new(r(0), s(0), W),
                Assignment::new(s(0), r(1), W),
                Assignment::new(r(1), r(0), W),
            ],
        );
        let plan = expand_ok(&copy);
        assert!(plan.used_scratch_reg);
        assert!(!plan.used_scratch_stack);
    }

    #[test]
    fn multiple_disjoint_cycles() {
        let copy = ParallelCopy::new(
            W,
            vec![
                Assignment::new(r(0), r(1), W),
                Assignment::new(r(1), r(0), W),
                Assignment::new(s(0), s(1), W),
                Assignment::new(s(1), s(0), W),
            ],
        );
        let plan = expand_ok(&copy);
        assert!(plan.used_scratch_stack);
        assert_parallel_semantics(&copy, &plan);
    }

    #[test]
    fn bystander_locations_are_preserved() {
        // r5、s5 既不是目的也不是被覆盖的中转，原值必须保留。
        let copy = ParallelCopy::new(
            W,
            vec![
                Assignment::new(r(1), r(0), W),
                Assignment::new(s(1), s(0), W),
            ],
        );
        let plan = expand_ok(&copy);
        let locs = vec![r(0), r(1), r(5), s(0), s(1), s(5)];
        let initial = initial_state(&locs);
        let mut st = initial.clone();
        execute(&mut st, &plan.moves, W).unwrap();
        assert_eq!(st.memory[&r(5)], initial.memory[&r(5)]);
        assert_eq!(st.memory[&s(5)], initial.memory[&s(5)]);
    }

    #[test]
    fn duplicate_destination_is_rejected() {
        let copy = ParallelCopy::new(
            W,
            vec![
                Assignment::new(r(0), r(1), W),
                Assignment::new(r(0), r(2), W),
            ],
        );
        assert_eq!(
            expand(&copy, &SCRATCH),
            Err(ExpandError::DuplicateDestination(r(0)))
        );
    }

    #[test]
    fn scratch_collision_is_rejected() {
        let copy = ParallelCopy::new(W, vec![Assignment::new(r(100), r(0), W)]);
        assert_eq!(
            expand(&copy, &SCRATCH),
            Err(ExpandError::ScratchCollision(SCRATCH.reg))
        );
        let copy2 = ParallelCopy::new(W, vec![Assignment::new(r(0), s(200), W)]);
        assert_eq!(
            expand(&copy2, &SCRATCH),
            Err(ExpandError::ScratchCollision(SCRATCH.stack))
        );
    }

    #[test]
    fn invalid_and_mismatched_widths_are_rejected() {
        assert_eq!(
            expand(&ParallelCopy::new(0, vec![]), &SCRATCH),
            Err(ExpandError::InvalidWidth(0))
        );
        let copy = ParallelCopy::new(W, vec![Assignment::new(r(0), r(1), 4)]);
        assert!(matches!(
            expand(&copy, &SCRATCH),
            Err(ExpandError::WidthMismatch { .. })
        ));
    }

    #[test]
    fn executing_missing_location_errors() {
        let mut st = State::new();
        st.memory.insert(r(0), Word(1));
        let res = execute(&mut st, &[Move::new(r(0), r(1), W)], W);
        assert_eq!(res, Err(MachineError::UndefinedLocation(r(1))));
    }

    #[test]
    fn all_stack_acyclic_chain_uses_only_scratch_register() {
        // 全部位置都是栈槽但无环：每条 stack->stack 经暂存寄存器拆两条，
        // 绝不能使用额外栈槽。
        let copy = ParallelCopy::new(
            W,
            vec![
                Assignment::new(s(3), s(2), W),
                Assignment::new(s(2), s(1), W),
                Assignment::new(s(1), s(0), W),
            ],
        );
        let plan = expand_ok(&copy);
        assert!(plan.used_scratch_reg);
        assert!(!plan.used_scratch_stack);
        // 三条逻辑搬移各拆两条机器指令。
        assert_eq!(plan.moves.len(), 6);
    }

    #[test]
    fn all_self_assignments_produce_empty_plan() {
        let copy = ParallelCopy::new(
            W,
            vec![
                Assignment::new(r(0), r(0), W),
                Assignment::new(s(0), s(0), W),
            ],
        );
        let plan = expand_ok(&copy);
        assert!(plan.moves.is_empty());
        assert_eq!(plan.dropped_self_assignments, 2);
        assert!(!plan.used_scratch_reg);
        assert!(!plan.used_scratch_stack);
    }

    #[test]
    #[ignore = "manual inspection: cargo test --offline -- --nocapture --ignored print_named_scenario"]
    fn print_named_scenario() {
        let copy = ParallelCopy::new(
            W,
            vec![
                Assignment::new(s(0), s(1), W),
                Assignment::new(s(1), s(0), W),
                Assignment::new(r(2), s(0), W),
            ],
        );
        let plan = expand(&copy, &SCRATCH).unwrap();
        println!("{plan:#?}");
        assert!(plan.used_scratch_stack);
    }
}
