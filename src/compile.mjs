import { isDeepStrictEqual } from 'node:util';
import {
  parsePath,
  formatPath,
  asArrayIndex,
  isPlainObject,
  pathStartsWith,
} from './paths.mjs';
import { TransactionError } from './errors.mjs';
import {
  IDENTITIES,
  createIdentity,
  initializeIdentities,
  materialize,
  cloneDocument,
  getIdentityList,
} from './document.mjs';

const WRITE_OPS = new Set(['remove', 'replace', 'insert', 'move']);

function normOp(op, index) {
  if (!op || typeof op !== 'object') {
    throw new TransactionError('InvalidOperation', `Operation ${index} must be an object`);
  }
  const type = op.op ?? op.type ?? op.operation;
  const alias = {
    del: 'remove',
    delete: 'remove',
    rm: 'remove',
    set: 'replace',
    add: 'insert',
    insert: 'insert',
    move: 'move',
    test: 'test',
    assert: 'test',
    testAbsent: 'testAbsent',
    absent: 'testAbsent',
  };
  const kind = alias[type] ?? type;
  if (!kind) throw new TransactionError('InvalidOperation', `Operation ${index} has no op/type`);
  return { ...op, kind, opIndex: index };
}

export function normalizeTransaction(input) {
  const rawOps = Array.isArray(input) ? input : (input?.operations ?? input?.ops ?? []);
  if (!Array.isArray(rawOps)) throw new TransactionError('InvalidTransaction', 'operations must be an array');
  return {
    id: input?.id ?? null,
    description: input?.description ?? '',
    operations: rawOps.map(normOp),
  };
}

function makeRoute() {
  return [];
}

export function analyzePath(root, rawPath) {
  const segments = parsePath(rawPath);
  const canonical = formatPath(segments);
  let current = root;
  const route = makeRoute();
  const bindings = [];
  let exists = true;
  let finalKind = 'missing';
  let failure = null;

  for (let i = 0; i < segments.length; i += 1) {
    const seg = segments[i];
    const last = i === segments.length - 1;

    if (Array.isArray(current)) {
      const len = current.length;
      const idx = asArrayIndex(seg, len);
      if (idx === null) {
        failure = { code: 'InvalidArrayIndex', segment: seg, index: i };
        exists = false;
        break;
      }
      if (idx >= len) {
        if (idx === len && last) {
          exists = false;
          finalKind = 'array-end-gap';
          route.push({ kind: 'gap', index: idx, beforeId: null });
          break;
        }
        failure = { code: 'PathNotFound', segment: seg, index: i };
        exists = false;
        break;
      }
      const ids = getIdentityList(current);
      const id = ids[idx];
      const step = { kind: 'element', id, initialIndex: idx };
      route.push(step);
      bindings.push({ path: formatPath(segments.slice(0, i + 1)), index: idx, id });
      current = current[idx];
      if (last) {
        finalKind = 'element';
        exists = true;
      }
      continue;
    }

    if (isPlainObject(current)) {
      if (Object.hasOwn(current, seg)) {
        route.push({ kind: 'key', key: seg });
        current = current[seg];
        if (last) {
          finalKind = 'key';
          exists = true;
        }
        continue;
      }
      if (last) {
        exists = false;
        finalKind = 'missing-key';
        route.push({ kind: 'missingKey', key: seg });
        break;
      }
      failure = { code: 'PathNotFound', segment: seg, index: i };
      exists = false;
      break;
    }

    failure = { code: 'PathNotFound', segment: seg, index: i };
    exists = false;
    break;
  }

  if (segments.length === 0) {
    exists = true;
    finalKind = 'root';
  }

  const parentRoute = finalKind === 'missing-key' || finalKind === 'array-end-gap'
    ? route.slice(0, -1)
    : route.slice(0, -1);
  const lastRoute = route.at(-1) ?? null;
  const allIdentityIds = bindings.map((b) => b.id);
  return {
    segments,
    canonical,
    exists,
    value: exists ? current : undefined,
    route,
    parentRoute,
    lastRoute,
    bindings,
    allIdentityIds,
    targetId: finalKind === 'element' ? lastRoute?.id : null,
    nearestElementId: bindings.at(-1)?.id ?? null,
    outermostElementId: bindings[0]?.id ?? null,
    finalKind,
    failure,
  };
}

