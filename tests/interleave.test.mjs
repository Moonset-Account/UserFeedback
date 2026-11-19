// Boundary / interleaving tests:
//  - two candidate tasks run concurrently and deliberately interleave their
//    external reads. Pairing is per task, so each task still gets its own
//    first/second value regardless of global completion order;
//  - overlay reads sit between replay reads without consuming replay slots or
//    corrupting the occurrence pairing of later reads;
//  - baseline side runs independently and its storage is the single source of
//    truth that candidate overlays can never touch.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ShadowExecutor } from '../src/shadow.mjs';
import { QueueStorage, gate } from './fixtures.mjs';

test('concurrent interleaved tasks keep per-task read pairing and per-task overlays', async () => {
  // R-C is read by BOTH tasks; the FIFO only exists to serve the baseline
  // run. Candidate replay must never draw on global/queue order.
  // Baseline read order of R-C is x#1, x#2, y#1, y#2, so the FIFO (which only
  // serves the baseline run) is queued in that order. Candidate replay must
  // never draw on global/queue order.
  const storage = new QueueStorage({
    'R-X': ['x1', 'x2'], // task-x: two reads of its own resource
    'R-C': ['c-x-1', 'c-x-2', 'c-y-1', 'c-y-2'],
  });

  const baselineX = {
    id: 'task-x',
    async impl(ctx) {
      const x1 = await ctx.read('R-X');
      const c1 = await ctx.read('R-C');
      const x2 = await ctx.read('R-X');
      const c2 = await ctx.read('R-C');
      await ctx.write('W-X', [x1, c1, x2, c2]);
      return { values: [x1, c1, x2, c2] };
    },
  };
  const baselineY = {
    id: 'task-y',
    async impl(ctx) {
      const c1 = await ctx.read('R-C');
      const c2 = await ctx.read('R-C');
      await ctx.write('W-Y', [c1, c2]);
      return { values: [c1, c2] };
    },
  };

  const exec = new ShadowExecutor(storage);
  await exec.runBaseline([baselineX, baselineY]);

  // Gates force the global read sequence:
  //   x:R-X(1) -> y:R-C(1) -> x:R-C(1) -> y:R-C(2) -> x:R-X(2) -> x:R-C(2)
  const g = {
    xFirstReadDone: gate(),
    yFirstReadDone: gate(),
    xSecondReadDone: gate(),
    ySecondReadDone: gate(),
  };

  const candidateX = {
    id: 'task-x',
    async impl(ctx) {
      const x1 = await ctx.read('R-X');
      g.xFirstReadDone.resolve();

      await g.yFirstReadDone.promise; // y's first read happens before x's second
      const c1 = await ctx.read('R-C');
      g.xSecondReadDone.resolve();

      await g.ySecondReadDone.promise; // y fully finishes before x's tail
      const x2 = await ctx.read('R-X');
      const c2 = await ctx.read('R-C');
      await ctx.write('W-X', [x1, c1, x2, c2]);
      return { values: [x1, c1, x2, c2] };
    },
  };
  const candidateY = {
    id: 'task-y',
    async impl(ctx) {
      await g.xFirstReadDone.promise;
      const c1 = await ctx.read('R-C');
      g.yFirstReadDone.resolve();

      await g.xSecondReadDone.promise;
      const c2 = await ctx.read('R-C');
      g.ySecondReadDone.resolve();

      await ctx.write('W-Y', [c1, c2]);
      return { values: [c1, c2] };
    },
  };

  const report = await exec.runCandidate([candidateX, candidateY], { concurrent: true });

  assert.equal(report.summary.allSubstantiated, true);
  assert.equal(report.summary.anyUnpairedRead, false);
  assert.deepEqual(report.candidateTaskOrder, ['task-x', 'task-y']); // launch order
  assert.ok(report.tasks['task-y'].resultsEqual); // y resolves its gates first

  // task-x observations are its own baseline values even though y interleaved.
  assert.deepEqual(
    report.tasks['task-x'].candidate.reads.map((r) => [r.resource, r.occurrence, r.source, r.value]),
    [
      ['R-X', 1, 'replay', 'x1'],
      ['R-C', 1, 'replay', 'c-x-1'],
      ['R-X', 2, 'replay', 'x2'],
      ['R-C', 2, 'replay', 'c-x-2'],
    ],
  );
  // task-y's two R-C reads are y's own values, never x's.
  assert.deepEqual(
    report.tasks['task-y'].candidate.reads.map((r) => [r.resource, r.occurrence, r.value]),
    [
      ['R-C', 1, 'c-y-1'],
      ['R-C', 2, 'c-y-2'],
    ],
  );

  // Task-global call orders are recorded independently per task marker.
  assert.deepEqual(report.tasks['task-x'].candidate.reads.map((r) => r.order), [1, 2, 3, 4]);
  assert.deepEqual(report.tasks['task-y'].candidate.reads.map((r) => r.order), [1, 2]);

  // Overlays are per-task; x never sees y's write and vice versa.
  assert.deepEqual(report.tasks['task-x'].candidate.overlaySnapshot, {
    'W-X': ['x1', 'c-x-1', 'x2', 'c-x-2'],
  });
  assert.deepEqual(report.tasks['task-y'].candidate.overlaySnapshot, {
    'W-Y': ['c-y-1', 'c-y-2'],
  });
  for (const id of ['task-x', 'task-y']) {
    assert.equal(report.tasks[id].resultsEqual, true);
    assert.ok(report.tasks[id].replayChecks.every((c) => c.equal));
  }

  // Baseline storage only ever saw the two baseline writes.
  assert.deepEqual(
    storage.writes.map((w) => w.resource),
    ['W-X', 'W-Y'],
  );
  // The candidate run performed no driver reads at all: all replay came from
  // the recorded baseline trace.
  assert.deepEqual(
    storage.reads.map((r) => r.resource),
    ['R-X', 'R-C', 'R-X', 'R-C', 'R-C', 'R-C'], // baseline only, 6 reads
  );
});

