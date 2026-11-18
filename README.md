# 执行前门禁（Pre-Execution Gate）

审批服务与执行服务分批升级时，同一份已批准请求在两端可能被解读成不同的目标或权限范围。
本项目在执行前增加一道门禁：接收**绑定同一请求摘要**的两份结构化解析结果、**各版本字段语义表**
与**审批覆盖参数**，还原有效参数、按业务标识对齐清单，并在任何跨端解读偏差出现时阻止执行且定位差异来源。

纯 ESM、零外部依赖，运行时：Node.js 24.15.0（仅使用内置 `node:test` / `node:assert`）。

## 入口与运行方式

- 库入口：`src/index.js`，主函数 `evaluateGate(input)`
- 演示入口：`node examples/demo.mjs`（打印一次 ALLOW 与一次 BLOCK 的完整输出）
- 测试入口：

  ```bash
  node --test "tests/**/*.test.mjs"
  # 或（package.json 已配置等价脚本）
  npm test
  ```

  注意：Node 24.15 下 `node --test tests/` 目录形式不被接受，必须使用 glob 模式。

## 输入契约

```js
evaluateGate({
  digest: 'sha256:...',            // 请求摘要，回执必须绑定同一摘要
  semanticTables: {                // 每个服务保留全部历史版本，ordinal 最大者为当前版本
    'approval-svc': [ /* v1, v2 语义表 */ ],
    'execution-svc': [ /* v2, v3 语义表 */ ]
  },
  approval: {
    service: 'approval-svc',
    parse: [ /* 审批侧原始结构化解析记录 */ ],
    coverage: [ /* 审批所覆盖的参数（基准） */ ]
  },
  execution: {
    service: 'execution-svc',
    parse: [ /* 执行侧原始结构化解析记录 */ ]
  },
  receipts: [ /* 两端本次解析回执：service/digest/versionLabel/sequence */ ],
  ledger: { /* 可选，历史回执台账；纯函数，不就地修改 */ }
})
```

语义表字段声明：`path`（支持 `target.region` 点路径）、`type`
（`string | integer | boolean | enum | set`）、`role`
（`identifier | op | target | scope | security | info`）、`required`、
`enum`、条件默认值 `defaults`、跨版本 `mappings`、范围 `rank`。

返回：

```js
{
  decision: 'ALLOW' | 'BLOCK',
  findings: [ { code, source, location, message, ...细节 } ], // 空数组表示放行
  ledger: { /* 仅有效回执推进后的新台账 */ },
  effective: { approval, execution, coverage, appliedDefaults, ... },
  comparedAt
}
```

输入结构本身不合法时抛 `GateInputError`（编程错误）；业务上“应被阻止”的结果不抛异常，
而是 `decision=BLOCK` 加 findings。

## 门禁判定规则（对应题面交叉约束）

1. **有效参数还原**：按各端当前版本语义表做类型/枚举校验；缺失字段按条件默认值定点迭代填充，
   依赖无法收敛报 `DEFAULT_CYCLE`；必填仍缺失报 `MISSING_FIELD`；语义表未声明的原始字段
   报 `UNDECLARED_FIELD`（失败关闭，不静默忽略）。
2. **清单按业务标识对齐**：以 `role=identifier` 字段对齐；同侧重复标识 -> `DUPLICATE_IDENTIFIER`
   （附两个数组位置）；执行侧新增审批不存在的标识 -> `IDENTIFIER_CONFLICT`；漏项 -> `IDENTIFIER_MISALIGNED`。
3. **操作与目标须和审批一致**：`op` 不一致 -> `OP_MISMATCH`；`target` 不一致 -> `TARGET_MISMATCH`；
   其他 `info` 参数不一致 -> `PARAM_MISMATCH`；审批未覆盖而执行侧出现的字段同样阻断。
4. **权限范围只允许收窄**：`set` 走子集判定，带 `rank` 的标量走序级判定；扩大 -> `SCOPE_WIDENED`；
   若扩大值来自条件默认值 -> `DEFAULT_EXPANDED`（显式标记默认值扩权，附 `viaDefault`）。