function requireExistingAnalysis(analysis, op) {
  if (analysis.failure) {
    throw new TransactionError(analysis.failure.code, `Invalid path for ${op.kind}: ${analysis.canonical}`, {
      path: analysis.canonical,
      ...analysis.failure,
    });
  }
  if (!analysis.exists) {
    throw new TransactionError('PathNotFound', `${op.kind} requires an existing path: ${analysis.canonical}`, {
      path: analysis.canonical,
    });
  }
}

function analyzeGap(root, op, pathValue) {
  let fullPath;
  let at;
  if (op.at !== undefined && (pathValue !== undefined || op.arrayPath !== undefined)) {
    fullPath = pathValue ?? op.arrayPath ?? op.path;
    at = op.at;
  } else {
    fullPath = pathValue ?? op.path;
  }

  const segments = parsePath(fullPath);
  let arraySegments;
  let gapIndex;
  if (at !== undefined) {
    arraySegments = segments;
    gapIndex = Number(at);
  } else {
    if (segments.length === 0) {
      throw new TransactionError('InvalidPath', 'Array gap path must include an index or "-"');
    }
    arraySegments = segments.slice(0, -1);
    const last = segments.at(-1);
    if (last === '-') gapIndex = Infinity;
    else if (/^(0|[1-9]\d*)$/.test(String(last))) gapIndex = Number(last);
    else {
      // Path may point directly at an array and use at; otherwise final is bad.
      arraySegments = segments;
      gapIndex = op.index ?? op.at;
    }
  }

  const arrayAnalysis = analyzePath(root, arraySegments);
  requireExistingAnalysis(arrayAnalysis, { kind: op.kind });
  if (!Array.isArray(arrayAnalysis.value)) {
    throw new TransactionError('NotAnArray', 'Insert/move destination must be an array gap', {
      path: arrayAnalysis.canonical,
    });
  }
  const len = arrayAnalysis.value.length;
  let idx = gapIndex === Infinity ? len : gapIndex;
  if (!Number.isInteger(idx) || idx < 0 || idx > len) {
    throw new TransactionError('InvalidArrayIndex', 'Gap index is out of bounds', {
      path: formatPath(arraySegments),
      index: gapIndex,
      length: len,
    });
  }
  const ids = getIdentityList(arrayAnalysis.value);
  return {
    arrayPath: arrayAnalysis.canonical,
    arraySegments,
    arrayRoute: arrayAnalysis.route,
    index: idx,
    beforeId: idx < len ? ids[idx] : null,
    gapKey: `${arrayAnalysis.canonical}\u0000gap\u0000${idx}\u0000${idx < len ? ids[idx] : 'END'}`,
  };
}

