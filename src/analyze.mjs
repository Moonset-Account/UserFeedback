import { AdmissionError } from './errors.mjs';
import { conditionFields, evaluateCondition } from './condition.mjs';
import {
  applyHookToState,
  cloneState,
  groupSteps,
  stateFingerprint,
  stateToObject,
} from './engine.mjs';
import { normalizeInitialObject, programSignature } from './program.mjs';
import { replayEvidence } from './witness.mjs';

export const DEFAULT_MAX_STATES = 200000;

/* ------------------------------------------------------------------ *
 * Static independence diagnostic
 * ------------------------------------------------------------------ */

/**
 * Hooks commute unless one writes a field the other reads or writes. Fields
 * that no hook writes are constant for the whole analysis, so merely sharing a
 * read does not couple two hooks. The returned groups are a diagnostic only:
 * the engine searches at the single-hook-step level and never materialises a
 * permutation, so reordering independent hooks does not multiply stored states.
 */
export function buildComponents(program) {
  const hooks = program.hooks;
  const count = hooks.length;
  const touched = hooks.map((hook) => {
    const set = conditionFields(hook.when);
    set.add(hook.action.field);
    return set;
  });
  const writes = hooks.map((hook) => hook.action.field);
  const parent = Array.from({ length: count }, (_, index) => index);
  const find = (x) => {
    let root = x;
    while (parent[root] !== root) root = parent[root];
    while (parent[x] !== root) {
      const next = parent[x];
      parent[x] = root;
      x = next;
    }
    return root;
  };
  for (let i = 0; i < count; i += 1) {
    for (let j = i + 1; j < count; j += 1) {
      if (touched[j].has(writes[i]) || touched[i].has(writes[j])) {
        const rootA = find(i);
        const rootB = find(j);
        if (rootA !== rootB) parent[rootB] = rootA;
      }
    }
  }
  const groups = new Map();
  for (let i = 0; i < count; i += 1) {
    const root = find(i);
    if (!groups.has(root)) groups.set(root, []);
    groups.get(root).push(hooks[i].id);
  }
  return [...groups.values()].map((ids) => [...ids].sort());
}

/* ------------------------------------------------------------------ *
 * Step-level transition system
 * ------------------------------------------------------------------ *
 *
 * A run executes hooks forever, subject to the rule that every round executes
 * each hook exactly once in any order. The pending set of a search state is the
 * set of hooks of the current round not yet executed, together with a flag
 * saying whether this round has already performed an effective write.
 *
 *   - executing a hook removes it from the pending set;
 *   - when the pending set empties, the round closes:
 *       * zero effective writes -> the run terminates; acceptance is evaluated
 *                                  on the current object;
 *       * at least one write    -> a new round starts with the full pending set
 *                                  and a cleared change flag. Writing values
 *                                  back to their originals is still a write.
 *
 * Search states are keyed by (object fingerprint, pending set, change flag).
 * Using the pending *set* rather than an order merges all interleavings that
 * reach the same tuple, so reordering independent hooks stores a single state
 * instead of every permutation. The state space is finite, so an infinite run
 * exists iff the transition graph of non-terminal steps has a cycle.
 */

function stateKey(fingerprint, pending, changed) {
  return `${fingerprint}\u0000${pending.join(',')}\u0000${changed ? 1 : 0}`;
}

function stepsTo(node) {
  const steps = [];
  let current = node;
  while (current.parent !== null) {
    steps.push(current.parent.hook);
    current = current.parent.node;
  }
  return steps.reverse();
}

function decisionOf(program, object) {
  return evaluateCondition(program.accept, object) ? 'accepted' : 'rejected';
}

/* ------------------------------------------------------------------ *
 * Cycle search
 * ------------------------------------------------------------------ */

