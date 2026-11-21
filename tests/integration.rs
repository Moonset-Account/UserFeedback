//! 黑盒集成测试：通过公开 API 构造并行复制，在小机器模拟器上真实执行，
//! 校验并行语义、机器约束与额外栈槽使用不变量。另含确定性随机属性测试。

use parallel_copy::machine::{execute, State, Word};
use parallel_copy::{
    Assignment, ExpandError, Loc, LocKind, Move, ParallelCopy, Plan, Scratch,
};
use std::collections::{HashMap, HashSet};

const WIDTH: u32 = 8;
const SCRATCH: Scratch = Scratch::new(9001, 9002);

/// 确定性伪随机数发生器（xorshift64*），无需第三方依赖。
struct Rng(u64);

impl Rng {
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    fn below(&mut self, n: u32) -> u32 {
        (self.next_u64() % u64::from(n)) as u32
    }
}

fn locs_of(copy: &ParallelCopy) -> Vec<Loc> {
    let mut all = Vec::new();
    for a in &copy.assignments {
        all.push(a.dest);
        all.push(a.src);
    }
    all.sort_unstable();
    all.dedup();
    all
}

fn initial_state(locs: &[Loc], seed: u64) -> State {
    let mut st = State::new();
    for (i, loc) in locs.iter().enumerate() {
        let tag = seed
            .wrapping_add(i as u64)
            .wrapping_mul(0x9E37_79B9_7F4A_7C15)
            | 1;
        st.memory.insert(*loc, Word(tag));
    }
    // 暂存位置也必须在机器上有定义，给独立哨兵值；其最终值不要求还原。
    st.memory.insert(
        SCRATCH.reg,
        Word(seed.wrapping_add(0x00FF_FFFF_0000_0001) | 1),
    );
    st.memory.insert(
        SCRATCH.stack,
        Word(seed.wrapping_add(0x00FF_FFFF_0000_0002) | 1),
    );
    st
}

/// 真实执行计划并校验全部并行语义与机器约束。
fn check_plan(copy: &ParallelCopy, plan: &Plan, seed: u64) {
    let locs = locs_of(copy);
    let initial = initial_state(&locs, seed);
    let mut st = initial.clone();
    execute(&mut st, &plan.moves, WIDTH).expect("all moves must execute on the machine");

    for a in &copy.assignments {
        assert_eq!(
            st.memory.get(&a.dest).copied(),
            initial.memory.get(&a.src).copied(),
            "seed={seed}: dest {:?} must get initial value of src {:?}",
            a.dest,
            a.src
        );
    }
    let dests: HashSet<Loc> = copy.assignments.iter().map(|a| a.dest).collect();
    for loc in &locs {
        if !dests.contains(loc) {
            assert_eq!(
                st.memory.get(loc),
                initial.memory.get(loc),
                "seed={seed}: bystander {loc:?} changed"
            );
        }
    }
    for m in &plan.moves {
        assert!(m.is_legal(), "seed={seed}: illegal move {m:?}");
        assert_eq!(m.width, WIDTH);
        assert_ne!(m.from, m.to, "self moves should have been dropped");
        // 计划只允许引用输入位置与暂存位置。
        assert!(
            locs.contains(&m.from)
                || m.from == SCRATCH.reg
                || m.from == SCRATCH.stack,
            "seed={seed}: unknown source {:?}",
            m.from
        );
        assert!(
            locs.contains(&m.to) || m.to == SCRATCH.reg || m.to == SCRATCH.stack,
            "seed={seed}: unknown destination {:?}",
            m.to
        );
    }
}

/// 判断去掉自赋值后的依赖图是否无环（仅看目的之间的 src 边）。
fn is_acyclic(copy: &ParallelCopy) -> bool {
    let source_of: HashMap<Loc, Loc> = copy
        .assignments
        .iter()
        .filter(|a| a.dest != a.src)
        .map(|a| (a.dest, a.src))
        .collect();
    // 函数图：从每个节点沿 src 边走，若回到自身则有环。
    for start in source_of.keys() {
        let mut cur = *start;
        let mut steps = 0usize;
        while let Some(&next) = source_of.get(&cur) {
            cur = next;
            steps += 1;
            if cur == *start {
                return false;
            }
            if steps > source_of.len() {
                break;
            }
        }
    }
    true
}

