import { compileTransaction } from './compile.mjs';
import { applyPatch, tryApplyPatch, invertPatch } from './apply.mjs';
import { errorResult } from './errors.mjs';
import { initializeIdentities } from './document.mjs';

export function runTransaction(doc, input) {
  try {
    initializeIdentities(doc);
    const patch = compileTransaction(doc, input);
    const applied = applyPatch(doc, patch);
    return { ok: true, doc: applied, patch };
  } catch (error) {
    return errorResult(error, { doc });
  }
}

export const applyTransaction = runTransaction;

export function runSequential(doc, tx1, tx2) {
  const first = runTransaction(doc, tx1);
  if (!first.ok) {
    return { ok: false, stage: 1, error: first.error, doc, attempts: [first] };
  }
  const inverse1 = invertPatch(doc, first.patch);
  const second = runTransaction(first.doc, tx2);
  if (!second.ok) {
    return {
      ok: false,
      stage: 2,
      error: second.error,
      doc: first.doc,
      patch1: first.patch,
      inverse1,
      attempts: [first, second],
    };
  }
  const inverse2 = invertPatch(first.doc, second.patch);
  return {
    ok: true,
    doc: second.doc,
    patch1: first.patch,
    patch2: second.patch,
    inverse1,
    inverse2,
    attempts: [first, second],
  };
}

export function applyCompiledPatch(doc, patch) {
  return tryApplyPatch(doc, patch);
}
