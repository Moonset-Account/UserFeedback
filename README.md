# section-folder

链接器只读段折叠与布局模块（Rust 1.95.0，零第三方依赖，仅用标准库）。

实现题目要求的三件事：

1. **段折叠（安全 ICF 风格）**：输入段字节、对齐、具名重定位槽（类型/宽度/偏移/加数）与
   带地址身份标记的符号，求满足规则的**最大等价划分**。
2. **布局与回填**：按代表名顺序对齐排地址，回填绝对或 PC 相对位移，并按给定槽宽做
   可表示性检查，溢出时返回带范围信息的错误。
3. **验证入口**：对任意未合并的段对，给出直接的身份/字节/对齐差异，或沿重定位边的
   **最短区分路径**（BFS）。

## 目录结构

```
Cargo.toml
src/
  lib.rs       公开入口与单元测试
  model.rs     输入/输出数据模型与错误类型
  prep.rs      输入校验、占位字节遮蔽、规范化重定位顺序
  fold.rs      静态签名 + 分区细化（partition refinement）求最大不动点
  layout.rs    对齐布局、绝对/相对回填、宽度范围检查
  explain.rs   最短区分路径 BFS 与直接差异诊断
tests/
  integration.rs  正常/边界/互指环/地址身份/布局/诊断/次序无关（24 项）
  concurrency.rs  多线程并行与置换交错（2 项）
```

## 公开 API

### 输入模型（`src/model.rs`）

| 类型 | 说明 |
| --- | --- |
| `Section { name, bytes, alignment, relocations }` | 输入段；`alignment` 必须为非零 2 的幂 |
| `Symbol { name, section, offset, address_taken }` | 具名符号；`address_taken=true` 表示程序会比较其地址 |
| `RelocType { kind, width }` / `RelocKind::{Absolute, Relative}` | 槽类型，宽度 1..=8 字节 |
| `Relocation { rtype, offset, target_symbol, addend }` | 重定位槽 |
| `LinkerInput { sections, symbols }` | 完整输入 |

### 三个入口函数

```rust
pub fn fold(input: &LinkerInput) -> Result<FoldResult, LinkerError>;
pub fn layout(input: &LinkerInput) -> Result<Layout, LinkerError>;
pub fn distinguish(
    input: &LinkerInput,
    section_a: &str,
    section_b: &str,
) -> Result<Option<Distinction>, LinkerError>;
```

- `fold` → `FoldResult { classes: Vec<EquivClass> }`，类按代表名（成员名最小值）排序；
  提供 `class_of(idx)` 与 `are_equivalent(name_a, name_b)`。
- `layout` → `Layout { placed: Vec<PlacedSection>, symbol_addresses }`，
  `PlacedSection.bytes` 为已回填的输出字节（小端）。
- `distinguish` 对等价段对返回 `Ok(None)`；否则返回 `Distinction`，含两条并行路径
  （`path_a`/`path_b`，直接差异时为空）与终止原因 `PathTermination`。

### 等价规则（折叠判定）

两段等价当且仅当同时满足：

1. 对齐、大小一致；
2. **遮住全部重定位占位字节后**内容一致（占位位置填零再比较，槽内原始字节不参与）；
3. 每个规范化槽（按偏移/类型/宽度/目标/加数全序排序，与输入次序无关）的类型、偏移、
   加数、目标符号的**段内位置**一致；
4. 槽的目标满足递归条件：
   - 目标符号带地址身份（`address_taken`）：必须是**同一个符号**（精确名比较），
     定义该符号的段恒为单例、不参与折叠；
   - 否则目标段必须属于同一等价类（允许相互递归，由不动点求解）。

算法先按 1–3 的**静态签名**做初始划分，再迭代加入「目标等价类 id」做分区细化，
直到划分稳定（最大不动点）。互指环在初始时按静态结构同组，细化过程中若被外部锚点
（如静态可区分的 sink 段、或地址敏感符号）区分则逐轮拆开，否则保持折叠——
这与链接器安全 ICF 的双模拟语义一致。

### 布局与回填

- 类按代表名字典序访问，地址从 0 开始，每段取满足对齐的下一个地址
  `align_up(cursor, alignment)`。
