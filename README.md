# fpiso — 可执行的浮点保护域隔离语义模型

`fpiso` 是一个纯 Python 3.11（无第三方依赖）的可执行语义模型，用来回答
“共享计算运行时在协程间切换时，上一保护域的舍入结果、舍入方式和黏滞异常
会不会带给下一个域”这个问题。

它把每个**保护域（protection domain）**建模为独立对象，各自拥有：

1. 自己的**舍入方式**（`rne` / `rna` / `rz` / `rd` / `ru`）；
2. 自己的**异常屏蔽位**（overflow / underflow / inexact 各自 masked 或 unmasked）；
3. 自己的**累计状态**（sticky 标志 + 待处理 pending trap 队列）。

所有操作数与中间结果都是任意精度有理数（`fractions.Fraction`），只在写回
目标格式时做 **一次** 舍入。模型支持：

* 加法 `add`
* 乘法 `mul`
* 一次舍入的乘加 `fma`（乘积保持精确，整个 `a*b+c` 只舍入一次）

按模式产生结果以及 **overflow / underflow / inexact** 标志。外层保护域被
暂停时可以嵌套进入**审计域（audit domain）**；审计域退出时只报告、只清理
自己的状态，**不能吞掉外层待处理异常，也不会把别域标志泄露进来**。

---

## 运行方法

要求 Python **3.11.6**，仅标准库，无需安装任何依赖。

```bash
# 全部单元测试
python3 -m unittest discover -s tests -v

# 运行全部演示场景（6 个）
python3 -m fpiso                 # 等价于 python3 -m fpiso demo
python3 -m fpiso demo nested     # 只运行一个场景
python3 -m fpiso demo fma

# 在单个域里求一次运算，并打印值 / 标志 / 黏滞态 / 待处理队列
python3 -m fpiso op add 1 1/16 --rounding rna --masks inexact
python3 -m fpiso op fma 9/8 9/8 -5/4
python3 -m fpiso op mul 1/64 1/64 \
    --format audit --precision 5 --emin -3 --emax 4 \
    --rounding ru --masks underflow
python3 -m fpiso formats      # 内置格式、舍入方式、标志列表
```

模块也可直接作为库使用：

```python
from fractions import Fraction as F
from fpiso import Domain, MaskBits, Flag, Format, RoundingMode, Runtime

small = Format("small", precision=4, emin=-2, emax=3)

# 两个用户态保护域
outer = Domain("outer", small, RoundingMode.RNE, MaskBits.unmasking(Flag.INEXACT))
audit = Domain("audit", Format("audit", 5, -3, 4), RoundingMode.RU,
               MaskBits.unmasking(Flag.UNDERFLOW))

runtime = Runtime(name="demo")
runtime.enter_domain(outer)
print(runtime.add(1, F(1, 16)))        # 17/16 是 1 与 9/8 的舍入中点
                                       # -> 1 flags={inexact} pending=[inexact]

runtime.enter_domain(audit, audit=True)      # outer 被暂停
print(runtime.observe())                     # 只能看到 audit 自己的状态
print(runtime.inspect("outer"))              # 观察通道：outer 的状态原封不动
print(runtime.mul(F(1, 64), F(1, 64)))       # audit 自己的下溢，只进 audit
report = runtime.exit_domain(consume_pending=True)
print(report.describe())                     # generated=[underflow]
print(runtime.observe())                     # 恢复后 = 暂停时快照，未被审计域污染
```

---

## 公开 API

从 `fpiso` 包导出（`fpiso/__init__.py`）：

| 名称 | 作用 |
| --- | --- |
| `Format(name, precision, emin, emax, has_infinity=True)` | 可配置的小型二进制格式（不可变） |
| `RoundingMode` | `RNE` / `RNA` / `RZ` / `RD` / `RU` |
| `SMALL` | 演示格式：p=4, emin=-2, emax=3，最大有限值 15 |
| `Flag` | `OVERFLOW` / `UNDERFLOW` / `INEXACT` |
| `MaskBits` | 每域屏蔽位；`all_masked()` / `all_unmasked()` / `unmasking(*flags)` |
| `Domain` | 一个保护域：格式、舍入、屏蔽位、sticky、pending |
| `Runtime` | 保护域栈：进入 / 暂停 / 恢复 / 退出与操作分发 |
| `DomainState` | 域可观察状态的不可变快照 |
| `OpResult` / `OpTrace` / `ExitReport` | 操作结果、乘加轨迹、退出报告 |
| `FPValue` | 有限值（含符号零）或无穷的结果值 |
| `round_exact(exact, fmt, rounding, zero_sign=1)` | 无状态的一次舍入 |
| `as_exact(value)` | 只接受精确输入（`int`/`Fraction`/`Decimal`/精确字符串） |

### `Domain` 关键方法

