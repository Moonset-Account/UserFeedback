/**
 * Synthetic, entirely local fixture data plus a recording runner.
 *
 * No network, no external service: the "database" is a Map of rows in memory.
 * The runner records every request batch it receives so tests can assert the
 * number of issues, the exact row contents and the error positions.
 */
import { subqueryKey } from './key.js';

const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);

/**
 * Frozen synthetic tables. Keys are SQL names used in scalar nodes.
 * Each row is an object of column -> value.
 */
export const SYNTHETIC_TABLES = Object.freeze({
  'orders.total': [
    { customer: 'alice', total: 100 },
    { customer: 'bob', total: 50 },
    { customer: 'carol', total: 75 },
  ],
  'orders.dupe': [
    { customer: 'alice', total: 100 },
    { customer: 'alice', total: 101 },
  ],
  'orders.empty': [{ customer: 'nobody', total: 0 }],
  'orders.by_region': [
    { region: 'north', total: 10 },
    { region: 'south', total: 20 },
    { region: 'north', total: 11 },
  ],
  'orders.by_customer_region': [
    { customer: 'alice', region: 'north', total: 42 },
    { customer: 'bob', region: 'north', total: 7 },
    { customer: 'carol', region: 'south', total: 9 },
  ],
  'settings.version': [{ version: 7 }],
  'settings.by_tenant': [
    { tenant: null, version: 9 },
    { tenant: 'acme', version: 3 },
  ],
  'unstable.flaky': [{ customer: 'alice', total: 1 }],
});

export class SyntheticRunner {
  /**
   * @param {object} opts
   * @param {Record<string, object[]>} [opts.tables]
   * @param {(request: object) => {rows?: unknown[][]|null, error?: object}|null} [opts.override]
   *        Return a response payload to override the table lookup, or null.
   * @param {'arrival'|'reverse'|'shuffle'} [opts.responseOrder]
   * @param {() => number} [opts.random]
   */
  constructor({ tables = SYNTHETIC_TABLES, override = null, responseOrder = 'arrival', random = Math.random } = {}) {
    this.tables = tables;
    this.override = override;
    this.responseOrder = responseOrder;
    this.random = random;
    /** @type {Array<{readView: string, requests: object[]}>} */
    this.batches = [];
    /** @type {object[]} flattened request log */
    this.requests = [];
    this.callCount = 0;
  }

  /**
   * Runner contract: receives the merged requests for one read-view batch and
   * answers all of them, possibly out of order.
   */
  async resolve({ readView, requests }) {
    this.callCount += 1;
    const snapshot = requests.map((request) => ({
      requestId: request.requestId,
      sql: request.sql,
      args: request.args.map((arg) =>
        arg.missing === true
          ? { name: arg.name, missing: true }
          : { name: arg.name, value: arg.value },
      ),
    }));
    this.batches.push({ readView, requests: snapshot });

    const responses = [];
    for (const request of requests) {
      const override = this.override ? this.override(request) : null;
      if (override) {
        responses.push({ requestId: request.requestId, ...override });
        this.requests.push({ ...request, response: override });
        continue;
      }
      const rows = this.lookup(request);
      this.requests.push({
        ...request,
        rows: rows.map((row) => row.slice()),
      });
      responses.push({ requestId: request.requestId, rows: rows.map((row) => row.slice()) });
    }

    return this.orderResponses(responses);
  }

  orderResponses(responses) {
    if (this.responseOrder === 'arrival') return responses;
    if (this.responseOrder === 'reverse') return responses.slice().reverse();
    if (this.responseOrder === 'shuffle') {
      const copy = responses.slice();
      for (let i = copy.length - 1; i > 0; i -= 1) {
        const j = Math.floor(this.random() * (i + 1));
        [copy[i], copy[j]] = [copy[j], copy[i]];
      }
      return copy;
    }
    throw new Error(`unknown responseOrder: ${this.responseOrder}`);
  }

  /**
   * Resolve one request against the synthetic tables.
   *
   * Table rows are objects. The scalar subquery selects the single column that
   * is not used as a correlated argument; requests are projected to
   * single-column rows, which is what the engine expects.
   *
   * A request argument missing:true means the correlated key is absent, so the
   * query matches nothing (SQL equality with an unknown key yields no rows).
   */
  lookup(request) {
    const table = this.tables[request.sql];
    if (!table) {
      throw new Error(`no synthetic table for sql: ${request.sql}`);
    }
    for (const arg of request.args) {
      if (arg.missing === true) return [];
    }
    const matched = table.filter((row) =>
      request.args.every((arg) => hasOwn(row, arg.name) && Object.is(row[arg.name], arg.value)),
    );
    const argNames = new Set(request.args.map((arg) => arg.name));
    const sample = table[0] ?? {};
    const selected = Object.keys(sample).filter((name) => !argNames.has(name));
    if (selected.length !== 1) {
      throw new Error(
        `synthetic table ${request.sql} must have exactly one non-argument column, found ${selected.length}`,
      );
    }
    const column = selected[0];
    return matched.map((row) => [row[column]]);
  }

  reset() {
    this.batches = [];
    this.requests = [];
    this.callCount = 0;
  }

  /** Number of wire requests actually sent to this runner. */
  get issuedCount() {
    return this.batches.reduce((sum, batch) => sum + batch.requests.length, 0);
  }
}

/** Convenience: compute a key the same way the engine does. */
export function keyOf(sql, args) {
  return subqueryKey(sql, args);
}

/**
 * Build a mutable copy of the synthetic tables for tests that need to inject
 * a changing read view.
 */
export function copyTables() {
  const out = {};
  for (const [name, rows] of Object.entries(SYNTHETIC_TABLES)) {
    out[name] = rows.map((row) => ({ ...row }));
  }
  return out;
}
