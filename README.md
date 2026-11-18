# clock_audit — 混用时钟到期判定审计组件

应用评审场景：找出把不同来源时钟（monotonic / wall / 未标明）混用的到期判定。
组件读取标注了**单位**和**进程代次**的简化数据流（时钟读取、期限计算、保存、
恢复、比较），对每次比较给出审计结论。

## 审计规则

1. **同一时钟域 + 统一单位**：两个时间点求差时必须来源相同；单位不同但可
   换算（ns/us/ms/s）时自动统一到纳秒并记录备注，单位缺失或未知则无法统一。
2. **旧进程单调期限需基准转换证明**：恢复（Restore）的 `monotonic` 期限若
   来自不同进程代次，只有存在匹配的 `BaselineProof`（记录代次间偏移）才可
   参与当前比较，否则 REJECT。`wall` 时钟不受此规则约束。
3. **单侧有效时钟仍给出另一侧证据路径**：比较中只有一侧使用有效时钟时，
   结论（PENDING）中仍列出有效侧的来源与完整证据路径（事件 id 链）。
4. **未标明来源进入待核对**：`source` 缺失或不在有效时钟清单内的数据，
   结论为 PENDING，等待人工核对。

结论分为 `ACCEPT`（比较有效，给出 diff 与是否到期）、`REJECT`（比较被阻止）、
`PENDING`（待核对）。每条结论都附带 deadline 与 now 两侧的证据路径。

## 实际入口

```bash
# 运行验收测试（unittest，12 个用例，全部真实断言）
.venv/bin/python -m unittest discover -s tests -t . -v

# 对 JSON 数据流执行审计（示例涵盖 ACCEPT/REJECT/PENDING 三种结论）
.venv/bin/python -m clock_audit examples/sample_flow.json
# 退出码：存在 REJECT 为 1，否则为 0
```

库用法：

```python
from clock_audit import Auditor, ClockRead, DeadlineCompute, Compare

auditor = Auditor()
findings = auditor.ingest([
    ClockRead("r1", source="monotonic", value=1000, unit="ms", generation=1),
    DeadlineCompute("d1", base="r1", duration=500, duration_unit="ms"),
    ClockRead("r2", source="monotonic", value=1200, unit="ms", generation=1),
    Compare("c1", deadline="d1", now="r2"),
])
```

JSON 数据流格式见 `examples/sample_flow.json`，事件类型：
`clock_read` / `deadline_compute` / `save` / `restore` / `baseline_proof` /
`compare`。

## 验证范围（tests/test_clock_audit.py）

- **可接受结果**：同域同单位比较；ms/s 混合单位统一换算；带基准转换证明的
  跨代次单调期限恢复比较（含证明偏移的差值断言）。
- **应被阻止/拒绝的结果**：monotonic 与 wall 跨时钟域求差（REJECT）；无
  证明的旧进程单调期限参与比较（REJECT）；对照组：wall 时钟跨代次不受限。
- **边界与交错场景**：diff == 0 恰好到期、差 1ns 未到期；同代次保存/恢复
  无需证明；同一数据流中 ACCEPT/REJECT/PENDING 交错并存且互不干扰；
  未标明来源与单位缺失进入 PENDING，且单侧有效时钟时另一侧证据路径完整
  保留。

## 目录结构

```
clock_audit/          # 组件源码
  model.py            # 事件模型（含单位换算表、有效时钟清单）
  audit.py            # 审计器与结论（Finding/ValueFact）
  loader.py           # JSON 数据流加载
  __main__.py         # CLI 入口
examples/sample_flow.json
tests/test_clock_audit.py
```
