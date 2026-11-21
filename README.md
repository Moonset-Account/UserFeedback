# 静态链接归档成员选择预演

`linker_sim.py` 模拟链接器对普通对象与归档（静态库）的符号解析过程，
用于预演静态链接会选入哪些对象。纯 Python 标准库实现，无第三方依赖。

## 语义规则

- **普通对象总是选入**，其定义与引用直接并入符号表。
- **归档按需抽取**：成员仅当能提供某个*当前未满足的强引用*的定义时才被
  选入；弱引用本身不触发抽取。
- **单个归档从头反复扫描直到稳定**：抽取成员可能引入新的未满足引用，
  因此每轮抽取后从头重扫该归档，直到一整轮不再抽取任何成员。
- **显式归档组**（`group`）：组内归档按顺序反复扫描直到整组稳定；
  组处理完毕后，组外已经越过的归档不回头。
- **强弱定义**：已选单元中的强定义可替代先前的弱定义；弱定义不覆盖任何
  已有定义；两个强定义并存时报来源冲突（保留先出现者，冲突双方来源均
  记录在 `conflicts` 中）。
- 强引用可把同一符号先前的弱未定义升级为强未定义。

## 输入格式

JSON 文件，顶层为 `{"inputs": [...]}`，记录按链接顺序排列：

```json
{"type": "object",  "name": "main.o",
 "defines": ["main"], "undefs": ["foo"]}

{"type": "archive", "name": "liba.a", "members": [
  {"name": "a1.o", "defines": ["foo"], "undefs": ["bar"]}
]}

{"type": "group", "archives": [ /* archive 记录…… */ ]}
```

符号条目可以是裸字符串（等价于强符号）或
`{"name": "sym", "kind": "strong"|"weak"}`。

## 输出

`link(inputs)` 返回（CLI 则打印）一份报告：

- `included_objects`：选入的普通对象（按输入顺序）；
- `selected_members`：归档成员抽取顺序，每项含 `archive`、`member`
  与触发抽取的符号 `trigger`；
- `symbols`：最终符号来源（`strong`/`weak` + 定义单元，归档成员记作
  `lib.a(member.o)`）；
- `unsatisfied_strong_undefs`：仍未满足的强引用及其引用方列表；
- `unsatisfied_weak_undefs`：未满足的弱引用（仅信息，不构成错误）；
- `conflicts`：强定义冲突（符号、双方来源、保留的来源）。

## 用法

```sh
python3 linker_sim.py examples/mutual_ungrouped.json
python3 linker_sim.py examples/mutual_grouped.json
```

## 归档组演示

`examples/` 下是两份相互依赖的归档：`liba.a(a1.o)` 定义 `foo`、引用
`bar`；`libb.a(b1.o)` 定义 `bar`、又引用 `baz`；而 `baz` 由
`liba.a(a2.o)` 定义。

- **不成组**（`mutual_ungrouped.json`）：扫描 `liba.a` 时 `baz` 还不是
  未定义，`a2.o` 不被抽取；等 `libb.a(b1.o)` 引入对 `baz` 的强引用时，
  `liba.a` 已经越过、不回头，`baz` 留在未满足的强引用里。
- **成组**（`mutual_grouped.json`）：组内反复扫描，第二轮回到 `liba.a`
  补抽 `a2.o`，`baz` 被满足。

对比两份输出即可看出：成组多选了 `liba.a(a2.o)` 这一个成员。

## 测试

```sh
python3 -m unittest discover tests
```

测试使用纯本地合成数据（普通对象、归档、强弱符号、相互依赖的归档组），
覆盖：普通对象必选、按需抽取、单归档重扫、弱引用不触发、强定义替代弱
定义、强定义冲突、归档组多选成员、组外归档不回头等场景。

## 验证状态声明

**本仓库的源码与测试按任务要求未在本环境中执行**：未运行任何代码、
命令、构建或测试，未发起任何外部请求。上述用法与测试命令仅供参考，
不声明已验证通过。
