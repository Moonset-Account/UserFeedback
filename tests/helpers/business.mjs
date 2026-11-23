// Synthetic business adapter used by the tests.
//
// It models the two things the service is allowed to assume:
//   1. execute(executionId, ...) applies the effect at most once per id, and
//      reports `{ok:true,outcome:'effective',effect}`;
//   2. query(executionId) answers `effective`, `not_effective` or `unknown`.
// Everything else (crashes, lost responses, double application) is observable
// through the ledger and counters below, so tests can assert on real effects
// rather than on the server's own bookkeeping.

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function createBusiness(options = {}) {
  const ledger = new Map(); // executionId -> { businessKey, operation, effect }
  const applied = []; // one entry per *actual* application
  const executeCalls = []; // every execute() invocation
  const queryCalls = []; // every query() invocation
  const queryOverrides = [...(options.queryOverrides ?? [])];
  let nextReceipt = 0;

  const latency = options.latencyMs ?? 0;
  const delayFor = (req) => (typeof latency === 'function' ? latency(req) : latency);

  function applyEffect(req) {
    const effect = {
      receiptId: `receipt-${++nextReceipt}`,
      businessKey: req.businessKey,
      operation: req.operation,
    };
    applied.push({ executionId: req.executionId, businessKey: req.businessKey, effect });
    ledger.set(req.executionId, {
      businessKey: req.businessKey,
      operation: req.operation,
      effect,
    });
    return effect;
  }

  return {
    applied,
    executeCalls,
    queryCalls,

    /** Number of times the effect was really applied for one business key. */
    effectsFor(businessKey) {
      return applied.filter((entry) => entry.businessKey === businessKey);
    },

    /** Latest receipt id recorded for a business key. */
    receiptFor(businessKey) {
      const entries = applied.filter((entry) => entry.businessKey === businessKey);
      return entries.length === 0 ? undefined : entries.at(-1).effect;
    },

    hasExecution(executionId) {
      return ledger.has(executionId);
    },

    async execute(req) {
      executeCalls.push({ executionId: req.executionId, businessKey: req.businessKey });
      await sleep(delayFor(req));

      const existing = ledger.get(req.executionId);
      if (existing) {
        // Same execution id again: idempotent replay, no second application.
        return { ok: true, outcome: 'effective', effect: existing.effect };
      }

      if (options.beforeApply) {
        const verdict = await options.beforeApply(req);
        if (verdict === 'unknown') {
          return { ok: false, outcome: 'unknown', error: { code: 'UNREACHABLE', message: 'no answer' } };
        }
        if (verdict) return verdict;
      }

      const effect = applyEffect(req);

      if (options.afterApply) {
        // Simulates the process dying (or the response being lost) after the
        // business took effect but before the server recorded a receipt.
        const verdict = await options.afterApply(req);
        if (verdict === 'unknown') {
          return { ok: false, outcome: 'unknown', error: { code: 'RESPONSE_LOST', message: 'response lost' } };
        }
        if (verdict) return verdict;
      }

      return { ok: true, outcome: 'effective', effect };
    },

    async query(req) {
      queryCalls.push({ executionId: req.executionId, businessKey: req.businessKey });
      if (typeof options.queryLatencyMs === 'number') await sleep(options.queryLatencyMs);
      if (queryOverrides.length > 0) return queryOverrides.shift();
      if (options.queryMode === 'unknown') return { status: 'unknown' };
      const entry = ledger.get(req.executionId);
      return entry
        ? { status: 'effective', effect: entry.effect }
        : { status: 'not_effective' };
    },
  };
}
