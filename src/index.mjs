/**
 * Public surface of the admission hook interpreter.
 */
export { AdmissionError } from './errors.mjs';
export {
  canonicalJson,
  deepEqual,
  isJsonValue,
  objectFingerprint,
} from './canonical.mjs';
export {
  compileCondition,
  conditionFields,
  evaluateCondition,
} from './condition.mjs';
export {
  MAX_HOOKS,
  defineProgram,
  normalizeInitialObject,
  programSignature,
} from './program.mjs';
export {
  applyHookToState,
  advanceRound,
  createRunCursor,
  executeStep,
  fullPendingSet,
  groupSteps,
  runRound,
  runRounds,
  stateFingerprint,
  stateToObject,
  validateOrder,
} from './engine.mjs';
export { analyze, buildComponents } from './analyze.mjs';
export {
  EVIDENCE_VERSION,
  replayEvidence,
  replayWitness,
  verifyEvidence,
} from './witness.mjs';
