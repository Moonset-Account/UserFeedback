# 消息桥订阅协商模块

一个零依赖、确定性的订阅协商器：根据源端顺序覆盖范围、源端/目标端确认粒度、通道池容量和源键到目标业务组的映射，判断每个订阅能够获得的保证，并分配独占目标通道。

运行环境已按 Node.js `v24.15.0` 验证。不需要安装依赖，也不访问网络或真实消息系统。

## 运行方法

```bash
node --test tests/*.test.mjs
```

也可以运行合成示例：

```bash
node examples/basic.mjs
```

`package.json` 同时提供了：

```bash
npm test
```

## 公开 API

```js
import {
  negotiate,
  negotiateSubscription,
  negotiateSubscriptions,
  createNegotiator,
  SubscriptionNegotiator,
  normalizeConfig,
  ACK_GRANULARITY,
  ORDERING_SCOPE,
  FAILURE_CATEGORY,
} from './src/negotiator.mjs';
```

### 纯函数式协商

```js
const report = negotiateSubscriptions(config, subscriptions, options);

// 单个订阅时直接返回 result：
const result = negotiate(config, subscription);
const sameResult = negotiateSubscription(config, subscription);
```

`negotiateSubscriptions` 返回整批结果和通道归属；`negotiate` / `negotiateSubscription` 在第二个参数是单个订阅对象时返回单个结果。

### 有状态通道池

需要模拟“协商、释放、后续交错协商”时使用类：

```js
const negotiator = new SubscriptionNegotiator(config);

const first = negotiator.negotiate(subscriptionA);
const secondReport = negotiator.negotiateAll([subscriptionB, subscriptionC], {
  atomic: true,
});

negotiator.release(first.id);
console.log(negotiator.snapshot());
```

方法：

- `negotiate(subscription, options?)`：协商一个订阅；成功后占用通道。
- `negotiateAll(subscriptions, { atomic = false }?)`：批量协商。
  - 默认非原子：能力和容量都满足的订阅会获得通道，其余返回拒绝原因。
  - `atomic: true`：只要有一个订阅被拒绝，本批中原本可行的订阅也标记为 `aborted`，不修改通道池。
- `release(subscriptionId)`：释放该订阅的全部独占通道和目标组保护。
- `snapshot()`：查看空闲通道、通道归属和受保护目标组。

## 配置格式

规范字段如下；代码也接受常见简写，例如 `source.ack`、`targetGroup`、`ordering`、`requestedChannels` 可写为数字。

```js
const config = {
  // 通道池：可以给数量，也可以给 channelIds 数组。
  channelCount: 3,

  source: {
    // message | source-key | source-range | target-group | batch | subscription | global
    acknowledgementGranularity: 'message',

    // none | source-key | source-range | global
    orderingScope: 'source-range',

    // orderingScope=source-range 时必需。
    // 也可以用 orderRanges: [{ id: 'R1', keys: ['a', 'b'] }]
    // 或 orderRanges: [['a', 'b'], ['c']]。
    ordering: {
      rangeForSourceKey: (sourceKey) => sourceKey,
    },
  },

  target: {
    acknowledgementGranularity: 'message',
  },

  // 源键 -> 目标业务组；也接受 Map 或普通对象。
  targetGroupForSourceKey: (sourceKey) => `group:${sourceKey}`,

  // 可选：目标侧已知源键全集。校验“覆盖整个目标组”时会包含这些键，
  // 即使某次订阅只声明了组内的一部分源键。
  knownSourceKeys: [],
};
```

确认粒度按“越细越能满足更粗请求”的保守顺序比较：

```text
message < source-key < source-range < target-group < batch < subscription < global
```

例如源端只能按 `source-key` 确认时，不能申报 `message`；两端中较粗的一端决定端到端“可提供的最细确认粒度”。

## 订阅格式

```js
const subscription = {
  id: 'orders-subscription',           // 可选，默认 subscription-1...
  sourceKeys: ['order-1', 'payment-1'], // 原始字符串/数字等源键；可省略为空数组

  acknowledgementGranularity: 'message',

  // none | source-key | source-range | target-group | global
  orderingScope: 'target-group',

  // 可选，默认 1；数字表示精确要求。
  requestedChannels: 2,

  // 也支持弹性范围：
  // requestedChannels: { min: 1, max: 4 },
};
```

每个成功订阅获得的通道都是该订阅独占的，不会分配给其他活动订阅。通道按最低可用 id 分配；同一顺序亲和单元通过排序后的轮询固定到其中一个通道。

## 目标组有序规则

当 `orderingScope: 'target-group'` 时，模块会：

1. 用 `targetGroupForSourceKey` 找出每个目标业务组的全部已知源键。
2. 检查这些源键是否都属于同一个非空源端顺序覆盖域：
   - `source-key` 只覆盖单个源键，因此只能覆盖单键目标组；
   - `source-range` 要求组内所有键解析到同一个 range id；
   - `global` 可以覆盖全部键。
3. 将同一个目标组固定到同一个目标通道。
4. 检查独占通道冲突：
   - 同一批中两个不同订阅写同一目标组，且其中一个要求目标组有序，则要求有序的订阅会被拒绝；
   - 已有一个目标组被固定保护时，后续任何写同一组的新订阅都会被拒绝，直到原订阅 `release`。

