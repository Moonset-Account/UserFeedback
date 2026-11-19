# 命名历史索引（Naming History Index）

调查人员手里只有**文件对象编号**，需要还原某一条事件刚提交时该对象有哪些**完整路径**。
本仓库实现一个按事件序号提交的命名历史索引：输入初始目录树和按序提交的
`link` / `unlink` / `rename` / `exchange` 事件，按序号查询当时的全部路径。

## 实际入口

环境：Python 3.11.6（`python3 -V` → `Python 3.11.6`）。无第三方依赖。

### 命令行

```bash
# 查询单个对象在某序号下的全部路径（0 = 初始树，k = 第 k 个事件提交后）
python3 -m naming_history examples/scenario.json --seq 3 --id f-main

# 指定序号下所有对象的路径
python3 -m naming_history examples/scenario.json --seq 3

# 机器可读输出
python3 -m naming_history examples/scenario.json --seq 3 --id f-main --json

# 查看已提交的事件序号
python3 -m naming_history examples/scenario.json --list-events
```

退出码：`0` 成功；`1` 初始树或事件非法（被拒绝，不提交）；`2` 参数/查询序号越界或场景文件不可读。

### 测试

```bash
python3 -m unittest discover -s tests -t tests -v
```

`tests/support.py` 会被测试模块 import，因此必须带 `-t tests`（把 `tests/` 同时作为
起始目录与顶层目录）。全部 47 个用例均为真实断言，无跳过、无桩。

## 场景文件格式

```json
{
  "initial": {
    "objects": {"root": "dir", "proj": "dir"},
    "entries": [
      {"dir": "root", "name": "proj", "id": "proj"},
      {"dir": "proj", "name": "a.txt", "id": "f1"}
    ]
  },
  "events": [
    {"op": "link", "dir": "root", "name": "b.txt", "id": "f1"},
    {"op": "unlink", "dir": "root", "name": "b.txt"},
    {"op": "rename", "from": {"dir": "root", "name": "proj"},
                     "to":   {"dir": "root", "name": "docs"}},
    {"op": "exchange", "a": {"dir": "root", "name": "docs"},
                       "b": {"dir": "root", "name": "other"}}
  ]
}
```

- `objects` 可省略：被用作 `dir` 的 id 推断为目录，只出现在 `id` 位置的推断为文件。
- `root` 是唯一的无父目录。

## 设计要点与题面约束的对应

实现是“每提交一个事件，追加一份**不可变命名空间快照**”（`naming_history/index.py`），
路径**从不存储**，每次查询按当前父链即时推导（`naming_history/model.py`）。
这一结构直接消除了题面点名的两类错误：

| 题面约束 | 实现方式 | 对应用例 |
| --- | --- | --- |
| 文件可有多个硬链接 | 一个对象可被任意多条目录项指向 | `test_hardlinks.py` |
| 目录只能有一个父目录 | 目录作为子项出现两次即 `InvalidEventError`；`link` 拒绝链接目录 | `test_directory_rename.py` |
| 改名移动目录会改变整棵子树的可见前缀 | 路径由父链推导，子项无需改写 | `test_directory_rename.py` |
| 按序号查询当时的全部路径 | 快照按序号索引，旧快照永不被改写 | `test_hardlinks.py::HistoryIsAppendOnlyTest` |
| 不能把后来才出现的同名对象算进去 | 每个序号只读自己的快照；未来对象抛 `FutureObjectError` | `test_name_reuse.py` |
| 交换的中间临时路径不得成为可查证据 | `exchange` 是**单次**状态提交，不产生任何临时名 | `test_exchange.py`、`test_atomicity_crosscheck.py` |

### 值得注意的判定

1. **同名复用**：`/report` 先属于 `v1`，删除后 `v2` 重新占用 `/report`。
   查询 `seq=2` 的 `v1` 会抛 `GoneObjectError`，不会返回 `/report`。
2. **未来对象**：查询早于对象出现序号的 `paths_at` 抛 `FutureObjectError`。
   两者都是 `UnknownObjectError` 的子类，调查方如需兼容可只捕获父类。
3. **对象编号不得复用**：一个对象完全离开命名空间（文件最后一条链接消失、空目录被删除）
   后，其 id 进入 `retired`，再被 `link` 引入会抛 `InvalidEventError`。
   否则同一编号在历史前后会指向两个不同对象，这正是“证据混淆”的根源。
4. **交换原子性**：`exchange` 只产生一个序号。对照用例
   `test_three_step_swap_exposes_a_scratch_path` 用三次 `rename` 走临时名，
   能观察到 `/.swap-tmp`；而 `exchange` 在任何序号都观察不到。
5. **被拒绝的事件不提交**：`commit()` 先校验再追加，失败时序号不前进，
   后续序号也不偏移（`test_atomicity_crosscheck.py::test_rejected_event_mid_timeline_does_not_shift_later_sequences`）。
6. **改名覆盖规则**：文件覆盖文件、目录覆盖**空**目录允许；目录覆盖非空目录或文件、
   文件覆盖目录均拒绝（`test_rename_rules.py`）。

## 验证范围

- 47 个 `unittest` 用例，覆盖：多硬链接、目录子树改名、目录单父约束、
  序号隔离、未来/已消失对象区分、id 复用拒绝、原子交换无临时名、
  改名覆盖规则、命令行端到端（含退出码与 JSON 输出）。
- 正反两面均有用例：既覆盖可接受结果，也覆盖应被阻止/拒绝的结果；
  `test_atomicity_crosscheck.py` 提供交错场景（同一时间线上 link/rename/exchange 组合）。
- 未执行的部分：无。全部用例已在 Python 3.11.6 下实际运行通过。

## 目录结构

```
naming_history/
  model.py   不可变命名空间：不变量校验、路径推导、四类事件
  index.py   按序号索引快照；commit 先校验后追加
  tree.py    初始树与事件流解析
  cli.py     命令行入口（python -m naming_history）
tests/       47 个验收用例（unittest）
examples/    可运行的示例场景
```
