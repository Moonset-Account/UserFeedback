import test from 'node:test';
import assert from 'node:assert/strict';

import { negotiate } from '../src/negotiator.mjs';

function routeEvent(result, event) {
  const assignment = result.keyAssignments.find((item) => item.sourceKey === event.key);
  if (!assignment) throw new Error(`no channel assignment for ${event.key}`);
  return assignment.channel;
}

test('synthetic interleaved stream: merged source keys stay on one channel and preserve source order', () => {
  const result = negotiate({
    channelCount: 3,
    source: {
      ack: 'message',
      ordering: {
        scope: 'source-range',
        rangeForSourceKey: () => 'R1',
      },
    },
    target: { ack: 'message' },
    targetGroup: (key) => ((key === 'a' || key === 'b') ? 'business-group-1' : key),
  }, {
    id: 'interleaved-merge',
    sourceKeys: ['a', 'b'],
    ack: 'message',
    ordering: 'target-group',
    requestedChannels: 2,
  });

  assert.equal(result.ok, true);

  // This is the total order supplied by source range R1. The two source keys
  // are deliberately interleaved before being merged into one target group.
  const sourceStream = [
    { sequence: 1, key: 'a', event: 'a-1' },
    { sequence: 2, key: 'b', event: 'b-1' },
    { sequence: 3, key: 'a', event: 'a-2' },
    { sequence: 4, key: 'b', event: 'b-2' },
    { sequence: 5, key: 'a', event: 'a-3' },
  ];

  const channelLogs = new Map(result.channels.map((channel) => [channel, []]));
  for (const event of sourceStream) {
    const channel = routeEvent(result, event);
    channelLogs.get(channel).push(event);
  }

  assert.deepEqual([...channelLogs.keys()], [1, 2]);
  assert.deepEqual(channelLogs.get(1).map((x) => x.event), ['a-1', 'b-1', 'a-2', 'b-2', 'a-3']);
  assert.deepEqual(channelLogs.get(2), []);

  const targetGroupOrder = channelLogs.get(1).map((x) => x.event);
  assert.deepEqual(targetGroupOrder, sourceStream.map((x) => x.event));
});

test('synthetic two-group stream: each target group keeps affinity while channels run in parallel', () => {
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
    targetGroup: (key) => ((key === 'a' || key === 'c')
      ? 'G1'
      : (key === 'b' || key === 'd') ? 'G2' : key),
  }, {
    id: 'parallel-groups',
    sourceKeys: ['a', 'b', 'c', 'd'],
    ack: 'message',
    ordering: 'target-group',
    requestedChannels: 2,
  });

  assert.equal(result.ok, true);

  // Arrival is interleaved across the two independent source ranges.
  const sourceStream = [
    { sequence: 1, key: 'a', event: 'G1-a1' },
    { sequence: 2, key: 'b', event: 'G2-b1' },
    { sequence: 3, key: 'c', event: 'G1-c1' },
    { sequence: 4, key: 'd', event: 'G2-d1' },
    { sequence: 5, key: 'a', event: 'G1-a2' },
    { sequence: 6, key: 'd', event: 'G2-d2' },
  ];

  const channelLogs = new Map(result.channels.map((channel) => [channel, []]));
  for (const event of sourceStream) {
    channelLogs.get(routeEvent(result, event)).push(event);
  }

  const channelOfG1 = result.keyAssignments.find((x) => x.targetGroup === 'G1').channel;
  const channelOfG2 = result.keyAssignments.find((x) => x.targetGroup === 'G2').channel;
  assert.notEqual(channelOfG1, channelOfG2);

  const g1Events = sourceStream.filter((x) => ['a', 'c'].includes(x.key)).map((x) => x.event);
  const g2Events = sourceStream.filter((x) => ['b', 'd'].includes(x.key)).map((x) => x.event);

  assert.deepEqual(channelLogs.get(channelOfG1).map((x) => x.event), g1Events);
  assert.deepEqual(channelLogs.get(channelOfG2).map((x) => x.event), g2Events);

  for (const group of ['G1', 'G2']) {
    const channelsForGroup = new Set(
      result.keyAssignments.filter((x) => x.targetGroup === group).map((x) => x.channel),
    );
    assert.equal(channelsForGroup.size, 1);
  }
});

test('route assignment is deterministic across repeated negotiations', () => {
  const config = {
    channelCount: 3,
    source: { ack: 'message', orderingScope: 'global' },
    target: { ack: 'message' },
    targetGroup: (key) => `G-${key}`,
  };
  const subscription = {
    id: 'determinism',
    sourceKeys: ['z', 'a', 'm'],
    ack: 'message',
    ordering: 'target-group',
    requestedChannels: 3,
  };

  const first = negotiate(config, subscription);
  const second = negotiate(config, subscription);
  assert.deepEqual(first.routes.map((r) => [r.affinity, r.channel]), second.routes.map((r) => [r.affinity, r.channel]));
  assert.deepEqual(first.keyAssignments, second.keyAssignments);
});
