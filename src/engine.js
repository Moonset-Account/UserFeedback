/**
 * Batched driver for correlated scalar subqueries.
 *
 * A ReadView is the unit of cache reuse: within one view, requests with the
 * same merge key (identical SQL + identical argument bindings) are issued to
 * the runner exactly once. A fresh view never reuses another view's results.
 *
 * Error policy:
 *   - Every outer row keeps only its first error.
 *   - The batch reports the error with the lowest outer row index.
 *   - A failed batch returns no values at all (no partial projection).
 *   - Rows at a higher index than the reported error are not resumed.
 */
import { CODES, EvalError } from './errors.js';
import { Suspension, startRow } from './interpreter.js';
import { canonicalArgs } from './key.js';

const STATUS = Object.freeze({
  NEW: 'new',
  SUSPENDED: 'suspended',
  DONE: 'done',
  ERROR: 'error',
});

const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
const isPlainObject = (v) =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

let viewSequence = 0;

function nextViewId() {
  viewSequence += 1;
  return `rv-${viewSequence}`;
}

function freezeRows(rows) {
  const copy = rows.map((row) => Object.freeze(row.slice()));
  return Object.freeze(copy);
}

/**
 * One read view: wraps a runner and keeps a cache keyed by subqueryKey().
 */
export class ReadView {
  /**
   * @param {{resolve: Function}} runner
   * @param {{id?: string}} [opts]
   */
  constructor(runner, opts = {}) {
    if (!runner || typeof runner.resolve !== 'function') {
      throw new TypeError('ReadView: runner.resolve must be a function');
    }
    this.runner = runner;
    this.id = opts.id ?? nextViewId();
    /** @type {Map<string, object>} */
    this.cache = new Map();
    /** @type {object[]} */
    this.log = [];
    this.requestCounter = 0;
    this.batchCounter = 0;
  }

