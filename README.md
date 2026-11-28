# 凭条核对模块（ticket_check）

离线核验一枚纸凭条的两联（窗口联与存根联）能否来自同一串随机格纹。

纸面原串是 `n_cells` 个二进制格。折叠后两联各自抄取其中一部分：折叠方案给出
「联名 + 联内位置 → 原串格号」的映射，于是两联的观测其实都是对**同一批原串格**的
观测。原串另需满足一组二元奇偶校验（每个校验列出若干原串格，其异或须等于给定
比特）。模块只在 GF(2) 上做线性代数，回答：

* 两联能否来自同一串；
* 若能，还有多少种原串，哪些**未观测**格已被唯一确定；
* 若不能，给出一组**相互矛盾且不可再删**的观测与校验；
* 追加勘误可替换指定观测，同时完整保留原始记录。

纯标准库，无第三方依赖，主分析路径从不用穷举。穷举只在小凭条上单独提供，用于
交叉核对计数。

## 公开 API

### 输入

| 名称 | 说明 |
| --- | --- |
| `FoldScheme(n_cells, folds)` | 折叠方案。`folds` 是 `{联名: 序列}`，序列下标是联内位置，值是该位置显示的原串格号。构造时校验格号范围与非空。 |
| `FoldScheme.opposing(n)` | 常见对折：窗口联从左到右、存根联从右到左显示同一串。 |
| `FoldScheme.from_permutation(perm, *, window=None, names=("window","stub"))` | 由显式格号置换构造。`perm[i]` 是存根联位置 `i` 对应的窗口联位置；`window` 可给出窗口联显示的格子子集（默认 `0..k-1`）。置换不合法会抛 `TicketError`。 |
| `Check(cells, parity=0, label="")` | 一条奇偶校验：所列格的异或须等于 `parity`。同一校验内不得重复列同一格。 |
| `Observation(ticket, position, value)` | 某联某位置的观测。`value=None` 表示该格未知（擦除）。 |
| `Erratum(ticket, position, value, note="")` | 勘误：替换该位置的有效观测；`value=None` 表示改为未知。原始观测记录不被销毁。多条勘误按传入顺序依次生效。 |

所有输入在构造或分析时做类型校验（`bool` 不被当作整数接受，越界格号、未知联名、
重复观测、校验内重复格号均抛 `TicketError`）。

### 核心函数

```python
analyze_ticket(scheme, checks, observations=(), errata=()) -> AnalysisResult
```

`AnalysisResult` 字段：

| 字段 | 含义 |
| --- | --- |
| `compatible` | 两联能否来自同一原串。 |
| `n_cells` | 原串格数。 |
| `rank` / `free_variables` | 约束的秩 / 自由度（仅相容时有值）。 |
| `solution_count` | 仍可能的原串数 `= 2 ** free_variables`（仅相容时有值）。 |
| `observed_cells` | `{格号: 有效观测值}`（已应用勘误）。 |
| `determined_unobserved` | `{格号: 值}`：未观测但被唯一确定的格。 |
| `undetermined_unobserved` | 仍未确定的格号元组（升序）。 |
| `contradiction` | 矛盾证据（仅不相容时有值），见下。 |
| `effective_observations` | 每个联内位置一条，含 `original` / `had_original` / `effective` / `amended`。 |
| `erratum_records` | 勘误审计轨迹，含每条勘误生效前的有效值、原始值、是否替换过既有观测。 |

辅助方法：`result.status`（`"consistent"` / `"contradictory"`）、`result.unobserved_cells`、
`result.describe()`（人类可读摘要）。

### 矛盾证据

`Contradiction`：

| 字段 | 含义 |
| --- | --- |
| `reason` | `"value_conflict"`（同一格被两处观测成不同值）或 `"parity_conflict"`（校验与观测组合矛盾）。 |
| `items` | `CertificateItem` 元组，每项含 `kind`、`label`、`cells`、`value`，观测项另含 `ticket` / `position`。 |
| `xor_rhs` | 恒为 1。 |

不变量：`items` 左端（各 `cells`）异或为空集，右端（各 `value`）异或为 1；
且**任删一项都不再矛盾**（不可约简）。`describe()` 会逐项列出。

### 小凭条穷举（仅用于核对）

```python
enumerate_solutions(scheme, checks, observations=(), errata=(), *, max_cells=20)
count_solutions_by_enumeration(scheme, checks, observations=(), errata=(), *, max_cells=20)
```