/** Iterative Tarjan SCC. */
function stronglyConnected(keys, adjacency) {
  const index = new Map();
  const lowlink = new Map();
  const onStack = new Set();
  const stack = [];
  const result = [];
  let counter = 0;
  for (const start of keys) {
    if (index.has(start)) continue;
    const frames = [{ key: start, edgeIndex: 0 }];
    index.set(start, counter);
    lowlink.set(start, counter);
    counter += 1;
    stack.push(start);
    onStack.add(start);
    while (frames.length > 0) {
      const frame = frames[frames.length - 1];
      const edges = adjacency.get(frame.key) ?? [];
      if (frame.edgeIndex < edges.length) {
        const to = edges[frame.edgeIndex].to;
        frame.edgeIndex += 1;
        if (!index.has(to)) {
          index.set(to, counter);
          lowlink.set(to, counter);
          counter += 1;
          stack.push(to);
          onStack.add(to);
          frames.push({ key: to, edgeIndex: 0 });
        } else if (onStack.has(to)) {
          lowlink.set(frame.key, Math.min(lowlink.get(frame.key), index.get(to)));
        }
        continue;
      }
      frames.pop();
      if (frames.length > 0) {
        const parentKey = frames[frames.length - 1].key;
        lowlink.set(
          parentKey,
          Math.min(lowlink.get(parentKey), lowlink.get(frame.key)),
        );
      }
      if (lowlink.get(frame.key) === index.get(frame.key)) {
        const component = [];
        let popped;
        do {
          popped = stack.pop();
          onStack.delete(popped);
          component.push(popped);
        } while (popped !== frame.key);
        result.push(component);
      }
    }
  }
  return result;
}

/**
 * Find one closed walk inside a strongly connected component.
 * Returns { nodes: [v0..v(k-1)], hooks: [h0..h(k-1)] } in the convention that
 * hooks[0] closes v(k-1) -> v0 and hooks[i] (i>=1) goes v(i-1) -> vi.
 */
function findCycleInComponent(component, adjacency) {
  const members = new Set(component);
  const color = new Map();
  for (const start of component) {
    if (color.has(start)) continue;
    const frames = [{ key: start, edgeIndex: 0 }];
    const pathEdges = [null];
    color.set(start, 1);
    while (frames.length > 0) {
      const frame = frames[frames.length - 1];
      const edges = (adjacency.get(frame.key) ?? []).filter((edge) =>
        members.has(edge.to),
      );
      if (frame.edgeIndex < edges.length) {
        const edge = edges[frame.edgeIndex];
        frame.edgeIndex += 1;
        const seen = color.get(edge.to);
        if (seen === undefined) {
          color.set(edge.to, 1);
          frames.push({ key: edge.to, edgeIndex: 0 });
          pathEdges.push(edge.hook);
          continue;
        }
        if (seen === 1) {
          const position = frames.findIndex((entry) => entry.key === edge.to);
          const nodes = frames.slice(position).map((entry) => entry.key);
          const hooks = [edge.hook, ...pathEdges.slice(position + 1)];
          return { nodes, hooks };
        }
      } else {
        color.set(frame.key, 2);
        frames.pop();
        pathEdges.pop();
      }
    }
  }
  return null;
}

function rotateCycle(cycle, startIndex) {
  const count = cycle.nodes.length;
  const nodes = [];
  const hooks = [];
  for (let step = 0; step < count; step += 1) {
    nodes.push(cycle.nodes[(startIndex + step) % count]);
    hooks.push(cycle.hooks[(startIndex + step + 1) % count]);
  }
  return { nodes, hooks };
}

/* ------------------------------------------------------------------ *
 * Analysis
 * ------------------------------------------------------------------ */

function factorial(n) {
  let value = 1;
  for (let i = 2; i <= n; i += 1) value *= i;
  return value;
}

/**
 * Decide convergence of every legal run from the caller's initial object.
 *
 * A run is a sequence of rounds; each round executes every hook exactly once in
 * an arbitrary order. The run ends at the first round with no effective write,
 * and acceptance is evaluated on that object. The analysis reports whether all
 * runs end with the same decision and object, or, otherwise, every failure kind
 * present with separately replayable evidence.
 */