test('overlay read between replay reads does not consume replay occurrence slots', async () => {
  const storage = new QueueStorage({ 'R-K': [100, 200] });
  const baseline = {
    id: 't',
    async impl(ctx) {
      const k1 = await ctx.read('R-K');
      const k2 = await ctx.read('R-K');
      await ctx.write('W', k1 + k2);
      return { k1, k2 };
    },
  };
  const candidate = {
    id: 't',
    async impl(ctx) {
      const k1 = await ctx.read('R-K'); // replay occ 1 -> 100
      await ctx.write('W', k1);
      const back = await ctx.read('W'); // overlay, must not shift R-K pairing
      const k2 = await ctx.read('R-K'); // replay occ 2 -> 200, not "unpaired"
      return { k1, k2, back };
    },
  };

  const exec = new ShadowExecutor(storage);
  await exec.runBaseline([baseline]);
  const report = await exec.runCandidate([candidate]);
  const t = report.tasks['t'];

  assert.equal(t.unsubstantiated, false);
  assert.deepEqual(
    t.candidate.reads.map((r) => [r.resource, r.occurrence, r.source, r.value]),
    [
      ['R-K', 1, 'replay', 100],
      ['W', 1, 'overlay', 100],
      ['R-K', 2, 'replay', 200],
    ],
  );
  assert.deepEqual(t.candidate.result, { k1: 100, k2: 200, back: 100 });
  assert.deepEqual(t.replayChecks, [
    { resource: 'R-K', occurrence: 1, equal: true },
    { resource: 'R-K', occurrence: 2, equal: true },
  ]);
});

test('candidate-only tasks are impossible: missing baseline trace is rejected even concurrently', async () => {
  const storage = new QueueStorage({ 'R-K': [1] });
  const exec = new ShadowExecutor(storage);
  await exec.runBaseline([{ id: 'known', async impl(ctx) { await ctx.read('R-K'); } }]);

  await assert.rejects(
    exec.runCandidate(
      [
        { id: 'known', async impl() {} },
        { id: 'stranger', async impl() {} },
      ],
      { concurrent: true },
    ),
    /stranger/,
  );
  // Rejected during registration: no candidate task slot was opened.
  assert.equal(exec.report().summary.candidateTasks, 0);
});
