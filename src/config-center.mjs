// Configuration center: publish + resolution module.
//
// Layer model (low -> high precedence):
//   base -> environment -> instance
//
// Values are either string literals or single-key references: { $ref: 'otherKey' }.
// A layer may explicitly shadow a key (tombstone), which removes the key from
// that layer and every layer inheriting it until a higher layer re-introduces it.
//
// A publish can change several layers atomically. Before commit, every
// reference visible to every registered instance is validated. A dangling
// reference or a reference cycle rejects the whole publish and leaves the
// previously published values untouched.

const REF = '$ref';
const SHADOW = '$shadow';

/** Error thrown (and returned in publish results) for dangling/cyclic references. */
export class ConfigReferenceError extends Error {
  /**
   * @param {object} info
   * @param {string} info.instanceId  instance in whose effective config the problem is visible
   * @param {'dangling'|'cycle'} info.kind
   * @param {string} info.originKey  key holding the first reference of the chain
   * @param {string[]} info.chain    reference chain (array of keys); for a cycle the last key repeats
   */
  constructor({ instanceId, kind, originKey, chain }) {
    const where = `instance '${instanceId}'`;
    const at = `starting at key '${originKey}'`;
    const path = chain.join(' -> ');
    const detail =
      kind === 'cycle'
        ? `reference cycle: ${path}`
        : `dangling reference: ${path}`;
    super(`${kind} in ${where}, ${at}: ${detail}`);
    this.name = 'ConfigReferenceError';
    this.instanceId = instanceId;
    this.kind = kind;
    this.originKey = originKey;
    this.chain = [...chain];
  }
}

/** Build a single-key reference value: ref(key) === { $ref: key }. */
export function ref(key) {
  if (typeof key !== 'string' || key.length === 0) {
    throw new TypeError("ref() requires a non-empty string key");
  }
  return { [REF]: key };
}

/** Tombstone value: the key is explicitly missing from this layer and its descendants. */
export function shadow() {
  return { [SHADOW]: true };
}

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

/**
 * Normalize a stored entry.
 * @returns {{kind:'literal', value:string} | {kind:'ref', target:string} | {kind:'shadow'}}
 */
function normalizeEntry(value, where) {
  if (typeof value === 'string') return { kind: 'literal', value };
  if (isPlainObject(value)) {
    const keys = Object.keys(value);
    if (keys.length === 1 && keys[0] === REF) {
      const target = value[REF];
      if (typeof target !== 'string' || target.length === 0) {
        throw new TypeError(`${where}: $ref must point to a non-empty string key`);
      }
      return { kind: 'ref', target };
    }
    if (keys.length === 1 && keys[0] === SHADOW && value[SHADOW] === true) {
      return { kind: 'shadow' };
    }
  }
  throw new TypeError(
    `${where}: value must be a string, a single-key reference { $ref } or a shadow { $shadow: true }`
  );
}

function cloneLayer(layer) {
  const out = Object.create(null);
  for (const [k, v] of Object.entries(layer)) out[k] = { ...v };
  return out;
}

export class ConfigCenter {
  #base = Object.create(null);
  #envs = new Map();   // envId -> layer
  #instances = new Map(); // instanceId -> { envId, layer }
  #version = 0;

  /** Register an environment layer above the base layer. */
  registerEnvironment(envId) {
    if (typeof envId !== 'string' || envId.length === 0) {
      throw new TypeError('envId must be a non-empty string');
    }
    if (this.#envs.has(envId)) {
      throw new Error(`environment '${envId}' is already registered`);
    }
    this.#envs.set(envId, Object.create(null));
  }

  /** Register an instance layer above the given environment layer. */
  registerInstance(instanceId, envId) {
    if (typeof instanceId !== 'string' || instanceId.length === 0) {
      throw new TypeError('instanceId must be a non-empty string');
    }
    if (this.#instances.has(instanceId)) {
      throw new Error(`instance '${instanceId}' is already registered`);
    }
    if (!this.#envs.has(envId)) {
      throw new Error(`environment '${envId}' is not registered`);
    }
    this.#instances.set(instanceId, { envId, layer: Object.create(null) });
  }

  /** Monotonic version counter; incremented only by successfully committed publishes. */
  get version() {
    return this.#version;
  }

