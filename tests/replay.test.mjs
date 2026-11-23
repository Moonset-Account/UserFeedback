import test from 'node:test';
import assert from 'node:assert/strict';

import { runSharedQueryReplay } from '../src/scenarios.js';

test('replay: two rows share a query, a third row fails earlier (script + test share one scenario)', async () => {
  const report = await runSharedQueryReplay();

  assert.equal(report.verdict, 'PASS', JSON.stringify(report.checks, null, 2));

  // Issue counts, explicitly.
  assert.equal(report.batched.requestsIssued, 1, 'one merged issue for the shared query');
  assert.equal(report.reference.requestsIssued, 2, 'naive interpreter issues once per row');
  assert.equal(report.batched.runnerCalls, 1);
  assert.deepEqual(report.batched.rowIndexesPerRequest, [[0, 1]]);

  // Row content: the shared request carries alice, and the answer is 100.
  assert.deepEqual(report.batched.requests[0].args, [{ name: 'customer', value: 'alice' }]);
  assert.deepEqual(report.batched.perRequest[0].rows, [[100]]);
  assert.deepEqual(
    report.reference.calls.map((call) => call.rows),
    [[[100]], [[100]]],
  );

  // Error position: row 2, path then/left, DIV_BY_ZERO.
  assert.equal(report.batched.ok, false);
  assert.equal(report.batched.error.rowIndex, 2);
  assert.equal(report.batched.error.code, 'DIV_BY_ZERO');
  assert.deepEqual(report.batched.error.path, ['then', 'left']);
  assert.equal(report.reference.ok, false);
  assert.equal(report.reference.error.rowIndex, 2);
  assert.equal(report.reference.error.code, 'DIV_BY_ZERO');
  assert.deepEqual(report.reference.error.path, ['then', 'left']);

  // No partial results.
  assert.equal(report.batched.values, null);
  assert.equal(report.reference.values, null);
});

test('replay checks are all true individually', async () => {
  const report = await runSharedQueryReplay();
  for (const check of report.checks) {
    assert.ok(check.pass, `failed check: ${check.name} -> ${JSON.stringify(check.detail)}`);
  }
});