- 绝对槽：`value = 目标符号地址 + addend`，按无符号范围 `[0, 2^(8w)-1]` 检查。
- 相对槽：`value = 目标地址 - (槽地址 + 槽宽) + addend`，按有符号范围
  `[-2^(8w-1), 2^(8w-1)-1]` 检查。
- 检查通过后按小端、补码截断写回；溢出返回 `LinkerError::Overflow { value, min, max, .. }`。

### 诊断（`distinguish`）

- 先查直接差异：地址身份钉住（`KeptByAddressIdentity`）、大小、对齐、掩码字节。
- 否则从段对出发做 BFS，按规范化槽并行沿重定位边走；遇到槽元组不一致→
  `RelocSlotMismatch`，遇到不同地址敏感符号→`ExactAddressSymbol`，遇到目标段直接
  差异→`Direct(..)`。已访问段对记入集合，环上不会无限循环；BFS 保证路径最短。

## 复现步骤

```bash
# 工具链：cargo 1.95.0 / rustc 1.95.0（本仓库验证版本）
cargo test --offline
```

无网络依赖、无第三方 crate；`--offline` 可直接在空缓存的本机标准库环境运行。

## 验证范围（33 个真实测试，无预置输出）

### 单元测试（`src/lib.rs`，4 项）
- 对齐向上取整边界；`RelocType` 显示；空输入合法；非法对齐/越界槽/非法宽度/重名报错。

### 集成测试（`tests/integration.rs`，27 项）

| 场景分类 | 覆盖点 |
| --- | --- |
| 正常折叠 | 相同段合并、字节不同不合并、代表名与成员顺序 |
| 占位遮蔽 | 仅槽内字节不同的段仍合并 |
| 槽语义 | 加数不同、槽偏移不同、目标符号段内位置不同均拆开；目标段本身仍可折叠 |
| 互指环（交错） | 两个同构三环在共享 sink 锚点下逐角色折叠；无锚同构环按双模拟整体折叠 |
| 地址身份 | 含 `address_taken` 符号的段为单例；同符号引用仍允许折叠；不同地址敏感符号在槽位即区分；**环末端仅多一个地址敏感入口的变体**行为 |
| 诊断 | 等价对返回 `None`；直接字节/对齐/身份差异；到地址敏感变体的最短路径；4 跳深层链末端槽不一致；不同地址敏感符号的 `ExactAddressSymbol` |
| 布局 | 代表名顺序、对齐填充、绝对回填（含加数）、PC 相对回填、符号地址表 |
| 宽度边界 | u8 边界 255 可表示/256 溢出；i8 边界 -128 可表示/-129 溢出，错误含 min/max |
| 次序无关 | 段与符号同时逆序、重映射索引后，布局地址/字节/符号表完全一致 |
| 跨入口一致性 | 全部段对上 `fold` 与 `distinguish` 结论一致；`layout` 的类与折叠类一一对应 |
| 端到端回填 | 折叠后互指环每个槽回填值等于目标符号最终地址，槽外字节原样保留 |
| 最大不动点 | 追加一个真正不同的段只细化、不错误合并既有等价关系 |
| 校验 | 未知段名、符号越界、重定位目标索引越界等错误路径 |

### 并发交错测试（`tests/concurrency.rs`，2 项）
- 8 线程并行对同一输入执行 `fold`/`layout`/`distinguish`，结果逐字节一致；
- 6 种段序置换在不同线程并发布局，全部收敛到同一份（代表名, 地址, 字节）集合。

## 明确的语义边界

- **无锚同构环**：若一个环没有任何外部可区分信息（无地址敏感符号、无静态差异），
  双模拟无法区分其角色，整个环折叠为一类。这是安全 ICF 的标准行为而非缺陷；
  实际链接输入中此类环必然通过符号地址或外部引用获得锚点，测试中以共享 sink 段
  与地址敏感变体覆盖该正常情形。
- 重叠重定位槽不做特殊拒绝：遮蔽与回填均按槽顺序确定性处理，后写槽覆盖先写槽。
- 所有随机性（哈希）仅用于内部分组，最终类与布局一律按名称确定性排序输出。
