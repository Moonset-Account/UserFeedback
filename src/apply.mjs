import { isDeepStrictEqual } from 'node:util';
import { analyzePath as analyzePathExport } from './compile.mjs';
import { TransactionError, errorResult } from './errors.mjs';
import {
  initializeIdentities,
  cloneDocument,
  materialize,
  getIdentityList,
  createIdentity,
  assertUniqueIdentities,
  captureIdentityTree,
  restoreIdentityTree,
} from './document.mjs';

function resolveRoute(root, route = []) {
  let current = root;
  for (const step of route) {
    if (step.kind === 'key') {
      if (current == null || typeof current !== 'object' || Array.isArray(current)) {
        throw new TransactionError('PathNotFound', `Expected object at ${step.key}`);
      }
      current = current[step.key];
    } else if (step.kind === 'element') {
      if (!Array.isArray(current)) {
        throw new TransactionError('NotAnArray', 'Identity route step expected an array');
      }
      const ids = getIdentityList(current);
      const index = ids.indexOf(step.id);
      if (index < 0) {
        throw new TransactionError('IdentityNotFound', `Bound element identity is absent: ${step.id}`);
      }
      current = current[index];
    } else {
      throw new TransactionError('InvalidRoute', `Cannot resolve route step ${step.kind}`);
    }
  }
  return current;
}

function resolveTarget(root, op) {
  const route = op.route ?? (op.path !== undefined ? analyzePathExport(root, op.path).route : []);
  if (route.length === 0) return { root: true };
  const parentRoute = route.slice(0, -1);
  const last = route.at(-1);
  const parent = resolveRoute(root, parentRoute);
  if (last.kind === 'key' || last.kind === 'missingKey') {
    return { parent, key: last.key, value: parent?.[last.key] };
  }
  if (last.kind === 'element') {
    if (!Array.isArray(parent)) throw new TransactionError('NotAnArray', 'Target parent is not an array');
    const ids = getIdentityList(parent);
    const index = ids.indexOf(last.id);
    if (index < 0) throw new TransactionError('IdentityNotFound', `Target identity absent: ${last.id}`);
    return { parent, array: parent, ids, index, id: last.id, value: parent[index] };
  }
  throw new TransactionError('InvalidRoute', 'Unsupported target route');
}

function runTests(root, tests = []) {
  for (const test of tests) {
    const analysis = analyzePathExport(root, test.path);
    if (test.kind === 'absent') {
      if (analysis.exists) {
        throw new TransactionError('TestFailed', 'composed/patch absent test failed', { path: test.path });
      }
    } else if (test.kind === 'exists') {
      if (!analysis.exists) {
        throw new TransactionError('TestFailed', 'composed existence test failed', { path: test.path });
      }
    } else if (test.kind === 'arrayLength') {
      if (!analysis.exists || !Array.isArray(analysis.value) || analysis.value.length !== test.length) {
        throw new TransactionError('TestFailed', 'composed array-length condition failed', {
          path: test.path, expectedLength: test.length,
          actualLength: Array.isArray(analysis.value) ? analysis.value.length : null,
        });
      }
    } else {
      if (!analysis.exists) {
        throw new TransactionError('TestFailed', 'patch value test path is missing', { path: test.path });
      }
      if (!isDeepStrictEqual(analysis.value, test.expected)) {
        throw new TransactionError('TestFailed', 'patch value test failed', {
          path: test.path, expected: test.expected, actual: analysis.value,
        });
      }
    }
  }
}

function makeCursorState(ops) {
  const cursors = new Map();
  for (const op of ops) {
    const gap = op.gap ?? op.destination;
    if ((op.kind === 'insertArray' || op.kind === 'move') && op.gapKey && gap) {
      if (!cursors.has(op.gapKey)) {
        cursors.set(op.gapKey, { arrayPath: gap.arrayPath, index: gap.index });
      }
    }
  }
  return { cursors, globalRefs: new Map(), globalRefsPrepared: false };
}

function prepareGlobalRefs(root, ops, state) {
  if (state.globalRefsPrepared) return;
  state.globalRefsPrepared = true;
  for (const op of ops) {
    if (op.kind !== 'arrayTransform') continue;
    for (const ref of op.order ?? []) {
      if (ref.kind !== 'globalInitial' || state.globalRefs.has(ref.initialPath)) continue;
      const analysis = analyzePathExport(root, ref.initialPath);
      if (!analysis.exists || !analysis.targetId) {
        throw new TransactionError('IdentityNotFound', 'global initial identity reference cannot be bound', {
          initialPath: ref.initialPath,
        });
      }
      state.globalRefs.set(ref.initialPath, { id: analysis.targetId, value: analysis.value });
    }
  }
}

