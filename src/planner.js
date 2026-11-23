/**
 * Fetch planner.
 *
 * Given the fields already available for an entity occurrence (its identity
 * key plus anything fetched earlier) and the fields still needed, find a
 * terminating sequence of service batch calls that obtains every needed
 * field at minimum total cost.
 *
 * Cost model: calling service s for `count` keys costs
 * ceil(count / s.batchLimit) * s.cost. The planner searches the (small,
 * finite) space of field-availability sets with Dijkstra, so it always
 * terminates and never re-applies a service that adds nothing.
 *
 * When no plan exists, the blocked analysis reports:
 *  - unreachableNeeded: needed fields that can never be fetched,
 *  - chain: a concrete dependency cycle among unreachable fields
 *           (e.g. ['email', 'region', 'email']) when services mutually
 *           require each other's keys and no starting key is available,
 *  - missing: key fields no service provides (unstartable roots).
 */
export function planEntityFetch(entity, { available = [], needed = [], count = 1 }) {
  const fieldIndex = new Map();
  const intern = (f) => {
    if (!fieldIndex.has(f)) fieldIndex.set(f, fieldIndex.size);
    return fieldIndex.get(f);
  };
  available.forEach(intern);
  needed.forEach(intern);
  for (const svc of Object.values(entity.services)) {
    svc.key.forEach(intern);
    svc.provides.forEach(intern);
  }
  const maskOf = (fields) =>
    fields.reduce((m, f) => m | (1n << BigInt(intern(f))), 0n);

  const startMask = maskOf(available);
  const needMask = maskOf(needed);
  if ((startMask & needMask) === needMask) {
    return { ok: true, stages: [], totalCost: 0 };
  }

  const services = Object.entries(entity.services).map(([name, svc]) => ({
    name,
    keyMask: maskOf(svc.key),
    provideMask: maskOf(svc.provides),
    cost: Math.ceil(count / svc.batchLimit) * svc.cost,
  }));

  // Dijkstra over availability bitmasks.
  const dist = new Map([[startMask, 0]]);
  const prev = new Map();
  const queue = [[0, startMask]];
  while (queue.length > 0) {
    let bestI = 0;
    for (let i = 1; i < queue.length; i++) if (queue[i][0] < queue[bestI][0]) bestI = i;
    const [d, u] = queue.splice(bestI, 1)[0];
    if (d > (dist.get(u) ?? Infinity)) continue;
    for (const svc of services) {
      if ((u & svc.keyMask) !== svc.keyMask) continue;
      const v = u | svc.provideMask;
      if (v === u) continue;
      const nd = d + svc.cost;
      if (nd < (dist.get(v) ?? Infinity)) {
        dist.set(v, nd);
        prev.set(v, [u, svc]);
        queue.push([nd, v]);
      }
    }
  }

  let bestMask = null;
  let bestCost = Infinity;
  for (const [m, d] of dist) {
    if ((m & needMask) === needMask && d < bestCost) {
      bestMask = m;
      bestCost = d;
    }
  }

  if (bestMask !== null) {
    const stages = [];
    let cur = bestMask;
    while (cur !== startMask) {
      const [p, svc] = prev.get(cur);
      stages.unshift({
        service: svc.name,
        provides: [...entity.services[svc.name].provides],
        cost: svc.cost,
      });
      cur = p;
    }
    return { ok: true, stages, totalCost: bestCost };
  }

  return { ok: false, blocked: analyzeBlocked(entity, available, needed) };
}

function analyzeBlocked(entity, available, needed) {
  // Fixpoint of everything obtainable from the available fields.
  const reachable = new Set(available);
  let changed = true;
  while (changed) {
    changed = false;
    for (const svc of Object.values(entity.services)) {
      if (
        svc.key.every((k) => reachable.has(k)) &&
        svc.provides.some((p) => !reachable.has(p))
      ) {
        svc.provides.forEach((p) => reachable.add(p));
        changed = true;
      }
    }
  }

  const allFields = new Set();
  for (const svc of Object.values(entity.services)) {
    svc.key.forEach((f) => allFields.add(f));
    svc.provides.forEach((f) => allFields.add(f));
  }
  const unreachable = [...allFields].filter((f) => !reachable.has(f));
  const unreachableSet = new Set(unreachable);

  const providersOf = (f) =>
    Object.values(entity.services).filter((svc) => svc.provides.includes(f));

  // Dependency edges among unreachable fields: f -> g when some provider of
  // f requires unreachable key g. A cycle here is the blocked field chain.
  const edges = new Map();
  for (const f of unreachable) {
    const targets = new Set();
    for (const svc of providersOf(f)) {
      for (const k of svc.key) if (unreachableSet.has(k)) targets.add(k);
    }
    edges.set(f, [...targets]);
  }

  const WHITE = 0;
  const GRAY = 1;
  const BLACK = 2;
  const color = new Map();
  const stack = [];
  let chain = [];
  const dfs = (u) => {
    color.set(u, GRAY);
    stack.push(u);
    for (const v of edges.get(u) ?? []) {
      if (chain.length > 0) return;
      const c = color.get(v) ?? WHITE;
      if (c === GRAY) {
        chain = [...stack.slice(stack.indexOf(v)), v];
        return;
      }
      if (c === WHITE) dfs(v);
    }
    stack.pop();
    color.set(u, BLACK);
  };
  for (const f of unreachable) {
    if (chain.length > 0) break;
    if ((color.get(f) ?? WHITE) === WHITE) dfs(f);
  }

  return {
    unreachableNeeded: needed.filter((f) => !reachable.has(f)),
    chain,
    missing: unreachable.filter((f) => providersOf(f).length === 0),
  };
}
