import { isDeepStrictEqual } from 'node:util';
import { parsePath, formatPath, asArrayIndex, pathStartsWith } from './paths.mjs';
import {
  initializeIdentities,
  cloneDocument,
  findIdentityPath,
  getIdentityList,
  collectIdentityPaths,
} from './document.mjs';
import { runSequential } from './transaction.mjs';
import { applyPatch } from './apply.mjs';
import { analyzePath } from './compile.mjs';
import { diffDocuments } from './diff.mjs';
import { TransactionError, errorResult } from './errors.mjs';

function mapPostPath({ postDoc, initialDoc, path }) {
  const segments = parsePath(path);
  let current = postDoc;
  let initialPath = [];
  let fresh = null;
  const bindings = [];

  for (const segment of segments) {
    if (Array.isArray(current)) {
      const idx = asArrayIndex(segment, current.length);
      if (idx === null || idx >= current.length) {
        return {
          origin: 'missing-projection',
          initialPath,
          projectedPath: formatPath([...initialPath, segment]),
          bindings,
        };
      }
      const id = getIdentityList(current)[idx];
      bindings.push({ postIndex: idx, id });
      const sourcePath = findIdentityPath(initialDoc, id);
      if (sourcePath) {
        initialPath = [...sourcePath];
        fresh = null;
      } else {
        fresh = { id, suffix: [] };
      }
      current = current[idx];
      continue;
    }

    if (current != null && typeof current === 'object') {
      initialPath.push(segment);
      if (fresh) fresh.suffix.push(segment);
      current = current[segment];
    } else {
      initialPath.push(segment);
      return {
        origin: 'missing-projection',
        initialPath,
        projectedPath: formatPath(initialPath),
        bindings,
      };
    }
  }

  if (fresh) return { origin: 'fresh', freshId: fresh.id, suffix: fresh.suffix, bindings };
  return { origin: 'initial', initialPath, bindings };
}

function firstEffectPrefix(patch1, kind, path) {
  return (patch1.ops ?? []).find((op) => op.kind === kind && op.path && (path === op.path || pathStartsWith(path, op.path)));
}

function translateSecondTests(patch2, context) {
  const initialTests = [];
  const derivedTests = [];

  for (const test of patch2.tests ?? []) {
    const mapped = mapPostPath({
      postDoc: context.intermediate,
      initialDoc: context.initial,
      path: test.path,
    });
    const mappedInitialPath = mapped.origin === 'initial' ? formatPath(mapped.initialPath) : null;
    const replacement = firstEffectPrefix(context.patch1, 'replace', test.path)
      ?? (mappedInitialPath ? firstEffectPrefix(context.patch1, 'replace', mappedInitialPath) : null);
    if (replacement) {
      derivedTests.push({
        source: 'second',
        kind: test.kind,
        postPath: test.path,
        firstWritePath: replacement.path,
        expected: test.expected,
        representation: 'static-write-obligation',
        status: 'encoded-by-net-value',
      });
      continue;
    }

    const deletion = firstEffectPrefix(context.patch1, 'remove', test.path)
      ?? (mappedInitialPath ? firstEffectPrefix(context.patch1, 'remove', mappedInitialPath) : null);
    if (deletion && test.kind === 'absent') {
      derivedTests.push({
        source: 'second',
        kind: 'absent',
        postPath: test.path,
        firstDeletePath: deletion.path,
        representation: 'conditional-absence-from-delete',
        status: 'encoded-by-net-remove',
      });
      continue;
    }

    if (mapped.origin === 'initial') {
      const initialPath = mappedInitialPath;
      if (test.kind === 'value') {
        initialTests.push({
          kind: 'value',
          path: initialPath,
          expected: test.expected,
          origin: 'second-translated',
          postPath: test.path,
        });
        derivedTests.push({
          source: 'second',
          kind: 'value',
          postPath: test.path,
          representation: 'initial-path-test',
          initialPath,
        });
      } else {
        const initialAnalysis = context.initialAtPath?.(initialPath);
        const initiallyMissing = initialAnalysis ? !initialAnalysis.exists : false;
        if (initiallyMissing) {
          initialTests.push({ kind: 'absent', path: initialPath, origin: 'second-translated', postPath: test.path });
          derivedTests.push({
            source: 'second', kind: 'absent', postPath: test.path,
            representation: 'initial-absent-test', initialPath,
          });
        } else {
          derivedTests.push({
            source: 'second',
            kind: 'absent',
            postPath: test.path,
            representation: 'conditional-index-transform',
            initialPath,
            status: 'encoded-by-net-array-transform-or-remove',
          });
        }
      }
      continue;
    }

    if (mapped.origin === 'fresh') {
      const insert = (context.patch1.ops ?? []).find((op) => op.newId === mapped.freshId);
      derivedTests.push({
        source: 'second',
        kind: test.kind,
        postPath: test.path,
        createdByOperation: insert?.opIndex ?? null,
        representation: 'created-object-obligation',
        status: 'folded-into-net-inserted-value',
        suffix: mapped.suffix,
        expected: test.expected,
      });
      continue;
    }

    derivedTests.push({
      source: 'second',
      kind: test.kind,
      postPath: test.path,
      representation: 'conditional-index-transform',
      status: 'mapped-to-net-structural-transform',
      projectedPath: mapped.projectedPath,
    });
  }

  return { initialTests, derivedTests };
}

