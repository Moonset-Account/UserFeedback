# 前端发布完整性判定模块（release-integrity-gate）

按地区拆分的前端页面入口，发布前需要确认：每个**已发布入口实际允许的分支**上，
运行时可达的全部脚本（地区主脚本及其回退链）都随构建导出，且实际字节摘要与
清单中的批准摘要一致。本模块在流水线中给出完整性判定，挡住两类问题：

1. **同名脚本换内容**：清单里的批准摘要针对旧字节，构建产物中的同名脚本内容已更换。
2. **只核地区脚本、漏核回退脚本**：回退条件触发时运行时仍会加载的回退脚本没有批准记录或缺件。

## 实际入口

| 入口 | 路径 | 说明 |
| --- | --- | --- |
| 判定模块（库） | `src/integrity.js` | 导出 `analyzeIntegrity`、`validateManifest`、`loadManifest`、`ManifestError` |
| 命令行 | `src/cli.js` | `node src/cli.js --manifest <清单路径> --build-root <构建目录>`；通过退出 `0`，阻断退出 `1` |
| 验收测试 | `tests/integrity.test.mjs` | 基于 `node:test`，`node --test tests/*.test.mjs` 运行（即 package.json 的 `test` 脚本） |

运行时：Node.js 24.15.0（仅使用内置模块 `node:crypto`、`node:fs/promises`、`node:path`、`node:test` 等，无外部依赖）。

## 清单结构（manifest）

```json
{
  "entries": [
    {
      "id": "home",
      "route": "/home",
      "status": "published",
      "branches": [
        {
          "id": "eu",
          "region": "eu",
          "enabled": true,
          "script": "js/home.eu.js",
          "fallback": {
            "script": "js/home.fallback.js",
            "condition": "region-unsupported",
            "fallback": { "script": "js/global.js", "condition": "cdn-error" }
          }
        }
      ]
    }
  ],
  "approvals": [
    { "script": "js/home.eu.js", "sha256": "<64 位十六进制摘要>", "approvedBy": "rel-bot", "summary": "release-1" }
  ]
}
```

约定：

- `status`：`published`（正式，默认值，其违规阻断门禁）或 `preview`（未发布预览，仍完整评估但不阻断）。
- `branches[].enabled`：显式 `false` 的分支不是运行时允许的分支，不参与核算。
- `fallback`：可嵌套表达多级回退链；链上每一级都是运行时可达引用，逐级核算字节与批准。
- `approvals`：按**脚本路径 + sha256** 批准；同一路径可有多条历史批准，实际摘要命中任一条即视为已批准。
- 脚本路径均相对构建目录（`buildRoot`）解析，越出构建目录的引用判 `PATH_TRAVERSAL`。

## 判定规则与违规码

对每个已发布入口的每个启用分支，沿「主脚本 → 各级回退」枚举全部可达引用，逐个检查：

| 违规码 | 触发条件 |
| --- | --- |
| `SCRIPT_MISSING` | 可达引用在构建产物中缺件（即使清单有批准也阻断） |
| `APPROVAL_MISSING` | 脚本存在但清单中没有对应路径的批准记录（覆盖「漏批回退脚本」） |
| `DIGEST_MISMATCH` | 有批准记录但实际 sha256 与所有批准摘要均不一致（覆盖「同名脚本换内容」） |
| `PATH_TRAVERSAL` | 引用路径经解析后越出构建目录 |

每条违规都带 `entryId`、`branchId`、`region`、`kind`（`primary` / `fallback`）与脚本路径，
结果中可直接定位入口与引用分支。清单结构本身不合法时抛 `ManifestError`（不与内容违规混淆）。

`analyzeIntegrity` 返回结构（节选）：

```json
{
  "ok": false,
  "violations": [],
  "previewViolations": [],
  "skippedEntries": [],
  "entries": [
    {
      "id": "home",
      "status": "published",
      "blocking": true,
      "enabledBranchCount": 1,
      "branches": [
        { "id": "eu", "region": "eu", "chain": [
          { "kind": "primary", "script": "js/home.eu.js", "exists": true, "approved": true, "sha256": "…" }
        ], "violations": [] }
      ],
      "violations": []
    }
  ]
}
```

`ok` 仅由**已发布入口**的违规决定；预览入口的问题进入 `previewViolations` 留痕，不阻塞正式入口。

## 验证范围

测试全部使用本地合成夹具（在 `os.tmpdir()` 下为每个场景新建独立目录并写入真实脚本字节，
测试结束自动清理），不联网、不接触真实目标。`tests/integrity.test.mjs` 共 10 个用例，覆盖：

- **可接受结果**：地区脚本与回退脚本均存在且摘要/批准一致 → 通过，并断言回退节点确实被核算。
- **应被阻止（同名换内容）**：构建字节与批准摘要不符 → `DIGEST_MISMATCH`，门禁失败。
- **应被阻止（漏批回退）**：只批准地区脚本、回退脚本无批准 → `APPROVAL_MISSING`，门禁失败。
- **应被阻止（可达缺件）**：回退脚本未随构建导出 → `SCRIPT_MISSING`，结果指出入口与分支。
- **交错/边界场景**：
  - 预览入口违规不阻塞健康的正式入口，但在 `previewViolations` 留痕；
  - 显式禁用分支不参与核算，全部禁用的正式入口标记 `skippedEntries` 且不产生违规；
  - 多级回退链逐级核算，深层回退缺件同样阻断；
  - `../` 路径越界 → `PATH_TRAVERSAL`；
  - 非法清单（缺字段、非法摘要、分支 id 重复）→ 抛 `ManifestError`；
  - CLI 真实子进程：通过场景退出码 `0`、阻断场景退出码 `1` 且报告含入口与分支。

运行：

```bash
node --test tests/*.test.mjs
# 或
npm test
```

本地核验结果：Node.js 24.15.0 下 10/10 用例通过（`npm test` 在当前沙箱中未获执行批准，
已用其脚本等价命令 `node --test tests/*.test.mjs` 实际跑通）。