5. **安全选项无法映射即阻止**：执行侧值先按 `mappings` 翻译回审批侧词汇表；安全字段映射缺项 ->
   `SECURITY_UNMAPPED`（附坏值），映射后仍不一致 -> `SECURITY_MISMATCH`；非安全枚举缺项 -> `ENUM_UNMAPPED`。
6. **审批端自洽**：审批侧解析结果与审批覆盖参数逐条比对，矛盾 -> `COVERAGE_MISMATCH` / `COVERAGE_INCOMPLETE`。
7. **回执时效**：摘要不符 -> `RECEIPT_DIGEST_MISMATCH`；版本落后当前执行版本 -> `RECEIPT_STALE`
   （迟到的旧解析回执不替代当前版本结果，且不推进台账）；未知/未来版本、缺回执、同版本 sequence
   倒退/重复分别对应 `RECEIPT_UNKNOWN_VERSION` / `RECEIPT_FUTURE_VERSION` / `RECEIPT_MISSING` /
   `RECEIPT_DUPLICATE`。只有被接受的回执才推进新台账。

所有 finding 都带 `source`（`approval-parse` / `execution-parse` / `approval-coverage` / `gate`）
与 `location`（如 `businessId=op-1 field=permissions`、`receipt:execution-svc`），用于定位差异来源；
一次调用可同时上报多条差异。

## 验证范围（tests/，node:test 真实断言，共 46 例）

| 文件 | 范围 |
| --- | --- |
| `tests/gate.test.mjs` | 端到端交叉约束 20 例：1 个可接受结果（ALLOW）、14 个应阻止结果、5 个边界/交错场景 |
| `tests/semantics.test.mjs` | 类型还原、条件默认值定点迭代、循环依赖、跨版本映射、范围子集/rank 判定 13 例 |
| `tests/receipts.test.mjs` | 单条回执时效判定与台账纯函数更新（含跨请求交错）9 例 |
| `tests/contract.test.mjs` | 非法输入抛 `GateInputError` 的输入契约 4 例 |

关键覆盖（满足“可接受 / 应阻止 / 边界交错”三类要求）：

- 可接受：执行侧显式收窄范围、安全选项跨版本可映射、条件默认值仅还原最小权限 -> ALLOW；
  范围与审批完全相等的边界也放行。
- 应阻止：目标/操作不一致、重复标识、标识冲突/错位、默认值扩大权限（`DEFAULT_EXPANDED`）、
  安全选项无法映射（`SECURITY_UNMAPPED`）、旧版本回执迟到、串摘要、回执缺失/重放、
  审批端自相矛盾、类型违规、未声明字段。
- 边界/交错：set 多一个元素即扩大、默认值扩权时台账仍记录有效回执、sequence 倒退被拒而新
  sequence 被接受、跨请求旧回执不污染当前台账、一次调用多差异全部上报且均带来源定位、
  入参 ledger 不被修改。

## 目录结构

```
src/
  errors.js      原因码、来源侧常量、GateInputError
  semantics.js   类型校验、条件默认值还原、跨版本映射、范围收窄判定
  receipts.js    回执时效校验与台账（纯函数）
  gate.js        门禁编排：七阶段判定管线
  index.js       对外入口
tests/
  fixtures/baseline.js   本地合成夹具（两服务多版本语义表、覆盖参数、回执）
  *.test.mjs             node:test 验收用例
examples/demo.mjs        可运行演示
```

## 范围与限制

- 全部数据为本地合成夹具，不包含任何真实目标；本项目只做防御性一致性校验，不发起网络请求、
  不执行任何实际操作。
- 清单对齐为单层业务标识（语义表中唯一的 `identifier` 字段）；嵌套清单需在上游展开后再送入门禁。
- 跨版本映射基于语义表内显式 `mappings`，不存在自动猜测；无法映射即按规则阻断。
