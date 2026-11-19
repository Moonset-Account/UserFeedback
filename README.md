# shared-write-check

宿主把资料副本传给低权限插件前的**可写共享身份核验库**。沿普通对象与数组的
**自有数据属性**比较两侧对象图的实际对象身份（object identity），输出每个
不可接受的共享对象从两侧可达的一对**最短路径**，并保证核验过程本身不执行
访问器 getter、不触发任何 Proxy 回调。

## 入口

- 库入口：`src/shared-write-check.mjs`
  - `verifySharedWritable(host, copy)`：主核验函数，默认导出与具名导出均可
  - `formatPath(segments)` / `formatSegment(segment)`：路径渲染辅助
- 验收测试：`tests/shared-write-check.test.mjs`（基于 `node:test` + `node:assert`，真实断言）
- 运行环境：Node.js 24.15.0，ESM（`"type": "module"`），零第三方依赖

## 运行验证

```bash
node --test 'tests/**/*.test.mjs'
# 或（若本地允许 npm）
npm test
```

## 核验规则（与题面约束逐条对应）

1. **身份比较而非内容比较**：内容相同的两个独立对象可以通过；只有两侧都能
   到达的**同一引用**才算共享。
2. **遍历范围**：仅沿普通对象（原型为 `Object.prototype` 或 `null`）与数组的
   自有数据属性递归，覆盖字符串键、symbol 键与不可枚举属性（`Reflect.ownKeys`
   + 属性描述符）。Map、Date、类实例等异构对象记录身份但不展开，共享时一律
   以 `non-plain-object` 拒绝。
3. **共享节点的接受条件**：自身及全部可达后代都 `Object.isFrozen`，且整条
   子图都是普通数据（无访问器、无代理、无函数、无异构对象）。满足则进入
   `accepted`；否则进入 `violations`，原因可能为：
   - `not-frozen`：自身或某个后代未冻结（附 `relativePath` 指向问题后代）
   - `non-plain-object` / `non-plain-descendant`：异构对象
   - `accessor-descendant` / `proxy-descendant` / `function-descendant`：子图含访问器/代理/函数
4. **访问器与代理不取值、不回调**：
   - 访问器属性通过属性描述符识别（`'get' in d || 'set' in d`），只记录
     `unverifiable`，绝不读取属性值，因此不触发 getter；
   - Proxy 识别使用 Node 内置 `util.types.isProxy`（引擎内部槽判断，不经过
     任何 trap），识别后只记录、不展开。已撤销代理同样可安全识别。
5. **循环引用收敛**：每侧 BFS 各自维护 Visited 集合，环与菱形引用都能终止；
   同一身份的路径保留 BFS 首次到达的**最短路径**。
6. **函数值**：不调用、不展开，标记为 `unverifiable`（原因 `function`）。
7. **原始值 / null 根**：无对象身份，直接通过。

## 返回结构

```js
{
  ok: boolean,            // violations 与 unverifiable 同时为空才为 true
  violations: [{          // 不可接受的共享对象
    identity,             // 共享引用本身
    hostPath, copyPath,   // 两侧最短路径（段数组：字符串或 symbol）
    hostPathString, copyPathString, // 渲染后的路径，根为 "<root>"
    reason,               // not-frozen / non-plain-object / ...
    relativePath, relativePathString, // 问题后代相对共享节点的路径（如适用）
  }],
  accepted: [{ identity, hostPath, copyPath, ... }], // 全冻结普通数据共享
  unverifiable: [{ side: 'host'|'copy', path, reason }],
  hostPaths, copyPaths,   // Map<对象, 最短路径条目>，供程序化消费
}
```

路径渲染约定：标识符风格键 → `.key`；数组下标/数字键 → `[0]`；其它字符串
→ `["..."]`；symbol → `[Symbol(desc)]`；空路径（根本身）→ `<root>`。

## 测试覆盖范围（17 个用例）

**可接受结果**
- 内容相同但相互独立的对象图通过
- 全冻结共享普通数据子图（含循环引用）通过，进入 `accepted`
- null 原型对象、原始值/null 根的处理

**应被阻止 / 拒绝的结果**
- 可写共享对象被报告，附两侧最短路径与 `not-frozen`
- 冻结父节点 + 未冻结后代（`relativePath` 指到具体后代）
- 类实例 / Map 等异构对象共享，即使冻结也拒绝
- 冻结共享子图中出现异构后代

**边界 / 交错场景**
- 循环引用收敛终止且身份正确识别
- 菱形图 BFS 最短路径选择
- 访问器、Proxy、代理根、函数分支均标记为无法核验
- 同一图中冻结共享（接受）+ 可写共享（拒绝）+ 访问器（无法核验）交错
- symbol 键与不可枚举属性的遍历与路径渲染、数组下标渲染

**计数替身（证明核验不触发业务代码）**
- `makeCountingAccessor`：计数 getter/setter，断言核验全程 `getCalls === 0`
- `makeCountingProxy`：对全部 13 种 trap 计数，断言核验全程 trap 总数为 0
  （含代理作为根、已撤销代理的冒烟验证）

## 设计边界与说明

- Proxy 的「零回调检测」依赖 Node 的 `util.types.isProxy`。纯 ECMAScript
  没有等价的无陷阱检测手段（任何属性观测都会触发 trap），因此本库的适用
  平台为 Node.js。
- 核验为只读操作，不修改、不冻结任何入参对象。
- 本库仅用于本地防御性核验，不包含任何目标探测或攻击逻辑。
