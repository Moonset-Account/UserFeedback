import { planEntityFetch } from './planner.js';
import { isCoReadable, mergeVectors } from './revisions.js';

/**
 * Federated query executor.
 *
 * executeQuery({ schema, query, reads, coReadable?, checkpoint?, timeoutPolicy? })
 *
 * query (root node):
 *   {
 *     name: 'me',                 // root response key (default 'root')
 *     type: 'User',
 *     key: { id: '1' },           // single item, or keys: [...] for a list
 *     list: false, itemNonNull: false,
 *     select: [
 *       { field: 'name', alias: 'n', nonNull: true },
 *       { field: 'bestFriend', alias: 'bf', nonNull: false,
 *         node: { type: 'User', select: [...] } },   // link selection;
 *                          // list/itemNonNull on the entry describe the link
 *     ],
 *   }
 *
 * reads: { [serviceName]: async (keys, ctx) => items }
 *   keys:  array of composite key objects projected onto the service key.
 *   items: array in ANY order of
 *          { key, fields, revision? }  — success (revision optional),
 *          { key, error: {message} }   — per-item failure,
 *          or throw a TimeoutError to fail the whole batch.
 *
 * Result: { done, data, errors, stats, vectors, checkpoint }
 *   stats.reads:  every batch actually issued (service, keys, count) — the
 *                 ground truth for read counts and key sources.
 *   stats.reused / stats.dropped: checkpoint records reused or discarded
 *                 on resume because their revisions are (not) still
 *                 co-readable.
 *   vectors:      revision read set committed per entity response path.
 *   checkpoint:   serializable snapshot of fetched records + pending
 *                 (timed-out) batches; pass it back as `checkpoint` to
 *                 resume with fresh read callbacks / coReadable vectors.
 */

const PROP = Symbol('null-propagation');
const CHECKPOINT_ABORT = Symbol('checkpoint-abort');

export async function executeQuery({
  schema,
  query,
  reads,
  coReadable = schema.coReadable ?? null,
  checkpoint = null,
  timeoutPolicy = 'error',
}) {
  const ctx = {
    schema,
    reads,
    coReadable,
    timeoutPolicy,
    records: new Map(), // entity type -> Map(keyString -> record)
    stats: { reads: [], reused: [], dropped: [], plans: [] },
    errors: [],
    pending: [],
    vectors: {},
  };

  if (checkpoint) preloadCheckpoint(ctx, checkpoint);

  const rootName = query.name ?? 'root';
  const rootKeys = query.list ? query.keys ?? [] : [query.key];
  const positions = rootKeys.map((key, i) => ({
    key,
    path: query.list ? [rootName, i] : [rootName],
    constraints: [],
  }));

  try {
    const results = await resolveNode(query, positions, ctx);
    const data = assembleRoot(query, results);
    return {
      done: true,
      data,
      errors: ctx.errors,
      stats: ctx.stats,
      vectors: ctx.vectors,
      checkpoint: makeCheckpoint(ctx),
    };
  } catch (e) {
    if (e === CHECKPOINT_ABORT) {
      return {
        done: false,
        data: null,
        errors: ctx.errors,
        stats: ctx.stats,
        vectors: ctx.vectors,
        checkpoint: makeCheckpoint(ctx),
      };
    }
    throw e;
  }
}

/* ------------------------------------------------------------------ */
/* record store                                                        */
/* ------------------------------------------------------------------ */

function keyString(obj) {
  return JSON.stringify(obj, Object.keys(obj ?? {}).sort());
}

function getRecord(ctx, type, key) {
  let byType = ctx.records.get(type);
  if (!byType) {
    byType = new Map();
    ctx.records.set(type, byType);
  }
  const ks = keyString(key);
  let rec = byType.get(ks);
  if (!rec) {
    rec = { key: { ...key }, fields: {}, revisions: {}, errors: {} };
    byType.set(ks, rec);
  }
  return rec;
}

