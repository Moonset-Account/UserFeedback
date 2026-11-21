import { validateVector } from './errors.mjs';

/**
 * Integer-weighted dot product used for local scoring. Weights are frozen
 * integer arrays; inputs are finite numbers.
 */
export function dot(weights, input) {
  if (weights.length !== input.length) {
    throw new RangeError(
      `dimension mismatch: weights length ${weights.length}, input length ${input.length}`,
    );
  }
  let sum = 0;
  for (let i = 0; i < weights.length; i++) sum += weights[i] * input[i];
  return sum;
}

/**
 * Score one input against a version's weight blocks.
 *
 * For every block id (ascending) take that block's integer weight vector,
 * dot it with the input vector, and sum the contributions. Returning the
 * per-block contributions makes version mixing visible: a score computed
 * from two different versions shows up as a mixture of the two versions'
 * block contributions.
 */
export function scoreVersion(blocks, blockIds, input) {
  validateVector(input, 'input');
  const perBlock = [];
  let total = 0;
  for (const blockId of blockIds) {
    const weights = blocks.get(blockId);
    if (weights === undefined) {
      throw new Error(`missing weights for block ${blockId}`);
    }
    const contribution = dot(weights, input);
    total += contribution;
    perBlock.push({ blockId, weights, contribution });
  }
  return { score: total, blocks: perBlock };
}
