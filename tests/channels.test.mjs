import test from 'node:test';
import assert from 'node:assert/strict';

import {
  FAILURE_CATEGORY,
  SubscriptionNegotiator,
  negotiateSubscriptions,
} from '../src/negotiator.mjs';

const config = {
  channelCount: 3,
  source: { ack: 'message', orderingScope: 'global' },
  target: { ack: 'message' },
  targetGroup: (key) => key,
};

test('allocates disjoint exclusive channels and reports ownership', () => {
  const report = negotiateSubscriptions(config, [
    { id: 'a', sourceKeys: ['a'], ack: 'message', requestedChannels: 1 },
    { id: 'b', sourceKeys: ['b'], ack: 'message', requestedChannels: 2 },
  ]);

  assert.equal(report.ok, true);
  assert.deepEqual(report.assignments.a.channels, [1]);
  assert.deepEqual(report.assignments.b.channels, [2, 3]);
  assert.deepEqual(report.channelOwners, [
    { channel: 1, subscriptionId: 'a' },
    { channel: 2, subscriptionId: 'b' },
    { channel: 3, subscriptionId: 'b' },
  ]);
  assert.equal(report.freeChannelCount, 0);
});

test('classifies capacity shortage with requested and available counts', () => {
  const report = negotiateSubscriptions({ ...config, channelCount: 2 }, [
    { id: 'big', sourceKeys: ['a'], ack: 'message', requestedChannels: 2 },
    { id: 'late', sourceKeys: ['b'], ack: 'message', requestedChannels: 1 },
  ]);

  assert.equal(report.ok, false);
  assert.equal(report.assignments.big.ok, true);
  assert.equal(report.assignments.late.ok, false);
  assert.equal(report.assignments.late.category, FAILURE_CATEGORY.CHANNEL_CAPACITY);
  assert.equal(report.assignments.late.reasonCode, 'CHANNEL_CAPACITY_EXCEEDED');
  assert.deepEqual(report.assignments.late.violations[0].details, {
    requestedChannels: 1,
    minChannels: 1,
    maxChannels: 1,
    availableChannelCount: 0,
    totalChannels: 2,
  });
  assert.equal(report.assignments.late.achievable.availableChannelCount, 0);
});

test('rejects a request larger than the whole pool even when pool is idle', () => {
  const result = new SubscriptionNegotiator({ ...config, channelCount: 2 }).negotiate({
    id: 'too-big',
    sourceKeys: ['a'],
    ack: 'message',
    requestedChannels: 3,
  });

  assert.equal(result.ok, false);
  assert.equal(result.category, FAILURE_CATEGORY.CHANNEL_CAPACITY);
  assert.equal(result.reasonCode, 'CHANNEL_REQUEST_EXCEEDS_POOL');
});

test('treats a zero-channel pool as a capacity boundary', () => {
  const negotiator = new SubscriptionNegotiator({ ...config, channelCount: 0 });
  const result = negotiator.negotiate({
    id: 'no-channel',
    sourceKeys: ['a'],
    ack: 'message',
  });

  assert.equal(result.ok, false);
  assert.equal(result.category, FAILURE_CATEGORY.CHANNEL_CAPACITY);
  assert.equal(result.reasonCode, 'CHANNEL_REQUEST_EXCEEDS_POOL');
  assert.equal(result.achievable.totalChannelCount, 0);
  assert.deepEqual(negotiator.snapshot().availableChannels, []);
});

test('an empty key set still receives the declared minimum exclusive channel', () => {
  const result = negotiateSubscriptions(config, [{
    id: 'idle-control-subscription',
    sourceKeys: [],
    ack: 'message',
  }]).results[0];

  assert.equal(result.ok, true);
  assert.deepEqual(result.channels, [1]);
  assert.deepEqual(result.routes, []);
});

test('supports elastic min/max channel requirements', () => {
  const negotiator = new SubscriptionNegotiator(config);
  const first = negotiator.negotiate({
    id: 'first',
    sourceKeys: ['a'],
    ack: 'message',
    requestedChannels: 2,
  });
  assert.deepEqual(first.channels, [1, 2]);

  const elastic = negotiator.negotiate({
    id: 'elastic',
    sourceKeys: ['b'],
    ack: 'message',
    requestedChannels: { min: 1, max: 3 },
  });
  assert.equal(elastic.ok, true);
  assert.deepEqual(elastic.channels, [3]);

  const impossible = negotiator.negotiate({
    id: 'impossible',
    sourceKeys: ['c'],
    ack: 'message',
    requestedChannels: { min: 2, max: 3 },
  });
  assert.equal(impossible.ok, false);
  assert.equal(impossible.category, FAILURE_CATEGORY.CHANNEL_CAPACITY);
  assert.equal(impossible.achievable.availableChannelCount, 0);
});