function transformCursors(state, arrayPath, change) {
  for (const cursor of state.cursors.values()) {
    if (cursor.arrayPath !== arrayPath) continue;
    if (change.type === 'insert') {
      if (cursor.index >= change.index) cursor.index += change.count;
    } else if (change.type === 'remove') {
      const end = change.index + change.count;
      if (cursor.index > change.index && cursor.index < end) cursor.index = change.index;
      else if (cursor.index >= end) cursor.index -= change.count;
    }
  }
}

function freshValue(value, preserveValue, identityTree = null) {
  const result = materialize(value, { preserve: preserveValue === true });
  if (identityTree) restoreIdentityTree(result, identityTree);
  return result;
}

function executeOne(root, op, state) {
  const ledgerEntry = { opIndex: op.opIndex ?? null, forwardKind: op.kind };

  if (op.kind === 'remove') {
    const route = op.route ?? (op.path !== undefined ? analyzePathExport(root, op.path).route : []);
    const target = resolveTarget(root, { ...op, route });
    if (target.root) throw new TransactionError('UnsupportedOperation', 'Removing the document root is not supported');
    if (target.array) {
      const { array, ids, index, id } = target;
      const oldValue = cloneDocument(array[index]);
      ledgerEntry.undo = {
        kind: 'insertArray',
        opIndex: op.opIndex,
        arrayRoute: route.slice(0, -1),
        fixedIndex: index,
        newId: id,
        value: oldValue,
        identityTree: captureIdentityTree(oldValue),
        preserveValue: true,
        inverse: true,
      };
      array.splice(index, 1);
      ids.splice(index, 1);
      transformCursors(state, op.path.replace(/\/[^/]*$/, ''), { type: 'remove', index, count: 1 });
    } else {
      const { parent, key } = target;
      const oldValue = cloneDocument(parent[key]);
      ledgerEntry.undo = {
        kind: 'insertKey',
        parentRoute: route.slice(0, -1),
        key,
        value: oldValue,
        identityTree: captureIdentityTree(oldValue),
        preserveValue: true,
        inverse: true,
      };
      delete parent[key];
    }
    return { root, ledgerEntry };
  }

  if (op.kind === 'replace') {
    const replacement = freshValue(op.value, op.preserveValue, op.identityTree);
    const route = op.route ?? (op.path !== undefined ? analyzePathExport(root, op.path).route : []);
    if (route.length === 0) {
      const oldValue = cloneDocument(root);
      ledgerEntry.undo = {
        ...op,
        route: [],
        value: oldValue,
        identityTree: captureIdentityTree(oldValue),
        preserveValue: true,
        inverse: true,
      };
      return { root: replacement, ledgerEntry };
    }
    const target = resolveTarget(root, { ...op, route });
    const oldValue = cloneDocument(target.value);
    ledgerEntry.undo = {
      kind: 'replace',
      route,
      path: op.path,
      value: oldValue,
      identityTree: captureIdentityTree(oldValue),
      preserveValue: true,
      inverse: true,
    };
    if (target.array) target.array[target.index] = replacement;
    else target.parent[target.key] = replacement;
    return { root, ledgerEntry };
  }

  if (op.kind === 'insertKey') {
    const parentRoute = op.parentRoute ?? analyzePathExport(root, op.parentPath ?? op.path?.replace(/\/[^/]*$/, '') ?? '').route;
    const parent = resolveRoute(root, parentRoute);
    const value = freshValue(op.value, op.preserveValue, op.identityTree);
    ledgerEntry.undo = { kind: 'removeKey', parentRoute, key: op.key, opIndex: op.opIndex };
    parent[op.key] = value;
    return { root, ledgerEntry };
  }

  if (op.kind === 'removeKey') {
    const parentRoute = op.parentRoute ?? analyzePathExport(root, op.parentPath ?? '').route;
    const parent = resolveRoute(root, parentRoute);
    const oldValue = cloneDocument(parent[op.key]);
    ledgerEntry.undo = {
      kind: 'insertKey', parentRoute, key: op.key,
      value: oldValue,
      identityTree: captureIdentityTree(oldValue),
      preserveValue: true, inverse: true,
    };
    delete parent[op.key];
    return { root, ledgerEntry };
  }

  if (op.kind === 'insertArray') {
    const array = resolveRoute(root, op.arrayRoute);
    if (!Array.isArray(array)) throw new TransactionError('NotAnArray', 'insertArray route did not resolve to an array');
    const ids = getIdentityList(array);
    const cursor = state.cursors.get(op.gapKey);
    const index = Number.isInteger(op.fixedIndex)
      ? op.fixedIndex
      : (cursor?.index ?? op.gap?.index ?? array.length);
    if (index < 0 || index > array.length) {
      throw new TransactionError('InvalidArrayIndex', 'insertion index is out of bounds', { index, length: array.length });
    }
    const value = freshValue(op.value, op.preserveValue, op.identityTree);
    const newId = op.newId ?? op.explicitId ?? createIdentity();
    array.splice(index, 0, value);
    ids.splice(index, 0, newId);
    transformCursors(state, op.gap?.arrayPath ?? op.arrayPath ?? '', { type: 'insert', index, count: 1 });
    ledgerEntry.undo = {
      kind: 'remove',
      route: [...(op.arrayRoute ?? []), { kind: 'element', id: newId }],
      path: op.path,
      opIndex: op.opIndex,
      inverse: true,
    };
    return { root, ledgerEntry };
  }

  if (op.kind === 'move') {
    const sourceArray = resolveRoute(root, op.sourceArrayRoute);
    if (!Array.isArray(sourceArray)) throw new TransactionError('NotAnArray', 'move source did not resolve to an array');
    const sourceIds = getIdentityList(sourceArray);
    const restoreIndex = sourceIds.indexOf(op.sourceIds[0]);
    if (restoreIndex < 0) throw new TransactionError('IdentityNotFound', 'moved source identity not found');

    ledgerEntry.undo = {
      kind: 'moveBack',
      opIndex: op.opIndex,
      fromArrayRoute: op.destination.arrayRoute,
      toArrayRoute: op.sourceArrayRoute,
      movedIds: [...op.sourceIds],
      restoreIndex,
      inverse: true,
    };

    const entries = op.sourceIds.map((id) => {
      const index = sourceIds.indexOf(id);
      if (index < 0) throw new TransactionError('IdentityNotFound', `move source identity missing: ${id}`);
      return { id, index, value: cloneDocument(sourceArray[index]) };
    }).sort((a, b) => b.index - a.index);

    const sourcePathKey = op.sourcePath ?? op.destination.arrayPath;
    for (const entry of entries) {
      sourceArray.splice(entry.index, 1);
      sourceIds.splice(entry.index, 1);
      transformCursors(state, sourcePathKey, { type: 'remove', index: entry.index, count: 1 });
    }

    const destinationArray = resolveRoute(root, op.destination.arrayRoute);
    if (!Array.isArray(destinationArray)) throw new TransactionError('NotAnArray', 'move destination did not resolve to an array');
    const destinationIds = getIdentityList(destinationArray);
    const cursor = state.cursors.get(op.gapKey);
    const insertIndex = cursor?.index ?? op.destination.index;
    if (insertIndex < 0 || insertIndex > destinationArray.length) {
      throw new TransactionError('InvalidArrayIndex', 'move destination out of bounds', { insertIndex });
    }
    const ordered = op.sourceIds.map((id) => entries.find((e) => e.id === id).value);
    destinationArray.splice(insertIndex, 0, ...ordered);
    destinationIds.splice(insertIndex, 0, ...op.sourceIds);
    transformCursors(state, op.destination.arrayPath, { type: 'insert', index: insertIndex, count: op.sourceIds.length });
    return { root, ledgerEntry };
  }

  if (op.kind === 'moveBack') {
    const fromArray = resolveRoute(root, op.fromArrayRoute);
    const toArray = resolveRoute(root, op.toArrayRoute);
    if (!Array.isArray(fromArray) || !Array.isArray(toArray)) {
      throw new TransactionError('NotAnArray', 'moveBack routes must resolve to arrays');
    }
    const fromIds = getIdentityList(fromArray);
    const entries = op.movedIds.map((id) => {
      const index = fromIds.indexOf(id);
      if (index < 0) throw new TransactionError('IdentityNotFound', `inverse move identity missing: ${id}`);
      return { id, index, value: cloneDocument(fromArray[index]) };
    }).sort((a, b) => b.index - a.index);
    for (const entry of entries) {
      fromArray.splice(entry.index, 1);
      fromIds.splice(entry.index, 1);
    }
    const targetIds = getIdentityList(toArray);
    const insertIndex = Math.max(0, Math.min(op.restoreIndex, toArray.length));
    const ordered = op.movedIds.map((id) => entries.find((e) => e.id === id).value);
    toArray.splice(insertIndex, 0, ...ordered);
    targetIds.splice(insertIndex, 0, ...op.movedIds);
    return { root, ledgerEntry };
  }

  if (op.kind === 'arrayTransform') {
    const arrayRoute = op.arrayRoute ?? analyzePathExport(root, op.arrayPath).route;
    const array = resolveRoute(root, arrayRoute);
    if (!Array.isArray(array)) throw new TransactionError('NotAnArray', 'arrayTransform path is not an array');
    const ids = getIdentityList(array);
    prepareGlobalRefs(root, state.ops, state);
    const oldArray = cloneDocument(array);
    const oldRoute = arrayRoute;
    const nextValues = [];
    const nextIds = [];
    for (const ref of op.order) {
      if (ref.kind === 'initial') {
        if (ref.index < 0 || ref.index >= array.length) {
          throw new TransactionError('InvalidArrayIndex', 'arrayTransform initial index out of bounds', ref);
        }
        nextValues.push(array[ref.index]);
        nextIds.push(ids[ref.index]);
      } else if (ref.kind === 'initialReset') {
        if (ref.index < 0 || ref.index >= array.length) {
          throw new TransactionError('InvalidArrayIndex', 'arrayTransform reset index out of bounds', ref);
        }
        nextValues.push(freshValue(ref.value, false));
        nextIds.push(ids[ref.index]);
      } else if (ref.kind === 'globalInitial') {
        const binding = state.globalRefs.get(ref.initialPath);
        if (!binding) throw new TransactionError('IdentityNotFound', 'missing global identity binding', ref);
        nextValues.push(binding.value);
        nextIds.push(binding.id);
      } else if (ref.kind === 'new') {
        nextValues.push(freshValue(ref.value, false));
        nextIds.push(ref.id ?? createIdentity());
      } else {
        throw new TransactionError('InvalidOperation', `Unknown arrayTransform ref ${ref.kind}`);
      }
    }
    ledgerEntry.undo = {
      kind: 'replace',
      route: oldRoute,
      path: op.arrayPath,
      value: oldArray,
      identityTree: captureIdentityTree(oldArray),
      preserveValue: true,
      inverse: true,
    };
    array.splice(0, array.length, ...nextValues);
    ids.splice(0, ids.length, ...nextIds);
    return { root, ledgerEntry };
  }

  // Diff/composition operations are handled here or in compose.mjs.
  if (op.kind === 'identityReplace') {
    throw new TransactionError('UnsupportedOperation', `Composition op should be executed by applyComposedPatch: ${op.kind}`);
  }

  throw new TransactionError('InvalidOperation', `Cannot apply operation: ${op.kind}`);
}

export function applyPatch(doc, patch, options = {}) {
  initializeIdentities(doc);
  const root = cloneDocument(doc);
  const tests = options.skipTests ? [] : (patch?.tests ?? []);
  runTests(root, tests);
  const ops = Array.isArray(patch) ? patch : (patch?.ops ?? []);
  const state = makeCursorState(ops);
  state.ops = ops;
  const ledger = [];
  let current = root;
  for (const op of ops) {
    const result = executeOne(current, op, state);
    current = result.root;
    if (options.record) ledger.push(result.ledgerEntry.undo);
  }
  if (options.assertUnique !== false) assertUniqueIdentities(current);
  if (options.record) return { doc: current, ledger };
  return current;
}

export function tryApplyPatch(doc, patch, options = {}) {
  try {
    return { ok: true, doc: applyPatch(doc, patch, options), patch };
  } catch (error) {
    return errorResult(error, { doc });
  }
}

export function invertPatch(before, patch) {
  const { ledger } = applyPatch(before, patch, { record: true });
  const ops = ledger.reverse().filter(Boolean);
  return {
    protocol: 'structural-patch/v1',
    kind: 'inverse-patch',
    tests: [],
    ops,
    generatedFromActualPreimage: true,
  };
}