function buildIndexRules(patch2, context) {
  const rules = [];
  const references = [];
  for (const op of patch2.ops ?? []) {
    const routeIds = [];
    const collect = (route) => {
      for (const step of route ?? []) if (step.kind === 'element') routeIds.push(step.id);
    };
    collect(op.route);
    collect(op.sourceArrayRoute);
    collect(op.arrayRoute);
    collect(op.destination?.arrayRoute);

    const mappings = [];
    for (const id of new Set(routeIds)) {
      const initialPath = findIdentityPath(context.initial, id);
      if (initialPath) {
        mappings.push({ initialPath: formatPath(initialPath), state: 'initial-or-moved-by-first' });
        rules.push({
          secondOperation: op.opIndex,
          postPath: op.path ?? null,
          initialPath: formatPath(initialPath),
          binding: 'initial-path-index-to-hidden-identity',
          rule: 'follow that identity through the net arrayTransform',
        });
      } else {
        const insert = (context.patch1.ops ?? []).find((candidate) => candidate.newId === id);
        mappings.push({ state: 'created-by-first', createdByOperation: insert?.opIndex ?? null });
        rules.push({
          secondOperation: op.opIndex,
          postPath: op.path ?? null,
          createdByOperation: insert?.opIndex ?? null,
          binding: 'symbolic-created-element',
          rule: 'resolve inside the net inserted value',
        });
      }
    }
    references.push({ opIndex: op.opIndex, kind: op.kind, postPath: op.path ?? null, mappings });
  }
  return { rules, references };
}

