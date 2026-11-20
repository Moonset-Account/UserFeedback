/**
 * Internal kernel state and shared builders.
 *
 * The state object is kept in a WeakMap so that nothing outside the kernel can
 * reach or mutate it. Subscription lives in its own module and only touches
 * state through this registry.
 */

import { matchesPrefix } from './keys.js';

export const INTERNALS = new WeakMap();

/**
 * A consistent, already-materialised view of every key matching `prefix` at
 * `state.headRevision`. Sorting by key keeps payloads deterministic and makes
 * snapshot equality straightforward for callers and tests.
 */
export function buildSnapshot(state, prefix) {
  const entries = [];
  for (const [key, entry] of state.entries) {
    if (!matchesPrefix(prefix, key, state.separator)) continue;
    entries.push({ key, value: state.clone(entry.value) });
  }
  entries.sort((left, right) => (left.key < right.key ? -1 : left.key > right.key ? 1 : 0));
  return { revision: state.headRevision, prefix, entries };
}

/**
 * Turn a stored internal change into a caller-owned event payload.
 */
export function materializeChange(state, change) {
  if (change.deleted) {
    return { key: change.key, deleted: true };
  }
  return { key: change.key, deleted: false, value: state.clone(change.value) };
}