function preloadCheckpoint(ctx, checkpoint) {
  for (const [type, recs] of Object.entries(checkpoint.records ?? {})) {
    for (const stored of Object.values(recs)) {
      const rec = {
        key: stored.key,
        fields: { ...stored.fields },
        revisions: { ...stored.revisions },
        errors: {},
      };
      if (isCoReadable(rec.revisions, ctx.coReadable)) {
        const byType = ctx.records.get(type) ?? new Map();
        byType.set(keyString(rec.key), rec);
        ctx.records.set(type, byType);
        ctx.stats.reused.push({ type, key: rec.key, revisions: { ...rec.revisions } });
      } else {
        ctx.stats.dropped.push({ type, key: rec.key, revisions: { ...rec.revisions } });
      }
    }
  }
}

function makeCheckpoint(ctx) {
  const records = {};
  for (const [type, byType] of ctx.records) {
    records[type] = {};
    for (const [ks, rec] of byType) {
      records[type][ks] = {
        key: rec.key,
        fields: { ...rec.fields },
        revisions: { ...rec.revisions },
      };
    }
  }
  return { records, pending: ctx.pending.map(({ entity, service, keys }) => ({ entity, service, keys })) };
}

/* ------------------------------------------------------------------ */
/* node resolution: plan, batch, fetch                                 */
/* ------------------------------------------------------------------ */

async function resolveNode(node, positions, ctx) {
  const entity = ctx.schema.entities[node.type];
  if (!entity) throw new Error(`unknown entity type ${node.type}`);
  if (positions.length === 0) return [];

  const recs = positions.map((p) => getRecord(ctx, node.type, p.key));
  const uniqueRecs = [...new Set(recs)];

  const needed = node.select.map((s) => s.field);
  for (const rec of uniqueRecs) {
    rec.missing = needed.filter((f) => !(f in rec.fields) && !(f in rec.errors));
  }

  if (uniqueRecs.some((r) => r.missing.length > 0)) {
    await fetchMissing(entity, node.type, uniqueRecs, ctx);
  }

  // Recurse into link selections, batching child keys across all positions
  // of this node (dedup happens at the shared record store).
  const linkData = new Map();
  for (const sel of node.select) {
    if (!sel.node) continue;
    const childPositions = [];
    const index = recs.map((rec, i) => {
      const err = rec.errors[sel.field];
      const raw = rec.fields[sel.field];
      if (err || raw == null) return null;
      const values = normalizeLinkValues(raw, sel, entity);
      const basePath = [...positions[i].path, sel.alias ?? sel.field];
      return values.map((v, j) => {
        childPositions.push({
          key: v.key,
          path: sel.list ? [...basePath, j] : basePath,
          constraints: v.constraints,
        });
        return childPositions.length - 1;
      });
    });
    const childResults =
      childPositions.length > 0 ? await resolveNode(sel.node, childPositions, ctx) : [];
    linkData.set(sel, { index, childResults });
  }

  return recs.map((rec, i) => assemble(node, rec, positions[i], i, linkData, ctx));
}

async function fetchMissing(entity, type, uniqueRecs, ctx) {
  const neededUnion = [...new Set(uniqueRecs.flatMap((r) => r.missing))];
  const keyFields = entity.key;
  const presentInAll = uniqueRecs.reduce(
    (acc, r) => acc.filter((f) => f in r.fields),
    Object.keys(uniqueRecs[0].fields),
  );
  const available = [...new Set([...keyFields, ...presentInAll])];
  const count = uniqueRecs.filter((r) => r.missing.length > 0).length;

  const plan = planEntityFetch(entity, { available, needed: neededUnion, count });
  if (!plan.ok) {
    ctx.stats.plans.push({ entity: type, ok: false, blocked: plan.blocked });
    const err = {
      message:
        `blocked: cannot resolve [${plan.blocked.unreachableNeeded.join(', ')}] on ${type}` +
        (plan.blocked.chain.length > 0
          ? `; dependency cycle: ${plan.blocked.chain.join(' -> ')}`
          : `; missing root keys: ${plan.blocked.missing.join(', ')}`),
      blocked: plan.blocked,
    };
    for (const rec of uniqueRecs) {
      for (const f of rec.missing) if (!(f in rec.errors)) rec.errors[f] = err;
    }
    return;
  }
  ctx.stats.plans.push({
    entity: type,
    ok: true,
    stages: plan.stages.map((s) => s.service),
    totalCost: plan.totalCost,
  });
  // A stage's relevant fields are the needed fields it provides plus the
  // key fields of later stages (intermediate extraction keys, e.g. region
  // fetched from profiles only so contacts/social can be called).
  const laterKeys = new Set();
  for (let i = plan.stages.length - 1; i >= 0; i--) {
    const stage = plan.stages[i];
    const svc = entity.services[stage.service];
    stage.relevant = svc.provides.filter(
      (f) => neededUnion.includes(f) || laterKeys.has(f),
    );
    for (const k of svc.key) laterKeys.add(k);
  }
  for (const stage of plan.stages) {
    await runStage(entity, type, stage, uniqueRecs, ctx);
  }
}