function uniqueTests(tests) {
  const seen = new Set();
  const result = [];
  for (const test of tests) {
    const key = `${test.kind}:${test.path}:${test.length ?? ''}:${JSON.stringify(test.expected)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(test);
  }
  return result;
}

function structuralTestsForNetOps(netOps) {
  const tests = [];
  for (const op of netOps) {
    if (op.kind === 'arrayTransform') {
      tests.push({ kind: 'arrayLength', path: op.arrayPath, length: op.initialLength, origin: 'structural-index-binding' });
    } else if (op.kind === 'replace' || op.kind === 'remove') {
      if (op.path !== '') tests.push({ kind: 'exists', path: op.path, origin: 'structural-target' });
    } else if (op.kind === 'insertKey') {
      tests.push({ kind: 'exists', path: op.parentPath ?? op.path.replace(/\/[^/]*$/, ''), origin: 'structural-parent' });
    }
  }
  return tests;
}

export function composeTransactions(tx1, tx2) {
  return {
    protocol: 'symbolic-composition/v1',
    kind: 'symbolic-composition',
    tx1,
    tx2,
    reusable: true,
    note: 'Call applyComposition(doc, composition) to derive initial-snapshot tests and one net commit for that document.',
  };
}

export function planComposition(baseDoc, tx1, tx2) {
  initializeIdentities(baseDoc);
  const initial = cloneDocument(baseDoc);
  const sequential = runSequential(initial, tx1, tx2);
  if (!sequential.ok) {
    return {
      ok: false,
      protocol: 'symbolic-composition/v1',
      kind: 'failed-composition-plan',
      stage: sequential.stage,
      error: sequential.error,
    };
  }

  const patch1 = sequential.patch1;
  const patch2 = sequential.patch2;
  const intermediate = sequential.attempts[0].doc;
  const finalDoc = sequential.doc;
  const resetPaths = (patch1.ops ?? []).filter((op) => op.kind === 'replace').map((op) => op.path);
  const identityPaths = collectIdentityPaths(initial);
  const diffOps = diffDocuments(initial, finalDoc, [], { resetPaths, initialRoot: initial, identityPaths });
  // All path edits bind to the initial arrangement and must run before any
  // identity reordering transform. Transform relative order remains depth-first.
  const netOps = [
    ...diffOps.filter((op) => op.kind !== 'arrayTransform'),
    ...diffOps.filter((op) => op.kind === 'arrayTransform'),
  ];

  const context = {
    initial,
    intermediate,
    patch1,
    patch2,
    initialAtPath: (p) => analyzePath(initial, p),
  };
  const translated = translateSecondTests(patch2, context);
  const mapped = buildIndexRules(patch2, context);
  const initialTests = uniqueTests([
    ...(patch1.tests ?? []).map((t) => ({ ...t, origin: t.origin ?? 'first' })),
    ...translated.initialTests,
    ...structuralTestsForNetOps(netOps),
  ]);

  const netPatch = {
    protocol: 'structural-patch/v1',
    kind: 'composed-net-patch',
    tests: initialTests,
    ops: netOps,
  };

  let netDoc;
  try {
    netDoc = applyPatch(initial, netPatch);
  } catch (error) {
    return {
      ok: false,
      protocol: 'symbolic-composition/v1',
      kind: 'failed-composition-plan',
      stage: 'net-application',
      error: errorResult(error).error,
    };
  }
  if (!isDeepStrictEqual(netDoc, finalDoc)) {
    return {
      ok: false,
      protocol: 'symbolic-composition/v1',
      kind: 'failed-composition-plan',
      stage: 'equivalence-check',
      error: new TransactionError('CompositionMismatch', 'net composition does not equal sequential result').toJSON(),
    };
  }

  return {
    ok: true,
    protocol: 'symbolic-composition/v1',
    kind: 'bound-composed-patch',
    initialTests,
    derivedTests: translated.derivedTests,
    indexRules: mapped.rules,
    references: mapped.references,
    netPatch,
    // Intentionally no baseDoc/preimage field.
    successConditions: 'All initialTests pass and initial array lengths match arrayLength conditions.',
  };
}

export function composePatches(patch1, patch2, options = {}) {
  const tx1 = patch1?.transaction ?? options.tx1 ?? patch1;
  const tx2 = patch2?.transaction ?? options.tx2 ?? patch2;
  if (options.base !== undefined) {
    return planComposition(options.base, tx1, tx2);
  }
  return composeTransactions(tx1, tx2);
}

export function executeComposed(doc, tx1OrComposition, maybeTx2) {
  const composition = maybeTx2 === undefined && tx1OrComposition?.kind === 'symbolic-composition'
    ? tx1OrComposition
    : composeTransactions(tx1OrComposition, maybeTx2);
  try {
    initializeIdentities(doc);
    const initial = cloneDocument(doc);
    const planned = planComposition(initial, composition.tx1, composition.tx2);
    if (!planned.ok) return planned;
    const composedDoc = applyPatch(initial, planned.netPatch);
    return { ok: true, doc: composedDoc, composition: planned };
  } catch (error) {
    return errorResult(error);
  }
}

export function applyComposedPatch(doc, boundComposition) {
  try {
    if (!boundComposition?.netPatch) {
      throw new TransactionError('InvalidComposition', 'Expected a bound composition with netPatch');
    }
    const docOut = applyPatch(doc, boundComposition.netPatch);
    return { ok: true, doc: docOut, composition: boundComposition };
  } catch (error) {
    return errorResult(error, { doc });
  }
}
