# 安全修复用例选择器（sec-case-selector）

合并门禁的安全修复用例持续累积、执行预算超限。本工具读取本地 JSON 清单，
**按构建独立**选出满足全部必测修复要求的最短用例组合，保留强制用例及其传递依赖，
输出逐条入选理由、预算对比与预计总耗时；预算与覆盖无法同时满足时给出结构化冲突诊断。

零运行时依赖，仅使用 Node.js 标准库（要求 Node >= 20）。

## 清单格式

```json
{
  "defaults": { "budgetMinutes": 30, "exhaustiveLimit": 20 },
  "builds": [
    { "id": "linux-x64", "budgetMinutes": 30, "requiredFixes": ["FIX-A", "FIX-B"] }
  ],
  "cases": [
    {
      "id": "SEC-001",
      "durationMinutes": 5,
      "builds": ["linux-x64"],
      "fixes": ["FIX-A"],
      "mandatory": true,
      "dependencies": ["SEC-008"]
    }
  ]
}
```

每条用例记录对应需求中的六个字段：

| 字段 | 含义 |
| --- | --- |
| `id` | 用例编号。允许重号，但重号条目的 `builds` 必须互不相交（即“只在某构建有效的重复用例”）；同一构建内编号唯一，否则报 `DUPLICATE_CASE_SCOPE_OVERLAP` |
| `durationMinutes` | 运行耗时（非负数，允许 0） |
| `builds` | 适用构建列表 |
| `fixes` | 该用例可验证的修复要求 |
| `mandatory` | 是否必须保留（仅对其适用构建生效），强制用例自动入选 |
| `dependencies` | 测试数据/前置用例依赖，按构建作用域解析；依赖必须同样适用该构建，否则报 `DEPENDENCY_NOT_APPLICABLE` |

`builds[].requiredFixes` 是该构建的必测修复要求；`budgetMinutes` 缺省时取 `defaults.budgetMinutes`。

## 选择算法

对每个构建独立求解一个**带强制项与传递依赖的加权集合覆盖**问题：

1. **必选基线**：全部强制用例作为根，计算传递依赖闭包（DFS，构建作用域），基线内任何用例都不可剔除。
2. **可行性检查**：若某条必测修复要求没有任何适用用例能验证，返回 `INFEASIBLE` / `REQUIREMENT_UNCOVERABLE`。
3. **求解最短组合**：
   - 可选根数量 ≤ `exhaustiveLimit`（默认 20）时，**穷举全部 2^n 子集**（Gray-code 增量枚举 + BigInt 位掩码），保证全局最优；
     同耗时平局先取根数量更少的方案（剔除被其他根闭包包含的冗余根），再按根编号字典序决胜。
   - 超限时使用**确定性贪心**：每步选择“新增单位修复覆盖耗时”最小的根，
     平局依次按新增耗时升序、编号字典序；结束后做反向冗余剪枝。不使用随机数或时间戳，结果可复现。
4. **预算判定**：最短组合仍超预算时返回 `OVER_BUDGET` / `OPTIMAL_OVER_BUDGET`，
   诊断中包含最短可达成耗时、超支量、强制基线耗时、基线之外仍需覆盖的要求，以及该答案是否已证明最优。

**依赖环**不阻断求解（环上闭包仍可正常计算），但会通过全局环检测输出确定性警告，列出环上的用例。

## 用法

```bash
# 文本报告（退出码 0=全部可行，2=存在超预算/不可行，3=清单校验失败）
node src/cli.js examples/manifest.example.json

# 机器可读 JSON
node src/cli.js examples/manifest.example.json --json

# 只求解指定构建；强制走贪心（穷举上限设为 0）
node src/cli.js examples/manifest.example.json --build arm64 --exhaustive-limit 0
```

也可作为库使用：`loadManifest` → `selectAll` / `selectForBuild`（见 `src/manifest.js`、`src/selector.js`）。

## 测试

```bash
npm test
```

29+ 个用例（`node:test`，无第三方依赖）覆盖：

- **小规模最优性核对**：60 轮 LCG 确定性随机清单，穷举结果与一份独立编写的朴素参考穷举
  （`test/helpers.js` 的 `referenceOptimal`，集合实现与生产代码完全不同）逐构建核对最短耗时；
  另验证贪心耗时永不优于穷举最优。
- **大规模确定性与诊断**：约 300 用例的随机清单两次求解深度相等；强制贪心路径下的
  超预算诊断（最短耗时/超支量/最优性标记）与不可行诊断。
- **只在某构建有效的重复用例**：重号且构建范围不相交时合法并独立求解；范围重叠时报校验错误。
- **依赖环**：环检测按构建作用域输出，环上闭包可参与选择；环仅存在于其他构建时不误报。
- 强制用例与传递依赖保留、不可覆盖要求、零必测要求、穷举平局规则、CLI 三种退出码与 JSON 结构。
