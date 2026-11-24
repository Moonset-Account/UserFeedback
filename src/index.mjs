export * from './paths.mjs';
export * from './document.mjs';
export { TransactionError } from './errors.mjs';
export { compileTransaction, normalizeTransaction, analyzePath } from './compile.mjs';
export { applyPatch, tryApplyPatch, invertPatch } from './apply.mjs';
export { runTransaction, applyTransaction, runSequential } from './transaction.mjs';
export { diffDocuments } from './diff.mjs';
export {
  composeTransactions,
  composePatches,
  planComposition,
  executeComposed,
  applyComposedPatch,
} from './compose.mjs';

import { parsePath } from './paths.mjs';
import { compileTransaction } from './compile.mjs';
import { applyPatch, invertPatch } from './apply.mjs';
import { runTransaction, runSequential } from './transaction.mjs';
import { composePatches, planComposition, executeComposed, applyComposedPatch } from './compose.mjs';

export function createEngine(options = {}) {
  return {
    options,
    parsePath,
    compile: compileTransaction,
    compilePatch: compileTransaction,
    apply: applyPatch,
    applyPatch,
    run: runTransaction,
    runTransaction,
    sequential: runSequential,
    runSequential,
    invert: invertPatch,
    invertPatch,
    compose: planComposition,
    planComposition,
    composePatches,
    composeTransactions,
    executeComposed,
    applyComposition: executeComposed,
    applyComposedPatch,
  };
}

export const compilePatch = compileTransaction;
export const compose = planComposition;
export const applyComposition = executeComposed;
