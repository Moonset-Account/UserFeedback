/**
 * Kernel core: revisioned key/value storage with a trimmed transaction log.
 *
 * Revision model
 * --------------
 * Every commit consumes exactly one global revision, whether or not the change
 * matches any particular prefix. That is what lets a subscriber that only
 * watches one prefix advance its cursor past transactions belonging to other
 * prefixes without losing position.
 *
 * The kernel keeps the last `maxHistory` transactions. Transactions dropped
 * from the log are summarised only by `trimmedThrough`, the highest revision
 * that can no longer be replayed. A subscriber whose cursor is below that line
 * cannot be served incrementally and is handed a reset instead.
 */

import { cloneValue } from './clone.js';
import { normalizeChanges } from './changes.js';
import { normalizePrefix, normalizeSeparator } from './keys.js';
import { buildSnapshot, INTERNALS, materializeChange } from './internals.js';
import { createSubscription } from './subscription.js';

export const DEFAULT_MAX_HISTORY = 1024;
export const DEFAULT_REVISION = 0;

export function createConfigCenter(options = {}) {
  if (options === null || typeof options !== 'object' || Array.isArray(options)) {
    throw new TypeError('createConfigCenter(options) expects an options object');
  }

  const separator = normalizeSeparator(options.separator);
  const maxHistory = resolveMaxHistory(options.maxHistory);
  const clone = resolveClone(options.clone);

  const state = {
    separator,
    maxHistory,
    clone,
    headRevision: DEFAULT_REVISION,
    trimmedThrough: DEFAULT_REVISION,
    entries: new Map(),
    log: [],
  };

  const center = {
    /** Highest committed revision. Zero until the first commit. */
    get revision() {
      return state.headRevision;
    },

    /** Highest revision whose transaction can no longer be replayed. */
    get trimmedThrough() {
      return state.trimmedThrough;
    },

    /** Number of transactions currently retained for incremental reads. */
    get historySize() {
      return state.log.length;
    },

    get separator() {
      return state.separator;
    },

    commit(changes) {
      return commitTransaction(state, changes);
    },

    /**
     * Consistent point-in-time view of one prefix. Intended for bootstrap and
     * for clients that do not need a long-lived subscription.
     */
    snapshot(prefix) {
      const normalized = normalizePrefix(prefix, state.separator);
      return buildSnapshot(state, normalized);
    },

    subscribe(subscriptionOptions = {}) {
      return createSubscription(center, subscriptionOptions);
    },
  };

  INTERNALS.set(center, state);
  return center;
}

function resolveMaxHistory(value) {
  if (value === undefined) return DEFAULT_MAX_HISTORY;
  if (value === Infinity) return Infinity;
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw new TypeError('options.maxHistory must be a non-negative integer or Infinity');
  }
  return value;
}

function resolveClone(value) {
  if (value === undefined) return cloneValue;
  if (typeof value !== 'function') {
    throw new TypeError('options.clone must be a function');
  }
  return value;
}

function commitTransaction(state, changes) {
  const normalized = normalizeChanges(changes);

  // Materialise values before touching any state so that a failing clone (for
  // example a function value) cannot leave a half-applied transaction behind.
  const prepared = new Map();
  for (const [key, change] of normalized) {
    prepared.set(
      key,
      change.deleted
        ? { key, deleted: true }
        : { key, deleted: false, value: state.clone(change.value) },
    );
  }

  const revision = state.headRevision + 1;

  for (const [key, change] of prepared) {
    if (change.deleted) {
      state.entries.delete(key);
    } else {
      state.entries.set(key, { value: change.value });
    }
  }

  state.headRevision = revision;
  state.log.push({ revision, changes: prepared });
  trimLog(state);

  return {
    revision,
    changes: [...prepared.values()].map((change) => materializeChange(state, change)),
  };
}

function trimLog(state) {
  if (state.maxHistory === Infinity) return;
  while (state.log.length > state.maxHistory) {
    const evicted = state.log.shift();
    state.trimmedThrough = evicted.revision;
  }
}
