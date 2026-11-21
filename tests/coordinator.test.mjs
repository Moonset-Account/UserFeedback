import test from 'node:test';
import assert from 'node:assert/strict';

import {
  createNotificationCoordinator,
  NotificationCoordinator,
  START,
  RECOVER,
} from '../src/coordinator.mjs';

/**
 * db-root ─┐
 *          ├─> api ──> web
 * network ─┘
 */
const GRAPH = [
  { cause: 'db-root', symptom: 'api' },
  { cause: 'network', symptom: 'api' },
  { cause: 'api', symptom: 'web' },
];

const A = 100; // aggregation window used by most tests

function start(faultId, nodeId, at) {
  return { kind: START, faultId, nodeId, at };
}

function recover(faultId, nodeId, at) {
  return { kind: RECOVER, faultId, nodeId, at };
}

function makeCoordinator(overrides = {}) {
  return createNotificationCoordinator({
    suppressions: GRAPH,
    aggregationMs: A,
    ...overrides,
  });
}

function compact(notifications) {
  return notifications.map(({ seq, kind, faultId, nodeId, at, trigger, pairedWithSeq, heldWhileSuppressed }) => ({
    seq,
    kind,
    faultId,
    nodeId,
    at,
    ...(trigger === undefined ? {} : { trigger }),
    ...(pairedWithSeq === undefined ? {} : { pairedWithSeq }),
    ...(heldWhileSuppressed === true ? { heldWhileSuppressed } : {}),
  }));
}

test('root-cause start notifies immediately and recovery is paired', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-db', 'db-root', 1_000),
    recover('f-db', 'db-root', 4_000),
  ]);

  assert.deepEqual(compact(out), [
    { seq: 1, kind: 'start', faultId: 'f-db', nodeId: 'db-root', at: 1_000, trigger: 'root-cause' },
    { seq: 2, kind: 'recover', faultId: 'f-db', nodeId: 'db-root', at: 4_000, trigger: 'recovery', pairedWithSeq: 1 },
  ]);
  assert.equal(out[0].seq, out[1].pairedWithSeq);
});

test('an uncovered symptom waits for the aggregation window before notifying', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-api', 'api', 0),
    recover('f-api', 'api', 500),
  ]);

  assert.deepEqual(compact(out), [
    { seq: 1, kind: 'start', faultId: 'f-api', nodeId: 'api', at: 100, trigger: 'aggregation-elapsed' },
    { seq: 2, kind: 'recover', faultId: 'f-api', nodeId: 'api', at: 500, trigger: 'recovery', pairedWithSeq: 1 },
  ]);
});

test('a symptom that recovers inside the aggregation window stays silent forever', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-api', 'api', 0),
    recover('f-api', 'api', 40),
    start('f-db', 'db-root', 10),
    recover('f-db', 'db-root', 5_000),
    start('f-api-again', 'api', 6_000), // different fault, also silent
    recover('f-api-again', 'api', 6_010),
  ]);

  // Only the root-cause incident produced notifications.
  assert.deepEqual(
    out.map((n) => `${n.kind}:${n.faultId}`),
    ['start:f-db', 'recover:f-db'],
  );
  assert.equal(out.length, 2);
});

test('an active ancestor holds the symptom past the deadline, and the last recovery releases it', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-db', 'db-root', 0),
    start('f-api', 'api', 100),
    start('f-net', 'network', 200),
    recover('f-db', 'db-root', 1_500),
    recover('f-net', 'network', 2_000),
    recover('f-api', 'api', 3_000),
  ]);

  assert.deepEqual(compact(out), [
    { seq: 1, kind: 'start', faultId: 'f-db', nodeId: 'db-root', at: 0, trigger: 'root-cause' },
    { seq: 2, kind: 'start', faultId: 'f-net', nodeId: 'network', at: 200, trigger: 'root-cause' },
    { seq: 3, kind: 'recover', faultId: 'f-db', nodeId: 'db-root', at: 1_500, trigger: 'recovery', pairedWithSeq: 1 },
    { seq: 4, kind: 'recover', faultId: 'f-net', nodeId: 'network', at: 2_000, trigger: 'recovery', pairedWithSeq: 2 },
    { seq: 5, kind: 'start', faultId: 'f-api', nodeId: 'api', at: 2_000, trigger: 'suppression-cleared' },
    { seq: 6, kind: 'recover', faultId: 'f-api', nodeId: 'api', at: 3_000, trigger: 'recovery', pairedWithSeq: 5 },
  ]);
});

