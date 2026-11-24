import test from 'node:test';
import assert from 'node:assert/strict';

import {
  FAILURE_CATEGORY,
  negotiate,
  negotiateSubscriptions,
} from '../src/negotiator.mjs';

const mergedGroup = (key) => {
  if (key === 'a' || key === 'b') return 'G-AB';
  if (key === 'c' || key === 'd') return 'G-CD';
  return key;
};

test('a target group merged from two source keys must share one source range and channel', () => {
  const result = negotiate({
    channelCount: 4,
    source: {
      ack: 'message',
      ordering: {
        scope: 'source-range',
        ranges: [{ id: 'R1', keys: ['a', 'b'] }],
      },
    },
    target: { ack: 'message' },
    targetGroup: mergedGroup,
  }, {
    id: 'merged',
    sourceKeys: ['a', 'b'],
    ack: 'message',
    ordering: 'target-group',
    requestedChannels: 2,
  });

  assert.equal(result.ok, true);
  assert.equal(result.keyAssignments.find((x) => x.sourceKey === 'a').channel, 1);
  assert.equal(result.keyAssignments.find((x) => x.sourceKey === 'b').channel, 1);
  assert.equal(result.routes.length, 1);
  assert.equal(result.routes[0].channel, 1);
  assert.deepEqual(result.routes[0].sourceKeys, ['a', 'b']);
  assert.equal(result.guarantees.ordering.sourceCoversTargetGroups, true);
});

test('per-key source order is enough when a target group contains only one source key', () => {
  const result = negotiate({
    channelCount: 2,
    source: {
      ack: 'message',
      orderingScope: 'source-key',
    },
    target: { ack: 'message' },
    targetGroup: (key) => `group-${key}`,
  }, {
    id: 'singleton-group',
    sourceKeys: ['a'],
    ack: 'message',
    ordering: 'target-group',
  });

  assert.equal(result.ok, true);
  assert.equal(result.keyAssignments[0].channel, 1);
});

test('rejects target-group ordering when merged keys have only independent source-key order', () => {
  const result = negotiate({
    channelCount: 2,
    source: {
      ack: 'message',
      orderingScope: 'source-key',
    },
    target: { ack: 'message' },
    targetGroup: mergedGroup,
  }, {
    id: 'not-covered',
    sourceKeys: ['a', 'b'],
    ack: 'message',
    ordering: 'target-group',
  });

  assert.equal(result.ok, false);
  assert.equal(result.category, FAILURE_CATEGORY.ORDERING);
  assert.equal(result.reasonCode, 'TARGET_GROUP_SOURCE_ORDER_COVERAGE_INSUFFICIENT');
  assert.match(result.reason, /未被同一个源端顺序范围/);
  assert.deepEqual(result.achievable.orderingScopes, ['none', 'source-key']);
});

test('detects interleaved source ranges whose keys are crossed by target groups', () => {
  // Source ranges are R1={a,c}, R2={b,d}. Target groups cross them as
  // G-AB={a,b}, G-CD={c,d}, so neither target group is source-covered.
  const crossedTargetGroup = (key) => {
    if (key === 'a' || key === 'b') return 'G-AB';
    if (key === 'c' || key === 'd') return 'G-CD';
    return key;
  };

  const result = negotiate({
    channelCount: 4,
    source: {
      ack: 'message',
      ordering: {
        scope: 'source-range',
        rangeForSourceKey: (key) => ({ a: 'R1', c: 'R1', b: 'R2', d: 'R2' })[key],
      },
    },
    target: { ack: 'message' },
    targetGroup: crossedTargetGroup,
  }, {
    id: 'crossed',
    sourceKeys: ['a', 'b', 'c', 'd'],
    ack: 'message',
    ordering: 'target-group',
    requestedChannels: 2,
  });

  assert.equal(result.ok, false);
  assert.equal(result.reasonCode, 'TARGET_GROUP_SOURCE_ORDER_COVERAGE_INSUFFICIENT');
  const uncovered = result.violations[0].details.uncoveredTargetGroups;
  assert.deepEqual(uncovered.map((x) => x.targetGroup).sort(), ['G-AB', 'G-CD']);
});

test('accepts interleaved ranges when target groups align with source coverage', () => {
  // Same interleaved R1/R2 key layout, but target groups are G1={a,c},
  // G2={b,d}. Each group is fully covered and can be pinned separately.
  const alignedTargetGroup = (key) => {
    if (key === 'a' || key === 'c') return 'G1';
    if (key === 'b' || key === 'd') return 'G2';
    return key;
  };

  const result = negotiate({
    channelCount: 2,
    source: {
      ack: 'message',
      ordering: {
        scope: 'source-range',
        rangeForSourceKey: (key) => ({ a: 'R1', c: 'R1', b: 'R2', d: 'R2' })[key],
      },
    },
    target: { ack: 'message' },
    targetGroup: alignedTargetGroup,
  }, {
    id: 'aligned',
    sourceKeys: ['a', 'b', 'c', 'd'],
    ack: 'message',
    ordering: 'target-group',
    requestedChannels: 2,
  });

  assert.equal(result.ok, true);
  const channelOf = (key) => result.keyAssignments.find((x) => x.sourceKey === key).channel;
  assert.equal(channelOf('a'), channelOf('c'));
  assert.equal(channelOf('b'), channelOf('d'));
  assert.notEqual(channelOf('a'), channelOf('b'));
});

