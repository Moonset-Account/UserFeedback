/**
 * In-memory write stub.
 *
 * Stands in for the real installer writer. It applies a validated plan
 * transactionally against a flat virtual tree:
 *
 *   1. every operation is checked against a *clone* of the current tree;
 *   2. the clone is mutated only if all operations succeed;
 *   3. on any failure the real tree and call log stay untouched.
 *
 * It never trusts the plan blindly — containment and type rules are
 * re-checked here as well, so the stub doubles as a test for the invariant
 * "rejected batches leave zero partial changes".
 */

import {
  DEFAULT_PATH_RULES,
  checkEntryPath,
  comparisonKeyForPath,
  isInsideRoot,
} from './path-rules.js';

export class MemoryWriteError extends Error {
  constructor(message, operation) {
    super(message);
    this.name = 'MemoryWriteError';
    this.operation = operation;
  }
}

export class MemoryWriteStub {
  /**
   * @param {Readonly<Record<string, {type: 'file'|'directory'}>> | ReadonlyMap<string, {type: 'file'|'directory'}>} [initialTree]
   * @param {import('./path-rules.js').PathRules} [rules]
   */
  constructor(initialTree = {}, rules = DEFAULT_PATH_RULES) {
    this.rules = { ...rules };
    /** @type {Map<string, {type: 'file'|'directory', contents?: unknown}>} key -> node */
    this.tree = new Map();
    /** @type {Array<{action: string, targetPath: string, at: number}>} */
    this.calls = [];

    const entries =
      initialTree instanceof Map ? Array.from(initialTree.entries()) : Object.entries(initialTree);
    for (const [path, node] of entries) {
      const check = checkEntryPath(path, this.rules);
      if (!check.ok) throw new MemoryWriteError(`invalid initial tree path: ${path}`, null);
      this.tree.set(comparisonKeyForPath(check.components, this.rules), {
        type: node.type,
        ...(node.type === 'file' ? { contents: node.contents } : {}),
      });
    }
  }

  /** Structural snapshot keyed by display path; safe to deep-compare. */
  snapshot() {
    const out = {};
    for (const [key, node] of this.tree) {
      out[key] = node.type === 'file' ? { type: 'file', contents: node.contents } : { type: 'directory' };
    }
    return out;
  }

  /**
   * Apply a plan atomically.
   * @param {{operations: Array<{action: string, type: 'file'|'directory', targetPath: string, contents?: unknown}>}} plan
   * @returns {Array<{action: string, targetPath: string}>} the calls that were committed
   */
  applyPlan(plan) {
    if (!plan || !Array.isArray(plan.operations)) {
      throw new MemoryWriteError('plan must contain an operations array', null);
    }

    const scratch = new Map(this.tree);
    const stagedCalls = [];
    let step = 0;

    for (const op of plan.operations) {
      const check = checkEntryPath(op.targetPath, this.rules);
      if (!check.ok) {
        throw new MemoryWriteError(
          `operation target failed structural check: ${check.reason}`,
          op,
        );
      }
      if (!isInsideRoot(check.components, this.rules)) {
        throw new MemoryWriteError('operation target escapes the isolated root', op);
      }

      const key = comparisonKeyForPath(check.components, this.rules);
      const existing = scratch.get(key);

      if (op.action === 'noop') {
        if (!existing) throw new MemoryWriteError('noop scheduled for missing path', op);
        if (existing.type !== op.type) {
          throw new MemoryWriteError('noop type does not match the stored node', op);
        }
      } else if (op.action === 'mkdir') {
        if (existing) throw new MemoryWriteError(`path already exists: ${op.targetPath}`, op);
        scratch.set(key, { type: 'directory' });
      } else if (op.action === 'writeFile') {
        if (existing) throw new MemoryWriteError(`path already exists: ${op.targetPath}`, op);
        scratch.set(key, { type: 'file', contents: op.contents });
      } else {
        throw new MemoryWriteError(`unknown action: ${op.action}`, op);
      }

      stagedCalls.push({ action: op.action, targetPath: op.targetPath, at: step++ });
    }

    // All operations verified against the clone — commit as one unit.
    this.tree = scratch;
    this.calls.push(...stagedCalls);
    return stagedCalls.map(({ action, targetPath }) => ({ action, targetPath }));
  }
}