function normalizeMove(root, op) {
  const count = op.count ?? op.length ?? 1;
  if (!Number.isInteger(count) || count <= 0) {
    throw new TransactionError('InvalidMove', 'move count must be a positive integer', { op });
  }

  let arrayPath;
  let start;
  if (typeof op.from === 'number' && op.path !== undefined) {
    arrayPath = op.path;
    start = op.from;
  } else {
    const fromSegments = parsePath(op.from ?? op.source);
    if (fromSegments.length === 0) throw new TransactionError('InvalidMove', 'move.from is required');
    const last = fromSegments.at(-1);
    start = Number(last);
    arrayPath = formatPath(fromSegments.slice(0, -1));
  }
  if (!Number.isInteger(start) || start < 0) {
    throw new TransactionError('InvalidMove', 'move start must be a non-negative integer', { start });
  }

  const sourceArrayAnalysis = analyzePath(root, arrayPath);
  requireExistingAnalysis(sourceArrayAnalysis, op);
  if (!Array.isArray(sourceArrayAnalysis.value)) {
    throw new TransactionError('NotAnArray', 'move source must be in an array', { path: arrayPath });
  }
  const sourceArray = sourceArrayAnalysis.value;
  if (start + count > sourceArray.length) {
    throw new TransactionError('InvalidMove', 'move fragment exceeds array bounds', {
      path: arrayPath, start, count, length: sourceArray.length,
    });
  }
  const sourceIds = getIdentityList(sourceArray).slice(start, start + count);
  const sourcePaths = sourceIds.map((id, offset) => formatPath([...parsePath(arrayPath), String(start + offset)]));

  let destinationInput = op.to ?? op.destination ?? op.path;
  let destination;
  if (typeof op.from === 'number' && typeof destinationInput === 'number') {
    destination = analyzeGap(root, { kind: 'move', at: destinationInput }, arrayPath);
  } else if (typeof destinationInput === 'number') {
    destination = analyzeGap(root, { kind: 'move', at: destinationInput }, arrayPath);
  } else {
    destination = analyzeGap(root, { kind: 'move', ...(op.at !== undefined ? { at: op.at } : {}) }, destinationInput);
  }

  // Moving a fragment to an internal gap is ambiguous; boundary gaps are no-op safe.
  const destArraySame = destination.arrayPath === sourceArrayAnalysis.canonical;
  if (destArraySame && destination.index > start && destination.index < start + count) {
    throw new TransactionError('InvalidMove', 'destination gap is inside the moved fragment', {
      arrayPath, start, count, destination: destination.index,
    });
  }

  return {
    arrayPath: sourceArrayAnalysis.canonical,
    arrayRoute: sourceArrayAnalysis.route,
    start,
    count,
    sourceIds,
    sourcePaths,
    destination,
  };
}

function checkTest(root, op, index) {
  const absent = op.absent === true || op.state === 'missing' || op.kind === 'testAbsent' || op.value === undefined && op.absent !== false && op.kind === 'testAbsent';
  const analysis = analyzePath(root, op.path);
  if (analysis.failure && !absent) {
    throw new TransactionError(analysis.failure.code, `Test path is invalid: ${analysis.canonical}`, {
      operationIndex: index, path: analysis.canonical,
    });
  }
  if (absent) {
    if (analysis.exists) {
      throw new TransactionError('TestFailed', 'expected path to be missing', {
        operationIndex: index, path: analysis.canonical, actual: analysis.value,
      });
    }
    return { opIndex: index, kind: 'absent', path: analysis.canonical };
  }
  if (!analysis.exists) {
    throw new TransactionError('TestFailed', 'expected value but path is missing', {
      operationIndex: index, path: analysis.canonical, expected: op.value,
    });
  }
  if (!isDeepStrictEqual(stripMeta(analysis.value), op.value)) {
    throw new TransactionError('TestFailed', 'old value test failed', {
      operationIndex: index,
      path: analysis.canonical,
      expected: op.value,
      actual: stripMeta(analysis.value),
    });
  }
  return { opIndex: index, kind: 'value', path: analysis.canonical, expected: op.value };
}

function stripMeta(value) {
  // isDeepStrictEqual ignores symbol-keyed properties, so this is mainly for error JSON.
  if (Array.isArray(value)) return value.map(stripMeta);
  if (isPlainObject(value)) return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, stripMeta(v)]));
  return value;
}

