//! 栈回溯模块的功能验证测试。
//!
//! 地址规划（合成数据）：
//! - 主栈段 Main: [0x1000, 0x2000)
//! - 备用栈段 Alt: [0x8000, 0x9000)

use stackwalk::*;

const MAIN_BASE: Addr = 0x1000;
const MAIN_SIZE: usize = 0x1000;
const ALT_BASE: Addr = 0x8000;
const ALT_SIZE: usize = 0x1000;

fn memory() -> (StackSegment, StackSegment) {
    (
        StackSegment::new(StackSeg::Main, MAIN_BASE, MAIN_SIZE),
        StackSegment::new(StackSeg::Alt, ALT_BASE, ALT_SIZE),
    )
}

fn normal(cfa_offset: u64) -> UnwindRule {
    UnwindRule::Normal { cfa_offset }
}

fn async_rule() -> UnwindRule {
    UnwindRule::Async {
        ip_off: 0,
        sp_off: 8,
        seg_off: 16,
    }
}

/// 正常场景：三级普通调用链，顶端返回地址为 0，正常结束。
#[test]
fn normal_call_chain_to_end() {
    let table = [
        UnwindEntry { start: 0x100, end: 0x200, rule: normal(0x20) },
        UnwindEntry { start: 0x200, end: 0x300, rule: normal(0x30) },
        UnwindEntry { start: 0x300, end: 0x400, rule: normal(0x40) },
    ];
    let (mut main, alt) = memory();
    // 帧0: sp=0x1000, caller_sp=0x1020, RA@[0x1018]=0x250 -> 查表用 0x24F 命中帧1
    main.write_u64(0x1018, 0x250).unwrap();
    // 帧1: sp=0x1020, caller_sp=0x1050, RA@[0x1048]=0x380 -> 0x37F 命中帧2
    main.write_u64(0x1048, 0x380).unwrap();
    // 帧2: sp=0x1050, caller_sp=0x1090, RA@[0x1088]=0 -> 结束
    main.write_u64(0x1088, 0).unwrap();
    let mem = Memory::new(vec![main, alt]);

    let start = Registers { ip: 0x150, sp: 0x1000, seg: StackSeg::Main };
    let result = Unwinder::new(&mem, &table).walk(&start);

    assert_eq!(result.stop, StopReason::EndOfChain);
    assert_eq!(result.frames.len(), 3);
    assert_eq!(result.frames[0].ip, 0x150);
    assert_eq!(result.frames[1].ip, 0x24F); // ra - 1 定位前一条调用指令
    assert_eq!(result.frames[1].sp, 0x1020);
    assert_eq!(result.frames[2].ip, 0x37F);
    assert_eq!(result.frames[2].sp, 0x1050);
    assert!(result.frames.iter().all(|f| f.seg == StackSeg::Main));
}

/// 边界场景一：异步事件保存的被打断指令指针恰好落在展开范围边界上。
///
/// 被打断的 ip = 0x200，恰为范围 B 的起点（半开区间 [0x200, 0x300)）。
/// 跨异步帧必须精确恢复 ip，不做 ra - 1，因此命中 B 的普通帧规则；
/// 若错误套用 ra - 1 会得到 0x1FF，落回异步范围 A。同时验证栈段从
/// 备用栈切回主栈。
#[test]
fn async_event_ip_on_range_boundary() {
    let table = [
        UnwindEntry { start: 0x100, end: 0x200, rule: async_rule() }, // A
        UnwindEntry { start: 0x200, end: 0x300, rule: normal(0x20) }, // B
    ];
    let (mut main, mut alt) = memory();
    // 异步帧位于备用栈 sp=0x8000，保存的被打断上下文：
    // ip=0x200（恰为 B 的起点）、sp=0x1100、段=Main。
    alt.write_saved_context(0x8000, 0x200, 0x1100, StackSeg::Main).unwrap();
    // 切回主栈后按 B 的规则展开：caller_sp=0x1120，RA@[0x1118]=0 结束。
    main.write_u64(0x1118, 0).unwrap();
    let mem = Memory::new(vec![main, alt]);

    let start = Registers { ip: 0x180, sp: 0x8000, seg: StackSeg::Alt };
    let result = Unwinder::new(&mem, &table).walk(&start);

    assert_eq!(result.stop, StopReason::EndOfChain);
    assert_eq!(result.frames.len(), 2);
    // 帧0：异步事件帧，在备用栈上。
    assert_eq!(result.frames[0].rule, async_rule());
    assert_eq!(result.frames[0].seg, StackSeg::Alt);
    // 帧1：精确恢复 ip=0x200，命中 B 的普通帧规则，且切回主栈。
    assert_eq!(result.frames[1].ip, 0x200);
    assert_eq!(result.frames[1].sp, 0x1100);
    assert_eq!(result.frames[1].seg, StackSeg::Main);
    assert_eq!(result.frames[1].rule, normal(0x20));
}

