# 配置中心：发布与解析模块

一个零依赖的分层配置中心，支持多层覆盖、单键引用解析、显式遮蔽，以及发布前对所有已登记实例的引用完整性（悬空 / 环）校验。仅使用 Node.js 标准库与 ECMAScript modules。

## 层级模型

优先级从低到高：

```
base（基础层） → environment（环境层） → instance（实例层）
```

- 每个环境在 base 之上；每个实例登记在某个环境之下。
- 高层键覆盖低层同名键；低层未定义的键沿继承链向上取。
- 值只能是三种形式：
  - 字符串字面量：`"8080"`
  - 单键引用：`ref("host")`，即 `{ "$ref": "host" }`
  - 显式遮蔽（墓碑）：`shadow()`，即 `{ "$shadow": true }`

## 语义约定

1. **引用在请求方实例的有效配置里解析。** 继承来的引用（例如 base 中的 `endpoint: {$ref: host}`）不按定义层解析，而是按请求实例合并后的视图逐跳查找；因此测试环境只覆盖 `host` 时，base 的 `endpoint` 在测试实例中解析到测试环境的 `host` 值。
2. **删除覆盖重新露出父层。** `delete` 只移除本层登记项，合并后该键重新取父层的值。
3. **显式遮蔽使键在此层及继承它的下层缺失。** 墓碑参与合并时直接删除该键；更下层若显式重新 `set` 该键，可以重新引入它。指向被遮蔽键的引用属于悬空引用。
4. **一笔发布可同时改多层，整体原子提交。** 校验在候选新状态上进行：任一已登记实例的有效配置中出现悬空引用或引用环，整笔发布被拒绝，已发布值与版本号维持上一版。
5. **错误可定位到具体实例与引用链。** 环的链以重复键收尾（如 `a -> b -> c -> a`）；悬空的链终止于缺失键。仅在单个测试实例形成的环同样阻止该笔发布，且只归因于该实例。
6. 同一变更里对同一键同时 `set` 与 `delete` 时，以 `set` 为准。
7. 发布时没有任何已登记实例，则引用检查空通过；之后登记的实例在读取时若遇到既有悬空/环，会以同样的 `ConfigReferenceError` 抛出。

## 公开 API

模块：`src/config-center.mjs`

### 辅助构造

- `ref(key)` → `{ $ref: key }`
- `shadow()` → `{ $shadow: true }`
- `ConfigReferenceError`：属性 `instanceId`、`kind`（`'dangling' | 'cycle'`）、`originKey`、`chain`

### `new ConfigCenter()`

| 方法 | 说明 |
| --- | --- |
| `registerEnvironment(envId)` | 登记环境层，重复 id 抛错 |
| `registerInstance(instanceId, envId)` | 在指定环境下登记实例层；未知环境 / 重复 id 抛错 |
| `publish(changes)` | 原子发布，返回成功或失败结果（见下） |
| `getEffectiveConfig(instanceId)` | 完全解析后的 `{ 键: 字符串 }`；遇到悬空/环抛 `ConfigReferenceError` |
| `getMergedConfig(instanceId)` | 合并后、引用解析前的视图，值为字符串 / `{ $ref }` / `{ $shadow }` |
| `resolve(instanceId, key)` | 解析单个键；键缺失按悬空处理（单键链） |
| `listEnvironments()` / `listInstances()` | 已登记 id 列表 |
| `version`（getter） | 单调版本号，初始 0，仅成功发布 +1 |

`publish(changes)` 的变更项：

```js
{
  layer: 'base' | { env: envId } | { instance: instanceId },
  set: { key: '字符串' | ref('x') | shadow() },   // 可选
  delete: ['key'],                                // 可选
}
```

返回：

```js
// 成功
{ ok: true, version: 1, committed: [{ layer, setKeys, deletedKeys }, ...] }
// 失败（状态与版本保持上一版）
{ ok: false, version: 0, errors: [ConfigReferenceError, ...] }
```

非法值（非字符串 / 非单键引用 / 非墓碑）、未知 scope 等输入错误以 `TypeError` / `Error` 同步抛出，且不修改任何状态。

## 复现步骤

要求 Node.js 24.15.0，无需安装依赖。

```bash
node --version            # 期望 v24.15.0
node --test "tests/**/*.test.mjs"
# 或
npm test
```

## 题面场景示例

```js
import { ConfigCenter, ref } from './src/config-center.mjs';

const cc = new ConfigCenter();
cc.registerEnvironment('test');
cc.registerInstance('i-test-1', 'test');

// base 把 endpoint 写成对 host 的引用；测试环境只覆盖 host
cc.publish([
  { layer: 'base', set: { host: 'base.internal', endpoint: ref('host') } },
  { layer: { env: 'test' }, set: { host: 'test.internal' } },
]);

cc.resolve('i-test-1', 'endpoint'); // 'test.internal' —— 继承引用在请求实例视图内解析
```

## 验证范围

测试文件：`tests/config-center.test.mjs`，共 6 组 25 个用例，全部通过 `node:test` + `node:assert/strict` 真实执行（无桩件、无预置演示输出）：

1. **题面场景（2）**：base 引用 + 测试环境只覆盖 host；实例级覆盖再压过环境值。
2. **正常分层（2）**：三级字面量覆盖；多跳引用链解析到字面量。
3. **删除露出父层（3）**：删环境覆盖回退 base；删实例覆盖回退环境；删除本层不存在的键不影响父层。
4. **显式遮蔽（3）**：墓碑使键在本层与下层缺失；下层重新 set 可恢复；引用落在被遮蔽键上判悬空并拒绝发布。
5. **原子发布与发布前校验（6）**：多层同发；任一实例悬空则整笔回滚；所有已登记实例都被检查；引用环返回实例与完整链；自引用；**仅在单个测试实例形成的环也阻止发布且不连累健康实例的错误归因**；失败后修正可重新提交。
6. **边界（9）**：无实例时空通过；后登记实例读取时暴露既有问题；缺失键的单键悬空链；同键 set/delete 以 set 为准；空发布仍升版本；非法值 / 未知 scope 拒绝且不改状态；重复登记与未知实例读取报错；环境间遮蔽隔离。

未执行项：无；`npm test` 脚本与直接运行的 node 命令等价（本环境 `npm` 调用需交互式授权，测试结果以直接执行 `node --test` 为准）。