function buildWriteDescriptor(root, op, index) {
  if (op.kind === 'remove') {
    const a = analyzePath(root, op.path);
    requireExistingAnalysis(a, op);
    return {
      opIndex: index, kind: 'remove', analysis: a,
      path: a.canonical, route: a.route,
      destructive: true,
      identityId: a.nearestElementId,
      allIdentityIds: a.allIdentityIds,
      containerPath: formatPath(a.segments.slice(0, -1)),
    };
  }
  if (op.kind === 'replace') {
    const a = analyzePath(root, op.path);
    requireExistingAnalysis(a, op);
    return {
      opIndex: index, kind: 'replace', analysis: a,
      path: a.canonical, route: a.route,
      value: op.value,
      destructive: true,
      identityId: a.nearestElementId,
      allIdentityIds: a.allIdentityIds,
      containerPath: formatPath(a.segments.slice(0, -1)),
    };
  }
  if (op.kind === 'insert') {
    // Object insertion when final key is not an array index.
    const segments = parsePath(op.path);
    const parentSegments = segments.slice(0, -1);
    const last = segments.at(-1);
    const parentAnalysis = segments.length === 0 ? null : analyzePath(root, parentSegments);
    if (parentAnalysis?.exists && isPlainObject(parentAnalysis.value) && last !== undefined) {
      if (Object.hasOwn(parentAnalysis.value, last) && op.absent !== false) {
        throw new TransactionError('PathExists', 'insert object key already exists; use replace', {
          path: formatPath(segments),
        });
      }
      return {
        opIndex: index, kind: 'insertKey',
        path: formatPath(segments),
        parentPath: parentAnalysis.canonical,
        parentRoute: parentAnalysis.route,
        key: last,
        value: op.value,
        destructive: false,
        containerPath: parentAnalysis.canonical,
        gapKey: `key:${parentAnalysis.canonical}\u0000${last}`,
      };
    }

    const gap = analyzeGap(root, op);
    const explicitId = op.identity ?? op.elementId ?? null;
    return {
      opIndex: index, kind: 'insertArray',
      path: formatPath([...gap.arraySegments, String(gap.index)]),
      gap,
      value: op.value,
      newId: explicitId ?? createIdentity(),
      explicitId,
      destructive: false,
      containerPath: gap.arrayPath,
      gapKey: gap.gapKey,
    };
  }
  if (op.kind === 'move') {
    const m = normalizeMove(root, op);
    return {
      opIndex: index, kind: 'move',
      path: m.sourcePaths[0],
      move: m,
      destructive: true,
      identityId: m.sourceIds[0] ?? null,
      allIdentityIds: m.sourceIds,
      sourceIds: m.sourceIds,
      sourcePaths: m.sourcePaths,
      containerPath: m.arrayPath,
      gapKey: m.destination.gapKey,
    };
  }
  throw new TransactionError('InvalidOperation', `Unsupported op: ${op.kind}`, { operationIndex: index });
}

function pathsConflict(destructive, other) {
  const destroyedPaths = destructive.kind === 'move'
    ? destructive.sourcePaths
    : [destructive.path];
  const candidatePaths = other.kind === 'insertArray' || other.kind === 'move'
    ? [other.containerPath, other.path]
    : [other.path, other.containerPath];

  for (const dp of destroyedPaths) {
    for (const cp of candidatePaths) {
      if (!cp) continue;
      if (cp === dp || pathStartsWith(cp, dp)) return { type: 'ancestor', destroyed: dp, write: cp };
    }
  }

  const destroyedIds = new Set(destructive.kind === 'move' ? destructive.sourceIds : destructive.allIdentityIds ?? []);
  const otherIds = new Set(other.allIdentityIds ?? []);
  for (const id of destroyedIds) {
    if (otherIds.has(id)) return { type: 'identity', id };
  }
  return null;
}