  listEnvironments() {
    return [...this.#envs.keys()];
  }

  listInstances() {
    return [...this.#instances.keys()];
  }

  /**
   * Publish one atomic change set spanning any number of layers.
   *
   * changes: Array<{ layer: 'base' | { env: envId } | { instance: instanceId },
   *                  set?: Record<string, string | { $ref } | { $shadow: true }>,
   *                  delete?: string[] }>
   *
   * All references visible to every registered instance are validated before
   * commit. On failure nothing is committed and the previous version stays live.
   *
   * @returns {{ ok: true, version: number, committed: object[] }
   *          | { ok: false, version: number, errors: ConfigReferenceError[] }}
   */
  publish(changes) {
    if (!Array.isArray(changes)) throw new TypeError('publish() expects an array of changes');

    // Work on deep copies so a rejected publish leaves live state untouched.
    const candidate = {
      base: cloneLayer(this.#base),
      envs: new Map([...this.#envs].map(([id, layer]) => [id, cloneLayer(layer)])),
      instances: new Map(
        [...this.#instances].map(([id, rec]) => [id, { envId: rec.envId, layer: cloneLayer(rec.layer) }])
      ),
    };

    const committed = [];
    for (const change of changes) {
      const layer = this.#resolveLayerForWrite(candidate, change.layer);
      const label = describeScope(change.layer);
      const applied = this.#applyChange(layer, change, label);
      committed.push({ layer: change.layer, ...applied });
    }

    // Validate every reference visible in every registered instance's effective config.
    const errors = [];
    const seen = new Set();
    for (const instanceId of candidate.instances.keys()) {
      const merged = this.#merge(candidate, instanceId);
      for (const [key, entry] of Object.entries(merged)) {
        if (entry.kind !== 'ref') continue;
        const result = this.#resolveIn(merged, key);
        if (result.ok) continue;
        const err = new ConfigReferenceError({
          instanceId,
          kind: result.kind,
          originKey: key,
          chain: result.chain,
        });
        const dedupeKey = `${instanceId}\u0000${result.kind}\u0000${result.chain.join('>')}`;
        if (!seen.has(dedupeKey)) {
          seen.add(dedupeKey);
          errors.push(err);
        }
      }
    }

    if (errors.length > 0) {
      // Reject: live state and version remain the previous published version.
      return { ok: false, version: this.#version, errors };
    }

    this.#base = candidate.base;
    this.#envs = candidate.envs;
    this.#instances = candidate.instances;
    this.#version += 1;
    return { ok: true, version: this.#version, committed };
  }

  #resolveLayerForWrite(candidate, scope) {
    if (scope === 'base' || (isPlainObject(scope) && scope.layer === 'base')) {
      return candidate.base;
    }
    if (isPlainObject(scope) && typeof scope.env === 'string') {
      const layer = candidate.envs.get(scope.env);
      if (!layer) throw new Error(`unknown environment '${scope.env}' in publish`);
      return layer;
    }
    if (isPlainObject(scope) && typeof scope.instance === 'string') {
      const rec = candidate.instances.get(scope.instance);
      if (!rec) throw new Error(`unknown instance '${scope.instance}' in publish`);
      return rec.layer;
    }
    throw new TypeError(
      "change.layer must be 'base', { env } or { instance }"
    );
  }

  #applyChange(layer, change, label) {
    if (!isPlainObject(change)) throw new TypeError(`${label}: change must be an object`);
    const set = change.set ?? {};
    const del = change.delete ?? [];
    if (!isPlainObject(set)) throw new TypeError(`${label}: 'set' must be an object`);
    if (!Array.isArray(del) || del.some((k) => typeof k !== 'string')) {
      throw new TypeError(`${label}: 'delete' must be an array of string keys`);
    }

    const setKeys = [];
    for (const [key, value] of Object.entries(set)) {
      layer[key] = normalizeEntry(value, `${label}.set['${key}']`);
      setKeys.push(key);
    }
    // Within one change, setting a key expresses its final state, so a
    // matching delete on the same key is ignored (set wins).
    const setKeySet = new Set(setKeys);
    const deletedKeys = [];
    for (const key of del) {
      if (setKeySet.has(key)) continue;
      if (Object.hasOwn(layer, key)) {
        delete layer[key];
        deletedKeys.push(key);
      }
    }
    return { setKeys, deletedKeys };
  }

  #merge(state, instanceId) {
    const rec = state.instances.get(instanceId);
    const merged = Object.create(null);
    const layers = [state.base, state.envs.get(rec.envId), rec.layer];
    for (const layer of layers) {
      for (const [key, entry] of Object.entries(layer)) {
        if (entry.kind === 'shadow') {
          delete merged[key]; // explicit masking: absent here and in descendants until re-set
        } else {
          merged[key] = entry;
        }
      }
    }
    return merged;
  }

  /**
   * Resolve a key starting from its (possibly inherited) reference entry.
   * Each hop is looked up in the requesting instance's own merged config,
   * so an inherited reference resolves against values visible to that instance.
   */
  #resolveIn(merged, startKey) {
    const chain = [startKey];
    const visited = new Set([startKey]);
    let entry = merged[startKey];
    if (!entry) {
      return { ok: false, kind: 'dangling', chain };
    }
    while (entry.kind === 'ref') {
      const target = entry.target;
      chain.push(target);
      if (visited.has(target)) {
        return { ok: false, kind: 'cycle', chain };
      }
      visited.add(target);
      entry = merged[target];
      if (!entry) {
        return { ok: false, kind: 'dangling', chain };
      }
    }
    // entry.kind === 'literal'
    return { ok: true, value: entry.value, chain };
  }

  /**
   * Effective (fully resolved) configuration of one instance: plain key -> string map.
   * Throws ConfigReferenceError if a visible reference dangles or forms a cycle.
   */
  getEffectiveConfig(instanceId) {
    const rec = this.#instances.get(instanceId);
    if (!rec) throw new Error(`unknown instance '${instanceId}'`);
    const merged = this.#merge(this.#currentState(), instanceId);
    const out = {};
    for (const [key, entry] of Object.entries(merged)) {
      if (entry.kind === 'ref') {
        const result = this.#resolveIn(merged, key);
        if (!result.ok) {
          throw new ConfigReferenceError({
            instanceId,
            kind: result.kind,
            originKey: key,
            chain: result.chain,
          });
        }
        out[key] = result.value;
      } else {
        out[key] = entry.value;
      }
    }
    return out;
  }

  /** Resolve one key for an instance, following references in that instance's effective config. */
  resolve(instanceId, key) {
    const rec = this.#instances.get(instanceId);
    if (!rec) throw new Error(`unknown instance '${instanceId}'`);
    const merged = this.#merge(this.#currentState(), instanceId);
    if (!Object.hasOwn(merged, key)) {
      throw new ConfigReferenceError({ instanceId, kind: 'dangling', originKey: key, chain: [key] });
    }
    const result = this.#resolveIn(merged, key);
    if (!result.ok) {
      throw new ConfigReferenceError({
        instanceId,
        kind: result.kind,
        originKey: key,
        chain: result.chain,
      });
    }
    return result.value;
  }

  /**
   * Effective layer content before reference resolution.
   * Entries are returned as public values: strings, { $ref } or { $shadow: true }.
   */
  getMergedConfig(instanceId) {
    const rec = this.#instances.get(instanceId);
    if (!rec) throw new Error(`unknown instance '${instanceId}'`);
    const merged = this.#merge(this.#currentState(), instanceId);
    const out = {};
    for (const [key, entry] of Object.entries(merged)) {
      if (entry.kind === 'literal') out[key] = entry.value;
      else if (entry.kind === 'ref') out[key] = ref(entry.target);
      else out[key] = shadow();
    }
    return out;
  }

  #currentState() {
    return { base: this.#base, envs: this.#envs, instances: this.#instances };
  }
}

function describeScope(scope) {
  if (scope === 'base') return 'base';
  if (isPlainObject(scope) && scope.layer === 'base') return 'base';
  if (isPlainObject(scope) && typeof scope.env === 'string') return `env['${scope.env}']`;
  if (isPlainObject(scope) && typeof scope.instance === 'string') {
    return `instance['${scope.instance}']`;
  }
  return 'layer';
}