export function analyze(program, initialObject, options = {}) {
  const maxStates = options.maxStates ?? DEFAULT_MAX_STATES;
  if (!Number.isInteger(maxStates) || maxStates < 1) {
    throw new AdmissionError(
      'INVALID_OPTIONS',
      'maxStates must be a positive integer',
      { maxStates },
    );
  }
  const initial = normalizeInitialObject(program, initialObject);
  const initialPlain = stateToObject(initial);
  const hookCount = program.hooks.length;
  const fullPending = [...program.hookIds].sort();
  const initialFp = stateFingerprint(initial);

  const nodes = new Map();
  const queue = [];
  const adjacency = new Map();
  const terminals = new Map();
  let transitions = 0;

  const makeNode = (state, pending, changed, parent) => ({
    key: stateKey(stateFingerprint(state), pending, changed),
    fingerprint: stateFingerprint(state),
    state,
    pending,
    changed,
    parent,
  });

  function ensureNode(state, pending, changed, parentNode, hook) {
    const key = stateKey(stateFingerprint(state), pending, changed);
    let node = nodes.get(key);
    if (node === undefined) {
      if (nodes.size >= maxStates) {
        throw new AdmissionError(
          'ANALYSIS_LIMIT',
          `reachable state space exceeded maxStates=${maxStates}`,
          { maxStates },
        );
      }
      node = makeNode(state, pending, changed, { node: parentNode, hook });
      nodes.set(key, node);
      queue.push(node);
    }
    return node;
  }

  function registerTerminal(state, node, hook) {
    const fingerprint = stateFingerprint(state);
    const decision = decisionOf(program, state);
    const outcomeKey = `${fingerprint}|${decision}`;
    if (terminals.has(outcomeKey)) return;
    const steps = node === null ? [] : [...stepsTo(node), hook];
    terminals.set(outcomeKey, {
      fingerprint,
      object: stateToObject(state),
      decision,
      steps,
    });
  }

  if (hookCount === 0) {
    registerTerminal(initial, null, null);
  } else {
    const root = makeNode(initial, fullPending, false, null);
    nodes.set(root.key, root);
    queue.push(root);
  }

  let head = 0;
  while (head < queue.length) {
    const node = queue[head];
    head += 1;
    const edges = [];
    for (const hookId of node.pending) {
      const hook = program.hooksById.get(hookId);
      const work = cloneState(node.state);
      const applied = applyHookToState(work, hook);
      const changed = node.changed || applied.changed;
      const pending = node.pending.filter((id) => id !== hookId);
      transitions += 1;
      if (pending.length === 0) {
        if (!changed) {
          registerTerminal(work, node, hookId);
          continue;
        }
        const child = ensureNode(work, fullPending, false, node, hookId);
        edges.push({ to: child.key, hook: hookId });
        continue;
      }
      const child = ensureNode(work, pending, changed, node, hookId);
      edges.push({ to: child.key, hook: hookId });
    }
    adjacency.set(node.key, edges);
  }

  /* ---- termination outcomes ---- */
  const terminationOutcomes = [...terminals.values()]
    .map((entry) => {
      const grouped = groupSteps(program, entry.steps);
      const rounds = grouped.rounds;
      return {
        decision: entry.decision,
        object: entry.object,
        fingerprint: entry.fingerprint,
        pathOrders: rounds.length === 0 ? [] : rounds.slice(0, -1),
        stableOrder: rounds.length === 0 ? [] : rounds[rounds.length - 1],
      };
    })
    .sort((a, b) => {
      if (a.decision !== b.decision) return a.decision === 'accepted' ? -1 : 1;
      return a.fingerprint < b.fingerprint
        ? -1
        : a.fingerprint > b.fingerprint
          ? 1
          : 0;
    });

  /* ---- cycles ---- */
  const components = stronglyConnected([...nodes.keys()], adjacency);
  const cycles = [];
  for (const component of components) {
    const members = new Set(component);
    const cyclic =
      component.length > 1 ||
      (adjacency.get(component[0]) ?? []).some((edge) => members.has(edge.to));
    if (!cyclic) continue;
    const found = findCycleInComponent(component, adjacency);
    if (found !== null) cycles.push(found);
  }

  /* ---- evidence ---- */
  const terminationWitnesses = terminationOutcomes.map((outcome) => ({
    version: 1,
    kind: 'termination',
    programSignature: undefined,
    programHookIds: [...program.hookIds],
    initialObject: structuredClone(initialPlain),
    path: outcome.pathOrders.map((round) => [...round]),
    stableRound: [...outcome.stableOrder],
    expected: {
      decision: outcome.decision,
      object: structuredClone(outcome.object),
    },
  }));

  const cycleWitnesses = cycles.map((cycle) => {
    let startIndex = cycle.nodes.findIndex(
      (key) => nodes.get(key).pending.length > 0 &&
        nodes.get(key).pending.length < hookCount,
    );
    if (startIndex < 0) startIndex = 0;
    const rotated = rotateCycle(cycle, startIndex);
    const startNode = nodes.get(rotated.nodes[0]);
    const grouped = groupSteps(program, stepsTo(startNode));
    const pending = [...startNode.pending].sort();
    return {
      version: 1,
      kind: 'nontermination',
      programSignature: undefined,
      programHookIds: [...program.hookIds],
      initialObject: structuredClone(initialPlain),
      path: grouped.rounds.map((round) => [...round]),
      prefix: grouped.prefix,
      cycle: rotated.hooks,
      expected: {
        startObject: stateToObject(startNode.state),
        endObject: stateToObject(startNode.state),
        startPending: [...pending],
        endPending: [...pending],
        startChanged: startNode.changed,
        endChanged: startNode.changed,
      },
    };
  });

  const signature = programSignature(program);
  for (const witness of [...terminationWitnesses, ...cycleWitnesses]) {
    witness.programSignature = signature;
  }

  /* ---- self-check every witness against the independent replayer ---- */
  for (const witness of [...terminationWitnesses, ...cycleWitnesses]) {
    const replay = replayEvidence(program, witness);
    if (!replay.ok) {
      throw new AdmissionError(
        'INTERNAL_WITNESS_INVALID',
        `generated ${witness.kind} witness failed replay: ${replay.failures
          .map((item) => item.code)
          .join(',')}`,
        { witness, failures: replay.failures },
      );
    }
  }

  /* ---- verdict ---- */
  const decisions = new Set(terminationOutcomes.map((entry) => entry.decision));
  const objects = new Set(terminationOutcomes.map((entry) => entry.fingerprint));
  const allRunsTerminate = cycles.length === 0;
  const uniformTermination =
    terminationOutcomes.length > 0 &&
    decisions.size === 1 &&
    objects.size === 1;

  const findings = [];
  if (cycles.length > 0) {
    findings.push({
      kind: 'nontermination',
      message:
        'a legal order sequence never reaches an unchanged round',
      witnesses: cycleWitnesses,
    });
  }
  if (terminationOutcomes.length > 0) {
    if (!uniformTermination) {
      findings.push({
        kind: 'inconsistent-results',
        message:
          'terminating runs end with different admission results or objects',
        outcomes: terminationOutcomes.map((entry) => ({
          decision: entry.decision,
          object: entry.object,
        })),
        witnesses: terminationWitnesses,
      });
    } else if (terminationOutcomes[0].decision === 'rejected') {
      findings.push({
        kind: 'stable-rejection',
        message: 'every terminating run ends on the same object but is rejected',
        outcomes: terminationOutcomes.map((entry) => ({
          decision: entry.decision,
          object: entry.object,
        })),
        witnesses: terminationWitnesses,
      });
    }
  }

  let verdict;
  if (findings.length === 0) verdict = 'admissible';
  else if (findings.length === 1) verdict = findings[0].kind;
  else verdict = 'multiple-failures';

  return {
    verdict,
    admissible: findings.length === 0,
    admission: findings.length === 0 ? 'accepted' : null,
    allRunsTerminate,
    uniformTermination,
    initialObject: initialPlain,
    terminationOutcomes: terminationOutcomes.map((entry, index) => ({
      decision: entry.decision,
      object: entry.object,
      witness: terminationWitnesses[index],
    })),
    nonterminationWitnesses: cycleWitnesses,
    findings,
    witnesses: [...terminationWitnesses, ...cycleWitnesses],
    components: buildComponents(program),
    stats: {
      statesExplored: nodes.size,
      stateTransitions: transitions,
      terminalOutcomes: terminationOutcomes.length,
      cyclicComponents: cycles.length,
      reachableStates: nodes.size,
      allPermutations: factorial(hookCount),
      distinctTerminationOutcomes: terminationOutcomes.length,
    },
  };
}