| 方法 / 属性 | 行为 |
| --- | --- |
| `add(x, y)` / `mul(x, y)` / `convert(x)` | 返回 `OpResult`：`value`、本步 `flags`、本步新增 `pending`、操作后 `state` |
| `fma(a, b, c)` | 返回 `OpTrace`：`product`（精确）、`exact`（精确和）、`result` |
| `sticky` / `pending` / `raised` | 只读的黏滞标志、待处理队列、当前未屏蔽的黏滞标志 |
| `take_pending()` / `clear_sticky()` / `clear_flags()` | 显式清理；暂停中的域会被拒绝 |
| `with_rounding` / `with_masks` / `with_format` / `copy()` | 返回带独立深拷贝 ledger 的新域 |
| `state()` / `describe()` | 不可变状态快照 / 一行文本 |

### `Runtime` 关键方法

| 方法 / 属性 | 行为 |
| --- | --- |
| `register(domain)` | 把域绑定到此运行时（同名不同对象会被拒绝） |
| `enter_domain(domain, label=None, audit=False)` | 严格 LIFO 进入；进入前把外层整体暂停并做快照 |
| `exit_domain(expected=None, consume_pending=False)` | 退出最内层域；`consume_pending` 只清该域自己的队列 |
| `domain_scope(...)` / `audit_scope(...)` | 上下文管理器，异常时也会正确退出并继续传播异常 |
| `add` / `mul` / `fma` / `convert` | 把操作分发给当前活动域 |
| `observe()` | **当前活动域**能读到的自身状态 |
| `inspect(name)` / `inspect_all()` | 观察通道：读取任一已注册域的状态（只读数据，不合并标志） |
| `active` / `depth` / `stack_names()` / `events` | 栈视图与切换日志 |

### 重要不变量（由运行时强制）

1. **只允许 LIFO 嵌套**：外层未暂停不能进新域，只有最内层能退出。
2. **暂停是全面的**：暂停时保存不可变快照；暂停中的域执行任何算术或清理
   都会抛 `SuspendedDomainError`。
3. **恢复校验**：恢复前比较实状态与快照，不一致即抛 `IsolationError`。
4. **无隐式搬运**：内层 pending 只出现在内层的退出报告里，绝不并入外层；
   内层退出也绝不会清空外层队列。
5. **休眠域不可写**：已注册但不在栈顶的域执行操作会抛 `IsolationError`，
   防止“悄悄写自己”的旁路。

---

## 语义细节

### 格式与舍入

格式 `p`（含隐含前导位的有效位宽）、`emin`、`emax`；子范数步长
`2**(emin-(p-1))`，最大有限值 `(2**p-1) * 2**(emax-(p-1))`。
舍入时先用“目标精度 + 无界指数范围”判定溢出，再按目标的合法步长做一次
精确舍入：

* **overflow**：无界结果绝对值 > 最大有限值。按舍入方向交付 `±inf` 或
  `±最大有限值`；`overflow` 一定伴随 `inexact`。
* **underflow**：结果不精确，且按无界指数舍入后仍严格小于最小正规数
  （IEEE 754 “舍入后判小”）。
* **inexact**：舍入结果 ≠ 精确结果。

`-0` 按 IEEE 754 规则由操作本身决定：同号零相加保留符号；异号零在 `rd`
下为 `-0`，否则 `+0`；乘法为符号异或；乘加为“零乘积与零加数同号则取该
符号”。

### 保护域与嵌套

* 每个 `Domain` 的 ledger 在构造时新建成，`with_*` / `copy()` 均深拷贝，
  两个域**不可能别名同一份 sticky/pending**。
* 运行时是唯一知道“当前活动域”的对象；没有任何模块级可变全局状态。
* 审计域退出时 `generated` 来自该域的 append-only pending 日志，因此即使
  域中途自己清过队列，报告仍精确反映本次任期真正排队的 trap（含重复）。
* `inspect()` 是运行时的**观察通道**，仅供 harness/演示读取暂停域状态；
  运行中的域没有任何 API 能触达别域 ledger。

---

## 演示场景（`python3 -m fpiso`）

| 场景 | 展示内容 |
| --- | --- |
| `midpoints` | 同一精确中点 `17/16`，五个只有舍入方式不同的域，得到 3 种结果、同一 `{inexact}` 事件 |
| `masks` | 同一 `1/3` 在不同屏蔽位下：值与黏滞态不变，只有 pending 队列不同 |
| `nested` | 外层暂停 → 审计域运行 → 审计退出；审计下溢不进入外层，外层 pending 不被吞掉；观察通道读数证明外层暂停期间状态不变 |
| `fma` | `fma(9/8,9/8,-5/4)` 与“先乘后加”的对照：融合路径保留精确乘积 `81/64`，最终 `1/64` 触发下溢；分离路径先舍入成 `5/4`，丢掉了真实误差 |
| `probe` | 两个兄弟域交替运行，逐步打印“各域此刻能读到的精确状态”，暂停域不变 |
| `contrast` | 一个故意共享全局状态的“泄露版”运行时，用于对照 fpiso 所防止的失败模式 |

### 实测输出摘录（本地运行，2026-11-28）

`python3 -m fpiso demo` 的实际输出（节选）：

