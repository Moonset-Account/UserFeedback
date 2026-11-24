/**
 * Naive reference implementation, written independently of src/analyze.mjs.
 *
 * It works only at round boundaries and explicitly enumerates every hook
 * permutation, so it is only usable for tiny programs. Tests use it to
 * cross-check the step-level analyser's results.
 */
import { evaluateCondition } from '../src/condition.mjs';

function clone(object) {
  return structuredClone(object);
}

function fingerprint(object) {
  const keys = Object.keys(object).sort();
  return JSON.stringify(
    keys.map((key) => [key, canonicalValue(object[key])]),
  );
}

function canonicalValue(value) {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonicalValue(value[key])]),
    );
  }
  return value;
}

function applyHook(object, hook) {
  if (!evaluateCondition(hook.when, object)) return false;
  const { kind, field } = hook.action;
  if (kind === 'set') {
    if (
      Object.hasOwn(object, field) &&
      fingerprint({ v: object[field] }) === fingerprint({ v: hook.action.value })
    ) {
      return false;
    }
    object[field] = clone(hook.action.value);
    return true;
  }
  if (!Object.hasOwn(object, field)) return false;
  delete object[field];
  return true;
}

export function* permutations(items) {
  if (items.length <= 1) {
    yield [...items];
    return;
  }
  for (let index = 0; index < items.length; index += 1) {
    const rest = [...items.slice(0, index), ...items.slice(index + 1)];
    for (const tail of permutations(rest)) {
      yield [items[index], ...tail];
    }
  }
}

/** One round outcome: resulting object and whether any write happened. */
export function naiveRound(program, object, order) {
  const work = clone(object);
  let writes = 0;
  for (const id of order) {
    if (applyHook(work, program.hooksById.get(id))) writes += 1;
  }
  return { object: work, writes };
}

/**
 * Exhaustive boundary-graph analysis with explicit permutation enumeration.
 * Returns { outcomes: Set<`${decision}|${fp}`>, terminalObjects, hasCycle, states }
 */
export function naiveAnalyze(program, initialObject) {
  const ids = program.hooks.map((hook) => hook.id);
  const orders = [...permutations(ids)];
  const seen = new Map();
  const queue = [clone(initialObject)];
  seen.set(fingerprint(initialObject), clone(initialObject));
  const terminalObjects = new Map();
  let hasCycle = false;

  const changedGraph = new Map();
  for (let head = 0; head < queue.length; head += 1) {
    const object = queue[head];
    const key = fingerprint(object);
    const edges = [];
    for (const order of orders) {
      // An empty program has one empty round.
      if (ids.length === 0) {
        const decision = evaluateCondition(program.accept, object)
          ? 'accepted'
          : 'rejected';
        terminalObjects.set(`${decision}|${key}`, { decision, object });
        continue;
      }
      const { object: next, writes } = naiveRound(program, object, order);
      if (writes === 0) {
        const decision = evaluateCondition(program.accept, object)
          ? 'accepted'
          : 'rejected';
        terminalObjects.set(`${decision}|${key}`, {
          decision,
          object: clone(object),
        });
      } else {
        const nextKey = fingerprint(next);
        edges.push(nextKey);
        if (!seen.has(nextKey)) {
          seen.set(nextKey, next);
          queue.push(next);
        }
      }
    }
    changedGraph.set(key, [...new Set(edges)]);
  }

  // Cycle reachable from the initial boundary state in the changed-round graph.
  const colors = new Map();
  const stack = [{ key: fingerprint(initialObject), index: 0 }];
  colors.set(stack[0].key, 1);
  while (stack.length > 0) {
    const frame = stack[stack.length - 1];
    const edges = changedGraph.get(frame.key) ?? [];
    if (frame.index >= edges.length) {
      colors.set(frame.key, 2);
      stack.pop();
      continue;
    }
    const to = edges[frame.index];
    frame.index += 1;
    const color = colors.get(to);
    if (color === 1) hasCycle = true;
    if (color === undefined) {
      colors.set(to, 1);
      stack.push({ key: to, index: 0 });
    }
  }

  return {
    terminalObjects: [...terminalObjects.values()],
    hasCycle,
    boundaryStates: seen.size,
  };
}
