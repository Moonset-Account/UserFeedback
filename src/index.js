/**
 * Public entry point.
 *
 *   import { createConfigCenter } from 'config-center-kernel';
 *
 *   const center = createConfigCenter({ maxHistory: 256 });
 *   center.commit([
 *     { key: 'app/db/host', value: 'db-1' },
 *     { key: 'app/db/port', value: 5432 },
 *   ]);
 *
 *   const sub = center.subscribe({ prefix: 'app/db' });
 *   const snapshot = sub.snapshot();   // pause allowed before the first poll
 *   sub.poll();                        // -> { type: 'changes', transactions: [...] }
 *
 * See README.md for the full protocol (snapshot, changes, reset) and the
 * reconnect contract.
 */

export { createConfigCenter, DEFAULT_MAX_HISTORY, DEFAULT_REVISION } from './kernel.js';
export { cloneValue } from './clone.js';
export {
  DEFAULT_SEPARATOR,
  matchesPrefix,
  normalizePrefix,
  normalizeSeparator,
} from './keys.js';
