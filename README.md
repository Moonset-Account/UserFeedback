# 内部包迁移依赖评审（internal-dependency-review）

内部包迁入统一仓库后，依赖评审需要区分**名字相近**与**真正可能被错误解析**的包。
本模块按生态的注册表优先级模拟每条依赖声明的实际解析路径，再结合按生态配置的
名称规范化规则与本地 Unicode 混淆字符映射，判断该路径是否会选中外部同名或混淆包。

核心判定原则：

- **阻断（block）**：仅当一条解析路径**实际选中外部注册表**，且该包与内部包
  - 显示名完全相同（`EXTERNAL_SAME_NAME`），或
  - 混淆骨架相同且至少存在一个被映射的混淆码点（`EXTERNAL_CONFUSABLE`）。
- **提示（hint）**：其余所有“相似但不可错误解析”的情况——
  仅规范化名相近（分隔符/大小写差异）、内部注册表已满足但外部存在影子包、
  解析失败但内部目录有候选等。
- **作用域、生态、注册表是身份的一部分**：匹配始终带 `(ecosystem, scope 身份, registry)`，
  绝不按显示名跨身份合并。`@acme/logger`、`@vendor/logger` 与无作用域 `logger` 是三个不同身份。

## 目录结构

```
src/
  index.js          公共导出
  analyzer.js       评审主流程（解析模拟 + 风险分级 + 规则敏感性）
  normalization.js  按规则步骤顺序执行的名称规范化（保留码点位置）
  skeleton.js       本地混淆映射加载、混淆骨架构造与位置对比
  directory.js      内部目录索引与重复目录项诊断
  rules.js          生态规则集/注册表优先级准备与冲突诊断
test/fixtures/      四个用例的本地合成数据（含西里尔混淆字符，无真实凭据）
tools/local_verify.mjs  本地核验入口（真实断言，非仅打印）
```

## 实际 API

```js
import {
  reviewDependencies,      // 主入口
  normalizeAndSkeleton,    // 单名称：规范化 + 混淆骨架
  loadConfusableMap,       // 加载本地混淆映射
  parseSpec,               // 拆作用域/裸名
} from './src/index.js';

const result = reviewDependencies({
  // 内部包目录；重复条目会被保留并单独诊断
  directory: [
    { ecosystem: 'npm', name: 'payroll-sdk', path: 'packages/npm/payroll-sdk' },
  ],
  // 项目依赖声明
  declarations: [
    { id: 'd1', ecosystem: 'npm', spec: 'payrоll-sdk' },
  ],
  // 按生态配置的规范化规则；同生态多套规则集视为冲突，分别求值
  normalization: {
    npm: {
      id: 'npm-standard',
      steps: [
        { op: 'scope', mode: 'keep' },                       // 或 'drop'
        { op: 'case-fold' },
        { op: 'strip-separators', chars: ['-', '_', '.'] },  // chars 可省略，默认 - _ .
      ],
    },
  },
  // 注册表优先级（顺序即解析顺序）
  registryPriority: { npm: ['npm-internal', 'npm-public'] },
  // 注册表及其包清单；external 标记内/外部身份
  registries: [
    { id: 'npm-internal', ecosystem: 'npm', external: false, packages: ['payroll-sdk'] },
    { id: 'npm-public',   ecosystem: 'npm', external: true,  packages: ['payrоll-sdk'] },
  ],
  // 本地 Unicode 混淆字符映射（单码点键）
  confusables: {
    о: { skeleton: 'o', name: 'CYRILLIC SMALL LETTER O' },
  },
});
// => { findings: [...], diagnostics: [...], summary: {...} }
```

### finding 关键字段

- `severity`：`block` | `hint`
- `kind`：`EXTERNAL_SAME_NAME` / `EXTERNAL_CONFUSABLE` / `SIMILAR_NORMALIZED_NAME` /
  `SHADOWED_EXTERNAL_LOOKALIKE` / `INTERNAL_CONFUSABLE_NAME` / `UNRESOLVED_WITH_INTERNAL_CANDIDATE`
- `resolutionPath`：`registryChain`、按顺序的 `checkedRegistries`（含 `matchedBy`/`matchedPackage`）、
  `selectedRegistry`、`selectedExternal`
- `confusablePositions`：每个混淆字符在**裸名中的码点索引**、含作用域时的 `absoluteIndex`、
  声明侧与内部侧的码点（`U+....`）
