/**
 * Subscription negotiation for a message bridge.
 *
 * The module is dependency-free and deliberately deterministic: channel ids
 * are allocated from the lowest available id and affinity groups are sorted
 * before being assigned round-robin to a subscription's exclusive channels.
 */

export const ACK_GRANULARITY = Object.freeze({
  MESSAGE: 'message',
  SOURCE_KEY: 'source-key',
  SOURCE_RANGE: 'source-range',
  TARGET_GROUP: 'target-group',
  BATCH: 'batch',
  SUBSCRIPTION: 'subscription',
  GLOBAL: 'global',
});

export const ORDERING_SCOPE = Object.freeze({
  NONE: 'none',
  SOURCE_KEY: 'source-key',
  SOURCE_RANGE: 'source-range',
  TARGET_GROUP: 'target-group',
  GLOBAL: 'global',
});

export const FAILURE_CATEGORY = Object.freeze({
  INVALID_CONFIGURATION: 'invalid-configuration',
  INVALID_SUBSCRIPTION: 'invalid-subscription',
  SOURCE_ACKNOWLEDGEMENT: 'source-acknowledgement',
  TARGET_ACKNOWLEDGEMENT: 'target-acknowledgement',
  ORDERING: 'ordering',
  CHANNEL_CAPACITY: 'channel-capacity',
});

const ACK_RANK = new Map([
  ['message', 0],
  ['source-key', 10],
  ['source-range', 15],
  ['target-group', 20],
  ['batch', 30],
  ['subscription', 40],
  ['global', 50],
]);

const ACK_ALIASES = new Map(Object.entries({
  message: 'message',
  record: 'message',
  msg: 'message',
  per_message: 'message',
  'per-message': 'message',

  source_key: 'source-key',
  sourcekey: 'source-key',
  key: 'source-key',
  per_source_key: 'source-key',
  'per-source-key': 'source-key',

  source_range: 'source-range',
  sourcerange: 'source-range',
  range: 'source-range',
  partition: 'source-range',

  target_group: 'target-group',
  targetgroup: 'target-group',
  group: 'target-group',
  business_group: 'target-group',
  businessgroup: 'target-group',

  batch: 'batch',

  subscription: 'subscription',
  sub: 'subscription',

  global: 'global',
  all: 'global',
}));

const ORDER_ALIASES = new Map(Object.entries({
  none: 'none',
  unordered: 'none',
  no_order: 'none',
  'no-order': 'none',

  source_key: 'source-key',
  sourcekey: 'source-key',
  key: 'source-key',
  per_key: 'source-key',

  source_range: 'source-range',
  sourcerange: 'source-range',
  range: 'source-range',
  partition: 'source-range',

  target_group: 'target-group',
  targetgroup: 'target-group',
  group: 'target-group',
  business_group: 'target-group',
  businessgroup: 'target-group',

  global: 'global',
  subscription: 'global',
  all: 'global',
}));

const CATEGORY_PRIORITY = [
  FAILURE_CATEGORY.INVALID_CONFIGURATION,
  FAILURE_CATEGORY.INVALID_SUBSCRIPTION,
  FAILURE_CATEGORY.SOURCE_ACKNOWLEDGEMENT,
  FAILURE_CATEGORY.TARGET_ACKNOWLEDGEMENT,
  FAILURE_CATEGORY.ORDERING,
  FAILURE_CATEGORY.CHANNEL_CAPACITY,
];

export class NegotiationConfigurationError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = 'NegotiationConfigurationError';
    this.category = FAILURE_CATEGORY.INVALID_CONFIGURATION;
    this.details = details;
  }
}

function normalizeToken(value) {
  return String(value).trim().toLowerCase().replace(/[\s-]+/g, '_');
}

export function canonicalAcknowledgementGranularity(value) {
  if (value === undefined || value === null) {
    throw new NegotiationConfigurationError('确认粒度不能为空');
  }
  if (ACK_RANK.has(value)) return value;
  const canonical = ACK_ALIASES.get(normalizeToken(value));
  if (!canonical) {
    throw new NegotiationConfigurationError(`未知的确认粒度: ${String(value)}`, { value });
  }
  return canonical;
}

function canonicalOrderingScope(value, { subscription = false } = {}) {
  if (value === undefined || value === null || value === '') return ORDERING_SCOPE.NONE;
  if (Object.values(ORDERING_SCOPE).includes(value)) {
    if (!subscription && value === ORDERING_SCOPE.TARGET_GROUP) {
      throw new NegotiationConfigurationError('源端顺序范围不能直接声明为 target-group；请使用 source-range/global 覆盖目标组');
    }
    return value;
  }
  const canonical = ORDER_ALIASES.get(normalizeToken(value));
  if (!canonical) {
    throw new NegotiationConfigurationError(`未知的顺序范围: ${String(value)}`, { value });
  }
  if (!subscription && canonical === ORDERING_SCOPE.TARGET_GROUP) {
    throw new NegotiationConfigurationError('源端顺序范围不能直接声明为 target-group；请使用 source-range/global 覆盖目标组');
  }
  return canonical;
}

function rankOfAck(value) {
  return ACK_RANK.get(canonicalAcknowledgementGranularity(value));
}