/// 边界场景二：普通返回地址恰好落在后一范围的起点。
///
/// 调用指令是范围 C 的最后一条指令，返回地址 0x410 恰为范围 D 的起点。
/// 普通帧必须用 ra - 1 = 0x40F 定位前一条调用指令，因此命中 C 的规则
/// （cfa_offset=0x20）；若错误地用 ra 直接查表会命中 D（cfa_offset=0x40），
/// 算出错误的调用者栈指针。
#[test]
fn return_address_on_next_range_start() {
    let table = [
        UnwindEntry { start: 0x400, end: 0x410, rule: normal(0x20) }, // C
        UnwindEntry { start: 0x410, end: 0x500, rule: normal(0x40) }, // D
    ];
    let (mut main, alt) = memory();
    // 帧0（C 的被调者）：sp=0x1000, caller_sp=0x1020, RA@[0x1018]=0x410。
    main.write_u64(0x1018, 0x410).unwrap();
    // 帧1 按 C 的规则（ra-1=0x40F 命中 C）：caller_sp=0x1040，RA@[0x1038]=0 结束。
    main.write_u64(0x1038, 0).unwrap();
    // 若帧1 错用 D 的规则会读 [0x1058]，放一个非零值以便区分。
    main.write_u64(0x1058, 0x9999).unwrap();
    let mem = Memory::new(vec![main, alt]);

    let start = Registers { ip: 0x408, sp: 0x1000, seg: StackSeg::Main };
    let result = Unwinder::new(&mem, &table).walk(&start);

    assert_eq!(result.stop, StopReason::EndOfChain);
    assert_eq!(result.frames.len(), 2);
    // 帧1 的 sp 来自帧0 的规则（C）：0x1000 + 0x20 = 0x1020。
    assert_eq!(result.frames[1].ip, 0x40F);
    assert_eq!(result.frames[1].rule, normal(0x20));
    assert_eq!(result.frames[1].sp, 0x1020);
    // 若帧1 错用 D 的规则，会读 [0x1058]=0x9999 并继续走向 NoUnwindInfo，
    // 而不是在 [0x1038]=0 处正常结束；上面的 EndOfChain 断言即核对这一点。
}