function checkWriteConflicts(writes) {
  for (let i = 0; i < writes.length; i += 1) {
    for (let j = i + 1; j < writes.length; j += 1) {
      const a = writes[i];
      const b = writes[j];

      // Same-gap insertions are explicitly ordered, never rejected.
      const bothGapInserts = (a.kind === 'insertArray' || a.kind === 'move')
        && (b.kind === 'insertArray' || b.kind === 'move')
        && a.gapKey && a.gapKey === b.gapKey;
      if (bothGapInserts) {
        if (a.sourceIds && b.sourceIds) {
          const overlap = a.sourceIds.find((id) => b.sourceIds.includes(id));
          if (overlap) {
            throw new TransactionError('WriteConflict', 'transaction aborts: overlapping moved fragments', {
              operations: [a.opIndex, b.opIndex], identity: overlap,
            });
          }
        }
        continue;
      }

      if (a.destructive) {
        const conflict = pathsConflict(a, b);
        if (conflict) {
          throw new TransactionError('WriteConflict', 'transaction aborts: write conflicts with an overwritten/deleted identity or ancestor', {
            operations: [a.opIndex, b.opIndex], ...conflict,
          });
        }
      }
      if (b.destructive) {
        const conflict = pathsConflict(b, a);
        if (conflict) {
          throw new TransactionError('WriteConflict', 'transaction aborts: write conflicts with an overwritten/deleted identity or ancestor', {
            operations: [a.opIndex, b.opIndex], ...conflict,
          });
        }
      }

      // Two ordinary writes that touch any common bound array identity conflict.
      // The nearest-identity case is the common one; intersecting the full chain
      // also rejects sibling edits inside one identity-bearing aggregate.
      const ordinaryA = a.kind === 'remove' || a.kind === 'replace';
      const ordinaryB = b.kind === 'remove' || b.kind === 'replace';
      if (ordinaryA && ordinaryB) {
        const commonIdentity = (a.allIdentityIds ?? []).find((id) => (b.allIdentityIds ?? []).includes(id));
        if (commonIdentity) {
          throw new TransactionError('WriteConflict', 'transaction aborts: two writes touch the same hidden element identity', {
            operations: [a.opIndex, b.opIndex], identity: commonIdentity,
          });
        }
      }

      // Overlapping move sources always conflict.
      if (a.sourceIds && b.sourceIds) {
        const overlap = a.sourceIds.find((id) => b.sourceIds.includes(id));
        if (overlap) {
          throw new TransactionError('WriteConflict', 'transaction aborts: overlapping moved fragments', {
            operations: [a.opIndex, b.opIndex], identity: overlap,
          });
        }
      }
    }
  }
}

export function compileTransaction(doc, input) {
  initializeIdentities(doc);
  const tx = normalizeTransaction(input);
  const tests = [];
  const writes = [];
  const ops = [];

  for (let i = 0; i < tx.operations.length; i += 1) {
    const op = tx.operations[i];
    if (op.kind === 'test' || op.kind === 'testAbsent') {
      tests.push(checkTest(doc, op, i));
      ops.push({ opIndex: i, kind: 'test', testIndex: tests.length - 1 });
    } else if (WRITE_OPS.has(op.kind)) {
      const w = buildWriteDescriptor(doc, op, i);
      writes.push(w);
      ops.push(toExecutableOp(w));
    } else {
      throw new TransactionError('InvalidOperation', `Unsupported operation: ${op.kind}`, { operationIndex: i });
    }
  }
  checkWriteConflicts(writes);

  return {
    protocol: 'structural-patch/v1',
    kind: 'patch',
    transactionId: tx.id,
    description: tx.description,
    transaction: tx,
    tests,
    ops: ops.filter((op) => op.kind !== 'test'),
    operationOrder: tx.operations.map((op) => op.kind),
    compiledAt: new Date(0).toISOString().slice(0, 10), // stable placeholder, no preimage
  };
}

function toExecutableOp(w) {
  if (w.kind === 'remove') {
    return { opIndex: w.opIndex, kind: 'remove', path: w.path, route: w.route };
  }
  if (w.kind === 'replace') {
    return { opIndex: w.opIndex, kind: 'replace', path: w.path, route: w.route, value: w.value };
  }
  if (w.kind === 'insertArray') {
    return {
      opIndex: w.opIndex, kind: 'insertArray', path: w.path,
      arrayRoute: w.gap.arrayRoute, gap: { ...w.gap }, gapKey: w.gapKey,
      value: w.value, newId: w.newId,
    };
  }
  if (w.kind === 'insertKey') {
    return {
      opIndex: w.opIndex, kind: 'insertKey', path: w.path,
      parentRoute: w.parentRoute, key: w.key, value: w.value,
    };
  }
  if (w.kind === 'move') {
    return {
      opIndex: w.opIndex, kind: 'move', path: w.path,
      sourceArrayRoute: w.move.arrayRoute,
      sourcePath: w.move.arrayPath,
      start: w.move.start,
      count: w.move.count,
      sourceIds: w.move.sourceIds,
      destination: { ...w.move.destination },
      gapKey: w.gapKey,
    };
  }
  return w;
}
