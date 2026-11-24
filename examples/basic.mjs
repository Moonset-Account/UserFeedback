import { negotiate } from '../src/negotiator.mjs';

const config = {
  channelCount: 3,
  source: {
    acknowledgementGranularity: 'message',
    ordering: {
      scope: 'source-range',
      rangeForSourceKey: (key) => ({ a: 'R1', b: 'R1', c: 'R2' })[key],
    },
  },
  target: {
    acknowledgementGranularity: 'message',
  },
  targetGroupForSourceKey: (key) => ((key === 'a' || key === 'b') ? 'business-group-1' : `group-${key}`),
};

const result = negotiate(config, {
  id: 'example-subscription',
  sourceKeys: ['a', 'b', 'c'],
  acknowledgementGranularity: 'message',
  orderingScope: 'target-group',
  requestedChannels: 2,
});

console.log(JSON.stringify({
  ok: result.ok,
  channels: result.channels,
  guarantees: result.guarantees,
  keyAssignments: result.keyAssignments,
}, null, 2));