按 `2**n_cells` 逐串枚举满足系统的原串；`n_cells > max_cells` 抛 `TicketError`。
它们的输出应与 `analyze_ticket` 的 `solution_count`、`determined_unobserved`、
`undetermined_unobserved` 完全一致，测试即以此交叉验证。

## 运行方法

环境：Python 3.11.6（仅标准库）。

```bash
# 全量测试
python3 -m unittest discover -s tests -v
```

在仓库根目录直接运行即可，无需安装任何依赖。

## 最小用法示例

```python
from ticket_check import Check, Erratum, FoldScheme, Observation, analyze_ticket

scheme = FoldScheme.opposing(4)          # 4 格，存根联为反向
checks = [Check(cells=(0, 1, 2), parity=0, label="row")]
observations = [
    Observation("window", 0, 1),
    Observation("window", 1, 0),
    Observation("stub", 2, 1),           # 显示原串格 1，与 window[1]=0 冲突
]
print(analyze_ticket(scheme, checks, observations).describe())

fixed = analyze_ticket(
    scheme, checks, observations,
    [Erratum("stub", 2, 0, note="misread")],   # 修正为 0
)
print(fixed.describe())
```

## 验证范围

`python3 -m unittest discover -s tests -v` 共 41 个用例，全部通过（约 0.13 s）。
分三块：

1. **`tests/test_ticket_check.py`** — 正常与边界：
   * 全观测相容（唯一解）、部分观测、未知（擦除）值；
   * 两联同格异值冲突、观测与校验冲突、空校验列表的边界（奇偶非 0 即矛盾）；
   * 大凭条（300 格）只剩 2 个已知格时的精确计数；
   * 勘误：修复冲突且保留原始记录、改为未知、引入新观测、多条按序生效、
     勘误本身制造矛盾；审计字段逐项断言；
   * 输入校验：重复观测、越界位置、`bool` 值、校验内重复格、越界格；
   * 枚举与解析结果逐项一致（含勘误场景）。

2. **`tests/test_certificates_and_scale.py`** — 矛盾证据与规模：
   * 证据不可约简性：随机 500 例 + 冗余结构 400 例，逐例用独立求解器验证
     「整组矛盾、任删一项即相容」，并核对 `XOR(lhs)=0, XOR(rhs)=1`；
   * 重复校验行等零和子集合不会混进证据；
   * 随机 120 例（含随机折叠、观测、擦除、勘误）与**穷举**及**独立 GF(2)
     求解器**三方对照：相容性、计数、秩、已定格、未定格全部一致；
   * 由隐藏串构造的可满足系统，已定格取值须等于隐藏串对应值；
   * 规模：400 格链式校验（秩 400，唯一解 / 无观测时 2 解且无格被单独固定）、
     350 格精确计数、400 格长环矛盾（证据含 400 项）检测 + 证书校验、
     220 格随机系统与独立求解器对照。

3. **`tests/reference.py`** — 测试专用独立实现：不 import 模块本体，用稠密
   位掩码高斯消元 + 零空间基，从头计算秩、特解、已定格。

### 实测记录

除单元测试外，开发期用一次性脚本（已删除，未留在仓库）做过更大规模抽查，结论
如下，可用上列测试复现同类断言：

* 17 万+ 随机矛盾样例（含重复/近重复校验、结构化链/环、观测与校验混合）：
  全部证据均满足「整组矛盾且任删一项即相容」，零反例。
* 3000 例随机系统与穷举 + 独立求解器三方对照：计数、秩、已定格、未定格全一致
  （其中 1240 例相容、1760 例矛盾）。
* 计时抽查：100 / 500 / 2000 格随机系统各在毫秒级；2000 格长环矛盾约 0.01 s。

## 说明与限制

* 求解在 GF(2) 上精确，计数用 `2 ** 自由度`，不涉及浮点，不会溢出。
* 未观测格的「唯一确定」指：在所有仍可能的原串中该格取值相同；被约束成与其它
  未观测格联动（例如整条链同涨同落）的格**不算**唯一确定，会列入
  `undetermined_unobserved`。
* 矛盾证据是**一组**不可约简证据，不保证是全局最小（最小矛盾集合是 NP 难问题）；
  也不保证唯一——不同的最小电路都可能被返回。
* 折叠方案是显式给定的格号映射，模块不推断折叠几何；同一格可被两联同时显示，
  这正是核对冲突的来源。