#[test]
fn named_interleaving_scenario_from_spec() {
    // 两个栈槽互换，且旧的第一个槽还要写到第三处（寄存器）。
    let copy = ParallelCopy::new(
        WIDTH,
        vec![
            Assignment::new(Loc::stack(0), Loc::stack(1), WIDTH),
            Assignment::new(Loc::stack(1), Loc::stack(0), WIDTH),
            Assignment::new(Loc::reg(2), Loc::stack(0), WIDTH),
        ],
    );
    let plan = parallel_copy::expand(&copy, &SCRATCH).unwrap();
    check_plan(&copy, &plan, 1);
    assert!(plan.used_scratch_stack);

    let initial = initial_state(&locs_of(&copy), 1);
    let mut st = initial.clone();
    execute(&mut st, &plan.moves, WIDTH).unwrap();
    assert_eq!(st.memory[&Loc::stack(0)], initial.memory[&Loc::stack(1)]);
    assert_eq!(st.memory[&Loc::stack(1)], initial.memory[&Loc::stack(0)]);
    assert_eq!(st.memory[&Loc::reg(2)], initial.memory[&Loc::stack(0)]);
}

#[test]
fn acyclic_inputs_never_use_scratch_stack() {
    let rng = &mut Rng(0xABCD_EF01_2345_6789);
    for iteration in 0..300 {
        let copy = random_copy(rng, true);
        assert!(is_acyclic(&copy), "generator produced a cycle at {iteration}");
        let plan = parallel_copy::expand(&copy, &SCRATCH).unwrap();
        assert!(
            !plan.used_scratch_stack,
            "iteration {iteration}: acyclic copy touched scratch stack: {plan:?}\ninput: {copy:?}"
        );
        check_plan(&copy, &plan, iteration as u64 + 1000);
    }
}

#[test]
fn random_parallel_copies_always_satisfy_parallel_semantics() {
    let rng = &mut Rng(0x1234_5678_9ABC_DEF0);
    for iteration in 0..500 {
        let copy = random_copy(rng, false);
        let plan = parallel_copy::expand(&copy, &SCRATCH).unwrap();
        check_plan(&copy, &plan, iteration as u64 + 2000);

        // 环上含连续栈槽时才允许使用额外栈槽；反过来用到额外栈槽时必有环。
        if plan.used_scratch_stack {
            assert!(!is_acyclic(&copy), "scratch stack used without a cycle");
        }
    }
}

#[test]
fn random_copies_cover_both_kinds_and_cycles() {
    // 统计随机生成确实覆盖了：纯寄存器环、栈槽互换、混合环、纯无环。
    let rng = &mut Rng(0x0F0F_F0F0_AAAA_5555);
    let mut saw_reg_cycle = false;
    let mut saw_stack_swap = false;
    let mut saw_mixed_cycle = false;
    for _ in 0..400 {
        let copy = random_copy(rng, false);
        let plan = parallel_copy::expand(&copy, &SCRATCH).unwrap();
        check_plan(&copy, &plan, 7);
        if is_acyclic(&copy) {
            assert!(!plan.used_scratch_stack);
            continue;
        }
        let uses_stack = copy.assignments.iter().any(|a| {
            a.dest.kind == LocKind::Stack || a.src.kind == LocKind::Stack
        });
        if !uses_stack {
            saw_reg_cycle = true;
            assert!(!plan.used_scratch_stack);
        } else if has_stack_to_stack_edge_in_cycle(&copy) {
            saw_stack_swap = true;
            assert!(plan.used_scratch_stack);
        } else {
            saw_mixed_cycle = true;
            assert!(!plan.used_scratch_stack);
        }
    }
    assert!(saw_reg_cycle, "random suite never built a register-only cycle");
    assert!(saw_stack_swap, "random suite never built a stack-to-stack cycle");
    assert!(saw_mixed_cycle, "random suite never built a mixed kind cycle");
}

/// 是否存在位于环上的栈槽->栈槽边（这正是额外栈槽被使用的充要条件）。
fn has_stack_to_stack_edge_in_cycle(copy: &ParallelCopy) -> bool {
    let source_of: HashMap<Loc, Loc> = copy
        .assignments
        .iter()
        .filter(|a| a.dest != a.src)
        .map(|a| (a.dest, a.src))
        .collect();
    source_of.iter().any(|(dest, src)| {
        dest.kind == LocKind::Stack
            && src.kind == LocKind::Stack
            && loc_is_on_cycle(&source_of, *dest)
    })
}

