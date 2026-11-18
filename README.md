# 本地机密配置发放模块

面向同一服务分批启动、多版本机密配置并存场景的本地发放模块。纯 Node.js 实现，仅依赖
`node:crypto` 等内置模块，无第三方依赖。

## 入口与运行

- 模块入口：`src/config-distributor.mjs`
  - `ConfigDistributor`：发放器主体（构造时可注入 `now` 时钟与 `defaultTtlMs`）
  - `DENY_REASONS`：拒绝原因常量（`unknown-lease` / `generation-mismatch` /
    `lease-expired` / `version-revoked` / `instance-unknown` / `field-not-approved`）
  - `DistributorError`：参数/状态类错误
- 验收测试：`tests/config-distributor.test.mjs`，基于 `node:test` 与 `node:assert`
- 运行方式：

```bash
node --test tests/*.test.mjs
# 或
npm test
```

注：在当前 Node 24.15.0 环境中，`node --test tests/` 会把目录参数当作模块入口报错，
因此使用显式 glob 指定测试文件。

要求 Node.js 24.15.0（开发与核验均使用该版本）。

## 模型与关键约束

### 版本化配置（不可变）

- `publishVersion(version, fields)`：发布一个新版本并把它设为当前版本；版本内容经深拷贝后
  冻结，重复发布同一版本号会被拒绝，调用方事后修改输入对象不影响已发布快照。
- `revokeVersion(version)`：紧急撤销，将该版本标记为 revoked。撤销不删除数据，但该版本上
  已发出的租约在读取时复检会立即被拒绝，新租约也不会钉住被撤销版本。
- 普通发布新版本**不**关闭旧版本：旧实例租约在 TTL 窗口内继续读到其钉住的完整旧版本。

### 实例登记与进程代次

- `registerInstance({ instanceId, generation, approvedFields })`：登记实例标识、进程代次
  与批准字段集合。代次必须单调递增；同一实例以更高代次重新登记（即重启）后，旧代次的全部
  租约立即标记为 superseded，**旧租约不可复用**。
- 旧代次申请新租约、登记代次回退均被拒绝。

### 读取租约

- `issueLease({ instanceId, generation, ttlMs })`：校验实例与代次后，钉住**当前一个完整
  版本**，返回绑定 `(实例, 代次, 版本, expiresAt)` 的租约。TTL 采用半开窗口：
  `now < expiresAt` 有效，`now === expiresAt` 即过期。
- `readConfig({ leaseId, fields })`：依次校验租约存在性、代次（含 superseded 复检）、
  TTL、版本撤销状态，再校验字段授权；任一条件不满足返回
  `{ granted:false, version, reason }`。
- **完整版本快照**：读取只从租约钉住的单一不可变版本取字段，版本切换与读取交错时不会出现
  跨版本字段拼接。
- **部分字段授权**：请求字段必须全部属于实例批准字段且存在于该版本，缺一即整次拒绝；
  授权通过时只返回所请求的子集，不返回多余字段。
- 异步边界（`barrier` 注入点）前后都会重新评估撤销、代次与 TTL 条件，避免基于陈旧状态
  发放。默认 barrier 不产生额外让出。

### 审计（最小化）

- 每次读取尝试（含未知租约）追加一条冻结审计记录，**仅含 `version` 与 `decision` 两个键**，
  `decision` 取值 `granted` / `denied`；未知租约的 `version` 为 `null`。
- 审计不记录实例标识、租约标识、字段名或任何机密值；`getAuditLog()` 返回副本，外部无法
  改写内部历史。

## 测试验证范围

`tests/config-distributor.test.mjs` 共 14 个用例，覆盖题面交叉约束：

| 类别 | 用例 |
| --- | --- |
| 可接受结果 | 完整授权读取；部分授权只取批准子集；新版本租约读取 |
| 应被阻止/拒绝 | 未批准字段整次拒绝；紧急撤销后旧租约与新租约均停止发放；重启后旧租约失效与代次回退拒绝；未知租约拒绝；参数非法拒绝 |
| 边界/交错 | TTL 到期前/到期时刻/到期后的半开窗口；读取等待期间普通版本切换仍返回完整旧版本；读取等待期间紧急撤销（快照后复检拒绝）；颁发租约等待期间发布新版本（钉住捕获时版本）；读取等待期间实例重启（等待后代次复检拒绝） |
| 审计与不可变性 | 审计仅含 version/decision 且无机密/标识泄漏；重复发布拒绝与输入突变不影响快照；返回配置冻结 |

## 范围说明

本模块仅在本地工作区内运行，全部测试使用合成夹具与可注入的虚拟时钟，不进行任何真实目标
探测、网络访问或外部服务调用。
