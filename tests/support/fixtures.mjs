/**
 * Shared fixtures for the adapter hot-swap tests.
 *
 * Weights are deterministic integers derived from (version, block, index) so
 * that a score computed from any mix of versions is instantly identifiable.
 */

/** Deterministic integer weight vector for a (version, block) pair. */
export function weightsFor(version, blockId, dim = 2) {
  return Array.from({ length: dim }, (_, i) => version * 100 + blockId * 10 + i);
}

/** Manifest for a version that needs one slot per block. */
export function manifestFor(version, blocks, slotCount = blocks.length) {
  return { version, blocks: [...blocks], slotCount };
}

/** Deliver every declared block for an attempt, in ascending order. */
export function deliverAll(attempt, version, dim = 2) {
  for (const blockId of attempt.blockIds) {
    attempt.deliver(blockId, weightsFor(version, blockId, dim));
  }
}

/** Start a load and fully deliver it, returning the new published version. */
export function loadAndPublish(registry, version, blocks, dim = 2) {
  const attempt = registry.beginLoad(manifestFor(version, blocks));
  deliverAll(attempt, version, dim);
  return registry.published;
}

/** Score every block of a published version in isolation, for expectations. */
export function expectedScore(version, blocks, input, dim = 2) {
  let total = 0;
  for (const blockId of [...blocks].sort((a, b) => a - b)) {
    const w = weightsFor(version, blockId, dim);
    for (let i = 0; i < input.length; i++) total += w[i] * input[i];
  }
  return total;
}
