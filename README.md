# cpu_sampler — 版本感知的 CPU 采样剖析组件

把「带时刻、进程号的模块装载/卸载事件」与「原始栈样本」按时间线回放，
用**采样发生时刻**对该进程有效的模块映射解析每个栈地址，并展开符号表
给定的内联调用链，输出从根到叶的完整调用栈。

本仓库是纯本地、无第三方依赖的 Python 实现，只包含源码、测试源码与本文档。

> **状态声明：测试与任何代码均未运行。** 本环境不执行命令、不构建、不跑测试。
> `tests/` 下的用例是针对合成数据编写的，未经执行验证；文中不声称"通过"，
> 只描述它们*预期*覆盖的行为。

---

## 为什么需要它

服务进程 `dlclose` 旧模块、再 `dlopen` 新构建，链接器很容易把新模块映射到
**旧模块刚释放的同一段地址**。如果剖析器在事后用"当前"符号表解释历史样本，
那么旧版本里采到的地址会被错误地归因到新版本的同地址函数，调用栈因此不可追溯。

本组件的做法：装载事件创建一个**映射代（mapping generation）**，它记录自己
源自哪一份 `module@version` 镜像；样本按采样时刻的有效映射解析。两代映射即使
基址、模块名相同，也各自绑定各自的镜像，于是**同一绝对地址落在不同版本上会得到
两份各自可追溯的调用栈**。

---

## 核心语义

### 1. 事件排序（输入可乱序）

排序键为 `(timestamp, phase, 输入序号)`：

| phase | 事件 |
|-------|------|
| 0 | `unload` |
| 1 | `load` |
| 2 | `sample` |

- 同一时刻：**先卸载，再装载，再采样**。因此新模块可以在旧模块卸载的同一瞬间
  复用其地址，此刻的样本归因到"变更之后"的映射。
- 同刻同相位的事件保留输入顺序（稳定排序），结果可复现。
- 输入顺序对最终结果没有任何影响。

### 2. 栈帧地址修正

栈按**根 → 叶**给出（`frames[0]` 是根，最后一帧是叶）。

- **叶帧**保存的是采样时的指令地址（instruction pointer），**原样**查表。
- **其余帧**保存的是返回地址（return address），它指向 call 指令**之后**。
  因此查表前**减去一个地址单位**（`RETURN_ADDRESS_ADJUSTMENT = 1`），
  才能落到调用者内的调用点。

这条差异在返回地址恰好等于下一个函数起始地址时尤其关键：不减 1 会错误地
归因到被调用函数，减 1 后正确落回调用者。

### 3. 内联链展开

符号表为一物理函数给出嵌套的内联区间树（`FunctionRange.inlines` /
`InlineFrame.children`）。命中某物理函数后，自外向内逐层下钻，把命中的每一层
依次输出为一帧：

```
物理函数 -> inline A -> inline B   ==>   [物理函数, inline A, inline B]
```

展开是**字面意义**的：一个原始栈帧可能贡献多帧，输出栈才是真正的根到叶展开。
每帧保留 `frame_index`（来自第几个原始帧）与 `inline_depth`（在链中的层级）。

### 4. 未定位地址保留占位

地址无法定位时**不丢弃**该帧，而是产出一个占位帧（`resolved=False`，
`name == "<unresolved>"`），并带上原因：

| `reason` | 含义 |
|----------|------|
| `no-mapping` | 该进程此刻没有映射覆盖该地址 |
| `outside-image` | 地址落在映射内但在镜像 `size` 之外（或修正后为负） |
| `no-function` | 在镜像内但落在函数之间的空洞 |

若地址落在某个映射内，占位帧仍记录该映射的 `module/version/base/generation`，
便于看出"当时属于哪个模块、只是没有符号"。

### 5. 递归帧保留

帧从不被合并或去重。递归导致的重复帧按出现次数原样保留；配合
`frame_index`，可以区分"同一次递归的两次出现"。

### 6. 每进程独立

映射按 `(pid, 地址区间)` 归属。不同进程可以映射同一模块的不同版本到同一基址，
互不干扰。

---

## 输入格式

`parse_trace()` 接受**已解码**的 Python 对象（如需从文件读取，请自行
`json.load`，本组件不触碰文件系统）。

```jsonc
{
  "images": [
    {
      "module": "libengine",
      "version": "1.0",
      "size": 4096,                 // 镜像地址范围 [0, size)
      "build_id": "engine-build-1", // 可选
      "functions": [
        {
          "name": "engine_loop",
          "lo": 0, "hi": 64,        // 相对模块基址的 [lo, hi)
          "inlines": [              // 可选，可多层嵌套
            {
              "name": "loop_step",
              "lo": 16, "hi": 48,
              "children": [
                { "name": "loop_body", "lo": 24, "hi": 40 }
              ]
            }
          ]
        },
        { "name": "engine_flush", "lo": 64, "hi": 96 }
      ]
    },
    {
      "module": "libengine",          // 同一模块的第二个构建
      "version": "2.0",
      "size": 4096,
      "build_id": "engine-build-2",
      "functions": [
        { "name": "engine_loop", "lo": 0, "hi": 56 },
        { "name": "engine_drain", "lo": 64, "hi": 96 }  // 同址，含义不同
      ]
    }
  ],
  "events": [
    { "type": "load",   "timestamp": 10, "pid": 1,
      "module": "libengine", "version": "1.0", "base": 65536, "size": 4096 },
    { "type": "sample", "timestamp": 20, "pid": 1, "frames": [65552, 65666] },
    { "type": "unload", "timestamp": 30, "pid": 1, "module": "libengine" },
    { "type": "load",   "timestamp": 30, "pid": 1,
      "module": "libengine", "version": "2.0", "base": 65536, "size": 4096 },
    { "type": "sample", "timestamp": 40, "pid": 1, "frames": [65552, 65666] }
  ]
}
```

