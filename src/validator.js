/**
 * Offline write-plan validator for the plugin installer.
 *
 * Input is a *synthesized, already parsed* archive manifest plus a virtual
 * file tree of the install target. Nothing here touches the real filesystem:
 * the output is either a deterministic, ordered write plan or a
 * PlanValidationError describing every violation found.
 *
 * All-or-nothing semantics: if any entry is invalid, no plan is produced and
 * no mutation is suggested (the in-memory writer stub proves this end to end).
 */

import {
  DEFAULT_PATH_RULES,
  checkEntryPath,
  comparisonKey,
  comparisonKeyForPath,
  isInsideRoot,
} from './path-rules.js';

export const ENTRY_TYPE = Object.freeze({
  FILE: 'file',
  DIRECTORY: 'directory',
});

/** Machine-readable violation codes. */
export const VIOLATION = Object.freeze({
  UNSUPPORTED_ENTRY_TYPE: 'UNSUPPORTED_ENTRY_TYPE',
  PATH_EMPTY_OR_NON_STRING: 'PATH_EMPTY_OR_NON_STRING',
  PATH_CONTAINS_NUL: 'PATH_CONTAINS_NUL',
  PATH_CONTAINS_BACKSLASH: 'PATH_CONTAINS_BACKSLASH',
  PATH_ABSOLUTE: 'PATH_ABSOLUTE',
  PATH_EMPTY_COMPONENT: 'PATH_EMPTY_COMPONENT',
  PATH_DOT_COMPONENT: 'PATH_DOT_COMPONENT',
  PATH_PARENT_COMPONENT: 'PATH_PARENT_COMPONENT',
  OUTSIDE_ROOT: 'OUTSIDE_ROOT',
  DUPLICATE_ENTRY: 'DUPLICATE_ENTRY',
  FILE_ANCESTOR_CONFLICT: 'FILE_ANCESTOR_CONFLICT',
  TARGET_TYPE_CONFLICT: 'TARGET_TYPE_CONFLICT',
  TARGET_ANCESTOR_IS_FILE: 'TARGET_ANCESTOR_IS_FILE',
  TARGET_TREE_INVALID: 'TARGET_TREE_INVALID',
});

/** Aggregated validation failure. Nothing partial is ever returned. */
export class PlanValidationError extends Error {
  /**
   * @param {string} message
   * @param {Array<{code: string, entryPath?: string, targetPath?: string, detail?: string, entryIndex?: number}>} violations
   */
  constructor(message, violations) {
    super(message);
    this.name = 'PlanValidationError';
    this.violations = violations;
  }
}

/**
 * @typedef {object} ArchiveEntry
 * @property {string} entryPath      Path as recorded in the parsed archive
 * @property {'file'|'directory'|string} type
 * @property {unknown} [contents]    Carried through for file entries; never
 *                                   interpreted by the validator
 */

/**
 * @typedef {object} TargetNode
 * @property {'file'|'directory'} type
 */

/**
 * Index the virtual target tree.
 *
 * The tree is a flat map of slash-separated relative paths to
 * `{ type: 'file' | 'directory' }` (a plain object or a Map). Flat rather
 * than nested so synthesized test fixtures stay trivial and no directory can
 * be "implied" without an explicit node.
 *
 * Target keys get the same NFC + case treatment as entries, so an existing
 * on-disk NFD name still collides with an NFC entry.
 *
 * @param {Readonly<Record<string, TargetNode>> | ReadonlyMap<string, TargetNode>} targetTree
 * @param {import('./path-rules.js').PathRules} rules
 * @returns {{byKey: Map<string, {type: string, displayPath: string}>, violations: Array<object>}}
 */
function indexTargetTree(targetTree, rules) {
  const entries =
    targetTree instanceof Map
      ? Array.from(targetTree.entries())
      : Object.entries(targetTree ?? {});

  /** @type {Map<string, {type: string, displayPath: string}>} */
  const byKey = new Map();
  const violations = [];

  for (const [rawPath, node] of entries) {
    if (typeof rawPath !== 'string' || rawPath.length === 0) {
      violations.push({
        code: VIOLATION.TARGET_TREE_INVALID,
        targetPath: String(rawPath),
        detail: 'target tree key must be a non-empty string',
      });
      continue;
    }
    const check = checkEntryPath(rawPath, rules);
    if (!check.ok) {
      violations.push({
        code: VIOLATION.TARGET_TREE_INVALID,
        targetPath: rawPath,
        detail: `target tree key failed structural check: ${check.reason}`,
      });
      continue;
    }
    if (!node || (node.type !== ENTRY_TYPE.FILE && node.type !== ENTRY_TYPE.DIRECTORY)) {
      violations.push({
        code: VIOLATION.TARGET_TREE_INVALID,
        targetPath: rawPath,
        detail: 'target node type must be "file" or "directory"',
      });
      continue;
    }
    const key = comparisonKeyForPath(check.components, rules);
    if (byKey.has(key)) {
      violations.push({
        code: VIOLATION.TARGET_TREE_INVALID,
        targetPath: rawPath,
        detail: 'target tree contains duplicate comparison keys',
      });
      continue;
    }
    byKey.set(key, { type: node.type, displayPath: check.normalizedPath });
  }
  return { byKey, violations };
}

