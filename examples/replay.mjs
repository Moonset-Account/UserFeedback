/**
 * Replay the headline scenario: two upstream root causes fail, then recover in
 * sequence, and the receiver sees an explicit notification order.
 *
 * Run with:  node examples/replay.mjs
 */
import { createNotificationCoordinator } from '../src/coordinator.mjs';

const suppressions = [
  { cause: 'db-root', symptom: 'api' },
  { cause: 'network', symptom: 'api' },
  { cause: 'api', symptom: 'web' },
];

const events = [
  { kind: 'start', faultId: 'INC-1001', nodeId: 'db-root', at: 0 },
  { kind: 'start', faultId: 'INC-1002', nodeId: 'network', at: 0 },
  { kind: 'start', faultId: 'INC-1003', nodeId: 'api', at: 0 },
  { kind: 'start', faultId: 'INC-1003', nodeId: 'api', at: 5_000 }, // re-delivery
  { kind: 'recover', faultId: 'INC-1001', nodeId: 'db-root', at: 30_000 },
  { kind: 'recover', faultId: 'INC-1002', nodeId: 'network', at: 45_000 },
  { kind: 'recover', faultId: 'INC-1003', nodeId: 'api', at: 60_000 },
];

const coordinator = createNotificationCoordinator({
  suppressions,
  aggregationMs: 10_000,
});

const notifications = coordinator.replay(events);

console.log('seq | kind    | fault     | node     | at (s) | trigger | paired');
console.log('----+---------+-----------+----------+--------+---------+-------');
for (const n of notifications) {
  const at = (n.at / 1000).toFixed(1).padStart(6);
  const paired = n.pairedWithSeq ?? '-';
  console.log(
    `${String(n.seq).padStart(3)} | ${n.kind.padEnd(7)} | ${n.faultId.padEnd(9)} | ` +
      `${n.nodeId.padEnd(8)} | ${at} | ${n.trigger.padEnd(19)} | ${paired}`,
  );
}

console.log(
  `\n${notifications.length} notifications from ${events.length} events ` +
    `(${events.length - notifications.length} re-deliveries suppressed).`,
);
