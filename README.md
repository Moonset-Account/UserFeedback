# jump-relaxer

精确布局与跳转松弛（relaxation）的独立 Rust crate：给定一段从地址 0 开始的
指令流（定长字节段、标签、零填充对齐、至多 12 条可长可短的相对跳转），枚举
所有“短/长”组合，选出**最终总字节数最少**的合法布局，并在并列时按输入次序
**优先让较早的跳转变短**。

- 工具链：Rust 1.95.0，`edition = "2021"`，`rust-version = "1.95.0"`
- 依赖：仅标准库，**无第三方依赖**
- 构建/测试：`cargo test --offline`

## 规则

| 元素 | 含义 |
| --- | --- |
| `Item::Bytes(Vec<u8>)` | 定长字节段，长度即占用字节数 |
| `Item::Label(String)` | 标签，占 0 字节 |
| `Item::Align(u32)` | 补零到下一个“2 的幂”边界；已对齐则不补 |
| `Item::Jump(String)` | 相对跳转，短跳 2 字节（有符号 8 位位移 −128..=127），长跳 5 字节（有符号 32 位） |

- 位移一律从**该跳转指令末尾**算起：`target − (start + size)`。
- 对齐填充长度：`(boundary − address % boundary) % boundary`，边界必须是
  2 的幂（`0`、3、5 … 报错）。
- 长跳“可达片段内任意标签”：若某条长跳的位移超出 `i32` 范围（例如由超大对齐
  造成），则返回错误而不是静默截断。
- 缩短一条跳转省下的字节可能被后续对齐**全部吸收**，因此比较的是**最终总字节
  数**，而不是局部“越短越好”。

### 搜索与判定

`Program::relax` 对每条跳转的短/长选择做穷举（≤ 12 条 → ≤ 4096 种），对每种
组合：

1. 顺序计算地址，得到标签地址与总大小；
2. 校验每条短跳位移落在 −128..=127、每条长跳落在 `i32` 范围；
3. 以 `(总大小, 输入序并列键)` 取最小，并列键把较早的跳转放在更高位，因此
   数值最小者即“最早可短则短”的字典序最优解。

复杂度 `O(2^n · m)`，`n ≤ 12`，`m` 为条目数。

## 公开 API

```text
jump_relaxer::Item::{Bytes, Label, Align, Jump}   // 以及 bytes/label/align/jump 构造器
jump_relaxer::Program                              // new / from_items / push /
                                                   // bytes / label / align / jump /
                                                   // items / relax
jump_relaxer::Program::relax() -> Result<Layout, LayoutError>

jump_relaxer::Layout {
    total_size: u64,
    labels: Vec<LabelAddress>,          // name, item_index, address
    alignments: Vec<AlignmentPadding>,  // item_index, boundary, address, padding
    jumps: Vec<PlacedJump>,             // item_index, target, start, form,
                                        // displacement, target_address
    placements: Vec<Placement>,         // 每个输入条目的地址/大小/种类
}
jump_relaxer::Layout::label_address(&str) -> Option<u64>
jump_relaxer::Layout::jump(item_index)    -> Option<&PlacedJump>
jump_relaxer::Layout::verify()            -> Result<(), LayoutError>  // 自洽性校验

jump_relaxer::JumpForm { Short, Long }    // size() / is_short()
jump_relaxer::PlacedJump::decoded_target() -> i128   // start + size + displacement

jump_relaxer::LayoutError { TooManyJumps, EmptyLabelName, DuplicateLabel,
                            UndefinedLabel, InvalidAlignment, AddressOverflow,
                            LongJumpOutOfRange, InconsistentLayout }
jump_relaxer::MAX_JUMPS = 12, SHORT_JUMP_SIZE = 2, LONG_JUMP_SIZE = 5
```

`Layout::verify()` 是真实的独立一致性检查（不依赖原始 `Program`）：它确认

- `placements` 从 0 开始无缝隙地铺满 `[0, total_size)`，且顺序与输入一致；
- 每个标签/对齐/跳转记录都能对上一个对应的 placement，地址与大小一致；
- 每条对齐的补零长度满足公式且边界为 2 的幂；
- 每条跳转的 `start + size + displacement` 精确等于其 `target_address`，
  目标地址等于对应标签的最终地址，且位移落在该形式的范围内。

测试里还有两个反例：篡改位移或补零后 `verify()` 必须报错。

## 命令行程

`src/bin/relax.rs` 是一个薄前端（解析文本 → 调用真实的 `Program::relax` 与
`verify` → 打印结果）：

```text
cargo run --offline --bin relax -- fixtures/normal.txt
```

输入每行一条指令，`#` 之后为注释，空行忽略：

```text
bytes 90 90 cc     # 十六进制字节对
zeros 126          # 指定数量的 0 字节
label entry
align 16
jump entry
```

输出总大小、标签地址、每处补零长度、每条跳转的形式与位移。解析错误、布局错误、
自洽性失败都会打印到 stderr 并以非零码退出。

## 复现步骤