test('pins every key of a global-order subscription to one channel', () => {
  const result = negotiate({
    channelCount: 4,
    source: { ack: 'message', orderingScope: 'global' },
    target: { ack: 'message' },
    targetGroup: mergedGroup,
  }, {
    id: 'global',
    sourceKeys: ['a', 'b', 'c'],
    ack: 'message',
    ordering: 'global',
    requestedChannels: 3,
  });

  assert.equal(result.ok, true);
  assert.equal(result.routes.length, 1);
  assert.equal(new Set(result.keyAssignments.map((x) => x.channel)).size, 1);
});

test('rejects global order when source ranges do not cover all subscription keys', () => {
  const result = negotiate({
    channelCount: 2,
    source: {
      ack: 'message',
      ordering: {
        scope: 'source-range',
        rangeForSourceKey: (key) => (key === 'a' ? 'R1' : 'R2'),
      },
    },
    target: { ack: 'message' },
  }, {
    id: 'not-global',
    sourceKeys: ['a', 'b'],
    ack: 'message',
    ordering: 'global',
  });

  assert.equal(result.ok, false);
  assert.equal(result.category, FAILURE_CATEGORY.ORDERING);
  assert.equal(result.reasonCode, 'GLOBAL_ORDERING_COVERAGE_INSUFFICIENT');
});

test('checks target-group coverage against configured known source keys, not only subscription keys', () => {
  const result = negotiate({
    channelCount: 2,
    knownSourceKeys: ['a', 'b'],
    source: {
      ack: 'message',
      ordering: {
        scope: 'source-range',
        rangeForSourceKey: (key) => (key === 'a' ? 'R1' : 'R2'),
      },
    },
    target: { ack: 'message' },
    targetGroup: mergedGroup,
  }, {
    id: 'partial-subscription',
    // The subscription only consumes a, but configured universe says b also
    // merges into G-AB. Source coverage must include b too.
    sourceKeys: ['a'],
    ack: 'message',
    ordering: 'target-group',
  });

  assert.equal(result.ok, false);
  assert.equal(result.reasonCode, 'TARGET_GROUP_SOURCE_ORDER_COVERAGE_INSUFFICIENT');
  const groups = result.violations[0].details.uncoveredTargetGroups;
  assert.equal(groups[0].targetGroup, 'G-AB');
  assert.deepEqual(groups[0].sourceKeys, ['a', 'b']);
});

test('source-range ordering pins all keys in one source range to one channel', () => {
  const result = negotiate({
    channelCount: 2,
    source: {
      ack: 'message',
      ordering: {
        scope: 'source-range',
        rangeForSourceKey: (key) => ({ a: 'R1', c: 'R1', b: 'R2', d: 'R2' })[key],
      },
    },
    target: { ack: 'message' },
  }, {
    id: 'range-order',
    sourceKeys: ['a', 'b', 'c', 'd'],
    ack: 'message',
    ordering: 'source-range',
    requestedChannels: 2,
  });

  assert.equal(result.ok, true);
  const channelOf = (key) => result.keyAssignments.find((x) => x.sourceKey === key).channel;
  assert.equal(channelOf('a'), channelOf('c'));
  assert.equal(channelOf('b'), channelOf('d'));
  assert.notEqual(channelOf('a'), channelOf('b'));
});

test('detects target groups split across subscriptions in one batch', () => {
  const config = {
    channelCount: 4,
    source: { ack: 'message', orderingScope: 'global' },
    target: { ack: 'message' },
    targetGroup: mergedGroup,
  };

  const report = negotiateSubscriptions(config, [
    { id: 'A', sourceKeys: ['a'], ack: 'message', ordering: 'target-group' },
    { id: 'B', sourceKeys: ['b'], ack: 'message', ordering: 'target-group' },
  ]);

  assert.equal(report.ok, false);
  assert.equal(report.assignments.A.category, FAILURE_CATEGORY.ORDERING);
  assert.equal(report.assignments.A.reasonCode, 'TARGET_GROUP_CROSS_SUBSCRIPTION_BATCH_CONFLICT');
  assert.equal(report.assignments.B.reasonCode, 'TARGET_GROUP_CROSS_SUBSCRIPTION_BATCH_CONFLICT');
  assert.deepEqual(report.channelOwners, []);
});

test('a target-order requester conflicts with another batch writer for the same group', () => {
  const config = {
    channelCount: 4,
    source: { ack: 'message', orderingScope: 'global' },
    target: { ack: 'message' },
    targetGroup: mergedGroup,
  };

  const report = negotiateSubscriptions(config, [
    { id: 'ordered', sourceKeys: ['a'], ack: 'message', ordering: 'target-group' },
    { id: 'plain', sourceKeys: ['b'], ack: 'message', ordering: 'none' },
  ]);

  assert.equal(report.assignments.ordered.ok, false);
  assert.equal(report.assignments.ordered.reasonCode, 'TARGET_GROUP_CROSS_SUBSCRIPTION_BATCH_CONFLICT');
  assert.equal(report.assignments.plain.ok, true);
  assert.notEqual(
    report.assignments.ordered.category === FAILURE_CATEGORY.CHANNEL_CAPACITY,
    true,
  );
});
