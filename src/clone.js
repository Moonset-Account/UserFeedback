/**
 * Value isolation.
 *
 * Every value that enters or leaves the kernel is cloned, so callers can never
 * mutate stored state through a reference they still hold, and a snapshot
 * handed out earlier can never observe later commits.
 */

export function cloneValue(value) {
  if (typeof structuredClone === 'function') {
    return structuredClone(value);
  }
  // Node >= 20 always has structuredClone; this branch only exists so the
  // module can be loaded in environments that predate it.
  if (value === undefined) return undefined;
  return JSON.parse(JSON.stringify(value));
}