/**
 * @param {readonly string[]} components
 * @returns {string[][]} every strict ancestor component chain, nearest first
 */
function ancestorChains(components) {
  const chains = [];
  for (let i = components.length - 1; i >= 1; i--) {
    chains.push(components.slice(0, i));
  }
  return chains;
}

/**
 * Validate a parsed manifest against the virtual target and build the plan.
 *
 * @param {ReadonlyArray<ArchiveEntry>} entries
 * @param {Readonly<Record<string, TargetNode>> | ReadonlyMap<string, TargetNode>} targetTree
 * @param {{rules?: import('./path-rules.js').PathRules, rootId?: string}} [options]
 * @returns {{rootId: string, rules: import('./path-rules.js').PathRules, operations: Array<{action: 'mkdir'|'writeFile'|'noop', type: 'file'|'directory', targetPath: string, entryPath: string|null, contents?: unknown}>}}
 * @throws {PlanValidationError} when one or more violations exist
 */
export function buildWritePlan(entries, targetTree, options = {}) {
  const rules = { ...DEFAULT_PATH_RULES, ...(options.rules ?? {}) };
  const rootId = options.rootId ?? '<isolated-root>';

  if (!Array.isArray(entries)) {
    throw new PlanValidationError('manifest must be an array of entries', [
      { code: VIOLATION.PATH_EMPTY_OR_NON_STRING, detail: 'manifest is not an array' },
    ]);
  }

  const { byKey, violations: targetViolations } = indexTargetTree(targetTree, rules);
  const violations = [...targetViolations];

  // Phase 1 — per-entry structural validation, type gate and root ownership.
  /**
   * @type {Array<{
   *   entryIndex: number,
   *   entryPath: string,
   *   targetPath: string,
   *   components: string[],
   *   key: string,
   *   type: 'file'|'directory',
   *   contents?: unknown,
   * }>}
   */
  const accepted = [];

  entries.forEach((entry, entryIndex) => {
    const entryPath = entry && entry.entryPath;

    if (!entry || (entry.type !== ENTRY_TYPE.FILE && entry.type !== ENTRY_TYPE.DIRECTORY)) {
      violations.push({
        code: VIOLATION.UNSUPPORTED_ENTRY_TYPE,
        entryIndex,
        entryPath: typeof entryPath === 'string' ? entryPath : undefined,
        detail: `unsupported entry type: ${entry == null ? String(entry) : String(entry.type)}; only ordinary files and directories are allowed`,
      });
      return;
    }

    const check = checkEntryPath(entryPath, rules);
    if (!check.ok) {
      violations.push({ code: check.reason, entryIndex, entryPath });
      return;
    }

    // Defense in depth: recompute the target and assert containment directly.
    if (!isInsideRoot(check.components, rules)) {
      violations.push({
        code: VIOLATION.OUTSIDE_ROOT,
        entryIndex,
        entryPath,
        targetPath: check.normalizedPath,
        detail: 'computed target escapes or does not belong to the isolated root',
      });
      return;
    }

    accepted.push({
      entryIndex,
      entryPath: check.normalizedPath,
      targetPath: check.normalizedPath,
      components: check.components,
      key: comparisonKeyForPath(check.components, rules),
      type: entry.type,
      ...(entry.type === ENTRY_TYPE.FILE ? { contents: entry.contents } : {}),
    });
  });

  // Phase 2 — collisions *within the batch*.
  /** @type {Map<string, number>} key -> index of first accepted entry */
  const seenInBatch = new Map();
  /** @type {Map<string, 'file'|'directory'>} all paths claimed in the batch */
  const batchByKey = new Map(accepted.map((e) => [e.key, e.type]));

  for (const e of accepted) {
    const first = seenInBatch.get(e.key);
    if (first !== undefined) {
      violations.push({
        code: VIOLATION.DUPLICATE_ENTRY,
        entryIndex: e.entryIndex,
        entryPath: e.entryPath,
        targetPath: e.targetPath,
        detail: `same target path as entry index ${first} under the declared normalization/case rules`,
      });
      continue;
    }
    seenInBatch.set(e.key, e.entryIndex);

    // A file entry sitting on an ancestor path forbids anything beneath it.
    for (const ancestor of ancestorChains(e.components)) {
      const ancestorKey = comparisonKeyForPath(ancestor, rules);
      if (batchByKey.get(ancestorKey) === ENTRY_TYPE.FILE) {
        violations.push({
          code: VIOLATION.FILE_ANCESTOR_CONFLICT,
          entryIndex: e.entryIndex,
          entryPath: e.entryPath,
          targetPath: e.targetPath,
          detail: `cannot place entry beneath a file entry: ${ancestor.join(rules.separator)}`,
        });
      }
    }
  }

  // Phase 3 — collisions against the already-existing virtual target tree.
  for (const e of accepted) {
    const existing = byKey.get(e.key);
    if (existing) {
      if (existing.type !== e.type) {
        violations.push({
          code: VIOLATION.TARGET_TYPE_CONFLICT,
          entryIndex: e.entryIndex,
          entryPath: e.entryPath,
          targetPath: e.targetPath,
          detail: `target already has a ${existing.type} at this path but entry is a ${e.type}`,
        });
      }
      // Same type is compatible: directories merge, identical files become
      // no-ops rather than blind overwrites.
    }

    for (const ancestor of ancestorChains(e.components)) {
      const ancestorKey = comparisonKeyForPath(ancestor, rules);
      if (byKey.get(ancestorKey)?.type === ENTRY_TYPE.FILE) {
        violations.push({
          code: VIOLATION.TARGET_ANCESTOR_IS_FILE,
          entryIndex: e.entryIndex,
          entryPath: e.entryPath,
          targetPath: e.targetPath,
          detail: `existing target file blocks this path: ${ancestor.join(rules.separator)}`,
        });
      }
    }
  }

  if (violations.length > 0) {
    violations.sort(
      (a, b) =>
        (a.entryIndex ?? -1) - (b.entryIndex ?? -1) ||
        String(a.entryPath ?? '').localeCompare(String(b.entryPath ?? '')) ||
        a.code.localeCompare(b.code),
    );
    throw new PlanValidationError(
      `write plan rejected with ${violations.length} violation(s)`,
      violations,
    );
  }

  // Phase 4 — assemble the deterministic plan.
  //
  // Every ancestor directory of every accepted entry is claimed first
  // (created only when missing); directories sort before files, each group
  // ascending by comparison key, so re-running on the same inputs yields a
  // byte-for-byte identical plan.
  /** @type {Map<string, {type: 'directory', components: string[], key: string, explicit: boolean, entryPath: string|null}>} */
  const dirByKey = new Map();
  /** @type {Map<string, (typeof accepted)[number]>} */
  const fileByKey = new Map();

  // Pass 1: claim every explicitly listed entry first, so an explicit
  // directory entry is never mistaken for an implicitly-created ancestor.
  for (const e of accepted) {
    if (e.type === ENTRY_TYPE.DIRECTORY) {
      dirByKey.set(e.key, {
        type: ENTRY_TYPE.DIRECTORY,
        components: e.components,
        key: e.key,
        explicit: true,
        entryPath: e.targetPath,
      });
    } else {
      fileByKey.set(e.key, e);
    }
  }

  // Pass 2: materialize missing ancestor directories of every accepted entry.
  for (const e of accepted) {
    for (const ancestor of ancestorChains(e.components)) {
      const key = comparisonKeyForPath(ancestor, rules);
      if (!dirByKey.has(key)) {
        dirByKey.set(key, {
          type: ENTRY_TYPE.DIRECTORY,
          components: ancestor,
          key,
          explicit: false,
          entryPath: null,
        });
      }
    }
  }

  /** @type {Array<object>} */
  const operations = [];

  for (const dir of [...dirByKey.values()].sort((a, b) => a.key.localeCompare(b.key))) {
    const targetPath = dir.components.map((c) => c).join(rules.separator);
    const existing = byKey.get(dir.key);
    operations.push({
      action: existing ? 'noop' : 'mkdir',
      type: ENTRY_TYPE.DIRECTORY,
      targetPath,
      entryPath: dir.entryPath,
    });
  }

  for (const file of [...fileByKey.values()].sort((a, b) => a.key.localeCompare(b.key))) {
    const existing = byKey.get(file.key);
    operations.push({
      action: existing ? 'noop' : 'writeFile',
      type: ENTRY_TYPE.FILE,
      targetPath: file.targetPath,
      entryPath: file.entryPath,
      contents: file.contents,
    });
  }

  return { rootId, rules: { ...rules }, operations };
}

export { comparisonKey };
