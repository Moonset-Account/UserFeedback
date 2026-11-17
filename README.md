# 可解释匿名化方案搜索（k-匿名 / l-多样）

为研究数据外发场景提供的纯标准库 Python 实现：在"整批每字段统一泛化层级"
（full-domain generalization）模型下，穷举所有字段层级向量，返回满足
k-匿名与 l-多样约束、且总损失最低的确定方案；无可行方案时明确报告。

## 文件

| 文件 | 说明 |
| --- | --- |
| `anonymizer.py` | 库：树结构、求解器、结果与逐组核验依据 |
| `tools/local_verify.py` | 本地核验入口（`--list` / `--case C01..C04`） |

## 数据模型与 API

```python
from anonymizer import GenNode, GeneralizationTree, anonymize

# 1. 为每个准标识字段构建有根泛化树（最多 4 层，含根）
age_tree = GeneralizationTree(
    GenNode("Age*", [
        GenNode("young", [GenNode("a18"), GenNode("a25")]),
        GenNode("mid",   [GenNode("a40"), GenNode("a52")]),
    ]),
    # missing_leaf=<叶节点> 可指定缺失值(None)的独立叶；本树未指定
)

# 2. 求解
result = anonymize(
    records=[{"age": "a18", "cond": "sA"}, ...],   # 记录（dict 列表）
    quasi_identifiers={"age": age_tree},           # 1..6 个字段
    sensitive_field="cond",                        # 敏感分类字段
    k=2, l=2,                                      # 组大小 / 敏感值多样性下限
    max_suppressed=3,                              # 最多可抑制记录数
    loss_weights={"age": [0, 1, 2]},               # 每字段每级损失权重
    suppression_penalty=10,                        # 每条抑制记录的固定惩罚
)
```

**记录取值解析**（`GeneralizationTree.resolve`）：

- `None` → 该树指定的缺失叶节点（未指定则报错）；
- `GenNode` 对象 → 必须是本树的叶（按**身份**校验）；
- 其他原始值 → 按叶节点显示名匹配，必须唯一；显示名重复时报错，
  调用方需直接传入叶节点对象（树中显示名允许重复，分组比较一律使用
  节点身份，绝不按名称合并）。

**层级语义**：层级 `g` 表示从叶向根走 `g` 步（到根为止）。字段最大层级
= 最深叶深度（≤3）。整批记录对每个字段使用同一层级。

**损失模型**：

```
per_record_loss = Σ_fields loss_weights[field][level[field]]
total_loss      = retained_count  * per_record_loss
                + suppressed_count * suppression_penalty
```

分组按泛化后准标识组合（节点身份元组）。保留组须满足 `size >= k` 且
不同敏感值数 `>= l`；不合格组**整组抑制**。抑制总数超过
`max_suppressed` 的层级向量不可行。同损失时按字段层级向量字典序（字段
顺序为 `quasi_identifiers` 的插入顺序）取最小者，结果确定。

**输出**（`AnonymizationResult`，另有 `to_dict()`）：

- `feasible` / `reason`：无可行方案时 `feasible=False`，`reason` 给出
  预算与可达的最小抑制数；
- `levels`：选定层级（每字段一个整数）；
- `retained`：保留数据（泛化后显示名 + 节点路径 + 敏感值 + 原始索引）；
- `suppressed_indices` / `suppressed_count`；
- `groups`：逐组核验依据（`GroupReport`：显示名键、身份路径键、成员
  索引、组大小、不同敏感值数、`meets_k`/`meets_l`、保留/抑制决定及理由）；
- `total_loss` / `generalization_loss` / `suppression_loss`；
- `candidates_evaluated`：实际评估的层级向量数（上限 4^6 = 4096）。

## 本地核验

```bash
.venv/bin/python tools/local_verify.py --list        # 列出用例
.venv/bin/python tools/local_verify.py --case C01    # 运行单组
```

每组输出一条 JSON：`case`、断言总数、`passed/failed/skipped`、确定性
种子（`seed=20261117`，数据为合成构造）、期望/观察摘要及失败明细。
任一断言失败、断言数为零或用例异常均以非零退出。全部使用合成数据，
不输出凭据或真实敏感样本。

- **C01** 小域穷举：2 字段 × 3 层级的全部 9 个向量，与核验器内独立
  暴力实现交叉对照 k/l 约束、整组抑制与最低总损失（场景 A 全保留、
  场景 B 抑制两个整组）。
- **C02** 损失核算：保留记录泛化损失 = 保留数 × 每记录权重、抑制惩罚
  = 抑制数 × 单价；构造 (0,1) 与 (1,0) 同损失并列，验证按层级向量
  字典序取确定结果，且两次运行结果一致。
- **C03** 同名不同身份节点（两个显示名均为 `Dup` 的中间节点不合并）、
  独立缺失叶（`None` 记录单独成组）、整批每字段统一层级；另覆盖重名
  叶解析报错、未知值/外来节点/无缺失叶等解析边界。
- **C04** 抑制预算边界（最小抑制数 = 预算可行、预算 −1 不可行并给出
  原因）、无可行解报告、最大声明规模（6 字段 × 4 层，4096 个候选向量）
  及超限（7 字段 / 5 层树 / 权重长度不符）拒绝；校验逐组核验依据字段
  完整且决定一致。

可重放样本：上述命令即可完整重放；样本全部在 `tools/local_verify.py`
内以合成字面量构造（如 8/9 条记录、敏感值标签 `sA`–`sE`），无外部数据
依赖。

## 未覆盖边界（审计时保留未知）

- 搜索为穷举：复杂度 O(4^字段数 × 记录数)，声明上限 6 字段 / 4 层；
  超出规模的性能未评估。
- 损失并列比较使用数值精确相等；浮点权重（如 0.1+0.2）的舍入误差
  可能改变并列判定，建议权重使用整数。
- 敏感值仅要求可哈希；语义层级（如敏感值本身的泛化）不支持。
- 抑制仅为整组抑制；记录级个别抑制、单元格级泛化不在模型内。
- 缺失值必须显式指定缺失叶；多缺失语义（NA/未知/拒绝回答）不区分。
- 权重长度必须与字段最大层级完全一致；层级饱和（浅叶提前到根）时
  更高层级的权重仍按声明计入。
- 核验包装器是运行入口而非合格证明；审计仍应阅读 `anonymizer.py`
  源码与真实运行日志。