交错源键也按这些规则判断。例如源端范围是 `R1={a,c}`、`R2={b,d}`：

- 目标组为 `{a,c}`、`{b,d}` 时可以协商，两组分别固定通道；
- 目标组交叉成 `{a,b}`、`{c,d}` 时，两个目标组都无法被单一源端顺序范围覆盖，返回顺序能力不足。

## 返回结果

成功结果示例结构：

```js
{
  ok: true,
  granted: true,
  status: 'granted',
  id: 'example',
  channels: [1, 2],
  channelCount: 2,
  exclusive: true,

  guarantees: {
    acknowledgement: {
      requested: 'message',
      effective: 'message',
      strongestAvailable: 'message',
      sourceGranularity: 'message',
      targetGranularity: 'message',
    },
    ordering: {
      requested: 'target-group',
      effective: 'target-group',
      sourceOrderingScope: 'source-range',
      targetGroupPinned: true,
      sourceCoversTargetGroups: true,
    },
    channels: {
      exclusive: true,
      channelIds: [1, 2],
      count: 2,
    },
  },

  // 每个目标组/源键/范围/全局亲和单元的通道路由。
  routes: [
    {
      affinityType: 'target-group',
      affinity: 'business-group-1',
      channel: 1,
      sourceKeys: ['a', 'b'],
      targetGroup: 'business-group-1',
    },
  ],

  // 每个源键最终走哪个通道，便于路由器直接查表。
  keyAssignments: [
    {
      sourceKey: 'a',
      targetGroup: 'business-group-1',
      sourceOrderRange: 'R1',
      channel: 1,
    },
  ],
}
```

批量报告还包含：

```js
{
  ok: true,
  committed: true,
  atomic: false,
  totalChannelCount: 3,
  allocatedChannelCount: 2,
  freeChannelCount: 1,
  results: [],
  assignments: { /* id -> result */ },
  channelOwners: [{ channel: 1, subscriptionId: 'a' }],
  errors: [],
}
```

## 失败分类

失败结果不会抛出（非法全局配置除外），而是返回：

```js
{
  ok: false,
  status: 'rejected',
  category: 'source-acknowledgement',
  reasonCode: 'SOURCE_ACK_GRANULARITY_UNSUPPORTED',
  reason: '...',
  violations: [/* 全部独立违规 */],
  achievable: {
    acknowledgementGranularity: 'source-key',
    orderingScopes: ['none', 'source-key'],
    availableChannelCount: 1,
    totalChannelCount: 3,
  },
}
```

主要 `category`：

- `source-acknowledgement`：源端确认粒度太粗。
- `target-acknowledgement`：目标端确认粒度太粗。
- `ordering`：源端顺序覆盖不足、目标组跨订阅/跨独占通道冲突、全局顺序覆盖不足。
- `channel-capacity`：通道池不足或申请数超过总池。
- `invalid-subscription` / `invalid-configuration`：输入格式或必填能力字段不合法。

常见 `reasonCode`：

- `SOURCE_ACK_GRANULARITY_UNSUPPORTED`
- `TARGET_ACK_GRANULARITY_UNSUPPORTED`
- `TARGET_GROUP_SOURCE_ORDER_COVERAGE_INSUFFICIENT`
- `TARGET_GROUP_CROSS_SUBSCRIPTION_BATCH_CONFLICT`
- `TARGET_GROUP_CROSS_SUBSCRIPTION_CHANNEL_CONFLICT`
- `TARGET_GROUP_EXISTING_CHANNEL_CONFLICT`
- `GLOBAL_ORDERING_COVERAGE_INSUFFICIENT`
- `CHANNEL_CAPACITY_EXCEEDED`
- `CHANNEL_REQUEST_EXCEEDS_POOL`
- `DUPLICATE_ACTIVE_SUBSCRIPTION_ID`

## 已验证范围

最近一次本地执行：

```text
node --test tests/*.test.mjs
tests 30
pass 30
fail 0
```

测试和合成数据覆盖：

- 默认单通道、多通道、通道归属和确定性分配。
- 确认粒度兼容、源端确认不足、目标端确认不足、细粒度满足粗请求。
- `source-key`、`source-range`、`global` 和 `target-group` 顺序申报。
- 两个源键合并到同一目标组时的源端范围覆盖和同通道固定。
- 单键目标组在仅有源键顺序时可满足。
- 交错源范围与目标组对齐、交叉两类情况。
- 配置中的 `knownSourceKeys` 包含未被本次订阅声明、但属于同一目标组的源键时的全组覆盖检查。
- 跨订阅目标组冲突、已有保护组阻止后续交错写入、`release` 后重新协商。
- 通道容量边界、零通道池、申请数超过总池、弹性 `{min,max}`、释放后再分配。
- 原子批次失败回滚和重复活动订阅 id。
- 空源键集合仍获得最低限度的一个独占通道。
- 用合成事件流实际写入通道日志，断言合并键始终在同一通道且保持源范围顺序；两组并行时各组事件顺序不串组。

未覆盖真实 Kafka/数据库/HTTP 目标、网络故障、持久化 offset 提交、性能压测或真实 exactly-once 语义；这些需要外部系统，且本题要求不联网、不安装依赖。