```
=== scenario 1: one exact midpoint, five domains ===
  rne: value 1     ... flags {inexact}  sticky {inexact}  pending []
  rna: value 9/8   ... flags {inexact}  sticky {inexact}  pending []
   rz: value 1     ... flags {inexact}  sticky {inexact}  pending []
   rd: value 1     ... flags {inexact}  sticky {inexact}  pending []
   ru: value 9/8   ... flags {inexact}  sticky {inexact}  pending []

=== scenario 3: nested audit domain over a suspended outer domain ===
  outer before nesting: sticky {overflow, inexact} pending ['inexact', 'overflow', 'inexact']
  audit reads itself : sticky {underflow, inexact} pending ['underflow']
  harness reads outer: sticky {overflow, inexact} pending ['inexact', 'overflow', 'inexact']  (unchanged)
  audit tries to drain outer pending -> blocked with SuspendedDomainError
  exit 'audit': ops=2 delta={underflow, inexact} generated=[underflow] remaining=[] consumed=[underflow]
  after resume      : sticky {overflow, inexact} pending ['inexact', 'overflow', 'inexact']

=== scenario 4: multiply-add rounds exactly once ===
  fused fma(9/8, 9/8, -5/4): exact product 81/64, exact sum 1/64,
    one rounding -> +0 flags {underflow, inexact}
  split mul then add: mul -> 5/4 flags {inexact}; add -> +0 flags {}
```

---

## 验证范围

测试位置：`tests/test_*.py`，运行 `python3 -m unittest discover -s tests -v`
（本机实测：**128 个测试全部通过**）。

| 文件 | 覆盖内容 |
| --- | --- |
| `test_format_and_rounding.py` | 格式派生量、可表示性、邻居、前驱/后继；五种舍入方式在中点、负数、有向舍入下的行为；溢出（含 `31/2` 舍入越界、finite-only 饱和、负号方向）；下溢（舍入后判小、精确子范数不报、舍入到最小正规数只报 inexact）；定向舍入区间夹逼 |
| `test_operations.py` | 操作数只接受精确值（拒绝二进制 `float`/`bool`/非法字符串/非有限 `Decimal`）；加减乘与单次舍入乘加的精确值、标志、符号零规则；融合与分离路径的结果差异 |
| `test_domain_state.py` | 屏蔽位与 pending 的关系、黏滞累计、`raised`、`take_pending`、`clear_*`、mask 变更不重写历史、快照不可变、`with_*`/`copy` 的 ledger 独立性 |
| `test_isolation_runtime.py` | 空栈错误、进入/退出报告、`expected` 名校验、上下文管理器异常退出；**暂停域不可执行/不可清理**、暂停期间状态冻结、审计退出不吞外层 pending、审计标志不泄露、休眠邻居不可写、LIFO 与跨运行时所有权、三层嵌套恢复、兄弟域交错、退出的“历史日志”精确性 |
| `test_visible_state.py` | 逐步走查：每个域在每一步能读到的**精确** `DomainState`（名称/舍入/屏蔽/sticky/pending），暂停与恢复的逐字段相等，以及三种格式对同一中点的对照 |
| `test_properties_and_stress.py` | 独立编写的暴力参考实现（二分查找整个可表示网格 + 独立 tie 规则 + 独立无界溢出判定）对 4 种格式、9 种分母、全部 5 种舍入方式做 **32040 组**逐项比对；随机嵌套调度下，每个域的状态必须与“单独运行同一操作流”完全一致（40 组随机调度，含 sticky 与 pending）；200 轮随机深度的进出栈不触发隔离错误 |
| `test_demo_and_cli.py` | 6 个演示场景可运行、确定性、关键画面；CLI 的 `demo`/`op`/`formats`、屏蔽参数、负数操作数、非法操作数错误码、`--format/--precision/--emin/--emax` 参数 |

### 证据状态说明

以上断言全部来自本机实际执行（`python3 3.11.6`，`python3 -m unittest
discover -s tests -v` 通过）。模型是**语义模型**而非硬件仿真：

* 没有与真实 CPU/FPU 的逐位比对（题面未要求，且模型允许任意小格式）；
* `test_properties_and_stress.py` 中的参考实现是**同一模型语义的独立第二
  实现**，用于交叉校验舍入内核，并非第三方权威实现；
* 未进行并发线程/异步 IO 测试：本模型把“协程切换”抽象为显式的
  `enter_domain` / `exit_domain`（或上下文管理器），切换点完全可枚举，
  这正是隔离语义需要建模的部分。

---

## 目录结构

```
fpiso/
  __init__.py   公开 API 与版本
  __main__.py   python3 -m fpiso
  cli.py        命令行：demo / op / formats
  demo.py       6 个脚本化演示场景
  exact.py      精确有理数输入转换（拒绝二进制 float）
  value.py      FPValue：有限值（含 -0）与无穷
  format.py     Format、RoundingMode、可表示网格、前驱/后继
  rounding.py   无状态单次舍入与溢出/下溢/非精确判定
  flags.py      Flag、MaskBits、StickyLedger
  ops.py        add / mul / fma 精确内核（单次舍入）
  domain.py     Domain：配置 + 独立的 sticky/pending ledger
  runtime.py    Runtime：保护域栈、暂停/恢复、退出报告
  errors.py     异常类型
tests/
  test_*.py     7 个测试文件，共 128 个测试
```