test('one releasing ancestor is not enough while another one stays active', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-db', 'db-root', 0),
    start('f-net', 'network', 0),
    start('f-api', 'api', 0),
    recover('f-db', 'db-root', 300),
  ]);

  assert.deepEqual(
    out.map((n) => `${n.kind}:${n.faultId}`),
    ['start:f-db', 'start:f-net', 'recover:f-db'],
  );
  assert.equal(out.some((n) => n.faultId === 'f-api'), false);
});

test('each fault keeps one stable number across re-delivery, coverage, and release', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-api', 'api', 0), // notifies at 100
    start('f-db', 'db-root', 200), // covers the already-notified api
    start('f-db', 'db-root', 200), // duplicate start
    recover('f-api', 'api', 250), // covered: recovery held
    recover('f-api', 'api', 260), // duplicate recovery
    start('f-api', 'api', 270), // duplicate start after the fact
    recover('f-db', 'db-root', 300),
  ]);

  const api = out.filter((n) => n.faultId === 'f-api');
  assert.equal(api.length, 2, 'exactly one start and one recover');
  assert.equal(api[0].kind, 'start');
  assert.equal(api[1].kind, 'recover');
  assert.equal(api[1].pairedWithSeq, api[0].seq);
  assert.equal(api[0].faultSeq, api[1].faultSeq, 'fault number is stable');

  const db = out.filter((n) => n.faultId === 'f-db');
  assert.equal(db.length, 2);
  assert.equal(db[0].faultSeq, db[1].faultSeq);
  assert.notEqual(api[0].faultSeq, db[0].faultSeq, 'distinct faults get distinct numbers');
});

test('a symptom whose window has not elapsed keeps aggregating after the ancestor clears', () => {
  // api started at 50, deadline 150.  db-root recovers at 80.  api must not
  // fire early at 80; it fires when its own window elapses.
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-db', 'db-root', 0),
    start('f-api', 'api', 50),
    recover('f-db', 'db-root', 80),
  ]);

  assert.deepEqual(compact(out), [
    { seq: 1, kind: 'start', faultId: 'f-db', nodeId: 'db-root', at: 0, trigger: 'root-cause' },
    { seq: 2, kind: 'recover', faultId: 'f-db', nodeId: 'db-root', at: 80, trigger: 'recovery', pairedWithSeq: 1 },
    { seq: 3, kind: 'start', faultId: 'f-api', nodeId: 'api', at: 150, trigger: 'aggregation-elapsed' },
  ]);
});

test('a symptom that stays active through coverage is not re-notified on re-exposure', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-api', 'api', 0), // start emitted at 100
    start('f-db', 'db-root', 200), // covers an already-notified, still-active api
    recover('f-db', 'db-root', 400), // re-exposes api
    recover('f-api', 'api', 500),
  ]);

  assert.deepEqual(
    out.map((n) => `${n.kind}:${n.faultId}`),
    ['start:f-api', 'start:f-db', 'recover:f-db', 'recover:f-api'],
  );
  assert.equal(out.filter((n) => n.kind === 'start' && n.faultId === 'f-api').length, 1);
});

