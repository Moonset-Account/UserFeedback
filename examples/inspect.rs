//! 人工核对用示例：真实调用展开器并在小机器模拟器上执行。
//!
//! 运行：`cargo run --offline --example inspect`
//!
//! 示例覆盖题目点名的交错场景：两个栈槽互换，且旧的第一个栈槽
//! 还要写到第三个目的（寄存器）。打印的每条指令都由展开器实时生成，
//! 再由模拟器真实执行并对照并行语义检查，不是预置文本。

use parallel_copy::machine::{execute, State, Word};
use parallel_copy::{Assignment, Loc, ParallelCopy, Scratch};

const WIDTH: u32 = 8;

fn main() {
    let s0 = Loc::stack(0);
    let s1 = Loc::stack(1);
    let r2 = Loc::reg(2);

    // 并行语义（同时发生）：s0 <- s1, s1 <- s0, r2 <- s0 的旧值。
    let copy = ParallelCopy::new(
        WIDTH,
        vec![
            Assignment::new(s0, s1, WIDTH),
            Assignment::new(s1, s0, WIDTH),
            Assignment::new(r2, s0, WIDTH),
        ],
    );
    let scratch = Scratch::new(100, 200);

    let plan = parallel_copy::expand(&copy, &scratch).expect("expansion succeeds");

    println!("parallel copy:");
    for a in &copy.assignments {
        println!("  {:?} <- {:?}", a.dest, a.src);
    }
    println!("\nsequential moves (all legal on the small machine):");
    for (i, m) in plan.moves.iter().enumerate() {
        println!("  {:>2}: {:?} <- {:?} ({} bytes)", i + 1, m.to, m.from, m.width);
    }
    println!(
        "\nscratch register used: {} | scratch stack used: {} | self-assignments dropped: {}",
        plan.used_scratch_reg, plan.used_scratch_stack, plan.dropped_self_assignments
    );

    // 真实执行：每个输入位置一个可辨识的初值。
    let mut state = State::new();
    state.memory.insert(s0, Word(0xA0));
    state.memory.insert(s1, Word(0xB1));
    state.memory.insert(r2, Word(0x00));
    state.memory.insert(scratch.reg, Word(0x00));
    state.memory.insert(scratch.stack, Word(0x00));

    execute(&mut state, &plan.moves, WIDTH).expect("plan executes on the machine");

    println!("\nfinal machine state:");
    println!("  s0 = {:#x} (want {:#x}, initial s1)", state.memory[&s0].0, 0xB1u64);
    println!("  s1 = {:#x} (want {:#x}, initial s0)", state.memory[&s1].0, 0xA0u64);
    println!("  r2 = {:#x} (want {:#x}, initial s0)", state.memory[&r2].0, 0xA0u64);

    assert_eq!(state.memory[&s0], Word(0xB1));
    assert_eq!(state.memory[&s1], Word(0xA0));
    assert_eq!(state.memory[&r2], Word(0xA0));
    println!("\nall destinations received the initial value of their specified source.");
}
