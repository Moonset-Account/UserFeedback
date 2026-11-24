import test from 'node:test';
import assert from 'node:assert/strict';

import {
  FAILURE_CATEGORY,
  NegotiationConfigurationError,
  negotiate,
  negotiateSubscriptions,
  normalizeConfig,
} from '../src/negotiator.mjs';

function baseConfig(overrides = {}) {
  return {
    channelCount: 3,
    source: {
      acknowledgementGranularity: 'message',
      orderingScope: 'global',
    },
    target: {
      acknowledgementGranularity: 'message',
    },
    targetGroupForSourceKey: (key) => key,
    ...overrides,
  };
}

test('normalizes common field aliases and exposes the channel pool', () => {
  const config = normalizeConfig({
    channels: 2,
    sourceAck: 'per-message',
    targetAck: 'message',
    sourceOrderingScope: 'global',
    targetGroup: (key) => `g-${key}`,
  });

  assert.equal(config.channelCount, 2);
  assert.deepEqual(config.channelIds, [1, 2]);
  assert.equal(config.source.acknowledgementGranularity, 'message');
  assert.equal(config.source.orderingScope, 'global');
  assert.equal(config.targetGroupForSourceKey('a'), 'g-a');
});

test('grants a basic subscription with one exclusive channel by default', () => {
  const result = negotiate(baseConfig(), {
    id: 'basic',
    sourceKeys: ['a'],
    acknowledgementGranularity: 'message',
  });

  assert.equal(result.ok, true);
  assert.equal(result.status, 'granted');
  assert.deepEqual(result.channels, [1]);
  assert.equal(result.exclusive, true);
  assert.equal(result.guarantees.channels.exclusive, true);
  assert.equal(result.guarantees.acknowledgement.strongestAvailable, 'message');
});

test('finer acknowledgement granularity can satisfy a coarser request', () => {
  const result = negotiate(baseConfig(), {
    id: 'coarse-ack',
    sourceKeys: ['a'],
    acknowledgementGranularity: 'subscription',
  });

  assert.equal(result.ok, true);
  assert.equal(result.guarantees.acknowledgement.effective, 'subscription');
  assert.equal(result.guarantees.acknowledgement.strongestAvailable, 'message');
});

test('reports source acknowledgement insufficiency as the primary category', () => {
  const config = baseConfig({
    source: {
      acknowledgementGranularity: 'source-key',
      orderingScope: 'global',
    },
  });

  const result = negotiate(config, {
    id: 'source-ack',
    sourceKeys: ['a'],
    acknowledgementGranularity: 'message',
  });

  assert.equal(result.ok, false);
  assert.equal(result.category, FAILURE_CATEGORY.SOURCE_ACKNOWLEDGEMENT);
  assert.equal(result.reasonCode, 'SOURCE_ACK_GRANULARITY_UNSUPPORTED');
  assert.match(result.reason, /源端确认粒度/);
  assert.equal(result.achievable.acknowledgementGranularity, 'source-key');
});

test('reports target acknowledgement insufficiency separately', () => {
  const config = baseConfig({
    target: { acknowledgementGranularity: 'target-group' },
  });

  const result = negotiate(config, {
    id: 'target-ack',
    sourceKeys: ['a'],
    acknowledgementGranularity: 'source-key',
  });

  assert.equal(result.ok, false);
  assert.equal(result.category, FAILURE_CATEGORY.TARGET_ACKNOWLEDGEMENT);
  assert.equal(result.reasonCode, 'TARGET_ACK_GRANULARITY_UNSUPPORTED');
  assert.equal(result.violations[0].details.side, 'target');
});

test('rejects invalid configuration and invalid subscriptions predictably', () => {
  assert.throws(
    () => normalizeConfig({ channelCount: -1, source: { ack: 'message' }, target: { ack: 'message' } }),
    NegotiationConfigurationError,
  );

  assert.throws(
    () => normalizeConfig({
      channelCount: 1,
      source: { ack: 'message', orderingScope: 'source-range' },
      target: { ack: 'message' },
    }),
    /orderRange/,
  );

  const report = negotiateSubscriptions(baseConfig(), [
    { id: 'missing-ack', sourceKeys: ['a'] },
  ]);
  assert.equal(report.ok, false);
  assert.equal(report.results[0].category, FAILURE_CATEGORY.INVALID_SUBSCRIPTION);
  assert.equal(report.results[0].reasonCode, 'ACK_GRANULARITY_REQUIRED');
});
