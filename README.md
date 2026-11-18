# 登录发现模块（login-discovery）

组织将部分邮箱域的登录委托给新身份源。本模块接收**已验证的组织域名归属**、**团队覆盖范围**与**组织授权切换记录**，为登录请求解析应前往的身份源，并签发/校验绑定团队与配置版本的登录邀请。

## 实际入口

- 模块入口：`src/login-discovery.mjs`
  - `createLoginDiscovery({ ownerships, teams, switches, members, transitionWindows })` 创建实例，返回：
    - `resolve({ email, at })` — 登录发现，成功时签发邀请 `{ invitationId, email, teamId, orgId, idp, configVersion, issuedAt }`，失败时返回 `{ ok: false, reason }`。
    - `validateReceipt({ invitationId, teamId, idp, configVersion })` — 回执校验，只对照签发时的邀请记录，不按当前配置重新解析。
    - `recordSwitch(record)` — 追加组织授权切换记录（交接），影响后续解析的身份源与配置版本。
    - `configVersionAt(at)` — 查询某时刻的配置版本。
  - `REASONS` — 全部拒绝原因常量。
- 测试入口：`npm test`（即 `node --test`，Node.js 24.15.0，自动发现 `tests/*.test.mjs`，使用 `node:test` + 真实断言）。

## 业务规则

1. **最具体归属优先**：子域单独托管时，按最长域名后缀匹配已验证归属；父域解析不受子域托管影响。
2. **冲突即暂停**：归属记录被标记 `conflicted`，或同一最具体域被多个组织同时 `verified`，对应范围（含其下级子域）暂停解析，返回 `ownership-suspended`。
3. **过渡窗口按成员登记**：团队存在已登记的旧身份源过渡窗口 `[startsAt, endsAt)` 时，窗口期内只能按成员登记决定身份源；未登记成员拒绝（`unregistered-member`），登记冲突拒绝（`member-registration-conflict`）。窗口期外按组织授权切换记录取当前身份源。
4. **邀请绑定**：签发的登录邀请绑定确定的 `teamId` 与 `configVersion`。
5. **交接后回执仍匹配当次选择**：交接（新增授权切换记录）后，回执必须匹配该次邀请签发时选择的身份源 / 团队 / 配置版本；拿新身份源或篡改版本冒充旧邀请的回执均被拒绝。

## 验证范围（tests/login-discovery.test.mjs）

- **可接受结果**
  - 子域单独托管时以更具体的已验证归属为准，父域解析不受影响。
  - 过渡窗口内已登记成员按登记身份源路由，邀请绑定团队与配置版本，一致回执被接受。
- **应被阻止或拒绝的结果**
  - 归属标记冲突的范围（含下级子域）被暂停。
  - 同一域被多组织同时验证归属 → 暂停。
  - 过渡窗口内未登记成员 → 拒绝，不猜测身份源。
  - 回执身份源 / 团队不符、邀请不存在 → 拒绝。
- **边界与交错场景**
  - 过渡窗口 `[startsAt, endsAt)` 起止边界：`startsAt` 进入窗口按成员登记，`endsAt` 前 1ms 仍在窗口内，`endsAt` 恰好离开窗口回到组织身份源。
  - 签发邀请后发生交接：新解析使用新身份源且配置版本变化；旧邀请的回执仍须匹配签发时选择的身份源与版本，冒充或篡改均被拒。
  - 非法邮箱格式与未知域名的拒绝路径。

## 运行

```sh
node --version   # 24.15.0
npm test         # node --test
```