/// 交错场景：两级嵌套异步帧，普通帧与异步帧交替，跨两次备用栈切换。
#[test]
fn two_level_nested_async_interleaved() {
    let table = [
        UnwindEntry { start: 0x100, end: 0x180, rule: normal(0x20) }, // H2 二级处理帧
        UnwindEntry { start: 0x180, end: 0x200, rule: async_rule() }, // A2 第二次异步事件
        UnwindEntry { start: 0x200, end: 0x280, rule: normal(0x20) }, // H1 一级处理帧
        UnwindEntry { start: 0x280, end: 0x300, rule: async_rule() }, // A1 第一次异步事件
        UnwindEntry { start: 0x300, end: 0x400, rule: normal(0x20) }, // M 被打断的主流程
    ];
    let (mut main, mut alt) = memory();
    // H2: sp=0x8800, caller_sp=0x8820, RA@[0x8818]=0x1C0 -> 0x1BF 命中 A2
    alt.write_u64(0x8818, 0x1C0).unwrap();
    // A2: sp=0x8820，保存 H1 现场：ip=0x240, sp=0x8400, seg=Alt
    alt.write_saved_context(0x8820, 0x240, 0x8400, StackSeg::Alt).unwrap();
    // H1: sp=0x8400, caller_sp=0x8420, RA@[0x8418]=0x2A0 -> 0x29F 命中 A1
    alt.write_u64(0x8418, 0x2A0).unwrap();
    // A1: sp=0x8420，保存主流程现场：ip=0x350, sp=0x1800, seg=Main
    alt.write_saved_context(0x8420, 0x350, 0x1800, StackSeg::Main).unwrap();
    // M: sp=0x1800, caller_sp=0x1820, RA@[0x1818]=0 结束
    main.write_u64(0x1818, 0).unwrap();
    let mem = Memory::new(vec![main, alt]);

    let start = Registers { ip: 0x120, sp: 0x8800, seg: StackSeg::Alt };
    let result = Unwinder::new(&mem, &table).walk(&start);

    assert_eq!(result.stop, StopReason::EndOfChain);
    assert_eq!(result.frames.len(), 5);
    let kinds: Vec<UnwindRule> = result.frames.iter().map(|f| f.rule).collect();
    assert_eq!(
        kinds,
        vec![normal(0x20), async_rule(), normal(0x20), async_rule(), normal(0x20)]
    );
    let segs: Vec<StackSeg> = result.frames.iter().map(|f| f.seg).collect();
    assert_eq!(
        segs,
        vec![StackSeg::Alt, StackSeg::Alt, StackSeg::Alt, StackSeg::Alt, StackSeg::Main]
    );
    // 跨 A1 后精确恢复主流程现场。
    let last = result.frames.last().unwrap();
    assert_eq!(last.ip, 0x350);
    assert_eq!(last.sp, 0x1800);
}

/// 三级嵌套异步帧：超过支持的两级，停止并报告。
#[test]
fn third_nested_async_exceeds_depth() {
    let table = [
        UnwindEntry { start: 0x100, end: 0x200, rule: normal(0x10) }, // N1
        UnwindEntry { start: 0x200, end: 0x300, rule: async_rule() }, // A1
        UnwindEntry { start: 0x300, end: 0x400, rule: normal(0x10) }, // N2
        UnwindEntry { start: 0x400, end: 0x500, rule: async_rule() }, // A2
        UnwindEntry { start: 0x500, end: 0x600, rule: normal(0x10) }, // N3
        UnwindEntry { start: 0x600, end: 0x700, rule: async_rule() }, // A3
    ];
    let (main, mut alt) = memory();
    // N1: sp=0x8000, RA@[0x8008]=0x250 -> A1
    alt.write_u64(0x8008, 0x250).unwrap();
    // A1: sp=0x8010 -> N2 (ip=0x350, sp=0x8100, Alt)
    alt.write_saved_context(0x8010, 0x350, 0x8100, StackSeg::Alt).unwrap();
    // N2: sp=0x8100, RA@[0x8108]=0x450 -> A2
    alt.write_u64(0x8108, 0x450).unwrap();
    // A2: sp=0x8110 -> N3 (ip=0x550, sp=0x8200, Alt)
    alt.write_saved_context(0x8110, 0x550, 0x8200, StackSeg::Alt).unwrap();
    // N3: sp=0x8200, RA@[0x8208]=0x650 -> A3（第三级，应停止）
    alt.write_u64(0x8208, 0x650).unwrap();
    let mem = Memory::new(vec![main, alt]);

    let start = Registers { ip: 0x100, sp: 0x8000, seg: StackSeg::Alt };
    let result = Unwinder::new(&mem, &table).walk(&start);

    assert_eq!(result.stop, StopReason::AsyncDepthExceeded { depth: 3 });
    // 已确认的帧：N1, A1, N2, A2, N3, A3。
    assert_eq!(result.frames.len(), 6);
    assert_eq!(result.frames.last().unwrap().rule, async_rule());
}