test('same instant: ancestor recovery is applied before the deadline sweep', () => {
  // api is due at 100 and db-root recovers at exactly 100, so api is released
  // by suppression-cleared in that instant.  web, still covered by api, does
  // not leak and fires only when api recovers.
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-db', 'db-root', 0),
    start('f-api', 'api', 0),
    start('f-web', 'web', 0),
    recover('f-db', 'db-root', 100),
    recover('f-api', 'api', 400),
  ]);

  assert.deepEqual(compact(out), [
    { seq: 1, kind: 'start', faultId: 'f-db', nodeId: 'db-root', at: 0, trigger: 'root-cause' },
    { seq: 2, kind: 'recover', faultId: 'f-db', nodeId: 'db-root', at: 100, trigger: 'recovery', pairedWithSeq: 1 },
    { seq: 3, kind: 'start', faultId: 'f-api', nodeId: 'api', at: 100, trigger: 'suppression-cleared' },
    { seq: 4, kind: 'recover', faultId: 'f-api', nodeId: 'api', at: 400, trigger: 'recovery', pairedWithSeq: 3 },
    { seq: 5, kind: 'start', faultId: 'f-web', nodeId: 'web', at: 400, trigger: 'suppression-cleared' },
  ]);
});

test('two ancestors recovering at the same instant release descendants upstream-first', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-db', 'db-root', 0),
    start('f-net', 'network', 0),
    start('f-api', 'api', 0),
    recover('f-net', 'network', 300),
    recover('f-db', 'db-root', 300),
  ]);

  // Both recoveries at 300 are processed in input order; the second one is the
  // last active ancestor and releases api exactly once.
  assert.deepEqual(
    out.map((n) => `${n.seq}|${n.kind}|${n.nodeId}|${n.at}|${n.trigger}`),
    [
      '1|start|db-root|0|root-cause',
      '2|start|network|0|root-cause',
      '3|recover|network|300|recovery',
      '4|recover|db-root|300|recovery',
      '5|start|api|300|suppression-cleared',
    ],
  );
});

test('suppression is transitive: the grandparent alone keeps a grandchild silent', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-db', 'db-root', 0),
    start('f-api', 'api', 0), // stays covered by db-root, never notified
    start('f-web', 'web', 0), // covered by db-root and api
    recover('f-api', 'api', 500), // silent recovery, still covered by db-root
    start('f-web-2', 'web', 600),
    recover('f-web-2', 'web', 620),
    recover('f-db', 'db-root', 1_000),
  ]);

  // f-web was released at 1_000 when db-root recovered; f-api and f-web-2
  // recovered before ever notifying, so they stay silent.
  assert.deepEqual(compact(out), [
    { seq: 1, kind: 'start', faultId: 'f-db', nodeId: 'db-root', at: 0, trigger: 'root-cause' },
    { seq: 2, kind: 'recover', faultId: 'f-db', nodeId: 'db-root', at: 1_000, trigger: 'recovery', pairedWithSeq: 1 },
    { seq: 3, kind: 'start', faultId: 'f-web', nodeId: 'web', at: 1_000, trigger: 'suppression-cleared' },
  ]);
  assert.equal(out.filter((n) => n.faultId === 'f-api').length, 0);
});

test('a notified symptom covered after the fact holds its only recovery until the last ancestor clears', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-api', 'api', 0),
    start('f-db', 'db-root', 200),
    recover('f-api', 'api', 300), // covered -> held
    start('f-api', 'api', 350), // re-sent start: no-op
    recover('f-api', 'api', 360), // re-sent recovery: no-op
    recover('f-db', 'db-root', 400),
  ]);

  assert.deepEqual(compact(out), [
    { seq: 1, kind: 'start', faultId: 'f-api', nodeId: 'api', at: 100, trigger: 'aggregation-elapsed' },
    { seq: 2, kind: 'start', faultId: 'f-db', nodeId: 'db-root', at: 200, trigger: 'root-cause' },
    { seq: 3, kind: 'recover', faultId: 'f-db', nodeId: 'db-root', at: 400, trigger: 'recovery', pairedWithSeq: 2 },
    {
      seq: 4,
      kind: 'recover',
      faultId: 'f-api',
      nodeId: 'api',
      at: 400,
      trigger: 'recovery',
      pairedWithSeq: 1,
      heldWhileSuppressed: true,
    },
  ]);

  const apiStarts = out.filter((n) => n.faultId === 'f-api' && n.kind === 'start');
  const apiRecoveries = out.filter((n) => n.faultId === 'f-api' && n.kind === 'recover');
  assert.equal(apiStarts.length, 1);
  assert.equal(apiRecoveries.length, 1);
  assert.equal(apiRecoveries[0].pairedWithSeq, apiStarts[0].seq);
});