  /**
   * Evaluate a batch of outer rows against one shared expression tree.
   *
   * @param {object} opts
   * @param {object} opts.tree
   * @param {Array<{params: Record<string, unknown>, id?: string}>} opts.rows
   * @param {'mark-missing'|'error'} [opts.missingCorrelation]
   * @returns {Promise<{
   *   ok: boolean,
   *   readView: string,
   *   values?: unknown[],
   *   error?: object,
   *   stats: object,
   * }>}
   */
  async evaluateBatch({ tree, rows, missingCorrelation = 'mark-missing' }) {
    if (!Array.isArray(rows)) {
      throw new TypeError('evaluateBatch: rows must be an array');
    }
    if (missingCorrelation !== 'mark-missing' && missingCorrelation !== 'error') {
      throw new TypeError(
        "evaluateBatch: missingCorrelation must be 'mark-missing' or 'error'",
      );
    }

    const allStates = rows.map((row, index) => {
      if (!isPlainObject(row) || !isPlainObject(row.params)) {
        throw new TypeError(`evaluateBatch: rows[${index}] must be { params: object }`);
      }
      const state = {
        index,
        params: row.params,
        label: row.id ?? null,
        status: STATUS.NEW,
        demand: null,
        suspension: null,
        value: undefined,
        error: null,
        done: null,
      };
      state.done = (value) => {
        state.value = value;
        state.status = STATUS.DONE;
      };
      return state;
    });

    const stats = {
      readView: this.id,
      batchRows: rows.length,
      passes: 0,
      requestBatches: 0,
      requestsIssued: 0,
      demandsServed: 0,
      cacheHits: 0,
    };
    const issued = [];
    const reused = [];
    const batchId = (this.batchCounter += 1);

    const failWith = (state, error) => {
      const normalized =
        error instanceof EvalError
          ? error
          : new EvalError({
              code: CODES.RUNNER,
              reason: error instanceof Error ? error.message : String(error),
              path: [],
            });
      if (!Number.isInteger(normalized.rowIndex) || normalized.rowIndex < 0) {
        normalized.rowIndex = state.index;
      }
      state.error = normalized;
      state.status = STATUS.ERROR;
    };

    const attempt = (state, fn) => {
      try {
        fn();
      } catch (error) {
        if (error instanceof Suspension) {
          state.demand = error.demand;
          state.suspension = error;
          state.status = STATUS.SUSPENDED;
          return;
        }
        failWith(state, error);
        return;
      }
      if (state.status === STATUS.SUSPENDED || state.status === STATUS.NEW) {
        failWith(
          state,
          new EvalError({
            code: CODES.RUNNER,
            reason: 'row evaluation completed without a value',
            path: [],
          }),
        );
      }
    };

    const start = (state) => {
      attempt(state, () => {
        startRow(
          {
            rowIndex: state.index,
            params: state.params,
            tree,
            missingCorrelation,
          },
          state.done,
        );
      });
    };

    const resume = (state, suspension, value) => {
      attempt(state, () => {
        state.demand = null;
        state.status = STATUS.NEW;
        suspension.resume(value);
      });
    };

    const serve = (state) => {
      const entry = this.cache.get(state.demand.key);
      const suspension = state.suspension;
      const demand = state.demand;
      stats.demandsServed += 1;
      if (entry.batchId === batchId) {
        entry.demandCount += 1;
        entry.rowIndexes.add(state.index);
      } else {
        // Served from an earlier batch's cache: keep this batch's row indexes
        // separate so per-batch stats never mix row numbers across batches.
        let rows = entry.reuseBy.get(batchId);
        if (!rows) {
          rows = new Set();
          entry.reuseBy.set(batchId, rows);
          reused.push(entry);
        }
        rows.add(state.index);
      }
      if (entry.servedOnce) stats.cacheHits += 1;
      entry.servedOnce = true;

      state.demand = null;

      if (entry.error) {
        state.error = new EvalError({
          code: entry.error.code,
          reason: entry.error.reason,
          rowIndex: state.index,
          path: demand.path,
          detail: { requestId: entry.requestId, sql: entry.sql },
        });
        state.status = STATUS.ERROR;
        return;
      }
      if (entry.rows.length > 1) {
        state.error = new EvalError({
          code: CODES.SCALAR_CARDINALITY,
          reason: `scalar subquery returned ${entry.rows.length} rows (at most 1 allowed)`,
          rowIndex: state.index,
          path: demand.path,
          detail: {
            requestId: entry.requestId,
            sql: entry.sql,
            rowCount: entry.rows.length,
          },
        });
        state.status = STATUS.ERROR;
        return;
      }
      resume(state, suspension, entry.rows.length === 0 ? null : entry.rows[0][0]);
    };

    let firstError = Infinity;
    let guard = 0;

    for (;;) {
      guard += 1;
      if (guard > 100000) {
        return {
          ok: false,
          readView: this.id,
          error: new EvalError({
            code: CODES.RUNNER,
            reason: 'batch evaluation failed to converge',
            path: [],
          }).toJSON(),
          stats: finishStats(stats, issued, reused),
        };
      }
      stats.passes += 1;

      for (let i = 0; i < allStates.length && i < firstError; i += 1) {
        const state = allStates[i];
        if (state.status === STATUS.NEW) {
          start(state);
        } else if (state.status === STATUS.SUSPENDED && this.cache.has(state.demand.key)) {
          serve(state);
        }
        if (state.status === STATUS.ERROR) {
          firstError = i;
          break;
        }
      }

      const pending = [];
      for (let i = 0; i < allStates.length && i < firstError; i += 1) {
        const state = allStates[i];
        if (state.status === STATUS.SUSPENDED && !this.cache.has(state.demand.key)) {
          pending.push(state);
        }
      }

      if (pending.length === 0) {
        if (firstError < Infinity) break;
        const active = allStates.some(
          (state) => state.status === STATUS.NEW || state.status === STATUS.SUSPENDED,
        );
        if (!active) break;
        continue;
      }

      const order = [];
      const byKey = new Map();
      for (const state of pending) {
        const { key, sql, args, path } = state.demand;
        let request = byKey.get(key);
        if (!request) {
          request = {
            requestId: ++this.requestCounter,
            key,
            sql,
            args: canonicalArgs(args),
            demandedBy: [],
            path,
          };
          byKey.set(key, request);
          order.push(request);
        }
        request.demandedBy.push(state.index);
      }

      const wire = Object.freeze(
        order.map((request) =>
          Object.freeze({
            requestId: request.requestId,
            sql: request.sql,
            args: request.args,
          }),
        ),
      );

      this.log.push({
        readView: this.id,
        pass: stats.passes,
        requests: wire.map((request) => ({
          requestId: request.requestId,
          sql: request.sql,
          args: request.args,
        })),
      });

      stats.requestBatches += 1;
      stats.requestsIssued += wire.length;

      let responses;
      try {
        responses = await this.runner.resolve({ readView: this.id, requests: wire });
      } catch (error) {
        return this.fail(
          new EvalError({
            code: CODES.RUNNER,
            reason: `runner.resolve failed: ${
              error instanceof Error ? error.message : String(error)
            }`,
            path: [],
            detail: { readView: this.id },
          }),
          stats,
          issued,
          reused,
        );
      }

      const validated = validateResponses(responses, wire);
      if (validated.error) {
        return this.fail(validated.error, stats, issued, reused);
      }

      for (const request of order) {
        const response = validated.byId.get(request.requestId);
        const entry = {
          requestId: request.requestId,
          key: request.key,
          sql: request.sql,
          args: request.args,
          rows: response.rows ? freezeRows(response.rows) : null,
          error: response.error ?? null,
          rowIndexes: new Set(request.demandedBy),
          demandCount: 0,
          servedOnce: false,
          batchId,
          reuseBy: new Map(),
        };
        this.cache.set(request.key, entry);
        issued.push(entry);
      }
    }

    if (firstError < Infinity) {
      return this.fail(allStates[firstError].error, stats, issued, reused);
    }

    const unfinished = allStates.find((state) => state.status !== STATUS.DONE);
    if (unfinished) {
      return this.fail(
        new EvalError({
          code: CODES.RUNNER,
          reason: `row ${unfinished.index} finished without a value`,
          path: [],
        }),
        stats,
        issued,
        reused,
      );
    }

    stats.perRequest = describeEntries(issued);
    stats.reusedFromEarlierBatches = describeReused(reused);
    return {
      ok: true,
      readView: this.id,
      values: allStates.map((state) => state.value),
      stats,
    };
  }