/// 回路：指令指针与栈指针组合重复，停止并指出回路。
#[test]
fn repeated_ip_sp_combination_is_a_cycle() {
    let table = [UnwindEntry { start: 0x100, end: 0x200, rule: normal(0) }];
    let (mut main, alt) = memory();
    // cfa_offset=0 使 sp 不变；RA@[0x1008]=0x101 -> ip 回到 0x100。
    main.write_u64(0x1008, 0x101).unwrap();
    let mem = Memory::new(vec![main, alt]);

    let start = Registers { ip: 0x100, sp: 0x1010, seg: StackSeg::Main };
    let result = Unwinder::new(&mem, &table).walk(&start);

    assert_eq!(
        result.stop,
        StopReason::Cycle { ip: 0x100, sp: 0x1010 }
    );
    assert_eq!(result.frames.len(), 1);
}

/// 取数越界：普通帧的返回地址槽位超出对应栈段边界。
#[test]
fn read_outside_segment_bounds_stops_with_fault() {
    let table = [UnwindEntry { start: 0x100, end: 0x200, rule: normal(0x2000) }];
    let (main, alt) = memory();
    let mem = Memory::new(vec![main, alt]);

    // caller_sp = 0x1000 + 0x2000 = 0x3000，读 [0x2FF8] 超出 Main [0x1000,0x2000)。
    let start = Registers { ip: 0x100, sp: 0x1000, seg: StackSeg::Main };
    let result = Unwinder::new(&mem, &table).walk(&start);

    assert_eq!(
        result.stop,
        StopReason::MemoryFault(MemFault { seg: StackSeg::Main, addr: 0x2FF8, len: 8 })
    );
    assert_eq!(result.frames.len(), 1);
}

/// 取数越界：异步帧保存的上下文槽位超出备用栈边界。
#[test]
fn async_context_read_outside_alt_segment_faults() {
    let table = [UnwindEntry { start: 0x100, end: 0x200, rule: async_rule() }];
    let (main, alt) = memory();
    let mem = Memory::new(vec![main, alt]);

    // sp=0x8FF0：ip 槽 [0x8FF0]、sp 槽 [0x8FF8] 可读，
    // 但 seg 槽 [0x9000] 越过 Alt 上界 0x9000。
    let start = Registers { ip: 0x100, sp: 0x8FF0, seg: StackSeg::Alt };
    let result = Unwinder::new(&mem, &table).walk(&start);

    assert_eq!(
        result.stop,
        StopReason::MemoryFault(MemFault { seg: StackSeg::Alt, addr: 0x9000, len: 8 })
    );
    assert_eq!(result.frames.len(), 1);
}

/// 指令指针不在任何展开范围内。
#[test]
fn ip_without_unwind_info_stops() {
    let table = [UnwindEntry { start: 0x100, end: 0x200, rule: normal(0x20) }];
    let (main, alt) = memory();
    let mem = Memory::new(vec![main, alt]);

    let start = Registers { ip: 0x500, sp: 0x1000, seg: StackSeg::Main };
    let result = Unwinder::new(&mem, &table).walk(&start);

    assert_eq!(result.stop, StopReason::NoUnwindInfo { ip: 0x500 });
    assert!(result.frames.is_empty());
}

/// 异步帧保存的栈段编号非法。
#[test]
fn invalid_saved_segment_stops() {
    let table = [UnwindEntry { start: 0x100, end: 0x200, rule: async_rule() }];
    let (main, mut alt) = memory();
    alt.write_u64(0x8000, 0x150).unwrap(); // ip
    alt.write_u64(0x8008, 0x1000).unwrap(); // sp
    alt.write_u64(0x8010, 7).unwrap(); // 非法段编号
    let mem = Memory::new(vec![main, alt]);

    let start = Registers { ip: 0x100, sp: 0x8000, seg: StackSeg::Alt };
    let result = Unwinder::new(&mem, &table).walk(&start);

    assert_eq!(result.stop, StopReason::InvalidSavedSegment { raw: 7 });
    assert_eq!(result.frames.len(), 1);
}