test('re-sent start does not extend the aggregation deadline', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-api', 'api', 0),
    start('f-api', 'api', 60), // re-send inside the window
    start('f-api', 'api', 90),
  ]);

  assert.equal(out.length, 1);
  assert.equal(out[0].at, 100);
  assert.equal(out[0].trigger, 'aggregation-elapsed');
});

test('duplicate events never add notifications, including after recovery', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-api', 'api', 0),
    start('f-api', 'api', 0),
    recover('f-api', 'api', 200),
    recover('f-api', 'api', 210),
    start('f-api', 'api', 220), // re-delivery of an already closed fault
    recover('f-api', 'api', 230),
  ]);

  assert.deepEqual(
    out.map((n) => `${n.kind}:${n.at}`),
    ['start:100', 'recover:200'],
  );
});

test('events at the deadline are handled before expiry is evaluated', () => {
  // A new suppressing ancestor arrives exactly when the symptom is due.  If
  // expiry ran first the symptom would have leaked a notification.
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-api', 'api', 0),
    start('f-db', 'db-root', 100),
    recover('f-db', 'db-root', 250),
  ]);

  assert.deepEqual(compact(out), [
    { seq: 1, kind: 'start', faultId: 'f-db', nodeId: 'db-root', at: 100, trigger: 'root-cause' },
    { seq: 2, kind: 'recover', faultId: 'f-db', nodeId: 'db-root', at: 250, trigger: 'recovery', pairedWithSeq: 1 },
    { seq: 3, kind: 'start', faultId: 'f-api', nodeId: 'api', at: 250, trigger: 'suppression-cleared' },
  ]);
});

test('a symptom recovering exactly at its deadline is silent, because events run first', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([start('f-api', 'api', 0), recover('f-api', 'api', 100)]);

  assert.deepEqual(out, []);
});

test('an ancestor recovering exactly at the deadline releases the symptom in the same instant', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-db', 'db-root', 0),
    start('f-api', 'api', 0),
    recover('f-db', 'db-root', 100),
  ]);

  assert.deepEqual(compact(out), [
    { seq: 1, kind: 'start', faultId: 'f-db', nodeId: 'db-root', at: 0, trigger: 'root-cause' },
    { seq: 2, kind: 'recover', faultId: 'f-db', nodeId: 'db-root', at: 100, trigger: 'recovery', pairedWithSeq: 1 },
    { seq: 3, kind: 'start', faultId: 'f-api', nodeId: 'api', at: 100, trigger: 'suppression-cleared' },
  ]);
});

test('replaying two upstream recoveries yields an explicit, stable notification order', () => {
  const events = [
    start('f-db', 'db-root', 0),
    start('f-net', 'network', 0),
    start('f-api', 'api', 0),
    recover('f-db', 'db-root', 1_000),
    recover('f-net', 'network', 2_000),
    recover('f-api', 'api', 3_000),
  ];

  const out = makeCoordinator().replay(events);
  const again = makeCoordinator().replay(events);

  // The receiver sees: both root causes start, then db-root recovers, then
  // network recovers and immediately releases api, then api recovers.
  assert.deepEqual(compact(out).map((n) => `${n.seq}|${n.kind}|${n.nodeId}|${n.at}`), [
    '1|start|db-root|0',
    '2|start|network|0',
    '3|recover|db-root|1000',
    '4|recover|network|2000',
    '5|start|api|2000',
    '6|recover|api|3000',
  ]);
  assert.deepEqual(compact(again), compact(out));
  assert.deepEqual(
    out.map((n) => n.seq),
    [1, 2, 3, 4, 5, 6],
  );
});

test('same-timestamp events keep their input order; other timestamps sort', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-net', 'network', 0),
    start('f-db', 'db-root', 0),
    start('f-api', 'api', 0),
  ]);

  assert.deepEqual(
    out.map((n) => `${n.seq}|${n.nodeId}|${n.at}`),
    ['1|network|0', '2|db-root|0'],
  );
});