- `shadowedExternal`：解析链上未被选中但存在同名/混淆影子包的外部注册表
- `ruleSensitive` / `ruleSetComparison`：同一声明在多套规则集下的结论是否变化

### 诊断码（diagnostics）

`DUPLICATE_DIRECTORY_ENTRY`、`CONFLICTING_NORMALIZATION_RULES`、`CONTRADICTORY_SCOPE_STEPS`、
`UNKNOWN_NORMALIZATION_OP`、`UNKNOWN_REGISTRY_REFERENCE`、`REGISTRY_ECOSYSTEM_MISMATCH`、
`MISSING_NORMALIZATION_RULES`、`MISSING_REGISTRY_PRIORITY`、`UNRESOLVED_DECLARATION`。

## 命令

```bash
node tools/local_verify.mjs --list                 # 列出可用用例
node tools/local_verify.mjs                        # 运行 C01..C04
node tools/local_verify.mjs --case C01             # 单用例
node tools/local_verify.mjs --case C01 --case C04  # 多用例
node tools/local_verify.mjs --case=C03             # 等号形式同样支持
```

退出码：全部断言通过 `0`；任一断言失败、入口缺失、未实现、零断言、整组跳过或用例/参数未知为非零。
每组输出一条 JSON 记录，含 `case`、实际断言数、`passed/failed/skipped`、样本或种子、
期望/观察摘要与错误信息。输出只含合成数据，不含凭据或私有账户内容。

## 可重放样本与覆盖点

| 用例 | 夹具 | 真实断言覆盖 |
| --- | --- | --- |
| C01 | `test/fixtures/C01-priority-resolution.json` | 同一声明在内部优先/公共优先两种顺序下的实际候选；同名阻断 vs 仅规范名相似的提示；无内部候选的正常外部依赖不产生风险项；解析路径逐注册表记录 |
| C02 | `test/fixtures/C02-identity-scope-ecosystem.json` | 不同作用域、有无作用域、不同生态、不同注册表身份不按显示名合并；规范化输出保留作用域；block 携带生态+注册表身份 |
| C03 | `test/fixtures/C03-confusable-skeleton.json` | 西里尔 `о`(U+043E)/`с`(U+0441) 的骨架折叠、裸名码点位置（4 与 0）、绝对位置与码点对比；公共优先阻断、内部优先降级为影子提示且路径可回溯 |
| C04 | `test/fixtures/C04-duplicates-conflicts-ordering.json` | 重复目录项单独诊断且保留参与匹配；同生态多套冲突规则分别求值并标记 `ruleSensitive`；drop/keep 作用域导致结论 block↔none 变化；pypi 仅步骤次序不同结论不变；单规则集内 keep/drop 自相矛盾的专门诊断 |

## 设计要点与未覆盖边界

设计要点：

- 规范化严格按 `steps` 数组顺序执行；`scope` 步骤只作用于作用域，`case-fold` 用
  `String.prototype.toLowerCase`，`strip-separators` 默认删除 `-` `_` `.`（不碰 `@` `/`）。
- 位置按 **Unicode 码点**（非 UTF-16 单元）计数，emoji/代理对不会错位。
- “规范化名相等则骨架必相等”，因此比较顺序固定为：显示名 → 规范化名 → 骨架，
  防止纯分隔符差异被误判成混淆阻断。
- 解析模拟在每个规则集语境下独立进行：先按原始显示名命中，再按该规则集的规范名命中，
  因此 drop-scope 后跨作用域的可解析性可以被如实模拟。
- 混淆映射只接受**单码点键**；多码点键被忽略。

未覆盖/已知边界（审计时应保留未知，不要默认安全）：

- 未实现版本范围/锁文件解析，包清单按精确字符串匹配，不处理 `^1.2.3`、dist-tag 或别名。
- `case-fold` 仅做 `toLowerCase()`，不是完整 Unicode TR30 大小写折叠；少数特殊字符（如 İ/ß）
  的折叠差异未专门处理。
- 不做网络请求、不校验注册表真实性，注册表内容完全来自输入；真实日志与线上注册表行为
  仍需人工核对，本工具结论不能替代审计。
- 不识别多码点组合的同形字（如带组合符号的字母序列）、双向控制符（RLO 等）与零宽字符，
  需扩展映射表与预处理阶段才能覆盖。
- 冲突规则集只做“分别求值 + 敏感性标记”，不替用户决定哪套规则权威；最终取舍需人工确认。