async function runStage(entity, type, stage, uniqueRecs, ctx) {
  const svc = entity.services[stage.service];
  const relevant = stage.relevant ?? svc.provides;
  const participates = uniqueRecs.filter((r) =>
    relevant.some((f) => !(f in r.fields) && !(f in r.errors)),
  );
  // Records whose extraction key cannot be assembled (an upstream stage
  // failed) inherit the failure instead of being sent with a partial key.
  const targets = [];
  for (const rec of participates) {
    const source = { ...rec.fields, ...rec.key };
    const missingKey = svc.key.filter((k) => source[k] === undefined);
    if (missingKey.length > 0) {
      for (const f of relevant) {
        if (!(f in rec.fields) && !(f in rec.errors)) {
          rec.errors[f] = {
            message: `cannot call ${stage.service}: key field(s) ${missingKey.join(', ')} unavailable`,
          };
        }
      }
      rec.missing = rec.missing.filter((f) => !(f in rec.errors));
      continue;
    }
    targets.push(rec);
  }
  if (targets.length === 0) return;

  // Dedup by the service's composite extraction key.
  const byProj = new Map();
  for (const rec of targets) {
    const proj = projectKey({ ...rec.fields, ...rec.key }, svc.key);
    const ps = keyString(proj);
    if (!byProj.has(ps)) byProj.set(ps, { proj, recs: [] });
    byProj.get(ps).recs.push(rec);
  }
  const entries = [...byProj.values()];

  for (let i = 0; i < entries.length; i += svc.batchLimit) {
    const batch = entries.slice(i, i + svc.batchLimit);
    const keys = batch.map((e) => e.proj);
    let items;
    try {
      items = await ctx.reads[stage.service](keys, { entity: type, service: stage.service });
    } catch (e) {
      if (e && e.name === 'TimeoutError') {
        ctx.pending.push({ entity: type, service: stage.service, keys });
        if (ctx.timeoutPolicy === 'checkpoint') throw CHECKPOINT_ABORT;
        for (const en of batch) {
          for (const rec of en.recs) {
            for (const f of relevant) {
              if (!(f in rec.fields) && !(f in rec.errors)) {
                rec.errors[f] = { message: `timeout reading ${stage.service}` };
              }
            }
            rec.missing = rec.missing.filter((f) => !(f in rec.errors));
          }
        }
        continue;
      }
      throw e;
    }
    ctx.stats.reads.push({ entity: type, service: stage.service, keys, count: keys.length });

    const seen = new Set();
    for (const item of items ?? []) {
      const ps = keyString(projectKey(item.key ?? {}, svc.key));
      const en = batch.find((e) => keyString(e.proj) === ps);
      if (!en) continue; // response item for a key we did not ask for: ignore
      seen.add(ps);
      if (item.error) {
        for (const rec of en.recs) {
          for (const f of relevant) {
            if (!(f in rec.fields) && !(f in rec.errors)) {
              rec.errors[f] = { message: item.error.message ?? String(item.error) };
            }
          }
          rec.missing = rec.missing.filter((f) => !(f in rec.errors));
        }
        continue;
      }
      for (const rec of en.recs) {
        for (const f of svc.provides) {
          if (item.fields && f in item.fields) rec.fields[f] = item.fields[f];
        }
        if (item.revision !== undefined) {
          rec.revisions = { ...rec.revisions, [stage.service]: item.revision };
        }
        rec.missing = rec.missing.filter((f) => !(f in rec.fields));
      }
    }
    // Keys the service never answered become per-item errors.
    for (const en of batch) {
      if (seen.has(keyString(en.proj))) continue;
      for (const rec of en.recs) {
        for (const f of relevant) {
          if (!(f in rec.fields) && !(f in rec.errors)) {
            rec.errors[f] = { message: `key not returned by ${stage.service}` };
          }
        }
        rec.missing = rec.missing.filter((f) => !(f in rec.errors));
      }
    }
  }
}