test('the onNotification sink observes exactly the returned order', () => {
  const seen = [];
  const coordinator = makeCoordinator({ onNotification: (n) => seen.push(n.seq) });
  const out = coordinator.replay([
    start('f-db', 'db-root', 0),
    start('f-api', 'api', 0),
    recover('f-db', 'db-root', 500),
    recover('f-api', 'api', 600),
  ]);

  assert.deepEqual(seen, out.map((n) => n.seq));
  assert.deepEqual(coordinator.notifications, out);
});

test('per-node fault counts: releasing one of two faults on the same node is not enough', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([
    start('f-db-primary', 'db-root', 0),
    start('f-db-secondary', 'db-root', 0),
    start('f-api', 'api', 0),
    recover('f-db-primary', 'db-root', 500),
  ]);

  assert.equal(out.some((n) => n.nodeId === 'api'), false);
  assert.equal(out.filter((n) => n.kind === 'recover').length, 1);
});

test('rejects a cyclic suppression graph', () => {
  assert.throws(
    () =>
      createNotificationCoordinator({
        aggregationMs: 10,
        suppressions: [
          { cause: 'a', symptom: 'b' },
          { cause: 'b', symptom: 'c' },
          { cause: 'c', symptom: 'a' },
        ],
      }),
    /cycle/i,
  );

  assert.throws(
    () =>
      createNotificationCoordinator({
        aggregationMs: 10,
        suppressions: [{ cause: 'a', symptom: 'a' }],
      }),
    /cycle/i,
  );
});

test('validates configuration and event input', () => {
  assert.throws(() => createNotificationCoordinator({}), /aggregationMs/);
  assert.throws(
    () => createNotificationCoordinator({ aggregationMs: -1 }),
    /aggregationMs/,
  );
  assert.throws(
    () => createNotificationCoordinator({ aggregationMs: 1, suppressions: 'nope' }),
    /suppressions/,
  );
  assert.throws(
    () =>
      createNotificationCoordinator({
        aggregationMs: 1,
        suppressions: [{ cause: '', symptom: 'x' }],
      }),
    /cause and symptom/,
  );

  const coordinator = makeCoordinator();
  assert.throws(() => coordinator.replay('nope'), /events/);
  assert.throws(() => coordinator.replay([{ kind: 'bogus', faultId: 'a', nodeId: 'b', at: 0 }]), /kind/);
  assert.throws(() => coordinator.replay([{ kind: START, faultId: '', nodeId: 'b', at: 0 }]), /faultId/);
  assert.throws(() => coordinator.replay([{ kind: START, faultId: 'a', nodeId: 'b', at: Number.NaN }]), /at/);
  assert.throws(
    () =>
      coordinator.replay([
        start('f-x', 'db-root', 0),
        recover('f-x', 'network', 1),
      ]),
    /belongs to node/,
  );
  assert.throws(
    () =>
      coordinator.replay([
        start('f-y', 'db-root', 0),
        start('f-y', 'network', 1),
      ]),
    /already bound/,
  );
});

test('a recovery for an unknown fault is ignored rather than invented', () => {
  const coordinator = makeCoordinator();
  const out = coordinator.replay([recover('ghost', 'api', 0), recover('ghost', 'api', 10)]);
  assert.deepEqual(out, []);
});

test('NotificationCoordinator is exported and usable directly', () => {
  const coordinator = new NotificationCoordinator({ suppressions: [], aggregationMs: 0 });
  const out = coordinator.replay([start('f-a', 'leaf', 5), recover('f-a', 'leaf', 9)]);
  assert.deepEqual(compact(out), [
    { seq: 1, kind: 'start', faultId: 'f-a', nodeId: 'leaf', at: 5, trigger: 'root-cause' },
    { seq: 2, kind: 'recover', faultId: 'f-a', nodeId: 'leaf', at: 9, trigger: 'recovery', pairedWithSeq: 1 },
  ]);
});

test('with a zero window, a symptom that starts and recovers together is silent', () => {
  const coordinator = makeCoordinator({ aggregationMs: 0 });
  const out = coordinator.replay([start('f-api', 'api', 50), recover('f-api', 'api', 50)]);
  assert.deepEqual(out, []);
});
