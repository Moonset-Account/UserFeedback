# 布尔电路信息流检查器

一个仅使用 Python 3.11 标准库的小型精确信息流检查器。输入由 **AND、OR、NOT、MUX** 构成的无环布尔电路，标注公开输入、至多 12 个保密输入、调用方批准公开的摘要输出，以及实际对外输出。

检查的合规属性是：

> 对每一种公开输入取值，如果两份秘密产生相同的批准摘要，那么它们在实际对外输出上也不可区分。

形式化地，设公开输入为 \(p\)，秘密为 \(s_1,s_2\)，摘要输出为 \(A\)，对外输出为 \(O\)，则安全条件为：

\[
\forall p,s_1,s_2:\quad
A(p,s_1)=A(p,s_2)\ \Longrightarrow\ O(p,s_1)=O(p,s_2)
\]

若不满足，检查器返回一对完整、确定、可重放的输入，并按 `external_outputs` 的顺序标出第一个不同的外部位。若满足，即使大量中间节点依赖秘密，结论仍然是安全的。

## 公开 API

推荐从包根导入：

```python
from info_flow_checker import (
    CircuitBuilder,
    Policy,
    check_information_flow,
    evaluate,
    evaluate_nodes,
    propagate_constants,
    circuit_from_dict,
    circuit_to_dict,
)
```

### 1. 构造电路

```python
from info_flow_checker import CircuitBuilder, check_information_flow

b = CircuitBuilder("cancellation")
p = b.input("p")
s = b.input("s")

not_s = b.not_(s, node_id="not_s")
left = b.and_(s, p, node_id="left")
right = b.and_(not_s, p, node_id="right")
decision = b.or_(left, right, node_id="decision")

b.output("decision", decision)
circuit = b.build()

report = check_information_flow(
    circuit,
    public_inputs=["p"],       # 至多 8 个
    secret_inputs=["s"],       # 至多 12 个
    summary_outputs=[],        # 允许公开的摘要；可为空
    external_outputs=["decision"],  # None 表示全部电路输出
)

print(report.secure)           # True
print(report.describe())
```

`MUX` 参数顺序固定为：

```python
mux(condition, value_if_condition_true, value_if_condition_false)
```

### 2. `Policy`

也可以显式构造策略对象：

```python
from info_flow_checker import Policy

policy = Policy(
    public_inputs=["p0", "p1"],
    secret_inputs=["a", "b"],
    summary_outputs=["summary"],
    external_outputs=["decision", "summary"],
)
report = check_information_flow(circuit, policy)
```

约束：

- 每个电路输入必须恰好被标为 `public_inputs` 或 `secret_inputs` 之一；
- 公开输入最多 8 个；
- 保密输入最多 12 个；
- 标签不可重复，公开/保密不可重叠；
- 摘要输出和对外输出必须是已定义的电路输出名；
- `summary_outputs` 与 `external_outputs` 允许重叠；
- `external_outputs=None` 表示按电路输出顺序暴露全部输出，`external_outputs=()` 表示没有外部位。

### 3. 报告与可重放反例

`SecurityReport` 的主要字段：

- `secure: bool`
- `counterexample: Counterexample | None`
- `public_assignment_count` / `secret_assignment_count`
- `public_assignments_checked`
- `leaking_public_assignment_count`
- 标签和输出顺序
- `to_dict()`：生成 JSON 兼容结果
- `assert_secure()`：不安全时抛出 `AssertionError`

不安全时，`Counterexample` 提供：

- `inputs_a`、`inputs_b`：两份完整输入；
- `public_assignment`：证明公开输入相同；
- `secret_assignment_a/b`；
- `summary_a/b`：证明批准摘要相同；
- `external_a/b`；
- `first_difference.index/output/value_a/value_b`；
- `public_index`、`secret_index_a/b`：确定性的枚举编号。

重放：

```python
outcome = report.counterexample.replay(circuit)
assert outcome.valid
```

`replay_counterexample(circuit, counterexample)` 会用普通标量求值重新计算两份输入，并核验：

1. 公开输入相同；
2. 摘要输出相同；
3. 对外输出确实不同；
4. 重放得到的首个差异与报告一致。

输入编号中，列表第一个名称对应整数编号的最低位。完整字典始终随反例返回，因此重放不依赖调用方记忆该编码。首个外部位则严格按 `external_outputs` 列表顺序确定。

### 4. 求值 API

```python
evaluate(circuit, {"p": True, "s": False})
# {"decision": True, ...}，仅返回命名输出

evaluate_nodes(circuit, {"p": True, "s": False})
# 包含所有中间节点，可用于观察秘密是否影响内部线路
```

赋值接受 Python `bool`，也接受严格的 `0/1`。

### 5. 常量传播与共享子图

```python
optimized = propagate_constants(circuit)
```

该过程保持所有输入、输出名称/顺序和布尔语义不变，支持：

