# 复合推理入口的模型装配模块

为复合推理流水线（向量编码 → 候选过滤 → 排序）在显存上限内选择各阶段的模型版本组合，并给出最高显存使用量；无法装配时给出具体冲突。

纯标准库实现，无第三方依赖，Python 3.11.6 验证通过。

## 装配模型

- **阶段按配置顺序串行运行**，相邻阶段间上一阶段的输出张量契约（`dtype` + `shape`）必须与下一阶段的输入契约完全一致（接口全匹配）。
- **工作区接续复用**：阶段依次执行，工作区可复用，峰值工作区取各阶段工作区的**最大值**而非求和。
- **权重全程驻留**：请求期间所有选中版本的权重都驻留显存；同名权重块为共享块，跨版本/跨阶段只计一次（同名字节数必须一致，否则配置校验报错）。
- **峰值显存 = 常驻权重总量 + 峰值工作区**，不超过 `vram_limit_bytes` 即为可行（含等于上限的边界）。
- **版本偏好**：每个阶段的版本按配置列出顺序视为偏好（越靠前越优先），按阶段顺序做字典序回溯搜索，返回第一个可行组合。因此当某阶段降一版即可兼容邻接模型并复用已加载权重时，该组合会被自然选中。

## 公开 API（`model_assembly.py`）

| 名称 | 说明 |
| --- | --- |
| `load_config(data)` | 从映射（如 JSON 解析结果）加载并校验配置，返回 `AssemblyConfig`；非法配置抛 `ValueError` |
| `assemble(config)` | 搜索可行组合，返回 `AssemblyResult` |
| `AssemblyResult` | `ok`、`selection`（选中的 `StageVersion` 序列）、`resident_weight_bytes`、`peak_workspace_bytes`、`peak_bytes`（最高使用量）、`interface_conflicts`、`memory_conflict`、`summary()` |
| `InterfaceConflict` | 相邻阶段某对版本的接口冲突：双方阶段/版本名 + 原因（dtype/shape 不一致的双方取值） |
| `MemoryConflict` | 接口全匹配组合中的最低峰值 `best_peak_bytes`、对应组合 `best_selection`、超出量 `excess_bytes` |
| `TensorContract` / `WeightBlock` / `StageVersion` / `Stage` / `AssemblyConfig` | 数据模型（frozen dataclass），也可直接编程构造 |

### 配置结构

```python
{
  "vram_limit_bytes": 4096,
  "stages": [
    {
      "name": "encoder",
      "versions": [                      # 按偏好从高到低排列
        {
          "version": "enc-v2",
          "input":  {"dtype": "float16", "shape": [1, 512]},
          "output": {"dtype": "float16", "shape": [1, 1024]},
          "weights": [{"name": "enc_backbone", "bytes": 1000}],
          "workspace_bytes": 256
        },
        {"version": "enc-v1", "...": "..."}
      ]
    },
    {"name": "filter",  "versions": ["..."]},
    {"name": "ranker",  "versions": ["..."]}
  ]
}
```

### 使用示例

```python
from model_assembly import load_config, assemble

result = assemble(load_config(config_dict))
if result.ok:
    print([v.version for v in result.selection], result.peak_bytes)
else:
    print(result.summary())   # 具体接口冲突或显存冲突（最低峰值组合及超出量）
```

## 运行方法

```bash
python3 -m unittest discover -s tests -v
```

## 验证范围（tests/test_model_assembly.py，18 个用例，全部本地合成数据）

- **正常场景**：顶部版本链全兼容时的选择结果与峰值报告；权重全程驻留求和；工作区复用取最大值而非求和（用"求和会超限、复用则可行"的用例实证）；共享权重块只计一次。
- **题面关键场景**：某阶段降一版以兼容邻接模型并复用已加载权重时选中该组合（断言具体版本选择与共享块去重后的常驻量）；字典序偏好下仅在必要阶段降版。
- **边界**：峰值恰好等于上限可行；上限差 1 字节时回退到更低峰值组合；单阶段流水线。
- **交错**：A→B 仅 b1 兼容、B→C 仅 b2 兼容、无单个中间版本同时满足两端时判定不可装配，并同时报告两端的具体接口冲突。
- **冲突报告**：接口全不匹配时给出双方阶段/版本与 dtype/shape 具体取值；存在接口全匹配组合但全部超限时，给出最低峰值组合、峰值与超出量。
- **配置校验**：空阶段/空版本、版本名重复、共享块字节数不一致、非法张量契约、负显存上限等均抛 `ValueError`。

## 文件结构

```
model_assembly.py            # 装配模块（数据模型 + 配置校验 + 回溯搜索）
tests/test_model_assembly.py # 单元测试（本地合成数据）
README.md
```
