/**
 * Public entry point.
 *
 * The whole task is about batched, order-preserving evaluation of correlated
 * scalar subqueries under IF branch pruning and read-view deduplication, so the
 * module exports the driver, the read view, the row interpreter, the key
 * encoder and the error type. The naive reference interpreter lives in
 * src/reference.js and is exported for oracle-style tests.
 */
export { EvalError, CODES, isEvalError } from './errors.js';
export { subqueryKey, encodeArgValue, canonicalArgs } from './key.js';
export { ReadView, evaluateBatch } from './engine.js';
export { Suspension, RowContext, startRow } from './interpreter.js';
export { ReferenceInterpreter } from './reference.js';
