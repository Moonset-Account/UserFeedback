# 只读删除文件取证恢复模块

针对“已删除文件旧簇链仍在，但现场字节不一定还属于它”的场景，实现一个
**只读取证**判定模块：输入删除时文件大小、逻辑偏移→物理簇映射、采集分块
内容以及分配 / 改写 / 记录缺口证据，仅对**同时满足连续归属依据与采集成功**
的区间输出原字节，其余区间逐段附拒绝原因，末簇按原文件大小裁切，报告中
每段都可回查到物理簇与簇内字节范围。

## 目录结构

```
readonly_forensics/
  __init__.py    公开 API 导出
  model.py       输入数据模型（frozen dataclass）与输入校验、JSON 构造
  engine.py      判定引擎：时间窗过滤、归属污染、采集校验、区段切分
  report.py      Segment / RecoveryReport、汇总、JSON 与文本渲染
  cli.py         命令行入口（python -m readonly_forensics.cli）
tests/
  test_recovery.py   unittest 验收测试（26 个用例）
examples/
  generate_sample_case.py  生成纯合成示例案例 case.sample.json
  smoke_check.py           端到端冒烟断言
  case.sample.json         合成夹具（由生成脚本产出）
```

## 输入模型（`RecoveryCase`）

| 字段 | 含义 |
| --- | --- |
| `file_size` | 删除时的原文件大小（字节） |
| `cluster_size` | 簇大小（正整数） |
| `deletion_time` / `acquisition_time` | 删除与采集时刻，判定窗口为 `[删除, 采集)` |
| `cluster_chain` | 逻辑簇序号→物理簇号的有序映射（元组，或 JSON 中的数组 / 映射） |
| `chunks` | 物理簇→采集分块 `Chunk`，状态为 `ok` / `missing` / `error` |
| `allocations` | 窗内物理簇被**其他对象**占用的记录（可已释放，可仍占用） |
| `overwrites` | 物理簇内字节区间被改写的记录（带簇内起止偏移与时刻） |
| `record_gaps` | 分配 / 释放记录缺口（作用域为整个物理簇，可开放到采集时刻） |

JSON 案例中 `ok` 分块的 `data` 使用 base64 字符串，由
`RecoveryCase.from_dict()` 解码。

## 判定规则（题面交叉约束的落地）

1. **时间窗**：只采纳 `deletion_time <= t < acquisition_time` 内的事件；
   删除前的改写 / 分配与采集时刻及之后的事件均不影响“删除时原文”。
2. **再分配一票否决**：窗内物理簇只要分配给过别的对象——即便后来释放、
   即便采集字节与原文逐字节相同——该范围一律 `reallocated`，绝不输出字节。
3. **记录缺口**：窗内存在记录缺口则归属不可追溯，标记 `record_gap`。
4. **改写**：窗内改写过的字节区间标记 `overwritten`；改写按簇内偏移把簇
   切成子区间，未改写部分仍可恢复。
5. **采集校验**：归属连续后还要通过采集层检查，按情况标记
   `acquisition_missing`（无分块或显式 missing）、`acquisition_error`
   （分块报错）、`acquisition_short`（短读，可用字节不足）。
6. **无映射**：簇链短于文件所需簇数时，缺失逻辑簇标记 `mapping_missing`。
7. **末簇裁切**：最后一个逻辑簇只判定到 `file_size` 为止，填充字节不输出。
8. **多原因交错**：同一范围命中多类问题时，报告原因按
   `reallocated > record_gap > overwritten` 取最严重者，但 `evidence`
   保留全部命中事件 ID，供人工回查。
9. **只读保证**：所有输入 dataclass 为 `frozen`，`chunks` 映射经
   `MappingProxyType` 封装；报告中的字节经显式拷贝，与输入证据解耦；
   模块不打开任何证据路径之外的文件，CLI 只读取案例文件。

## 区段与物理回查

每个 `Segment` 携带：

- 文件维度：`file_offset_start/end`、`logical_cluster`、`length`
- 物理维度：`physical_cluster` 与 `cluster_offset_start/end`
  （无映射区段物理坐标为 `None`）
- `status` / `reason` / 中文 `detail` / `evidence`（簇链引用 + 事件 ID）
- 已恢复区段的 `data`（拒绝区段为 `None`，不做猜测填充）

不变量：`file_offset_start - logical_cluster * cluster_size
== cluster_offset_start`，且所有区段长度之和恰好等于 `file_size`。

## 实际入口

Python API：

```python
from readonly_forensics import RecoveryCase, recover_file

case = RecoveryCase.from_dict(case_dict)   # 或直接构造 dataclass
report = recover_file(case)
report.to_dict()        # JSON 友好结构（字节为 base64）
report.materialize()    # 拼接全部已恢复字节，拒绝区段缺失不填充
report.render_text()    # 人工阅读文本
```

命令行（在仓库根目录执行）：

```bash
# 先生成合成示例
python3 -I examples/generate_sample_case.py

# JSON 报告到标准输出
python3 -I -c "import sys; sys.path.insert(0,'.'); \
from readonly_forensics.cli import main; main(['examples/case.sample.json'])"

# 文本报告写到新文件（不写回证据）
python3 -I -c "import sys; sys.path.insert(0,'.'); \
from readonly_forensics.cli import main; \
main(['examples/case.sample.json','--format','text','--output','report.txt'])"
```

> 说明：`python -I` 隔离模式不把当前目录加入 `sys.path`，因此示例通过
> 显式 `sys.path.insert` 指定工作区路径；常规模式下可直接使用
> `python -m readonly_forensics.cli ...`。

## 测试与验证范围

运行测试（二选一，均在仓库根目录）：

```bash
python3 -m unittest discover -s tests          # 常规模式
python3 -I -m unittest discover -s tests       # 隔离模式（可发现 cwd）
```

端到端冒烟：

```bash
python3 -I examples/smoke_check.py
```

`tests/test_recovery.py` 共 26 个用例，全部含真实断言，覆盖：

- **可接受结果**：三簇完整恢复、末簇裁切到 100 字节、簇整数倍文件、
  每段物理回查坐标自洽、`materialize()` 内容精确。
- **应被阻止 / 拒绝**：
  - 再分配后已释放且字节碰巧相同 → `reallocated`（核心交叉约束）；
  - 再分配持续到采集时刻；
  - 记录缺口（开放缺口、删除前缺口被忽略）；
  - 簇内改写切分、采集缺失 / 错误 / 短读、映射缺失。
- **边界与交错**：时间窗边界（删除时刻含、采集时刻不含）、同簇
  “恢复—拒绝—恢复”交错切分、多原因优先级与证据合并、簇链短于需求、
  相邻簇一成一拒。
- **输入校验与只读**：簇链超长 / 重复、改写越界与空区间、时间顺序、
  分块状态与数据不匹配、dataclass 不可变、输入字节不被修改且输出为副本。
- **序列化与 CLI**：base64 JSON 往返判定等价、CLI 标准输出 JSON 与
  文本文件输出均含事件证据与物理位置。

## 设计边界

- 模块不接触真实磁盘 / 镜像，所有证据由调用方以数据结构提供；本仓库
  示例均为本地合成夹具，不含任何实际目标数据。
- 不做内容猜测或 carving：归属或采集任一证据不足即拒绝，拒绝字节不参与
  `materialize()` 拼接。
- 时间戳只要求可比较（整数 / 浮点），不绑定具体时间体系。