/* ------------------------------------------------------------------ */
/* assembly: scatter to positions, null-hierarchy propagation          */
/* ------------------------------------------------------------------ */

function assemble(node, rec, pos, posIndex, linkData, ctx) {
  const pathStr = pos.path.map(String).join('.');

  // Stitch validity: everything assembled for this occurrence must come
  // from one consistent snapshot (record reads + inbound link constraints).
  const merged = mergeVectors(rec.revisions, ...(pos.constraints ?? []));
  if (merged === null || !isCoReadable(merged, ctx.coReadable)) {
    const constraints = (pos.constraints ?? []).map(
      (c) => `${c.service}@${c.revision}`,
    );
    ctx.errors.push({
      path: pos.path,
      message:
        `stitch conflict at ${pathStr}: reads ${JSON.stringify(rec.revisions)}` +
        (constraints.length > 0
          ? ` plus inbound key constraint(s) ${constraints.join(', ')}`
          : '') +
        (merged === null
          ? ' disagree on a service revision'
          : ' are not covered by any co-readable revision vector') +
        `; the assembled entity never existed in a single snapshot`,
      evidence: { revisions: { ...rec.revisions }, constraints: pos.constraints ?? [] },
    });
    return PROP;
  }
  ctx.vectors[pathStr] = merged;

  const out = {};
  for (const sel of node.select) {
    const alias = sel.alias ?? sel.field;
    const fieldPath = [...pos.path, alias];

    if (!sel.node) {
      const err = rec.errors[sel.field];
      if (err) {
        ctx.errors.push({
          path: fieldPath,
          message: err.message,
          ...(err.blocked ? { blocked: err.blocked } : {}),
        });
        if (sel.nonNull) return PROP;
        out[alias] = null;
      } else {
        out[alias] = rec.fields[sel.field] ?? null;
      }
      continue;
    }

    // link selection
    const err = rec.errors[sel.field];
    if (err) {
      ctx.errors.push({
        path: fieldPath,
        message: err.message,
        ...(err.blocked ? { blocked: err.blocked } : {}),
      });
      if (sel.nonNull) return PROP;
      out[alias] = null;
      continue;
    }
    const raw = rec.fields[sel.field];
    if (raw == null) {
      if (sel.nonNull) {
        ctx.errors.push({ path: fieldPath, message: `non-null link ${alias} resolved to null` });
        return PROP;
      }
      out[alias] = null;
      continue;
    }
    const { index, childResults } = linkData.get(sel);
    const idxs = index[posIndex] ?? [];
    if (!sel.list) {
      const child = childResults[idxs[0]];
      if (child === PROP) {
        if (sel.nonNull) return PROP;
        out[alias] = null;
      } else {
        out[alias] = child;
      }
      continue;
    }
    const items = idxs.map((ci) => childResults[ci]);
    if (items.some((c) => c === PROP)) {
      if (sel.itemNonNull) {
        // Non-null items failed: the whole list position propagates upward.
        if (sel.nonNull) return PROP;
        out[alias] = null;
      } else {
        out[alias] = items.map((c) => (c === PROP ? null : c));
      }
    } else {
      out[alias] = items;
    }
  }
  return out;
}

function assembleRoot(query, results) {
  if (!query.list) return results[0] === PROP ? null : results[0];
  if (results.some((r) => r === PROP)) {
    if (query.itemNonNull) return null;
    return results.map((r) => (r === PROP ? null : r));
  }
  return results;
}

/* ------------------------------------------------------------------ */
/* link values                                                         */
/* ------------------------------------------------------------------ */

function normalizeLinkValues(raw, sel, entity) {
  const provider = entity.fieldService[sel.field];
  const toValue = (v) => {
    if (v == null) return null;
    if (typeof v === 'object' && 'key' in v) {
      const constraints =
        v.keyRevision !== undefined && provider
          ? [{ service: provider, revision: v.keyRevision }]
          : [];
      return { key: v.key, constraints };
    }
    return { key: v, constraints: [] };
  };
  const arr = Array.isArray(raw) ? raw : [raw];
  return arr.map(toValue).filter((v) => v !== null);
}

function projectKey(source, fields) {
  const out = {};
  for (const f of fields) out[f] = source[f];
  return out;
}