export function coarserAcknowledgementGranularity(a, b) {
  const ca = canonicalAcknowledgementGranularity(a);
  const cb = canonicalAcknowledgementGranularity(b);
  return ACK_RANK.get(ca) >= ACK_RANK.get(cb) ? ca : cb;
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function compareLabels(a, b) {
  const sa = String(a);
  const sb = String(b);
  if (sa < sb) return -1;
  if (sa > sb) return 1;
  return 0;
}

function compareIds(a, b) {
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return compareLabels(a, b);
}

function keyLabel(key) {
  if (typeof key === 'string') return key;
  if (typeof key === 'symbol') return key.toString();
  try {
    return String(key);
  } catch {
    return Object.prototype.toString.call(key);
  }
}

function isValidSourceKey(key) {
  return ['string', 'number', 'boolean', 'bigint', 'symbol'].includes(typeof key)
    && key !== null
    && key !== undefined;
}

function uniquePrimitiveKeys(keys) {
  const seen = new Set();
  const result = [];
  for (const key of keys) {
    if (!seen.has(key)) {
      seen.add(key);
      result.push(key);
    }
  }
  return result;
}

function makeGroupResolver(value) {
  if (value === undefined || value === null) {
    return (key) => key;
  }
  if (typeof value === 'function') return value;
  if (value instanceof Map) {
    return (key) => (value.has(key) ? value.get(key) : undefined);
  }
  if (isPlainObject(value)) {
    return (key) => {
      if (Object.prototype.hasOwnProperty.call(value, key)) return value[key];
      const label = keyLabel(key);
      if (Object.prototype.hasOwnProperty.call(value, label)) return value[label];
      return undefined;
    };
  }
  throw new NegotiationConfigurationError('targetGroupForSourceKey 必须是函数、Map 或普通对象');
}

function makeRangeResolver(value) {
  if (value === undefined || value === null) return null;
  if (typeof value === 'function') return value;

  if (value instanceof Map) {
    const map = new Map();
    for (const [rangeId, keys] of value.entries()) {
      if (!Array.isArray(keys)) {
        throw new NegotiationConfigurationError('source orderRange Map 的值必须是源键数组', { rangeId });
      }
      for (const key of keys) map.set(key, rangeId);
    }
    return (key) => (map.has(key) ? map.get(key) : undefined);
  }

  if (Array.isArray(value)) {
    const map = new Map();
    value.forEach((entry, index) => {
      let rangeId;
      let keys;
      if (Array.isArray(entry)) {
        rangeId = `range-${index + 1}`;
        keys = entry;
      } else if (isPlainObject(entry)) {
        rangeId = entry.id ?? entry.name ?? entry.rangeId ?? `range-${index + 1}`;
        keys = entry.keys ?? entry.sourceKeys ?? entry.members;
      } else {
        throw new NegotiationConfigurationError('source orderRanges 数组项必须是键数组或 {id, keys} 对象');
      }
      if (!Array.isArray(keys)) {
        throw new NegotiationConfigurationError('source orderRange 缺少 keys 数组', { rangeId });
      }
      for (const key of keys) map.set(key, rangeId);
    });
    return (key) => (map.has(key) ? map.get(key) : undefined);
  }

  if (isPlainObject(value)) {
    return makeRangeResolver(value.ranges ?? value.entries);
  }

  throw new NegotiationConfigurationError('source orderRangeForSourceKey/orderRanges 格式无效');
}

function normalizeChannelIds(configInput) {
  if (Array.isArray(configInput.channelIds)) {
    const ids = [...configInput.channelIds];
    if (ids.length === 0) return [];
    const unique = new Set(ids.map(String));
    if (unique.size !== ids.length) {
      throw new NegotiationConfigurationError('channelIds 不能包含重复通道');
    }
    return ids.sort(compareIds);
  }

  if (Array.isArray(configInput.channels)) {
    const ids = [...configInput.channels];
    return ids.sort(compareIds);
  }

  const rawCount = configInput.channelCount
    ?? configInput.channels
    ?? configInput.availableChannels
    ?? configInput.maxConcurrentChannels
    ?? 1;

  if (typeof rawCount !== 'number' || !Number.isInteger(rawCount) || rawCount < 0) {
    throw new NegotiationConfigurationError('channelCount 必须是大于等于 0 的整数', { value: rawCount });
  }
  return Array.from({ length: rawCount }, (_, index) => index + 1);
}

export function normalizeConfig(input = {}) {
  if (!isPlainObject(input)) {
    throw new NegotiationConfigurationError('配置必须是对象');
  }

  const sourceInput = input.source ?? {};
  const targetInput = input.target ?? {};
  const ackInput = input.acknowledgement ?? input.ack ?? {};

  const sourceAckValue = sourceInput.acknowledgementGranularity
    ?? sourceInput.ackGranularity
    ?? sourceInput.ack
    ?? input.sourceAcknowledgementGranularity
    ?? input.sourceAckGranularity
    ?? input.sourceAck
    ?? ackInput.sourceGranularity
    ?? ackInput.source;

  const targetAckValue = targetInput.acknowledgementGranularity
    ?? targetInput.ackGranularity
    ?? targetInput.ack
    ?? input.targetAcknowledgementGranularity
    ?? input.targetAckGranularity
    ?? input.targetAck
    ?? ackInput.targetGranularity
    ?? ackInput.target;

  if (sourceAckValue === undefined) {
    throw new NegotiationConfigurationError('缺少 source.acknowledgementGranularity');
  }
  if (targetAckValue === undefined) {
    throw new NegotiationConfigurationError('缺少 target.acknowledgementGranularity');
  }

  let sourceAck;
  let targetAck;
  try {
    sourceAck = canonicalAcknowledgementGranularity(sourceAckValue);
    targetAck = canonicalAcknowledgementGranularity(targetAckValue);
  } catch (error) {
    if (error instanceof NegotiationConfigurationError) throw error;
    throw error;
  }

  const orderingInput = sourceInput.ordering ?? sourceInput.order ?? {};
  const sourceOrderingScope = canonicalOrderingScope(
    orderingInput.scope
      ?? sourceInput.orderingScope
      ?? input.sourceOrderingScope
      ?? ORDERING_SCOPE.NONE,
  );

  const rangeValue = orderingInput.rangeForSourceKey
    ?? orderingInput.resolveRange
    ?? orderingInput.orderRangeForSourceKey
    ?? orderingInput.ranges
    ?? sourceInput.orderRangeForSourceKey
    ?? sourceInput.orderRanges
    ?? input.sourceOrderRangeForSourceKey
    ?? input.sourceOrderRanges;

  const rangeResolver = makeRangeResolver(rangeValue);
  if (sourceOrderingScope === ORDERING_SCOPE.SOURCE_RANGE && !rangeResolver) {
    throw new NegotiationConfigurationError(
      'source.orderingScope=source-range 时必须提供 orderRangeForSourceKey 或 orderRanges',
    );
  }

  const groupValue = input.targetGroupForSourceKey
    ?? input.targetGroup
    ?? input.groupForSourceKey
    ?? input.targetGroupResolver
    ?? targetInput.groupForSourceKey
    ?? targetInput.targetGroupForSourceKey
    ?? input.targetGroups;

  const targetGroupResolver = makeGroupResolver(groupValue);
  const channelIds = normalizeChannelIds(input);
  const knownSourceKeys = uniquePrimitiveKeys(
    input.knownSourceKeys ?? input.sourceKeys ?? input.knownKeys ?? [],
  );

  return Object.freeze({
    channelIds: Object.freeze(channelIds),
    channelCount: channelIds.length,
    source: Object.freeze({
      acknowledgementGranularity: sourceAck,
      orderingScope: sourceOrderingScope,
      orderRangeForSourceKey: rangeResolver,
    }),
    target: Object.freeze({
      acknowledgementGranularity: targetAck,
    }),
    targetGroupForSourceKey: targetGroupResolver,
    knownSourceKeys: Object.freeze(knownSourceKeys),
  });
}

function invalidSubscription(id, index, message, details = {}) {
  return {
    id: id ?? `subscription-${index + 1}`,
    sub: null,
    violations: [{
      category: FAILURE_CATEGORY.INVALID_SUBSCRIPTION,
      code: details.code ?? 'INVALID_SUBSCRIPTION',
      message,
      details,
    }],
  };
}

function normalizeChannelRequirement(raw) {
  if (raw === undefined || raw === null) {
    return { min: 1, max: 1, requested: 1 };
  }

  if (typeof raw === 'number' || typeof raw === 'bigint') {
    const value = Number(raw);
    if (!Number.isInteger(value) || value < 1) {
      return { error: '通道数必须是大于等于 1 的整数' };
    }
    return { min: value, max: value, requested: value };
  }

  if (isPlainObject(raw)) {
    const exact = raw.exact ?? raw.requested ?? raw.count ?? raw.channelCount;
    if (exact !== undefined) {
      const value = Number(exact);
      if (!Number.isInteger(value) || value < 1) {
        return { error: 'requestedChannels 必须是大于等于 1 的整数' };
      }
      return { min: value, max: value, requested: value };
    }

    const min = Number(raw.min ?? raw.minChannels ?? 1);
    const max = Number(raw.max ?? raw.maxChannels ?? min);
    if (!Number.isInteger(min) || min < 1 || !Number.isInteger(max) || max < min) {
      return { error: '通道范围必须满足 1 <= min <= max，且均为整数' };
    }
    return { min, max, requested: undefined };
  }

  return { error: '通道要求必须是整数或 {min,max} 对象' };
}

function normalizeSubscription(input, index = 0) {
  if (!isPlainObject(input)) {
    return invalidSubscription(
      input?.id,
      index,
      '订阅必须是对象',
      { code: 'INVALID_SUBSCRIPTION' },
    );
  }

  const id = input.id ?? input.subscriptionId ?? input.name ?? `subscription-${index + 1}`;
  if (typeof id !== 'string' && typeof id !== 'number') {
    return invalidSubscription(undefined, index, '订阅 id 必须是字符串或数字', {
      code: 'INVALID_SUBSCRIPTION_ID',
    });
  }
  const canonicalId = String(id);

  const rawKeys = input.sourceKeys
    ?? input.keys
    ?? input.sourceKey
    ?? input.source_keys
    ?? [];
  const keyList = Array.isArray(rawKeys)
    ? rawKeys
    : rawKeys instanceof Set
      ? [...rawKeys]
      : [rawKeys];

  const invalidKey = keyList.find((key) => !isValidSourceKey(key));
  if (invalidKey !== undefined || keyList.some((key) => key === null || key === undefined)) {
    return invalidSubscription(canonicalId, index, 'sourceKeys 必须是字符串、数字等原始源键', {
      code: 'INVALID_SOURCE_KEY',
      value: String(invalidKey),
    });
  }

  const sourceKeys = uniquePrimitiveKeys(keyList);
  const violations = [];

  let ackValue = input.acknowledgementGranularity
    ?? input.ackGranularity
    ?? input.ack
    ?? input.acknowledgement;
  if (isPlainObject(ackValue)) {
    ackValue = ackValue.granularity ?? ackValue.acknowledgementGranularity ?? ackValue.scope;
  }

  let ack;
  if (ackValue === undefined) {
    violations.push({
      category: FAILURE_CATEGORY.INVALID_SUBSCRIPTION,
      code: 'ACK_GRANULARITY_REQUIRED',
      message: '订阅必须申报 acknowledgementGranularity',
      details: {},
    });
  } else {
    try {
      ack = canonicalAcknowledgementGranularity(ackValue);
    } catch {
      violations.push({
        category: FAILURE_CATEGORY.INVALID_SUBSCRIPTION,
        code: 'UNKNOWN_ACK_GRANULARITY',
        message: `未知的确认粒度: ${String(ackValue)}`,
        details: { value: String(ackValue) },
      });
    }
  }

  let orderingValue = input.orderingScope ?? input.ordering ?? input.order ?? ORDERING_SCOPE.NONE;
  if (isPlainObject(orderingValue)) {
    orderingValue = orderingValue.scope ?? orderingValue.orderingScope ?? ORDERING_SCOPE.NONE;
  }

  let ordering = ORDERING_SCOPE.NONE;
  try {
    ordering = canonicalOrderingScope(orderingValue, { subscription: true });
  } catch (error) {
    violations.push({
      category: FAILURE_CATEGORY.INVALID_SUBSCRIPTION,
      code: 'UNKNOWN_ORDERING_SCOPE',
      message: error.message,
      details: { value: String(input.orderingScope ?? input.ordering) },
    });
  }

  const channelRequirement = normalizeChannelRequirement(
    input.requestedChannels
      ?? input.channelRequirement
      ?? input.channelCount
      ?? input.channels
      ?? input.concurrency,
  );
  if (channelRequirement.error) {
    violations.push({
      category: FAILURE_CATEGORY.INVALID_SUBSCRIPTION,
      code: 'INVALID_CHANNEL_REQUIREMENT',
      message: channelRequirement.error,
      details: {},
    });
  }

  return {
    id: canonicalId,
    sub: {
      id: canonicalId,
      sourceKeys,
      acknowledgementGranularity: ack,
      orderingScope: ordering,
      channelRequirement: channelRequirement.error ? null : channelRequirement,
    },
    violations,
  };
}

function buildTopology(config, knownKeys) {
  const uniqueKeys = uniquePrimitiveKeys(knownKeys);
  const groupByKey = new Map();
  const targetGroupMembers = new Map();
  const resolutionErrors = [];

  for (const key of uniqueKeys) {
    let group;
    try {
      group = config.targetGroupForSourceKey(key);
    } catch (error) {
      resolutionErrors.push({
        category: FAILURE_CATEGORY.INVALID_CONFIGURATION,
        code: 'TARGET_GROUP_RESOLVER_THREW',
        message: `targetGroupForSourceKey 对源键 ${keyLabel(key)} 抛出异常: ${error.message}`,
        details: { sourceKey: keyLabel(key), error: error.message },
      });
      continue;
    }

    if (group === undefined || group === null) {
      resolutionErrors.push({
        category: FAILURE_CATEGORY.INVALID_CONFIGURATION,
        code: 'TARGET_GROUP_UNRESOLVED',
        message: `无法从源键 ${keyLabel(key)} 解析目标组`,
        details: { sourceKey: keyLabel(key) },
      });
      continue;
    }

    groupByKey.set(key, group);
    if (!targetGroupMembers.has(group)) targetGroupMembers.set(group, []);
    targetGroupMembers.get(group).push(key);
  }

  return { knownKeys: uniqueKeys, groupByKey, targetGroupMembers, resolutionErrors };
}

function sourceCoverage(config, key) {
  const scope = config.source.orderingScope;
  const keyLabelValue = keyLabel(key);
  if (scope === ORDERING_SCOPE.NONE) {
    return { scope, keyOrdered: false, coverageId: null, rangeId: null };
  }
  if (scope === ORDERING_SCOPE.SOURCE_KEY) {
    return {
      scope,
      keyOrdered: true,
      coverageId: `source-key:${typeof key}:${keyLabelValue}`,
      rangeId: null,
    };
  }
  if (scope === ORDERING_SCOPE.GLOBAL) {
    return { scope, keyOrdered: true, coverageId: 'global', rangeId: 'global' };
  }

  let rangeId;
  try {
    rangeId = config.source.orderRangeForSourceKey?.(key);
  } catch (error) {
    return {
      scope,
      keyOrdered: true,
      coverageId: null,
      rangeId: null,
      resolverError: error.message,
    };
  }

  if (rangeId === undefined || rangeId === null) {
    return { scope, keyOrdered: true, coverageId: null, rangeId: null };
  }

  return {
    scope,
    keyOrdered: true,
    coverageId: `source-range:${typeof rangeId}:${keyLabel(rangeId)}`,
    rangeId,
  };
}

function primaryCategory(violations) {
  for (const category of CATEGORY_PRIORITY) {
    if (violations.some((item) => item.category === category)) return category;
  }
  return null;
}

function sortViolations(violations) {
  return [...violations].sort(
    (a, b) => CATEGORY_PRIORITY.indexOf(a.category) - CATEGORY_PRIORITY.indexOf(b.category),
  );
}

function buildRoutes(sub, topology, coverageByKey, channels) {
  const unitMap = new Map();
  const unitChannel = new Map();

  function unitFor(key) {
    const group = topology.groupByKey.get(key);
    const coverage = coverageByKey.get(key);

    if (sub.orderingScope === ORDERING_SCOPE.GLOBAL) {
      return {
        mapKey: 'global:all',
        unitType: 'global',
        unit: '__all__',
        name: 'global',
        rangeId: ORDERING_SCOPE.GLOBAL,
      };
    }

    if (sub.orderingScope === ORDERING_SCOPE.TARGET_GROUP) {
      const name = keyLabel(group);
      return {
        mapKey: `target-group:${name}`,
        unitType: 'target-group',
        unit: group,
        name,
        rangeId: coverage?.rangeId ?? null,
      };
    }

    if (sub.orderingScope === ORDERING_SCOPE.SOURCE_RANGE) {
      const rangeId = coverage?.rangeId;
      const name = keyLabel(rangeId);
      return {
        mapKey: `source-range:${name}`,
        unitType: 'source-range',
        unit: rangeId,
        name,
        rangeId,
      };
    }

    // NONE and SOURCE_KEY both retain affinity per source key. For NONE this
    // is only a deterministic routing choice, not an ordering promise.
    const name = keyLabel(key);
    return {
      mapKey: `source-key:${typeof key}:${name}`,
      unitType: ORDERING_SCOPE.SOURCE_KEY,
      unit: key,
      name,
      rangeId: coverage?.rangeId ?? null,
    };
  }

  for (const key of sub.sourceKeys) {
    const unit = unitFor(key);
    if (!unitMap.has(unit.mapKey)) {
      unitMap.set(unit.mapKey, {
        affinityType: unit.unitType,
        affinity: unit.name,
        affinityValue: unit.unit,
        sourceOrderRange: unit.rangeId,
        sourceKeys: [],
        _mapKey: unit.mapKey,
      });
    }
    unitMap.get(unit.mapKey).sourceKeys.push(key);
  }

  const units = [...unitMap.values()].sort((a, b) => compareLabels(a._mapKey, b._mapKey));
  units.forEach((unit, index) => {
    const channel = channels[index % channels.length];
    unit.channel = channel;
    unitChannel.set(unit._mapKey, channel);
  });

  const routes = units.map(({ _mapKey, affinityValue, ...route }) => {
    const sourceKeys = route.sourceKeys.sort(compareLabels);
    const targetGroups = uniquePrimitiveKeys(
      sourceKeys.map((key) => topology.groupByKey.get(key)).filter((value) => value !== undefined),
    ).sort(compareLabels);

    return {
      ...route,
      sourceKeys,
      targetGroup: route.affinityType === 'target-group' ? affinityValue : undefined,
      targetGroups,
    };
  });

  const keyAssignments = sub.sourceKeys.map((key) => {
    const unit = unitFor(key);
    return {
      sourceKey: key,
      targetGroup: topology.groupByKey.get(key) ?? null,
      sourceOrderRange: coverageByKey.get(key)?.rangeId ?? null,
      channel: unitChannel.get(unit.mapKey),
    };
  }).sort((a, b) => compareLabels(keyLabel(a.sourceKey), keyLabel(b.sourceKey)));

  return { routes, keyAssignments };
}

function rejectedResult({
  id,
  index,
  requested,
  violations,
  config,
  availableChannelIds,
  topology,
  sub,
}) {
  const sorted = sortViolations(violations);
  const category = primaryCategory(sorted);
  const sourceRank = config?.source ? ACK_RANK.get(config.source.acknowledgementGranularity) : null;
  const targetRank = config?.target ? ACK_RANK.get(config.target.acknowledgementGranularity) : null;
  const finestAck = sourceRank !== null && targetRank !== null
    ? (sourceRank >= targetRank ? config.source.acknowledgementGranularity : config.target.acknowledgementGranularity)
    : null;

  let achievableOrderingScopes = [ORDERING_SCOPE.NONE];
  if (sub && config) {
    const coverages = sub.sourceKeys.map((key) => sourceCoverage(config, key));
    if (coverages.every((item) => item.keyOrdered)) {
      achievableOrderingScopes.push(ORDERING_SCOPE.SOURCE_KEY);
    }
    if (
      config.source.orderingScope === ORDERING_SCOPE.GLOBAL
      || (
        config.source.orderingScope === ORDERING_SCOPE.SOURCE_RANGE
        && coverages.every((item) => item.rangeId !== null && item.rangeId !== undefined)
      )
    ) {
      achievableOrderingScopes.push(ORDERING_SCOPE.SOURCE_RANGE);
    }

    const touchedGroups = uniquePrimitiveKeys(
      sub.sourceKeys.map((key) => topology?.groupByKey.get(key)).filter((g) => g !== undefined),
    );
    const targetCoverable = touchedGroups.every((group) => {
      const members = topology.targetGroupMembers.get(group) ?? [];
      const ids = members.map((key) => sourceCoverage(config, key).coverageId);
      return ids.length > 0 && ids.every(Boolean) && new Set(ids).size === 1;
    });
    if (targetCoverable) achievableOrderingScopes.push(ORDERING_SCOPE.TARGET_GROUP);

    const globalIds = coverages.map((item) => item.coverageId).filter(Boolean);
    if (
      sub.sourceKeys.length === 0
      || (globalIds.length === sub.sourceKeys.length && new Set(globalIds).size === 1)
    ) {
      achievableOrderingScopes.push(ORDERING_SCOPE.GLOBAL);
    }
    achievableOrderingScopes = [...new Set(achievableOrderingScopes)];
  }

  return {
    id: id ?? `subscription-${index + 1}`,
    index,
    ok: false,
    granted: false,
    status: 'rejected',
    category,
    reasonCode: sorted[0]?.code ?? 'NEGOTIATION_FAILED',
    reason: sorted[0]?.message ?? '订阅无法满足',
    violations: sorted,
    requested: requested ?? null,
    actual: null,
    guarantees: null,
    channels: [],
    channelCount: 0,
    exclusive: false,
    routes: [],
    keyAssignments: [],
    achievable: config ? {
      acknowledgementGranularity: finestAck,
      orderingScopes: achievableOrderingScopes,
      availableChannelCount: availableChannelIds?.length ?? 0,
      totalChannelCount: config.channelCount,
    } : null,
  };
}

function planSubscription({
  config,
  normalized,
  index,
  topology,
  availableChannelIds,
  activeWritersByGroup,
  protectedGroups,
  batchGroupSubs,
  activeIdTaken,
  batchDuplicateId,
}) {
  const { id, sub } = normalized;
  const requested = {
    id,
    sourceKeys: sub.sourceKeys,
    acknowledgementGranularity: sub.acknowledgementGranularity,
    orderingScope: sub.orderingScope,
    channelRequirement: sub.channelRequirement,
  };

  const violations = [...normalized.violations, ...topology.resolutionErrors];

  if (activeIdTaken) {
    violations.push({
      category: FAILURE_CATEGORY.INVALID_SUBSCRIPTION,
      code: 'DUPLICATE_ACTIVE_SUBSCRIPTION_ID',
      message: `订阅 id ${id} 已存在；请先 release 或使用 replace 重新协商`,
      details: { subscriptionId: id },
    });
  }
  if (batchDuplicateId) {
    violations.push({
      category: FAILURE_CATEGORY.INVALID_SUBSCRIPTION,
      code: 'DUPLICATE_SUBSCRIPTION_ID_IN_BATCH',
      message: `批次中存在重复订阅 id: ${id}`,
      details: { subscriptionId: id },
    });
  }

  if (sub.acknowledgementGranularity) {
    const requestedRank = ACK_RANK.get(sub.acknowledgementGranularity);
    const sourceRank = ACK_RANK.get(config.source.acknowledgementGranularity);
    const targetRank = ACK_RANK.get(config.target.acknowledgementGranularity);

    if (sourceRank > requestedRank) {
      violations.push({
        category: FAILURE_CATEGORY.SOURCE_ACKNOWLEDGEMENT,
        code: 'SOURCE_ACK_GRANULARITY_UNSUPPORTED',
        message: `源端确认粒度 ${config.source.acknowledgementGranularity} 比申报的 ${sub.acknowledgementGranularity} 更粗，无法提供所要求的确认保证`,
        details: {
          side: 'source',
          sourceGranularity: config.source.acknowledgementGranularity,
          requestedGranularity: sub.acknowledgementGranularity,
        },
      });
    }
    if (targetRank > requestedRank) {
      violations.push({
        category: FAILURE_CATEGORY.TARGET_ACKNOWLEDGEMENT,
        code: 'TARGET_ACK_GRANULARITY_UNSUPPORTED',
        message: `目标端确认粒度 ${config.target.acknowledgementGranularity} 比申报的 ${sub.acknowledgementGranularity} 更粗，无法提供所要求的确认保证`,
        details: {
          side: 'target',
          targetGranularity: config.target.acknowledgementGranularity,
          requestedGranularity: sub.acknowledgementGranularity,
        },
      });
    }
  }

  const coverageByKey = new Map();
  for (const key of sub.sourceKeys) {
    coverageByKey.set(key, sourceCoverage(config, key));
  }

  const touchedGroups = uniquePrimitiveKeys(
    sub.sourceKeys
      .map((key) => topology.groupByKey.get(key))
      .filter((group) => group !== undefined),
  );

  for (const key of sub.sourceKeys) {
    if (!topology.groupByKey.has(key)) {
      violations.push({
        category: FAILURE_CATEGORY.ORDERING,
        code: 'TARGET_GROUP_UNRESOLVED_FOR_SUBSCRIPTION',
        message: `源键 ${keyLabel(key)} 无法解析目标组，不能安全安排目标组有序通道`,
        details: { sourceKey: keyLabel(key) },
      });
    }
  }

  const groupCoverage = touchedGroups.map((group) => {
    const members = topology.targetGroupMembers.get(group) ?? [];
    const memberStatuses = members.map((key) => ({
      sourceKey: key,
      coverageId: sourceCoverage(config, key).coverageId,
    }));
    const ids = memberStatuses.map((item) => item.coverageId).filter(Boolean);
    const covered = ids.length === members.length
      && members.length > 0
      && new Set(ids).size === 1;
    return { group, members, memberStatuses, covered };
  });

  if (sub.orderingScope === ORDERING_SCOPE.SOURCE_KEY) {
    if (config.source.orderingScope === ORDERING_SCOPE.NONE) {
      violations.push({
        category: FAILURE_CATEGORY.ORDERING,
        code: 'SOURCE_KEY_ORDERING_UNSUPPORTED',
        message: '源端未提供任何源键顺序保证',
        details: { sourceOrderingScope: config.source.orderingScope },
      });
    }
  }

  if (sub.orderingScope === ORDERING_SCOPE.SOURCE_RANGE) {
    const rangeKnown = config.source.orderingScope === ORDERING_SCOPE.GLOBAL
      || (
        config.source.orderingScope === ORDERING_SCOPE.SOURCE_RANGE
        && sub.sourceKeys.every((key) => coverageByKey.get(key)?.rangeId != null)
      );
    if (!rangeKnown) {
      violations.push({
        category: FAILURE_CATEGORY.ORDERING,
        code: 'SOURCE_RANGE_ORDERING_UNSUPPORTED',
        message: '源端不能为订阅的全部源键提供可识别的顺序范围',
        details: { sourceOrderingScope: config.source.orderingScope },
      });
    }
  }

  if (sub.orderingScope === ORDERING_SCOPE.TARGET_GROUP) {
    const uncovered = groupCoverage.filter((item) => !item.covered);
    if (config.source.orderingScope === ORDERING_SCOPE.NONE || uncovered.length > 0) {
      violations.push({
        category: FAILURE_CATEGORY.ORDERING,
        code: 'TARGET_GROUP_SOURCE_ORDER_COVERAGE_INSUFFICIENT',
        message: '存在目标业务组未被同一个源端顺序范围完整覆盖，无法保证按目标组有序',
        details: {
          uncoveredTargetGroups: uncovered.map((item) => ({
            targetGroup: keyLabel(item.group),
            sourceKeys: item.members.map(keyLabel),
            sourceCoverageIds: item.memberStatuses.map((status) => status.coverageId),
          })),
          sourceOrderingScope: config.source.orderingScope,
        },
      });
    }
  }

  if (sub.orderingScope === ORDERING_SCOPE.GLOBAL && sub.sourceKeys.length > 0) {
    const ids = sub.sourceKeys.map((key) => coverageByKey.get(key)?.coverageId).filter(Boolean);
    if (ids.length !== sub.sourceKeys.length || new Set(ids).size !== 1) {
      violations.push({
        category: FAILURE_CATEGORY.ORDERING,
        code: 'GLOBAL_ORDERING_COVERAGE_INSUFFICIENT',
        message: '订阅的全部源键不在同一个源端顺序范围内，无法保证订阅全局有序',
        details: {
          sourceKeys: sub.sourceKeys.map(keyLabel),
          sourceCoverageIds: sub.sourceKeys.map((key) => coverageByKey.get(key)?.coverageId ?? null),
        },
      });
    }
  }

  // Exclusive-channel ownership checks. A protected target group cannot gain
  // another subscription writer because that writer would own different
  // channels. This fails closed to preserve an already promised order.
  for (const group of touchedGroups) {
    const protector = protectedGroups?.get(group);
    if (protector && protector.subscriptionId !== id) {
      violations.push({
        category: FAILURE_CATEGORY.ORDERING,
        code: 'TARGET_GROUP_EXISTING_CHANNEL_CONFLICT',
        message: `目标组 ${keyLabel(group)} 已由订阅 ${protector.subscriptionId} 的通道 ${protector.channel} 固定，不能再分配给独占通道的订阅 ${id}`,
        details: {
          targetGroup: keyLabel(group),
          existingSubscriptionId: protector.subscriptionId,
          existingChannel: protector.channel,
          requestedSubscriptionId: id,
        },
      });
    }

    if (sub.orderingScope === ORDERING_SCOPE.TARGET_GROUP) {
      const activeWriters = activeWritersByGroup?.get(group);
      const otherActive = activeWriters ? [...activeWriters].filter((writerId) => writerId !== id) : [];
      if (otherActive.length > 0 && !(protector && protector.subscriptionId !== id)) {
        violations.push({
          category: FAILURE_CATEGORY.ORDERING,
          code: 'TARGET_GROUP_CROSS_SUBSCRIPTION_CHANNEL_CONFLICT',
          message: `目标组 ${keyLabel(group)} 正被其他订阅 ${otherActive.join(', ')} 写入；独占通道无法保证始终走同一目标通道`,
          details: {
            targetGroup: keyLabel(group),
            otherSubscriptionIds: otherActive,
            requestedSubscriptionId: id,
          },
        });
      }

      const batchOwners = batchGroupSubs?.get(group);
      const otherBatch = batchOwners ? [...batchOwners].filter((ownerId) => ownerId !== id) : [];
      if (otherBatch.length > 0) {
        violations.push({
          category: FAILURE_CATEGORY.ORDERING,
          code: 'TARGET_GROUP_CROSS_SUBSCRIPTION_BATCH_CONFLICT',
          message: `目标组 ${keyLabel(group)} 在同一批请求中还属于订阅 ${otherBatch.join(', ')}；独占通道无法保证全组走同一通道`,
          details: {
            targetGroup: keyLabel(group),
            otherSubscriptionIds: otherBatch,
            requestedSubscriptionId: id,
          },
        });
      }
    }
  }

  const requirement = sub.channelRequirement;
  let grantCount = 0;
  if (requirement) {
    if (requirement.min > config.channelCount) {
      violations.push({
        category: FAILURE_CATEGORY.CHANNEL_CAPACITY,
        code: 'CHANNEL_REQUEST_EXCEEDS_POOL',
        message: `订阅至少需要 ${requirement.min} 个通道，但配置只有 ${config.channelCount} 个通道`,
        details: {
          requestedChannels: requirement.requested,
          minChannels: requirement.min,
          maxChannels: requirement.max,
          totalChannels: config.channelCount,
        },
      });
    } else {
      grantCount = Math.min(requirement.max, availableChannelIds.length);
      if (grantCount < requirement.min) {
        violations.push({
          category: FAILURE_CATEGORY.CHANNEL_CAPACITY,
          code: 'CHANNEL_CAPACITY_EXCEEDED',
          message: `订阅至少需要 ${requirement.min} 个独占通道，当前仅 ${availableChannelIds.length} 个空闲通道`,
          details: {
            requestedChannels: requirement.requested,
            minChannels: requirement.min,
            maxChannels: requirement.max,
            availableChannelCount: availableChannelIds.length,
            totalChannels: config.channelCount,
          },
        });
      }
    }
  }

  const availableSorted = [...availableChannelIds].sort(compareIds);
  const channels = violations.length === 0 && requirement
    ? availableSorted.slice(0, grantCount)
    : [];

  if (violations.length > 0) {
    return rejectedResult({
      id,
      index,
      requested,
      violations,
      config,
      availableChannelIds: availableSorted,
      topology,
      sub,
    });
  }

  const { routes, keyAssignments } = buildRoutes(sub, topology, coverageByKey, channels);
  const finestAck = coarserAcknowledgementGranularity(
    config.source.acknowledgementGranularity,
    config.target.acknowledgementGranularity,
  );

  const result = {
    id,
    index,
    ok: true,
    granted: true,
    status: 'granted',
    category: null,
    reasonCode: null,
    reason: null,
    violations: [],
    requested,
    actual: {
      acknowledgementGranularity: sub.acknowledgementGranularity,
      strongestAcknowledgementGranularity: finestAck,
      orderingScope: sub.orderingScope,
      exclusiveChannels: true,
      targetGroupPinned: sub.orderingScope === ORDERING_SCOPE.TARGET_GROUP,
    },
    guarantees: {
      acknowledgement: {
        requested: sub.acknowledgementGranularity,
        effective: sub.acknowledgementGranularity,
        strongestAvailable: finestAck,
        sourceGranularity: config.source.acknowledgementGranularity,
        targetGranularity: config.target.acknowledgementGranularity,
      },
      ordering: {
        requested: sub.orderingScope,
        effective: sub.orderingScope,
        sourceOrderingScope: config.source.orderingScope,
        targetGroupPinned: sub.orderingScope === ORDERING_SCOPE.TARGET_GROUP,
        sourceCoversTargetGroups: sub.orderingScope === ORDERING_SCOPE.TARGET_GROUP
          ? groupCoverage.every((item) => item.covered)
          : null,
      },
      channels: {
        exclusive: true,
        channelIds: channels,
        count: channels.length,
      },
    },
    channels,
    channelCount: channels.length,
    exclusive: true,
    routes,
    keyAssignments,
    achievable: null,
  };

  Object.defineProperty(result, '_sub', {
    value: sub,
    enumerable: false,
    configurable: true,
    writable: false,
  });

  return result;
}

export class SubscriptionNegotiator {
  constructor(config = {}) {
    this.config = normalizeConfig(config);
    this.usedChannels = new Set();
    this.allocations = new Map();
    this.writersByGroup = new Map();
    this.protectedGroups = new Map();
  }

  _activeKeys() {
    return [...this.allocations.values()].flatMap((entry) => entry.sub.sourceKeys);
  }

  _availableChannels(extraUsed = new Set()) {
    return this.config.channelIds.filter(
      (channel) => !this.usedChannels.has(channel) && !extraUsed.has(channel),
    );
  }

  negotiateAll(subscriptions = [], options = {}) {
    if (!Array.isArray(subscriptions)) {
      throw new NegotiationConfigurationError('negotiateAll 的参数必须是订阅数组');
    }
    const atomic = options.atomic ?? false;
    const normalizedList = subscriptions.map((input, index) => normalizeSubscription(input, index));

    const idCounts = new Map();
    for (const normalized of normalizedList) {
      if (normalized.sub) {
        idCounts.set(normalized.id, (idCounts.get(normalized.id) ?? 0) + 1);
      }
    }

    const batchKeys = normalizedList.flatMap((normalized) => normalized.sub?.sourceKeys ?? []);
    const knownKeys = uniquePrimitiveKeys([
      ...this.config.knownSourceKeys,
      ...this._activeKeys(),
      ...batchKeys,
    ]);
    const topology = buildTopology(this.config, knownKeys);

    const batchGroupSubs = new Map();
    for (const normalized of normalizedList) {
      if (!normalized.sub) continue;
      for (const key of normalized.sub.sourceKeys) {
        const group = topology.groupByKey.get(key);
        if (group === undefined) continue;
        if (!batchGroupSubs.has(group)) batchGroupSubs.set(group, new Set());
        batchGroupSubs.get(group).add(normalized.id);
      }
    }

    const results = [];
    const tentativeUsed = new Set();

    normalizedList.forEach((normalized, index) => {
      if (!normalized.sub) {
        results.push(rejectedResult({
          id: normalized.id,
          index,
          requested: null,
          violations: normalized.violations,
          config: this.config,
          availableChannelIds: this._availableChannels(tentativeUsed),
          topology,
        }));
        return;
      }

      const result = planSubscription({
        config: this.config,
        normalized,
        index,
        topology,
        availableChannelIds: this._availableChannels(tentativeUsed),
        activeWritersByGroup: this.writersByGroup,
        protectedGroups: this.protectedGroups,
        batchGroupSubs,
        activeIdTaken: this.allocations.has(normalized.id),
        batchDuplicateId: (idCounts.get(normalized.id) ?? 0) > 1,
      });

      results.push(result);
      if (result.ok) result.channels.forEach((channel) => tentativeUsed.add(channel));
    });

    const anyRejected = results.some((result) => !result.ok);
    const committed = !atomic || !anyRejected;

    if (committed) {
      for (const result of results) {
        if (result.ok) this._commit(result, topology);
      }
    } else {
      for (const result of results) {
        if (result.ok) {
          result.status = 'aborted';
          result.ok = false;
          result.granted = false;
          result.committed = false;
          result.abortReasonCode = 'ATOMIC_BATCH_ROLLED_BACK';
          result.reason = '原子批次中存在被拒绝的订阅，因此该可行订阅也未分配通道';
          result.category = result.category ?? 'channel-capacity';
        }
      }
    }

    const plannedOwners = results
      .filter((result) => result.status === 'granted')
      .flatMap((result) => result.channels.map((channel) => ({
        channel,
        subscriptionId: result.id,
      })))
      .sort((a, b) => compareIds(a.channel, b.channel));

    const allocatedCount = plannedOwners.length;
    const report = {
      ok: !anyRejected && committed,
      atomic,
      committed,
      totalChannelCount: this.config.channelCount,
      allocatedChannelCount: allocatedCount,
      freeChannelCount: this.config.channelCount - allocatedCount,
      results,
      assignments: Object.fromEntries(results.map((result) => [result.id, result])),
      channelOwners: plannedOwners,
      errors: results.filter((result) => result.status === 'rejected'),
      topologyErrors: topology.resolutionErrors,
    };

    if (!committed) {
      report.errors = results.filter((result) => !result.ok);
    }

    return report;
  }

  negotiate(subscription, options = {}) {
    const report = this.negotiateAll([subscription], { atomic: true, ...options });
    return report.results[0];
  }

  _commit(result, topology) {
    this.allocations.set(result.id, { result, sub: result._sub });
    result.channels.forEach((channel) => this.usedChannels.add(channel));

    for (const assignment of result.keyAssignments) {
      const group = assignment.targetGroup;
      if (group === null || group === undefined) continue;
      if (!this.writersByGroup.has(group)) this.writersByGroup.set(group, new Set());
      this.writersByGroup.get(group).add(result.id);
    }

    if (result.requested.orderingScope === ORDERING_SCOPE.TARGET_GROUP) {
      for (const route of result.routes) {
        if (route.affinityType !== ORDERING_SCOPE.TARGET_GROUP) continue;
        const group = route.targetGroup;
        if (group === undefined) continue;
        if (!this.protectedGroups.has(group)) {
          this.protectedGroups.set(group, {
            subscriptionId: result.id,
            channel: route.channel,
          });
        }
      }
    }

    result.committed = true;
  }

  release(subscriptionId) {
    const id = String(subscriptionId);
    const allocation = this.allocations.get(id);
    if (!allocation) return false;

    allocation.result.channels.forEach((channel) => this.usedChannels.delete(channel));

    for (const [group, writers] of this.writersByGroup.entries()) {
      writers.delete(id);
      if (writers.size === 0) this.writersByGroup.delete(group);
    }

    for (const [group, protector] of this.protectedGroups.entries()) {
      if (protector.subscriptionId === id) this.protectedGroups.delete(group);
    }

    this.allocations.delete(id);
    return true;
  }

  snapshot() {
    return {
      totalChannelCount: this.config.channelCount,
      availableChannels: this._availableChannels(),
      channelOwners: [...this.usedChannels].sort(compareIds).map((channel) => {
        for (const [subscriptionId, allocation] of this.allocations.entries()) {
          if (allocation.result.channels.includes(channel)) {
            return { channel, subscriptionId };
          }
        }
        return { channel, subscriptionId: null };
      }),
      subscriptions: [...this.allocations.keys()],
      protectedTargetGroups: [...this.protectedGroups.entries()].map(([targetGroup, value]) => ({
        targetGroup: keyLabel(targetGroup),
        ...value,
      })),
    };
  }
}

export function negotiateSubscriptions(config, subscriptions, options = {}) {
  const negotiator = new SubscriptionNegotiator(config);
  return negotiator.negotiateAll(subscriptions, options);
}

export function negotiate(config, subscriptions, options = {}) {
  if (Array.isArray(subscriptions)) {
    return negotiateSubscriptions(config, subscriptions, options);
  }
  const negotiator = new SubscriptionNegotiator(config);
  return negotiator.negotiate(subscriptions, options);
}

export function negotiateSubscription(config, subscription, options = {}) {
  return negotiate(config, subscription, options);
}

export default {
  SubscriptionNegotiator,
  NegotiationConfigurationError,
  negotiate,
  negotiateSubscription,
  negotiateSubscriptions,
  createNegotiator: (config) => new SubscriptionNegotiator(config),
  normalizeConfig,
  canonicalAcknowledgementGranularity,
  coarserAcknowledgementGranularity,
  ACK_GRANULARITY,
  ORDERING_SCOPE,
  FAILURE_CATEGORY,
};
