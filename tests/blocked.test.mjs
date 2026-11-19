// Rejected / blocked-path tests:
//  - an unpaired external read (unknown resource, or extra occurrence) keeps
//    the observed difference and marks the dependent result unsubstantiated;
//  - candidate trial writes never enter baseline storage;
//  - an unknown task marker is rejected up front;
//  - divergent business values are surfaced instead of being hidden.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ShadowExecutor, UnknownTaskError } from '../src/shadow.mjs';
import { QueueStorage, baselineTaskA, candidateTaskAWithUnknownRead, candidateTaskAWithExtraOccurrence } from './fixtures.mjs';

function buildQueues() {
  return new QueueStorage({
    'R-A': [10],
    'R-B': [{ value: 5 }],
    'R-NEW': [99], // value the fallback driver would observe
  });
}

test('unpaired read of an unknown resource: difference retained, result marked unsubstantiated', async () => {
  const storage = buildQueues();
  const exec = new ShadowExecutor(storage);
  await exec.runBaseline([baselineTaskA]);
  const writesBefore = storage.writes.length;

  const report = await exec.runCandidate([candidateTaskAWithUnknownRead]);
  const t = report.tasks['task-a'];

  assert.equal(t.unsubstantiated, true);
  assert.equal(report.summary.allSubstantiated, false);
  assert.equal(report.summary.anyUnpairedRead, true);

  // The observed fallback value is retained rather than silently dropped.
  assert.deepEqual(t.missingBasis, [
    {
      resource: 'R-NEW',
      occurrence: 1,
      reason: 'resource-never-read-by-baseline',
      observedValue: 99,
      fallbackError: undefined,
    },
  ]);

  // Paired reads before/after the unpaired one still replay exactly.
  const reads = t.candidate.reads;
  assert.deepEqual(
    reads.map((r) => [r.resource, r.source, r.value]),
    [
      ['R-A', 'replay', 10],
      ['R-NEW', 'fallback', 99],
      ['R-B', 'replay', { value: 5 }],
    ],
  );
  assert.ok(t.replayChecks.every((c) => c.equal));

  // The candidate did continue and produced a value, but its result differs
  // and must not be treated as verified evidence.
  assert.equal(t.resultsEqual, false);
  assert.deepEqual(t.candidate.result, { task: 'a', sum: 114, extra: 99 });
  assert.deepEqual(t.baseline.result, { task: 'a', sum: 15 });

  // Trial write stays in the overlay; baseline storage is untouched.
  assert.ok(t.candidate.writes.every((w) => w.layer === 'overlay'));
  assert.equal(storage.writes.length, writesBefore);
});

test('extra read occurrence of a known resource: occurrence-level unpaired, still unsubstantiated', async () => {
  const storage = new QueueStorage({ 'R-A': [10, 20], 'R-B': [{ value: 5 }] });
  const exec = new ShadowExecutor(storage);
  await exec.runBaseline([baselineTaskA]);

  const report = await exec.runCandidate([candidateTaskAWithExtraOccurrence]);
  const t = report.tasks['task-a'];

  assert.equal(t.unsubstantiated, true);
  assert.deepEqual(t.missingBasis.map((m) => [m.resource, m.occurrence, m.reason, m.observedValue]), [
    ['R-A', 2, 'read-occurrence-without-baseline', 20],
  ]);
  // The first R-A read remains a valid replay; the second is the fallback.
  assert.deepEqual(
    t.candidate.reads.map((r) => [r.resource, r.occurrence, r.source, r.value]),
    [
      ['R-A', 1, 'replay', 10],
      ['R-A', 2, 'fallback', 20],
      ['R-B', 1, 'replay', { value: 5 }],
    ],
  );
  assert.equal(storage.writes.length, 1); // only the baseline W-A write
});

test('fallback driver failure is recorded on the unpaired event and rethrown to the task', async () => {
  const storage = buildQueues();
  const failingFallback = {
    async read() {
      throw new Error('fallback unavailable');
    },
    async write() {
      throw new Error('fallback is read-only');
    },
  };
  const exec = new ShadowExecutor(storage, { fallbackDriver: failingFallback });
  await exec.runBaseline([baselineTaskA]);
  const report = await exec.runCandidate([candidateTaskAWithUnknownRead]);
  const t = report.tasks['task-a'];

  assert.equal(t.unsubstantiated, true);
  assert.equal(t.candidate.status, 'rejected');
  assert.deepEqual(t.candidate.error, { name: 'Error', message: 'fallback unavailable' });
  assert.deepEqual(t.missingBasis[0].fallbackError, {
    name: 'Error',
    message: 'fallback unavailable',
  });
  // Baseline storage still received nothing from the candidate run.
  assert.deepEqual(storage.writes.map((w) => w.resource), ['W-A']);
});

test('candidate whose business value diverges is reported as not equal', async () => {
  const storage = buildQueues();
  const exec = new ShadowExecutor(storage);
  await exec.runBaseline([baselineTaskA]);

  const divergent = {
    id: 'task-a',
    async impl(ctx) {
      const a = await ctx.read('R-A');
      const b = await ctx.read('R-B');
      await ctx.write('W-A', a + b.value + 1); // wrong arithmetic, same IO shape
      return { task: 'a', sum: a + b.value + 1 };
    },
  };
  const report = await exec.runCandidate([divergent]);
  const t = report.tasks['task-a'];

  assert.equal(t.unsubstantiated, false); // every read was paired...
  assert.equal(t.resultsEqual, false); // ...but the result still diverges
  const compare = t.writeDiffs.find((d) => d.resource === 'W-A');
  assert.equal(compare.sameSequence, false);
  assert.deepEqual(compare.baselineValues, [15]);
  assert.deepEqual(compare.candidateValues, [16]);
  // The divergent trial write never reaches baseline storage.
  assert.deepEqual(storage.writes.map((w) => w.value), [15]);
});

test('unknown task marker is rejected before any candidate work starts', async () => {
  const storage = buildQueues();
  const exec = new ShadowExecutor(storage);
  await exec.runBaseline([baselineTaskA]);

  await assert.rejects(
    exec.runCandidate([{ id: 'ghost', async impl() {} }]),
    (err) => err instanceof UnknownTaskError && err.taskId === 'ghost',
  );
  assert.deepEqual(exec.report().candidateTaskOrder, []);
  assert.deepEqual(storage.writes.map((w) => w.resource), ['W-A']);
});

test('running the same candidate task twice is rejected', async () => {
  const storage = buildQueues();
  const exec = new ShadowExecutor(storage);
  await exec.runBaseline([baselineTaskA]);
  await exec.runCandidate([
    { id: 'task-a', async impl(ctx) { await ctx.read('R-A'); } },
  ]);
  await assert.rejects(
    exec.runCandidate([
      { id: 'task-a', async impl(ctx) { await ctx.read('R-A'); } },
    ]),
    /already been run/,
  );
});

test('bad driver or task shape is rejected with a TypeError', async () => {
  assert.throws(() => new ShadowExecutor({}), TypeError);
  const exec = new ShadowExecutor(storageDriver());
  await assert.rejects(exec.runBaseline([{ id: 'x' }]), TypeError);
  await assert.rejects(exec.runBaseline([{ impl: async () => {} }]), TypeError);
});

function storageDriver() {
  return new QueueStorage({ 'R-A': [1] });
}
