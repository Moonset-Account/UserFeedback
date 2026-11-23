/**
 * Replay scenarios used by both scripts/replay.mjs and tests/replay.test.mjs.
 *
 * Keeping the scenario in one module means the demo output and the asserting
 * test exercise the exact same code path and the exact same inputs.
 */
import { evaluateBatch, ReferenceInterpreter } from './index.js';
import { SyntheticRunner, copyTables } from './fixtures.js';

/**
 * Scenario A: two outer rows share one subquery and a third row fails earlier.
 *
 *   row 0: IF has_total THEN ((0 / divisor) + scalar(orders.total WHERE customer))
 *   row 1: same shape, same customer as row 0  -> same request
 *   row 2: same shape, divisor = 0
 *
 * The failing left operand is evaluated before the scalar, so row 2 raises
 * DIV_BY_ZERO before reaching any subquery.
 */
export function sharedQueryWithEarlierFailure() {
  const tree = {
    kind: 'if',
    cond: { kind: 'param', name: 'hasTotal' },
    then: {
      kind: 'add',
      left: {
        kind: 'div',
        left: { kind: 'const', value: 0 },
        right: { kind: 'param', name: 'divisor' },
      },
      right: {
        kind: 'scalar',
        sql: 'orders.total',
        args: [{ name: 'customer', param: 'customer' }],
      },
    },
    else: { kind: 'const', value: null },
  };
  const rows = [
    { id: 'o1', params: { hasTotal: true, customer: 'alice', divisor: 1 } },
    { id: 'o2', params: { hasTotal: true, customer: 'alice', divisor: 1 } },
    { id: 'o3', params: { hasTotal: true, customer: 'bob', divisor: 0 } },
  ];
  return { tree, rows };
}

function summarizeRequests(batches) {
  return batches.flatMap((batch, batchIndex) =>
    batch.requests.map((request) => ({
      batch: batchIndex + 1,
      readView: batch.readView,
      requestId: request.requestId,
      sql: request.sql,
      args: request.args,
    })),
  );
}

/** Distinct (sql, args) keys among the reference interpreter's calls. */
function distinctRequestKeys(calls) {
  const keys = new Set();
  for (const call of calls) {
    keys.add(JSON.stringify([call.sql, call.args]));
  }
  return keys;
}

/**
 * Run the replay with both implementations and return a structured report.
 * All assertions are data, so callers (script, test) can reuse them.
 */
export async function runSharedQueryReplay() {
  const { tree, rows } = sharedQueryWithEarlierFailure();

  const batchedRunner = new SyntheticRunner({
    tables: copyTables(),
    responseOrder: 'reverse',
  });
  const batched = await evaluateBatch({ tree, rows, runner: batchedRunner });

  const refRunner = new SyntheticRunner({ tables: copyTables(), responseOrder: 'reverse' });
  const reference = new ReferenceInterpreter(refRunner);
  const refResult = await reference.run({ tree, rows });

  const batchedKeys = new Set(
    (batched.stats.perRequest ?? []).map((entry) => JSON.stringify([entry.sql, entry.args])),
  );
  const referenceKeys = distinctRequestKeys(reference.calls);

  const checks = [];
  const check = (name, pass, detail) => checks.push({ name, pass, detail });

  check('batched batch fails', batched.ok === false, batched.ok);
  check(
    'failed batch carries no values (no partial results)',
    batched.ok === false && batched.values === undefined,
    batched.values,
  );
  check(
    'error position matches: row 2, DIV_BY_ZERO, path then/left',
    batched.ok === false &&
      batched.error.code === 'DIV_BY_ZERO' &&
      batched.error.rowIndex === 2 &&
      JSON.stringify(batched.error.path) === JSON.stringify(['then', 'left']),
    batched.ok ? null : batched.error,
  );
  check(
    'row-by-row interpreter reports the same error position and code',
    refResult.ok === false &&
      refResult.error.code === 'DIV_BY_ZERO' &&
      refResult.error.rowIndex === 2 &&
      JSON.stringify(refResult.error.path) === JSON.stringify(['then', 'left']),
    refResult.ok ? null : refResult.error,
  );
  check(
    'shared subquery issued once for rows 0 and 1',
    (batched.stats.perRequest ?? []).some(
      (entry) => entry.sql === 'orders.total' && entry.rowIndexes.join(',') === '0,1',
    ),
    batched.stats.perRequest,
  );
  check(
    'no subquery issued for row 2 (fails before reaching it)',
    !(batched.stats.perRequest ?? []).some((entry) => entry.rowIndexes.includes(2)),
    batched.stats.perRequest,
  );
  check(
    'batched issue count equals distinct requests demanded row by row',
    batchedRunner.issuedCount === referenceKeys.size,
    { batched: batchedRunner.issuedCount, distinctReference: referenceKeys.size },
  );
  check(
    'batched request payloads match the row-by-row payloads',
    batchedKeys.size === referenceKeys.size &&
      [...batchedKeys].every((key) => referenceKeys.has(key)),
    { batchedKeys: [...batchedKeys], referenceKeys: [...referenceKeys] },
  );
  check(
    'reference demanded the shared query once per row (2 calls), batched merged to 1',
    reference.calls.length === 2 && batchedRunner.issuedCount === 1,
    { referenceCalls: reference.calls.length, batchedIssued: batchedRunner.issuedCount },
  );
  check(
    'request row content is identical between the two runs',
    JSON.stringify(reference.calls[0]?.rows) === JSON.stringify(batched.stats.perRequest?.[0]?.rows),
    { reference: reference.calls[0]?.rows, batched: batched.stats.perRequest?.[0]?.rows },
  );
  check(
    'row 0 and row 1 would resolve to the same scalar value',
    reference.calls[0]?.rows?.[0]?.[0] === 100 &&
      reference.calls[1]?.rows?.[0]?.[0] === 100,
    reference.calls.map((call) => call.rows),
  );

  const failed = checks.filter((entry) => !entry.pass);

  return {
    scenario: 'shared-subquery-plus-earlier-failure',
    rows: rows.map((row, index) => ({ index, id: row.id, params: row.params })),
    batched: {
      ok: batched.ok,
      values: batched.ok ? batched.values : null,
      error: batched.ok ? null : batched.error,
      readView: batched.readView,
      runnerCalls: batchedRunner.callCount,
      requestsIssued: batchedRunner.issuedCount,
      requests: summarizeRequests(batchedRunner.batches),
      perRequest: batched.stats.perRequest,
      rowIndexesPerRequest: (batched.stats.perRequest ?? []).map((entry) => entry.rowIndexes),
    },
    reference: {
      ok: refResult.ok,
      values: refResult.ok ? refResult.values : null,
      error: refResult.ok ? null : refResult.error,
      runnerCalls: refRunner.callCount,
      requestsIssued: refRunner.issuedCount,
      calls: reference.calls.map((call) => ({
        ordinal: call.ordinal,
        rowIndex: call.rowIndex,
        path: call.path,
        sql: call.sql,
        args: call.args,
        rows: call.rows,
      })),
    },
    checks,
    verdict: failed.length === 0 ? 'PASS' : 'FAIL',
  };
}
