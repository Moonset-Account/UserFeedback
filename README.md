# bufreview — 记录缓冲区复制初始化覆盖审查

源码审查组件：接收**简化控制流 + 字段布局 + 抽象写入/复制指令**（JSON 规格），
审查"系统把记录缓冲区的一部分复制到外部诊断接口"这一操作是否可能带出
未初始化的字段或对齐空间。

## 判定语义

- **写入只覆盖实际区间**：`write_field` 按字段布局区间、`write` 按
  `offset/length` 精确覆盖，不做对齐放大。
- **汇合取交集（must-analysis）**：清零整个缓冲区的分支与只写字段的分支
  汇合后，只有**所有路径都已初始化**的区间才被视为安全；不动点迭代，支持回边。
- **复制检查**：输出区间完整被覆盖 → `SAFE`；覆盖不全 → `UNSAFE`，
  并列出每个未初始化输出区间及**未覆盖它的具体路径**（节点 id 序列，
  简单路径枚举，超过 128 条标记 `paths_truncated`）。
- **动态长度**：复制长度为 `"dynamic"` 且缓冲区缺少静态布局（`size: null`）
  → `PENDING`（留待核实）；有静态布局时按缓冲区长度求值。缓冲区长度动态时，
  `zero_buffer` 视为覆盖 `[0, +∞)`。
- **整体判定**：任一复制 `UNSAFE` → `REJECTED`；否则任一 `PENDING` →
  `PENDING`；否则 `ACCEPTABLE`。

## 实际入口

```bash
.venv/bin/python main.py <spec.json>     # 或：.venv/bin/python -m bufreview <spec.json>
```

- 输入：JSON 规格（`buffer.size`/`buffer.fields`、`nodes[].ops/succ`、`entry`），
  指令为 `write_field` / `write` / `zero_buffer` / `copy`，示例见
  `examples/gap_spec.json`。
- 输出：stdout 打印 JSON 报告（判定、安全区间、未初始化区间与对应路径）。
- 退出码：`0` = ACCEPTABLE，`1` = REJECTED，`2` = PENDING。

## 验证范围（tests/test_review.py，unittest，14 例）

```bash
.venv/bin/python -m unittest discover -s tests -v
```

- **可接受结果**：清零分支与完整字段写分支汇合后复制整个缓冲区判 SAFE；
  相邻原始写入拼接成连续覆盖。
- **应被拒绝结果**：字段间对齐空洞 `[8,12)` 仅清零分支覆盖 → REJECTED，
  报告空洞区间与"只写字段"路径；字段只在单条分支写入不被信任。
- **边界/交错场景**：复制终点恰好落在已初始化边界（SAFE）与越出一字节
  （UNSAFE）并存于同一汇合点；嵌套分支中仅部分子路径初始化时，报告精确到
  具体子路径。
- **动态长度**：缺布局 → PENDING（留待核实）；有布局按缓冲区长度求值；
  动态缓冲区长度下 `zero_buffer` 覆盖静态复制区间。
- **CLI**：退出码 0/1/2 与 JSON 报告内容；非法规格（未知字段、悬空后继）
  在加载期抛 `ValueError`。

## 目录结构

```
main.py              # 实际入口
bufreview/
  intervals.py       # 半开区间集合代数（end=None 表示 +∞）
  model.py           # 规格模型与静态校验
  analysis.py        # must-analysis、复制检查、路径报告
  cli.py             # 命令行入口
examples/gap_spec.json
tests/test_review.py
```