test('release makes channels available for later interleaved negotiations', () => {
  const negotiator = new SubscriptionNegotiator({ ...config, channelCount: 2 });

  const a = negotiator.negotiate({ id: 'A', sourceKeys: ['a'], ack: 'message' });
  const b = negotiator.negotiate({ id: 'B', sourceKeys: ['b'], ack: 'message' });
  assert.deepEqual(a.channels, [1]);
  assert.deepEqual(b.channels, [2]);

  const blocked = negotiator.negotiate({ id: 'C', sourceKeys: ['c'], ack: 'message' });
  assert.equal(blocked.ok, false);
  assert.equal(blocked.reasonCode, 'CHANNEL_CAPACITY_EXCEEDED');

  assert.equal(negotiator.release('A'), true);
  const c = negotiator.negotiate({ id: 'C', sourceKeys: ['c'], ack: 'message' });
  assert.equal(c.ok, true);
  assert.deepEqual(c.channels, [1]);

  const snapshot = negotiator.snapshot();
  assert.deepEqual(snapshot.availableChannels, []);
  assert.deepEqual(snapshot.channelOwners.map((x) => x.subscriptionId), ['C', 'B']);
  assert.equal(negotiator.release('missing'), false);
});

test('an existing protected target group blocks a later overlapping writer until release', () => {
  const targetGroup = (key) => ((key === 'a' || key === 'b') ? 'G' : key);
  const negotiator = new SubscriptionNegotiator({ ...config, targetGroup });

  const ordered = negotiator.negotiate({
    id: 'ordered',
    sourceKeys: ['a'],
    ack: 'message',
    ordering: 'target-group',
  });
  assert.equal(ordered.ok, true);
  assert.equal(ordered.keyAssignments[0].channel, 1);

  const interleavingWriter = negotiator.negotiate({
    id: 'plain-writer',
    sourceKeys: ['b'],
    ack: 'message',
    ordering: 'none',
  });
  assert.equal(interleavingWriter.ok, false);
  assert.equal(interleavingWriter.category, FAILURE_CATEGORY.ORDERING);
  assert.equal(
    interleavingWriter.reasonCode,
    'TARGET_GROUP_EXISTING_CHANNEL_CONFLICT',
  );
  assert.deepEqual(negotiator.snapshot().subscriptions, ['ordered']);
  assert.deepEqual(negotiator.snapshot().protectedTargetGroups, [
    { targetGroup: 'G', subscriptionId: 'ordered', channel: 1 },
  ]);

  assert.equal(negotiator.release('ordered'), true);
  const retry = negotiator.negotiate({
    id: 'plain-writer',
    sourceKeys: ['b'],
    ack: 'message',
  });
  assert.equal(retry.ok, true);
  assert.deepEqual(retry.channels, [1]);
});

test('atomic batch rolls back feasible earlier allocations when a later request fails', () => {
  const negotiator = new SubscriptionNegotiator({ ...config, channelCount: 2 });
  const report = negotiator.negotiateAll([
    { id: 'good-big', sourceKeys: ['a'], ack: 'message', requestedChannels: 2 },
    { id: 'bad', sourceKeys: ['b'], ack: 'message', requestedChannels: 1 },
  ], { atomic: true });

  assert.equal(report.ok, false);
  assert.equal(report.committed, false);
  assert.equal(report.assignments['good-big'].status, 'aborted');
  assert.equal(report.assignments.bad.status, 'rejected');
  assert.deepEqual(negotiator.snapshot().subscriptions, []);
  assert.deepEqual(negotiator.snapshot().availableChannels, [1, 2]);
});

test('duplicate active subscription ids are rejected without replacing ownership', () => {
  const negotiator = new SubscriptionNegotiator(config);
  const first = negotiator.negotiate({ id: 'same', sourceKeys: ['a'], ack: 'message' });
  const duplicate = negotiator.negotiate({ id: 'same', sourceKeys: ['b'], ack: 'message' });

  assert.equal(first.ok, true);
  assert.equal(duplicate.ok, false);
  assert.equal(duplicate.reasonCode, 'DUPLICATE_ACTIVE_SUBSCRIPTION_ID');
  assert.deepEqual(negotiator.snapshot().subscriptions, ['same']);
});