- `unload` 的 `version`、`base` 都可省略。省略时按 `module` 匹配；
  若匹配到多条活动映射，必须补 `version` 或 `base` 消歧，否则报错。
- 所有区间均为**左闭右开** `[lo, hi)`。
- 未知字段、缺失必填字段、类型错误一律报错，避免拼写错误被静默吞掉。

上例回放后（`base = 0x10000`）：

- 样本 20（1.0）：`65552 = 0x10010` 是**调用者帧**，查表前减 1 得
  `0x1000F`，相对 `0x0F`，落在 `engine_loop` 内、但在 `loop_step`
  （相对 `[0x10, 0x30)`）之前 → `[engine_loop]`；`65666 = 0x10082` 是
  **叶帧**，相对 `0x82` 偏出 `engine_loop`/`engine_flush`，落入空洞
  → 占位帧 `no-function`。
- 样本 40（2.0，同一基址同一地址）：`0x0F` 仍在 `engine_loop`（2.0 无内联）
  → `[engine_loop]`；`0x82` 仍为空洞 → 占位帧，但 `version` 记为 `2.0`。

两个样本使用了完全相同的绝对地址，却分别绑定各自的构建，这就是"同址跨版本
各自可追溯"。注意 `0x10082` 是叶帧，按指令地址原样查表、不减 1；若把它当作
返回地址减 1，就会错误地偏移一个单位。

---

## 用法

```python
from cpu_sampler import Profiler, SymbolTable, build_profile

result = build_profile(trace_dict)          # 解析 + 回放
for stack in result.stacks:
    print(stack.describe())

# 或分步：
table = SymbolTable([image_v1, image_v2])
result = Profiler(table).run(events)

result.sample_count
result.placeholder_count
result.live_mappings                          # 回放结束时仍活动的映射
```

聚合：

```python
from cpu_sampler import build_call_tree, aggregate_stacks

tree = build_call_tree(result.stacks)         # 合并调用树（含内联层级）
for depth, node in tree.walk():
    print("  " * depth, node.name, node.inclusive, node.self_samples)

for spot in aggregate_stacks(result.stacks):  # 叶热点
    print(spot.name, spot.self_samples)
```

### 输出帧的字段

`ResolvedFrame` 同时保留"采样原值"和"实际查表值"，便于审计：

| 字段 | 说明 |
|------|------|
| `frame_index` | 该帧来自第几个原始栈帧（根为 0） |
| `inline_depth` | 在该原始帧内联链中的层级（0 为物理函数） |
| `role` | `leaf` / `call-site` / `inline` / `placeholder` |
| `address` | 采样给出的绝对地址，原样 |
| `lookup_address` | 实际查表的地址（返回地址已减 1） |
| `name` | 该层符号名 |
| `chain` | 该原始帧的外→内内联链 |
| `module` / `version` / `build_id` | 解析所依据的镜像身份 |
| `base` / `relative_address` | 所属映射基址与相对偏移 |
| `generation` | 装载事件的映射代号 |
| `reason` | 占位原因（仅占位帧） |

---

## 目录

```
cpu_sampler/
  __init__.py    公共导出
  errors.py      异常层级（均派生自 ProfilerError）
  model.py       不可变数据模型 + 构造期不变式校验
  symbols.py     符号表：二分定位函数，逐层下钻内联链
  events.py      事件类型与 (timestamp, phase, sequence) 排序
  mapping.py     每进程活动映射表；映射代与卸载匹配
  profiler.py    回放、地址修正、内联展开、占位
  frames.py      输出帧 / 输出栈
  report.py      调用树与叶热点聚合
  trace.py       JSON 对象解析（无 I/O）
tests/
  fixtures.py                   合成镜像与事件构造器
  test_resolution.py            叶/返回地址、内联展开、占位、递归
  test_mapping.py               乱序、同刻排序、跨版本地址复用、每进程
  test_symbols_and_parsing.py   符号表校验、解析、调用树
  test_readme_example.py        文档示例的回放回归
pyproject.toml                  仅打包元数据（无依赖）
```

## 不变式与错误

构造期即校验，非法符号表不会"带病"进入查询：

- 区间必须 `lo < hi`；
- 内联子区间不得越出父区间；
- 兄弟区间不得重叠（可相邻）；
- 函数区间不得越出镜像 `size`；
- 同一 `module@version` 不得注册两份不同镜像。

回放期错误：

| 异常 | 场景 |
|------|------|
| `UnknownImageError` | `load` 引用了未提供的镜像 |
| `MappingConflictError` | 同一进程内装载区间重叠，或同址重复装载 |
| `UnloadWithoutMappingError` | 卸载找不到活动映射 |
| `MappingIdentityError` | 卸载指定的版本与活动映射不符 |
| `AmbiguousMappingError` | 卸载匹配到多条活动映射 |
| `InputFormatError` | 输入不符合本文档 schema |
| `SymbolTableError` | 符号表本身非法 |

## 运行测试（本次未执行）

```sh
python -m unittest discover -s tests -t .
```

再次强调：以上命令**未在本环境中运行**，测试结果未经验证。