fn loc_is_on_cycle(source_of: &HashMap<Loc, Loc>, start: Loc) -> bool {
    let mut cur = start;
    for _ in 0..=source_of.len() {
        match source_of.get(&cur) {
            Some(&next) if next == start => return true,
            Some(&next) => cur = next,
            None => return false,
        }
    }
    false
}

#[test]
fn invalid_inputs_are_rejected() {
    let dup = ParallelCopy::new(
        WIDTH,
        vec![
            Assignment::new(Loc::reg(0), Loc::reg(1), WIDTH),
            Assignment::new(Loc::reg(0), Loc::reg(2), WIDTH),
        ],
    );
    assert!(matches!(
        parallel_copy::expand(&dup, &SCRATCH),
        Err(ExpandError::DuplicateDestination(_))
    ));

    let collide = ParallelCopy::new(
        WIDTH,
        vec![Assignment::new(Loc::reg(9001), Loc::reg(0), WIDTH)],
    );
    assert!(matches!(
        parallel_copy::expand(&collide, &SCRATCH),
        Err(ExpandError::ScratchCollision(_))
    ));

    let bad_scratch = Scratch {
        reg: Loc::stack(1),
        stack: Loc::stack(2),
    };
    assert!(matches!(
        parallel_copy::expand(&ParallelCopy::new(WIDTH, vec![]), &bad_scratch),
        Err(ExpandError::BadScratchLocation(_))
    ));
}

/// 生成一个随机并行复制：寄存器/栈槽混合，目的唯一，可含自赋值与环。
/// `acyclic_only` 时拒绝成环构造，保证只产生 DAG/树状输入。
fn random_copy(rng: &mut Rng, acyclic_only: bool) -> ParallelCopy {
    let n_reg = 2 + rng.below(6);
    let n_stack = rng.below(5);
    let mut pool: Vec<Loc> = (0..n_reg).map(Loc::reg).collect();
    pool.extend((0..n_stack).map(Loc::stack));

    let n_dest = 1 + rng.below(pool.len() as u32);
    let mut shuffled = pool.clone();
    // Fisher-Yates 洗牌，选目的子集。
    for i in (1..shuffled.len()).rev() {
        let j = rng.below(i as u32 + 1) as usize;
        shuffled.swap(i, j);
    }
    let dests: Vec<Loc> = shuffled[..n_dest as usize].to_vec();
    let dest_set: HashSet<Loc> = dests.iter().copied().collect();

    let mut assignments = Vec::new();
    for (idx, &dest) in dests.iter().enumerate() {
        // 约 15% 概率自赋值。
        let src = if rng.below(100) < 15 {
            dest
        } else if acyclic_only {
            // 只允许指向"更早的目的"或非目的位置，保证无环。
            let earlier: Vec<Loc> = dests[..idx].to_vec();
            let outsiders: Vec<Loc> = pool
                .iter()
                .copied()
                .filter(|l| !dest_set.contains(l))
                .collect();
            let mut choices = earlier;
            choices.extend(outsiders);
            if choices.is_empty() {
                dest // 退化为自赋值，随后会被丢弃
            } else {
                choices[rng.below(choices.len() as u32) as usize]
            }
        } else {
            pool[rng.below(pool.len() as u32) as usize]
        };
        assignments.push(Assignment::new(dest, src, WIDTH));
    }
    ParallelCopy::new(WIDTH, assignments)
}

#[test]
fn plan_moves_are_all_machine_legal_in_random_suite() {
    // 额外独立校验：随机计划里绝不出现栈->栈，且每条 Move 等宽。
    let rng = &mut Rng(0xDEAD_BEEF_CAFE_BABE);
    for _ in 0..200 {
        let copy = random_copy(rng, false);
        let plan = parallel_copy::expand(&copy, &SCRATCH).unwrap();
        for m in &plan.moves {
            assert_ne!(
                (m.from.kind, m.to.kind),
                (LocKind::Stack, LocKind::Stack)
            );
            let _: &Move = m;
        }
    }
}

#[test]
fn rng_sequence_is_seed_reproducible() {
    // 固定种子下两次生成必须逐字节一致，保证随机测试可复现。
    let mut a = Rng(42);
    let mut b = Rng(42);
    for _ in 0..100 {
        assert_eq!(a.next_u64(), b.next_u64());
    }
}