```bash
# 离线构建与全部测试（单元/集成/文档测试 + 真实调用 CLI 的端到端测试）
cargo test --offline

# 手动运行三个夹具
cargo run --offline --bin relax -- fixtures/normal.txt
cargo run --offline --bin relax -- fixtures/tie.txt
cargo run --offline --bin relax -- fixtures/boundary.txt
```

### 实际输出（本机 Rust 1.95.0 实测）

`fixtures/normal.txt`（正常场景：前向/后向短跳 + 对齐）：

```text
total size: 22 bytes

labels:
  top          0x0008 (8)
  bottom       0x0014 (20)

alignment padding:
  item 2   align 16  at 0x0008: 8 zero byte(s)

jumps:
  item 3   short at 0x0010 -> bottom     displacement +2
  item 6   short at 0x0014 -> top        displacement -14
```

`fixtures/tie.txt`（缩短被对齐吸收的并列场景）：

```text
total size: 10 bytes

labels:
  target       0x000a (10)

alignment padding:
  item 1   align 8   at 0x0002: 6 zero byte(s)

jumps:
  item 0   short at 0x0000 -> target     displacement +8
  item 2   short at 0x0008 -> target     displacement +0
```

这里两种布局总大小都是 10：两条都短 → 补零 6；第一条长 → 补零 3。省下的 3 字节
全被对齐吃掉，但并列裁决仍选“更早的跳转为短”。

`fixtures/boundary.txt`（边界场景：+127、−128 恰好合法，−133 必须长跳）：

```text
total size: 302 bytes

labels:
  fwd          0x0081 (129)
  back         0x00a9 (169)

alignment padding:
  (none)

jumps:
  item 0   short at 0x0000 -> fwd        displacement +127
  item 6   short at 0x0127 -> back       displacement -128
  item 7   long at 0x0129 -> back       displacement -133
```

## 测试与验证范围

`cargo test --offline` 当前实测：**27 个集成测试 + 5 个 CLI 测试 + 1 个文档测试，
全部通过**（另有 0 个单元测试、0 个 ignored）。测试按题面场景分组：

**正常场景**

- `bytes_labels_and_align_are_placed`：字节/标签/对齐地址
- `already_aligned_site_adds_no_padding`、`alignment_boundary_one_is_a_no_op`
- `short_forward_and_backward_jumps`、`long_jump_used_when_short_cannot_reach`
- `mixed_short_and_long_jumps`、`self_reference_and_zero_displacement`
- `builder_api_matches_from_items`、`relaxation_is_deterministic`

**边界场景**

- `short_displacement_upper_bound_is_inclusive`：+126 短、+127 短、+128 长
- `short_displacement_lower_bound_is_inclusive`：−128 短、更远则长
- `zero_length_bytes_and_empty_program`
- `maximum_jump_count_is_accepted`：恰好 12 条
- `too_many_jumps_are_rejected`：13 条
- `long_jump_out_of_range_is_reported`：`align(1<<31)` 使长跳位移越界
- `duplicate_labels_are_rejected`、`undefined_targets_are_rejected`、
  `empty_label_names_are_rejected`、`non_power_of_two_alignment_is_rejected`

**交错场景**

- `alignment_that_absorbs_the_saving_produces_a_tie`
- `tie_is_broken_towards_the_earliest_jump_being_short`（同时用独立参考模型列出
  四种组合的总大小 `(0,10) (1,10) (2,13) (3,13)`）
- `alignments_between_jumps_interleave_with_relaxation`
- `labels_at_alignment_boundaries_report_final_addresses`
- `every_placement_tiles_the_fragment`

**交叉验证（独立参考模型）**

`matches_reference_model_on_random_programs` 用确定性 PRNG 生成 40 000 个随机
程序（1–4 个标签，0–4 条跳转，随机字节/对齐，Fisher–Yates 打乱），对每个程序：

1. 参考模型 `ref_relax` 完全独立地按定义枚举所有短/长组合、校验位移范围、
   按 `(总大小, 字典序短优先)` 选解；
2. 比较 crate 输出的总大小、每条跳转的形式/起始地址/位移、对齐补零、标签地址。

两者在全部 40 000 例上一致。这不是预置输出，而是每次测试现算并对比。

**CLI 端到端**

`tests/cli.rs` 通过 `CARGO_BIN_EXE_relax` **真实执行二进制**，在三个夹具上把
stdout 与库对同一输入的实测结果逐项比对；另测缺失文件与非 2 的幂对齐的报错
路径。

### 未执行 / 不在验证范围

- 未运行 `cargo clippy`、`cargo fmt --check`（本次环境未获准执行；代码可通过
  `cargo test --offline` 编译且无 `unsafe`，启用了 `#![forbid(unsafe_code)]`
  与 `#![warn(missing_docs)]`）。
- 不做真实机器码编码/反汇编，也不链接、加载或执行任何输出；只在字节布局层面
  验证。`Bytes` 的内容不参与计算。
- 不涉及网络、外部账户或系统数据；全部输入为本地合成的测试夹具与随机数据。
- 未做性能基准；复杂度上界为 ≤ 4096 × 条目数。
