// Acceptable-path tests:
//  - candidate reads the same business return values as the baseline;
//  - tasks may finish in swapped order while each task keeps its own values;
//  - the second read of a resource inside one task returns that task's second
//    baseline value;
//  - candidate writes stay in the overlay and later reads see the update.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ShadowExecutor } from '../src/shadow.mjs';
import { MapStorage } from '../src/storage.mjs';
import {
  QueueStorage,
  baselineTaskA,
  baselineTaskB,
  baselineTaskC,
  candidateTaskA,
  candidateTaskB,
  candidateTaskC,
} from './fixtures.mjs';

function buildQueues() {
  return new QueueStorage({
    'R-A': [10],
    'R-B': [{ value: 5 }],
    // task-b reads R-C twice and must get its own 1st then 2nd value
    'R-C': [2, 7],
  });
}

const baselineTasks = [baselineTaskA, baselineTaskB];
const candidateTasks = [candidateTaskA, candidateTaskB];

test('baseline records resources, per-task call order and return values', async () => {
  const storage = buildQueues();
  const exec = new ShadowExecutor(storage);
  const report = await exec.runBaseline(baselineTasks);

  assert.deepEqual(report.baselineTaskOrder, ['task-a', 'task-b']);

  const traceA = report.tasks['task-a'].baseline;
  assert.deepEqual(
    traceA.reads.map((r) => [r.resource, r.order, r.occurrence, r.value]),
    [
      ['R-A', 1, 1, 10],
      ['R-B', 2, 1, { value: 5 }],
    ],
  );
  assert.deepEqual(traceA.writes, [{ resource: 'W-A', order: 1, value: 15 }]);

  const traceB = report.tasks['task-b'].baseline;
  assert.deepEqual(
    traceB.reads.map((r) => [r.resource, r.order, r.occurrence, r.value]),
    [
      ['R-C', 1, 1, 2],
      ['R-C', 2, 2, 7],
    ],
  );
  assert.deepEqual(traceB.result, { task: 'b', delta: 5 });

  // Baseline writes really hit the storage driver.
  assert.deepEqual(
    storage.writes.map((w) => [w.resource, w.value]),
    [
      ['W-A', 15],
      ['W-B', 5],
    ],
  );
});

test('candidate reproduces baseline business values and writes only to its overlay', async () => {
  const storage = buildQueues();
  const exec = new ShadowExecutor(storage);
  await exec.runBaseline(baselineTasks);
  const driverWritesBeforeCandidate = storage.writes.length;

  const report = await exec.runCandidate(candidateTasks);

  assert.equal(report.summary.allSubstantiated, true);
  assert.equal(report.summary.anyUnpairedRead, false);

  for (const id of ['task-a', 'task-b']) {
    const t = report.tasks[id];
    assert.equal(t.unsubstantiated, false);
    assert.equal(t.resultsEqual, true);
    assert.ok(t.replayChecks.every((c) => c.equal));
    assert.deepEqual(t.missingBasis, []);
  }

  // task-b's second read of R-C must be its own second value (7), not 2.
  const readsB = report.tasks['task-b'].candidate.reads;
  assert.deepEqual(
    readsB.map((r) => [r.resource, r.occurrence, r.source, r.value]),
    [
      ['R-C', 1, 'replay', 2],
      ['R-C', 2, 'replay', 7],
    ],
  );

  // All candidate writes are overlay-layer and the storage driver is untouched.
  for (const id of ['task-a', 'task-b']) {
    assert.ok(report.tasks[id].candidate.writes.every((w) => w.layer === 'overlay'));
  }
  assert.equal(storage.writes.length, driverWritesBeforeCandidate);

  // Overlay content is captured per task and never merged into storage.
  assert.deepEqual(report.tasks['task-a'].candidate.overlaySnapshot, { 'W-A': 15 });
  assert.deepEqual(storage.writes.map((w) => w.resource), ['W-A', 'W-B']);
});

test('tasks may finish in swapped order without changing any task’s observations', async () => {
  const storage = buildQueues();
  const exec = new ShadowExecutor(storage);
  await exec.runBaseline(baselineTasks);

  const swapped = [candidateTaskB, candidateTaskA];
  const report = await exec.runCandidate(swapped);

  assert.deepEqual(report.candidateTaskOrder, ['task-b', 'task-a']);
  assert.equal(report.summary.allSubstantiated, true);

  // Even though task-b ran first, task-a still observes task-a's own baseline
  // values and task-b keeps its two distinct R-C values.
  const readsA = report.tasks['task-a'].candidate.reads;
  assert.deepEqual(readsA.map((r) => r.value), [10, { value: 5 }]);
  const readsB = report.tasks['task-b'].candidate.reads;
  assert.deepEqual(readsB.map((r) => r.value), [2, 7]);

  for (const id of ['task-a', 'task-b']) {
    assert.equal(report.tasks[id].resultsEqual, true);
  }
});

test('candidate read after its own write sees the overlay update, not storage', async () => {
  // MapStorage supports real write-then-read semantics for the baseline side.
  const storage = new MapStorage({ 'R-D': 3 });
  const exec = new ShadowExecutor(storage);
  await exec.runBaseline([baselineTaskC]);
  const report = await exec.runCandidate([candidateTaskC]);

  const reads = report.tasks['task-c'].candidate.reads;
  assert.deepEqual(
    reads.map((r) => [r.resource, r.source, r.value]),
    [
      ['R-D', 'replay', 3],
      ['W-C', 'overlay', 30],
    ],
  );
  assert.deepEqual(report.tasks['task-c'].candidate.overlaySnapshot, { 'W-C': 30 });
  assert.equal(report.tasks['task-c'].resultsEqual, true);

  // The read-back was served by the overlay and consumed no baseline replay
  // slot: only one replay check exists, for R-D.
  assert.deepEqual(report.tasks['task-c'].replayChecks, [
    { resource: 'R-D', occurrence: 1, equal: true },
  ]);

  // Baseline storage holds the baseline write; the candidate added nothing.
  assert.deepEqual(storage.snapshot(), { 'R-D': 3, 'W-C': 30 });
});

test('values cross the boundary as clones, so fixture aliasing cannot fake equality', async () => {
  const storage = new MapStorage({ obj: { nested: 1 } });
  const baseline = {
    id: 't',
    async impl(ctx) {
      const v = await ctx.read('obj');
      v.nested = 999; // mutating the returned object must not corrupt storage/trace
      return { saw: 1 };
    },
  };
  const candidate = {
    id: 't',
    async impl(ctx) {
      const v = await ctx.read('obj');
      return { saw: v.nested };
    },
  };
  const exec = new ShadowExecutor(storage);
  await exec.runBaseline([baseline]);
  const report = await exec.runCandidate([candidate]);

  assert.equal(report.tasks['t'].baseline.reads[0].value.nested, 1);
  assert.equal(report.tasks['t'].candidate.reads[0].value.nested, 1);
  assert.deepEqual(storage.snapshot(), { obj: { nested: 1 } });
});
