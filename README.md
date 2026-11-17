# 结构化样本缩减库（reducer）

把“能稳定触发解析器异常的结构化样本”在不破坏复现的前提下缩减到便于人工复核：
删除记录必须连带依赖闭包，字段缩减不得破坏声明的引用与长度约束；
只有格式仍有效、且多次试验全部复现**原失败指纹**的候选才能替换当前样本。
超时与其他异常一律不算成功。试验有总预算，可暂停并从保存的候选与
试验记录继续；缓存严格绑定**候选内容 + 试验器版本 + 所需重复次数**。

## 目录结构

```
reducer/
  model.py       # Sample / Record / FieldSpec，规范化哈希
  validator.py   # StructuralValidator（引用完整性、必填、min_length）+ 调用方校验器适配
  tester.py      # Tester 协议、safe_run 五分类、接受条件 all_matches
  cache.py       # TrialCache：内容哈希/试验器版本/重复次数三要素绑定
  engine.py      # Reducer：依赖闭包、变换枚举、预算、暂停恢复、最小性报告
  stubs.py       # 确定性试验桩与演示样本（无外部状态、可重放）
tools/
  local_verify.py  # C01-C04 本地核验入口（真实断言，JSON 输出，非零退出语义）
  demo_run.py      # 端到端演示：依赖删除 / 偶发同指纹 / 暂停恢复
examples/
  demo_sample.json # 合成演示样本（非敏感数据，哈希 aef65871f722d3d0…）
```

## 实际 API

### 数据模型（`reducer.model`）

- `FieldSpec(name, kind="string"|"reference"|"references"|"any", min_length=0, optional=False)`
- `Record(rid, fields: dict, specs: dict[str, FieldSpec])`
- `Sample(records: list[Record])`
  - `content_hash()`：规范化 JSON（键排序、紧凑分隔）的 SHA-256，缓存绑定的内容要素；
  - `canonical_json()` / `to_dict()` / `from_dict()` / `copy()`。

### 校验器（`reducer.validator`）

- `StructuralValidator().check(sample) -> list[str]`：返回违例标签
  （`duplicate_id` / `dangling_reference` / `min_length` / `missing_required_field` /
  `type_error`），`validate(sample) -> bool`；
- `CallableValidator(fn, structural=None)`：适配调用方自定义
  `validate(sample) -> bool`；自定义函数返回 False 或抛异常均视为无效，
  结构约束始终先执行。

### 试验器（`reducer.tester`）

调用方对象只需两个成员：

- `version: str`——试验器版本，参与缓存绑定；
- `run(sample) -> str`——返回失败指纹；超时抛 `TimeoutError`，其他失败抛任意异常。

`safe_run(tester, sample, target)` 把每次试验归为：

| 状态 | 含义 | 是否计成功 |
| --- | --- | --- |
| `match` | 返回值等于目标指纹 | 是 |
| `other_fp` | 正常返回但指纹不同 | 否 |
| `timeout` | 抛出 TimeoutError | 否 |
| `error` | 抛出其他异常 | 否 |
| `fp_error` | 返回值不是字符串 | 否 |

`all_matches(results, required_repeats)`：恰好 required_repeats 次且**全部** match 才接受。

### 引擎（`reducer.engine`）

```python
reducer = Reducer(
    sample,                 # 初始样本
    tester,                 # 满足 Tester 协议的对象
    validator=None,         # 默认 StructuralValidator
    target_fingerprint="FP-...",
    required_repeats=3,     # 每个候选必须连续复现的次数
    budget=100,             # 全流程总试验预算（硬上限）
    cache=None,             # 可传入已有 TrialCache
)
report = reducer.run()      # ReductionReport
```

两类单次允许变换（`generate_transforms` 确定性枚举）：

1. `delete_records`：删除一条记录时自动带上**依赖闭包**
   （`dependency_closure`：谁引用了它、谁传递依赖它，一并删除），
   删除后重新跑结构校验，杜绝悬空引用；
2. 字段级：字符串在 `min_length` 之上逐字符截断（`truncate_field`）、
   可选字段整体移除（`remove_field`）；引用字段不直接改写。

报告 `ReductionReport.stop_reason` 只有两种结论：

- `minimal`：当前枚举的全部单次变换都已尝试且无一可接受，即
  **变换级最小**（相对本引擎固定变换集，不声称全局数学最小）；
- `budget_exhausted`：总预算在评估中途耗尽，**不**宣称最小；
  中断的变换在恢复后会重新评估。

暂停/恢复：

```python
reducer.save_state("state.json")
reducer2 = Reducer.load_state("state.json", tester=tester, new_budget=300)
reducer2.run()
```

状态含当前候选、试验计数、已尝试变换、完整报告与缓存；恢复时若
试验器 `version` 与保存时不一致会直接抛 `ValueError` 拒绝恢复。