- 常量节点传播与折叠；
- `AND`/`OR`/`NOT`/`MUX` 的局部恒等式；
- 重复参数去重；
- 双重否定消除；
- 常量选择器的 MUX 分支选择；
- 两个 MUX 分支相同时消去 MUX；
- 折叠后的死代码清理；
- 保留非恒定共享子图：同一节点仍只计算一次并可被多个输出/门引用。

检查器本身是语义精确的，不依赖污点近似；常量传播是独立可用的电路变换，也可通过以下参数在检查前执行：

```python
check_information_flow(circuit, policy, constant_propagation=True)
```

## JSON 电路格式

```json
{
  "name": "example",
  "inputs": ["p", "s"],
  "nodes": [
    {"id": "not_s", "op": "not", "args": ["s"]},
    {"id": "g", "op": "and", "args": ["p", "s"]},
    {"id": "m", "op": "mux", "args": ["p", "g", "not_s"]}
  ],
  "outputs": {
    "decision": "m"
  }
}
```

可用操作名：

- `input`、`const`
- `and`（别名 `&`）
- `or`（别名 `|`）
- `not`（别名 `!`）
- `mux`（别名 `ite`、`if`、`select`）

常量节点示例：

```json
{"id": "tt", "op": "const", "value": true}
```

读取/序列化：

```python
circuit = circuit_from_dict(document)
document = circuit_to_dict(circuit)
```

也支持 `{"circuit": {...}, "policy": {...}}` 文档和紧凑节点形式。

## 命令行

```bash
python3 -m info_flow_checker examples/cancellation.json
```

退出码：

- `0`：检查完成且安全；
- `1`：检查完成但发现可区分秘密；
- `2`：JSON、电路或策略无效。

加 `--propagate-constants` 可在检查前执行常量传播。

## 实现方法与复杂度

检查器不是简单的“秘密位是否到达输出”污点分析，因此不会把“秘密到达中间线但在输出处抵消”误报为泄漏。

对每个公开赋值：

1. 用一个 Python 大整数作为覆盖全部 \(2^k\) 个秘密赋值的位集，\(k\le 12\)，所以位集最多 4096 位；
2. 每个电路节点只按拓扑顺序计算一次，AND/OR/NOT/MUX 分别对应大整数位运算；
3. 按摘要位向量对秘密分组；
4. 同组中若外部位向量不同，则得到反例。

这种分组避免枚举所有秘密对的笛卡尔积。最坏枚举规模为：

\[
2^8 \times 2^{12} = 1{,}048{,}576
\]

个完整输入组合；所有 256 个公开取值都会被扫描，并统计存在泄漏的公开取值数量。

## 题面要求的抵消电路核验

核心核验电路为：

```text
not_s = NOT s
left  = s AND p
right = not_s AND p
out   = left OR right
```

当 `p=1` 时：

- `(p=1, s=0)`：`left=0, right=1`
- `(p=1, s=1)`：`left=1, right=0`

秘密确实改变了中间分支，但两种情况下：

```text
out = left OR right = 1 = p
```

测试还枚举了 `p/s` 的四种组合，确认 `out == p`，检查报告为安全。另有 `MUX(s, p, p)` 电路核验“秘密影响选择线，但两个分支相同”的抵消情形。

## 运行测试

要求 Python 3.11.6，无需安装依赖：

```bash
python3 -m unittest discover -s tests -v
```

最近一次实际复核（Python 3.11.6）通过，27 个测试全部 OK，完整运行约 1.1 秒：

```text
Ran 27 tests ...

OK
```

## 已验证范围

`tests/` 中的 27 个 `unittest` 用例覆盖：

- 四种门的真值、MUX 参数顺序、标量求值；
- 多输出共享同一 DAG 子图；
- JSON/字典往返、操作别名、环和未定义引用拒绝、缺失赋值；
- 直接秘密泄漏、仅在特定公开值下触发的条件泄漏；
- 摘要碰撞导致的泄漏，以及外部输出顺序决定的“首个不同外部位”；
- 外部输出恰好等于批准摘要时的安全反证；
- AND/OR 分支抵消和 MUX 等分支抵消；
- 空摘要、空对外输出、零公开位、零秘密位、无常量输入电路；
- 恰好 8 个公开位 + 12 个保密位的安全/泄漏边界；
- 9 个公开位或 13 个保密位的拒绝；
- 未标注输入、标签重叠、未知输出等策略错误；
- 摘要输出与对外输出重叠的交错情况；
- 常量折叠、恒等式、死代码消除和共享子图保留；
- CLI 安全示例、泄漏退出码 `1`、无效文档退出码 `2`、常量传播开关和 JSON 可序列化反例；
- 160 个确定性随机 DAG 与独立标量分组预言机比对；
- 另外 60 个随机电路上的常量传播前后全赋值等价及判定一致性。

所有验证均使用本地合成数据，不访问网络、真实目标或凭据，也不安装依赖。
