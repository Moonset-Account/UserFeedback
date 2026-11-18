# 端点响应隔离处置模块（endpoint-quarantine）

把检测系统已确认的可疑对象从原位置隔离到受控工作区，并为误报保留可还原能力。
纯 Node.js 内置模块实现，无第三方依赖，运行环境 Node.js `v24.15.0`（`engines: >=24`）。

## 实际入口

| 用途 | 入口 |
| --- | --- |
| 处理模块（库 API） | `src/quarantine.js`，导出 `QuarantineManager` 及全部错误类型 |
| 命令行入口 | `src/cli.js`（`package.json` 的 `bin: quarantine`） |
| 错误码定义 | `src/errors.js` |
| 文件系统 / 摘要工具 | `src/storage.js`（流式 SHA-256、temp+rename 原子写） |
| 验收测试 | `tests/*.test.mjs`，基于 `node:test` + `node:assert/strict` |
| 手工冒烟 | `scripts/smoke-cli.mjs`，以子进程驱动真实 CLI |

### 库 API

```js
import { QuarantineManager } from './src/quarantine.js';

const manager = await new QuarantineManager('/path/to/store').init();

// 检测系统下发的处置对象
const detection = {
  objectId: 'det-1001',          // 对象标识，必填非空
  originalPath: '/data/sample',  // 原位置，必填非空
  digest: '<sha256 hex>',        // 扫描时摘要，必须 64 位十六进制
  owner: 'host-a/user-7',        // 原属主，必填非空
  summary: 'Trojan 命中',         // 可选摘要说明
};

await manager.quarantine(detection); // 隔离（或续跑 / 幂等确认）
await manager.reconcile();           // 中断重启后续判
await manager.restore('det-1001');   // 误报还原
manager.getStatus('det-1001');
manager.listEntries();
manager.getEvents();
```

### CLI

```bash
node src/cli.js --root <处置工作区> quarantine --detection <检测结果JSON>
node src/cli.js --root <处置工作区> reconcile
node src/cli.js --root <处置工作区> restore --object-id <对象标识>
node src/cli.js --root <处置工作区> status [--object-id <对象标识>]
```

退出码：`0` 成功；`10` 内容变化需重新确认（`RECONFIRM_REQUIRED`）；
`11` 还原冲突且双份已保留（`RESTORE_CONFLICT`）；`20` 其他处置错误；`21` 输入 / 文件错误。

## 处置工作区布局

```
<root>/
├── manifest.json          # 持久清单：每条对象的状态、摘要、属主、原位置、冲突信息、事件
├── copies/
│   └── <objectId>.bin     # 唯一有效隔离副本（重复处置不会产生第二个）
└── conflicts/
    ├── <文件名>            # 占用原位置的其他对象内容
    ├── restored-<文件名>   # 被还原的隔离对象内容
    └── restored-<文件名>.conflict.json  # 冲突标记 sidecar
```

清单与副本均通过“同目录临时文件 + rename”原子落盘；进程在任意时刻被杀，
重启后只会看到旧版本或新版本，不会出现半截 JSON / 半成品副本。

## 状态与交叉约束

对象状态：`quarantining → quarantined → restored`（`restored` 可再次被检测而重新隔离）。

1. **消费检测三要素**：`objectId`、`digest`、`owner`（外加 `originalPath`、可选 `summary`），
   输入不完整或摘要非 SHA-256 十六进制时抛 `INVALID_DETECTION`，且不残留半成品条目。
2. **内容变化暂停**：隔离前重新计算原位置文件摘要，与检测摘要不一致即抛
   `RECONFIRM_REQUIRED`，原文件与隔离区都不被改动；检测系统携带与当前磁盘一致的新
   摘要重新下发后，处置继续，`reconfirmCount` 累加。
3. **原位置缺失**：首次隔离时原文件已不存在且无副本，抛 `MISSING_ORIGINAL`，不凭空造副本。
4. **中断重启续判**（`reconcile`，只依赖清单与磁盘、不依赖内存状态）：
   - 原文件已删、副本有效且摘要匹配 → 直接收尾；
   - 副本已在、原文件未删且摘要匹配 → 删除原文件并以 `resumed` 收尾；
   - 副本摘要与记录不符 → 清除失效副本并抛 `COPY_TAMPERED`，绝不提升为有效副本；
   - 原文件与副本均缺失 → 抛 `MISSING_ORIGINAL` 阻断。
5. **幂等**：同一 `objectId` 重复隔离只校验唯一副本并返回 `already-quarantined`；
   重复还原返回 `already-restored`，不产生新文件；同一对象身份不允许原位置漂移。
6. **还原冲突**：原位置被占用且占用者摘要 ≠ 隔离对象摘要时，两份内容都保留到
   `conflicts/` 并写 `.conflict.json` 标记，原位置放回隔离对象内容，同时抛
   `RESTORE_CONFLICT`；若占用者与隔离对象摘要完全一致，则不重复保留、直接还原。

## 验证范围

运行：

```bash
npm test          # 等价 node --test tests/*.test.mjs（17 个用例）
npm run smoke     # 真实 CLI 子进程端到端冒烟
```

`tests/` 全部使用 `node:test` 真实断言，夹具在 `os.tmpdir()` 下独立创建并在用例后清理，
均为本地合成样本，不涉及任何真实端点探测或攻击载荷。覆盖矩阵：

| 交叉约束 | 可接受结果 | 被阻止 / 拒绝结果 | 边界 / 交错场景 |
| --- | --- | --- | --- |
| 隔离与幂等 | 正常隔离、误报还原、重复处置单副本 | 非法输入拒绝且无残留 | 重复还原幂等 |
| 重新确认 | 新摘要重新确认后放行 | 内容变化 `RECONFIRM_REQUIRED`、原文件缺失拒绝 | 阻断→放行交错；多对象一个阻断不影响另一个 |
| 中断续判 | 两种中断阶段重启后 `resumed` 收尾 | 副本篡改 `COPY_TAMPERED` 阻断 | 全新进程实例 reconcile、损坏副本不被提升 |
| 还原冲突 | 空闲位置还原、同内容占用直接还原 | 异内容占用双份保留 + 冲突标记 + 退出码 11 | 冲突目录三文件、同摘要边界 |
| CLI 入口 | 隔离/状态/reconcile/还原全链路 | 退出码 10、11 真实触发 | 子进程跨进程验证持久化 |

## 范围说明

本模块仅在本地工作区对合成样本做防御性隔离、还原与状态核验，不包含目标探测、
攻击或漏洞利用能力；所有产物默认保留在指定的处置工作区内。