### 缓存（`reducer.cache`）

`TrialCache` 的键 = SHA-256(内容哈希 + 试验器版本 + required_repeats)。
任一要素变化即缓存未命中，避免旧版本试验结论或更低重复次数的记录被复用。

## 核验命令

使用工作区内的 `.venv`，以隔离模式运行（不读取 `PYTHONPATH` 等环境污染）：

```bash
.venv/bin/python -I tools/local_verify.py --list
.venv/bin/python -I tools/local_verify.py --case C01
.venv/bin/python -I tools/local_verify.py --case C02
.venv/bin/python -I tools/local_verify.py --case C03
.venv/bin/python -I tools/local_verify.py --case C04
.venv/bin/python -I tools/local_verify.py --case all
```

退出码：全部断言通过为 0；任一断言失败、用例异常、零断言、整组跳过为 1；
参数错误/未知用例为 2。每组输出 JSON，含 `case / assertions / passed /
failed / skipped / failures / observations` 及样本哈希前缀、种子参数、
期望与观察摘要；不输出原始样本字段值或任何凭据。

### 用例与实际断言数（当前运行结果）

| 用例 | 关注点 | 断言数 | 结果 |
| --- | --- | --- | --- |
| C01 | 依赖闭包删除、悬空引用检测、min_length 约束、合法截断保持指纹、引擎端到端保留依赖链 | 13 | 全通过 |
| C02 | 偶发同指纹拒绝、超时拒绝、其他异常拒绝且与超时区分、确定性重复匹配接受、引擎不替换样本 | 12 | 全通过 |
| C03 | 预算硬上限与耗尽停止、状态保存/恢复、恢复与连续运行同结果、版本不一致拒绝、缓存三要素绑定、部分试验记录跨暂停补齐 | 15 | 全通过 |
| C04 | 变换级最小（二次运行零接受）、缩减结果保留原失败类别、预算停止不宣称最小、接受变换均有完整重复 match、样本确实缩小 | 11 | 全通过 |

演示：

```bash
.venv/bin/python -I tools/demo_run.py
```

## 可重放样本与确定性说明

- 合成样本：`examples/demo_sample.json`，6 条记录，依赖链
  `r_leaf -> r_mid -> r_target`；内容哈希前缀 `aef65871f722d3d0`。
- 所有试验桩（`reducer/stubs.py`）无外部状态：
  - `DependencyTester`：完整依赖链存在才返回目标指纹 `FP-PARSER-CRASH-42`，
    否则返回 `FP-DIFFERENT-CATEGORY`；
  - `FlakyMatchTester`：每 3 次调用中第 2 次返回目标指纹（由累计调用数决定，
    跨暂停恢复仍确定），演示“偶发同指纹”永远无法满足 required_repeats=3；
  - `TimeoutTester` / `OtherErrorTester`：分别稳定抛超时与 `RuntimeError`；
  - `AlwaysTargetTester`：恒返回目标指纹。
- 变换枚举顺序固定（记录顺序、字段名排序），同种子同预算的运行可逐字节复现；
  暂停后恢复的最终候选哈希与一次性连续运行一致（见 C03 与 demo 第三场景）。

## 结论表述纪律（不确定性不得宣传为最小）

- 仅当 `stop_reason == "minimal"` 才能表述为“单次允许变换无法再缩减”；
  这是**变换级**结论，不排除未来新增变换类型后继续缩小；
- `budget_exhausted` 只能表述为“因预算停止，最小性未知”；
- 偶发 match、超时、其他异常的候选一律拒绝，绝不替换当前样本；
- 核验包装器只是运行入口，不是独立合格证明；审计应阅读 `reducer/` 源码、
  保存的状态 JSON 与报告中的逐次试验记录，对无法证实的内容保留未知。

## 未覆盖的边界（如实声明）

1. 不实现真正的并行/墙钟超时调度：超时由试验器自身抛 `TimeoutError`
   表达，引擎负责分类与计数；
2. 最小性是相对固定变换集（闭包删除、字符串前缀截断、可选字段移除）的
   贪心变换级最小，不保证全局最小，也不做 ddmin 等更复杂搜索；
3. 字符串缩减只做前缀截断，不尝试字符替换/空白折叠等语义变换；
4. 引用字段不支持部分删除（多引用列表的逐项缩减未建模），引用调整只能
   通过记录闭包删除发生；
5. 状态文件为本地 JSON，未做加密/签名；恢复时校验 state_version 与
   试验器版本，但调用方需自行保证状态文件来源可信；
6. 校验器协议为同步本地调用，未覆盖远程/异步格式服务；
7. 未对超大样本做性能专项优化（变换枚举为 O(记录数 × 字段数) 量级）。