  fail(error, stats, issued, reused) {
    stats.perRequest = describeEntries(issued);
    stats.reusedFromEarlierBatches = describeReused(reused);
    return {
      ok: false,
      readView: this.id,
      error: error.toJSON(),
      stats,
    };
  }
}

/** Convenience wrapper: fresh read view for a single batch. */
export function evaluateBatch(opts) {
  const view = new ReadView(opts.runner, opts.readView ? { id: opts.readView } : {});
  return view.evaluateBatch(opts);
}

function finishStats(stats, issued) {
  stats.perRequest = describeEntries(issued);
  return stats;
}

function describeEntries(entries) {
  return entries.map((entry) => ({
    requestId: entry.requestId,
    key: entry.key,
    sql: entry.sql,
    args: entry.args,
    rowIndexes: [...entry.rowIndexes].sort((a, b) => a - b),
    demandCount: entry.demandCount,
    rowCount: entry.rows ? entry.rows.length : null,
    rows: entry.rows ? entry.rows.map((row) => row.slice()) : null,
    errorCode: entry.error ? entry.error.code : null,
  }));
}

/** Describe cache entries that were reused from earlier batches in this view. */
function describeReused(entries) {
  return entries.map((entry) => ({
    requestId: entry.requestId,
    key: entry.key,
    sql: entry.sql,
    args: entry.args,
    reusedByBatches: [...entry.reuseBy.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([reuseBatchId, rows]) => ({
        batch: reuseBatchId,
        rowIndexes: [...rows].sort((a, b) => a - b),
      })),
    rowCount: entry.rows ? entry.rows.length : null,
    rows: entry.rows ? entry.rows.map((row) => row.slice()) : null,
    errorCode: entry.error ? entry.error.code : null,
  }));
}

function badResponse(reason, detail = null) {
  return new EvalError({ code: CODES.BAD_RESPONSE, reason, path: [], detail });
}

const isValidErrorPayload = (error) =>
  isPlainObject(error) &&
  (error.code === undefined || typeof error.code === 'string') &&
  (error.reason === undefined || typeof error.reason === 'string');

/**
 * Responses may arrive in any order and are matched by requestId. Each
 * request must be answered exactly once, with either `rows` or `error`.
 */
function validateResponses(responses, requests) {
  if (!Array.isArray(responses)) {
    return { error: badResponse('runner response must be an array') };
  }
  const expected = new Set(requests.map((request) => request.requestId));
  const byId = new Map();

  for (let i = 0; i < responses.length; i += 1) {
    const response = responses[i];
    if (!isPlainObject(response)) {
      return { error: badResponse(`response[${i}] must be an object`) };
    }
    if (!Number.isInteger(response.requestId)) {
      return { error: badResponse(`response[${i}].requestId must be an integer`) };
    }
    if (!expected.has(response.requestId)) {
      return {
        error: badResponse('response for unknown requestId', {
          requestId: response.requestId,
        }),
      };
    }
    if (byId.has(response.requestId)) {
      return {
        error: badResponse('duplicate response for requestId', {
          requestId: response.requestId,
        }),
      };
    }
    const hasRows = hasOwn(response, 'rows');
    const hasError = hasOwn(response, 'error');
    if (hasRows === hasError) {
      return {
        error: badResponse('response must carry exactly one of rows or error', {
          requestId: response.requestId,
        }),
      };
    }
    if (hasError) {
      if (!isValidErrorPayload(response.error)) {
        return {
          error: badResponse('response.error must be { code?, reason? }', {
            requestId: response.requestId,
          }),
        };
      }
      byId.set(response.requestId, {
        error: {
          code:
            typeof response.error.code === 'string' && response.error.code.length > 0
              ? response.error.code
              : CODES.RUNNER,
          reason:
            typeof response.error.reason === 'string' && response.error.reason.length > 0
              ? response.error.reason
              : 'runner reported a query error',
        },
      });
      continue;
    }
    if (!Array.isArray(response.rows)) {
      return {
        error: badResponse('response.rows must be an array', {
          requestId: response.requestId,
        }),
      };
    }
    for (let r = 0; r < response.rows.length; r += 1) {
      const row = response.rows[r];
      if (!Array.isArray(row) || row.length !== 1) {
        return {
          error: badResponse(
            'scalar subquery result rows must be single-column arrays',
            { requestId: response.requestId, rowOffset: r },
          ),
        };
      }
    }
    byId.set(response.requestId, { rows: response.rows });
  }

  for (const requestId of expected) {
    if (!byId.has(requestId)) {
      return { error: badResponse('missing response for requestId', { requestId }) };
    }
  }
  return { byId };
}
