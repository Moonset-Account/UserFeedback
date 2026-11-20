# ports 数组复合多键索引

## 组件说明

文档包含一个 `ports` 数组，每个端口元素记录所属机房（字符串 `room`）
与整数速率（`speed`）：

```json
{
  "id": "d1",
  "ports": [
    {"room": "west", "speed": 1000},
    {"room": "east", "speed": 9000}
  ]
}
```

本组件在该数组上实现一个**有序复合多键索引**，支持文档的新增、
整体替换与删除，并提供两种查询语义：

1. **同元素查询**（`same_element=True`，默认）：机房相等与速率下限
   两个条件必须由**同一个端口元素**满足；
2. **跨元素查询**（`same_element=False`）：允许同一文档里的
   **两个不同端口元素**分别满足机房条件和速率条件。

无论一个文档有多少个匹配端口，结果中该文档只返回一次。

## 目录结构

```
port_index/
    __init__.py        # 对外导出 Port / PortCompositeIndex / QueryResult
    index.py           # 索引实现（仅依赖 Python 标准库）
tests/
    __init__.py
    test_index.py      # 本地合成数据单元测试（unittest，无外部依赖）
README.md
```

## 索引结构与查询原理

### 索引条目

为每个端口元素维护一条有序条目 `(room, speed, doc_id)`，用
`bisect` 维护排序。同一文档里两个完全相同的端口会产生两条条目，
这与多键索引 "数组每个元素一条索引键" 的语义一致。

### 同元素查询：索引直接给出结果，回表 0 次

复合条目 `(room, speed, doc_id)` 同时携带两个条件的字段，一次
范围扫描 `(room, min_speed) ... room 分片末尾` 即可确定匹配条目。
条目存在本身就证明了同一元素同时满足两个条件，因此：

- `fetches == 0`（不需要回表取文档）；
- 对同一文档的多条匹配条目做去重，文档只返回一次。

### 跨元素查询：候选回表过滤

复合键无法用单条条目证明 "A 元素满足机房、B 元素满足速率" 这种
跨元素合取。因此先扫描该 `room` 分片的全部条目并去重得到候选
文档，再**逐篇回表**，在文档上检查两个独立的存在性条件：

- 存在一个端口 `room == 目标机房`；
- 存在一个端口 `speed >= 速率下限`。

`fetches` 等于去重后的候选文档数（没有候选时为 0）。

### 执行统计（`QueryResult`）

| 字段 | 含义 |
| --- | --- |
| `doc_ids` | 去重后的匹配文档 id 元组 |
| `entries_examined` | 扫描过的索引条目数（去重前），即候选条目数 |
| `candidate_docs` | 扫描条目涉及的去重候选文档数 |
| `fetches` | 回表次数；同元素查询恒为 0，跨元素查询等于候选文档数 |
| `same_element` | 本次查询使用的语义 |

## API

```python
from port_index import PortCompositeIndex

index = PortCompositeIndex()

# 写操作（重复 id 新增抛 ValueError；替换/删除不存在的 id 抛 KeyError）
index.add_document({"id": "d1", "ports": [{"room": "west", "speed": 1000}]})
index.replace_document({"id": "d1", "ports": [{"room": "west", "speed": 9000}]})
index.delete_document("d1")

# 同元素查询：默认
index.query("west", 5000)

# 跨元素查询：允许两个端口分别承担条件
index.query("west", 5000, same_element=False)

# 其它
index.entry_count     # 索引条目总数（重复端口各计一条）
len(index)            # 已索引文档数
"d1" in index         # 文档是否存在
index.get_document("d1")  # 按 id 回表取文档
```

速率下限为闭区间（`speed >= min_speed`）。`speed` 必须是非负整数，
`room` 必须是非空字符串，非法输入抛出 `TypeError` / `ValueError`。

## 题面场景走查

1. 文档 `d1` 初始为 **西区慢口（west/1000）+ 东区快口（east/9000）**，
   另有对照文档 `d2`（west/8000）。
   - `query("west", 5000)` 同元素查询：d1 的 west 口只有 1000，
     不满足；结果只有 d2，`fetches == 0`。
   - `query("west", 5000, same_element=False)` 跨元素查询：
     d1 的 west 口满足机房、east 快口满足速率，d1、d2 均命中，
     d1、d2 各回表一次。
2. 将 d1 替换为 **西区快口（west/9000）** 后，同元素查询结果中
   **多出 d1**：结果为 d1、d2，仍然零回表、文档去重。
3. 若 d1 有两个完全相同的 west/8000 端口（两条索引条目），
   替换为只剩一个后条目数减半；只要剩下的端口仍满足条件，
   d1 依然出现在结果中，且只出现一次。

## 测试

测试位于 `tests/test_index.py`，全部使用测试内手工构造的本地
合成数据（普通字典），无网络、无外部服务、无第三方依赖，覆盖：

- 替换前后同元素/跨元素查询结果变化与统计值；
- 重复端口的条目计数、去重与删除后仍然命中；
- 跨元素查询的回表过滤（有 west 口但无高速率端口的文档被剔除）；
- 速率闭区间边界、多文档去重；
- 新增/替换/删除的生命周期与错误契约、输入校验、入参副本隔离。

运行方式（在项目根目录）：

```bash
python -m unittest discover -s tests -v
```

> **注意：按照本次任务约束，未执行任何代码或测试，以上测试尚未
> 实际运行，不声明验证通过。** 实现仅使用 Python 标准库
> （`bisect`、`dataclasses`、`typing`），逻辑正确性依据代码审查
> 与走查保证，待允许执行时再运行确认。

## 设计取舍

- 仅实现题面要求的本地基础设施组件：单进程内存索引，未引入
  持久化、并发控制或网络接口。
- 同元素查询零回表是复合多键索引的核心收益；跨元素查询的回表
  次数被完整统计，调用方可以直接对比两种语义的代价。
- 替换采用 "先移除旧文档全部条目、再按新端口重建" 的方式，
  保证重复端口删除时索引条目与文档严格一致。
- 文档以副本形式存储，调用方在外部修改已提交的入参不会影响
  索引内部状态。
