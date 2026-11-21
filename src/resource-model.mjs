/**
 * Co-owned resource model (server-side-apply style field ownership).
 *
 * A resource value plus a per-leaf-path owner set. Controllers (managers)
 * repeatedly apply declarations; ownership is shared when values agree,
 * conflicts are reported per path, and a failed apply is completely atomic.
 *
 * Leaf paths use dot notation for object members and `[name=...]` for
 * associative list elements, e.g.
 *
 *   spec.replicas
 *   spec.containers[name=web].image
 *
 * See README.md for the full rule set.
 */

import { isDeepStrictEqual } from 'node:util';

const PROPERTY_ESCAPE = /[\\.[\]]/g;
const NAME_ESCAPE = /[\\\]]/g;

export class OwnershipConflictError extends Error {
  /**
   * @param {Array<{path:string,reason:string,currentValue:unknown,requestedValue:unknown,owners:string[]}>} conflicts
   */
  constructor(conflicts) {
    const paths = conflicts.map((conflict) => conflict.path);
    super(`declaration conflicts with existing field ownership at: ${paths.join(', ')}`);
    this.name = 'OwnershipConflictError';
    this.paths = paths;
    this.conflicts = conflicts;
  }
}

export function isPlainObject(value) {
  if (value === null || typeof value !== 'object') return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

/**
 * A "container list" is an array whose elements are all plain objects with a
 * unique, non-empty string `name`. An empty array is a container list with no
 * declared elements. Arrays holding any other value (scalars, nulls, mixed or
 * duplicate/unnamed objects) are ordinary leaf values.
 */
export function containerListNames(value) {
  if (!Array.isArray(value)) return null;
  const names = [];
  const seen = new Set();
  for (const item of value) {
    if (!isPlainObject(item)) return null;
    if (typeof item.name !== 'string' || item.name === '') return null;
    if (seen.has(item.name)) return null;
    seen.add(item.name);
    names.push(item.name);
  }
  return names;
}

export function formatPath(segments) {
  let rendered = '';
  for (const segment of segments) {
    if (segment.type === 'property') {
      const key = segment.key.replace(PROPERTY_ESCAPE, (character) => `\\${character}`);
      rendered += rendered === '' ? key : `.${key}`;
    } else {
      const name = segment.name.replace(NAME_ESCAPE, (character) => `\\${character}`);
      rendered += `[name=${name}]`;
    }
  }
  return rendered;
}

function cloneValue(value) {
  if (Array.isArray(value)) return value.map(cloneValue);
  if (isPlainObject(value)) {
    const clone = {};
    for (const key of Object.keys(value)) setOwn(clone, key, cloneValue(value[key]));
    return clone;
  }
  return value;
}

/**
 * Assign an own enumerable property without going through the `__proto__`
 * setter. Declarations may arrive from parsed JSON and must not be able to
 * swap an object's prototype.
 */
function setOwn(target, key, value) {
  Object.defineProperty(target, key, { value, enumerable: true, writable: true, configurable: true });
}

function createAccumulator() {
  return {
    /** @type {Map<string, {path:string, segments:Array<object>, value:unknown}>} */
    leaves: new Map(),
    /** @type {Map<string, {path:string, segments:Array<object>, kind:'object'|'list', value:unknown}>} */
    containers: new Map(),
  };
}

/**
 * True when a declaration subtree actually declares at least one field or
 * list element. Empty objects and empty container lists declare nothing, so
 * they must never materialize new structure in the resource.
 */
function declaresSomething(value) {
  if (isPlainObject(value)) {
    return Object.entries(value).some(([, child]) => child !== undefined && declaresSomething(child));
  }
  const names = containerListNames(value);
  if (names !== null) {
    if (names.length === 0) return false;
    return value.some((item) => Object.entries(item).some(
      ([key, child]) => key !== 'name' && child !== undefined && declaresSomething(child),
    ));
  }
  return true;
}

/**
 * Flatten a value into owned leaves and structural nodes. The `name` member
 * of an associative-list element is identity, not a field. Object members
 * whose value is `undefined` are treated as "not declared".
 */
function flatten(value, segments, accumulator) {
  if (!declaresSomething(value)) return;

  if (isPlainObject(value)) {
    if (segments.length > 0) {
      accumulator.containers.set(formatPath(segments), {
        path: formatPath(segments),
        segments,
        kind: 'object',
        value,
      });
    }
    for (const [key, child] of Object.entries(value)) {
      if (child === undefined) continue;
      flatten(child, [...segments, { type: 'property', key }], accumulator);
    }
    return;
  }

  const names = containerListNames(value);
  if (names !== null) {
    if (segments.length > 0) {
      accumulator.containers.set(formatPath(segments), {
        path: formatPath(segments),
        segments,
        kind: 'list',
        value,
      });
    }
    for (const name of names) {
      const item = value.find((candidate) => candidate.name === name);
      const elementSegments = [...segments, { type: 'element', name }];
      accumulator.containers.set(formatPath(elementSegments), {
        path: formatPath(elementSegments),
        segments: elementSegments,
        kind: 'object',
        value: item,
      });
      for (const [key, child] of Object.entries(item)) {
        if (key === 'name' || child === undefined) continue;
        flatten(child, [...elementSegments, { type: 'property', key }], accumulator);
      }
    }
    return;
  }

  const path = formatPath(segments);
  accumulator.leaves.set(path, { path, segments, value });
}

function resolveValue(root, segments) {
  let current = root;
  for (const segment of segments) {
    if (segment.type === 'property') {
      if (!isPlainObject(current) || !Object.hasOwn(current, segment.key)) return { found: false };
      current = current[segment.key];
    } else {
      if (!Array.isArray(current)) return { found: false };
      const item = current.find((candidate) => isPlainObject(candidate) && candidate.name === segment.name);
      if (!item) return { found: false };
      current = item;
    }
  }
  return { found: true, value: current };
}

function isPrefix(prefix, segments) {
  if (prefix.length >= segments.length) return false;
  for (let index = 0; index < prefix.length; index += 1) {
    const left = segments[index];
    const right = prefix[index];
    if (left.type !== right.type) return false;
    if (left.type === 'property' && left.key !== right.key) return false;
    if (left.type === 'element' && left.name !== right.name) return false;
  }
  return true;
}

function findLiveLeafAncestor(segments, liveLeaves) {
  for (const leaf of liveLeaves.values()) {
    if (isPrefix(leaf.segments, segments)) return leaf;
  }
  return null;
}

function isStructurallyEmpty(value, isListElement) {
  if (Array.isArray(value)) return value.length === 0;
  if (isPlainObject(value)) {
    const keys = Object.keys(value);
    if (isListElement) return keys.every((key) => key === 'name');
    return keys.length === 0;
  }
  return false;
}

/**
 * Merge a declaration into a live value. Plain objects merge key-by-key,
 * container lists merge element-by-element by `name`, everything else is a
 * leaf replacement. The result is a fresh tree; inputs are never mutated.
 *
 * List order: identity is the `name`, never the position. Elements that
 * already exist keep their live order; elements that are new to this
 * declaration are appended in declaration order. Reordering a declaration is
 * therefore a no-op on the stored value, and it can never transfer ownership.
 */
function mergeValue(liveValue, declaredValue) {
  if (isPlainObject(declaredValue)) {
    const base = isPlainObject(liveValue) ? liveValue : {};
    const merged = {};
    for (const [key, value] of Object.entries(base)) setOwn(merged, key, cloneValue(value));
    for (const [key, value] of Object.entries(declaredValue)) {
      // `undefined` means "not declared"; empty objects and empty lists
      // declare no fields either. Only omission can remove something.
      if (value === undefined || !declaresSomething(value)) continue;
      // Only own members count as the live base: `base['__proto__']` would
      // otherwise resolve to Object.prototype.
      const liveChild = Object.hasOwn(base, key) ? base[key] : undefined;
      setOwn(merged, key, mergeValue(liveChild, value));
    }
    return merged;
  }

  const declaredNames = containerListNames(declaredValue);
  if (declaredNames !== null) {
    const base = containerListNames(liveValue) !== null ? liveValue : [];
    const declaredByName = new Map(declaredValue.map((item) => [item.name, item]));
    const liveNames = new Set(base.map((item) => item.name));
    const merged = [];
    for (const item of base) {
      const declared = declaredByName.get(item.name);
      if (declared === undefined) {
        merged.push(cloneValue(item));
        continue;
      }
      const element = mergeValue(item, declared);
      // An element that carries nothing but its identity is not a field; do
      // not materialize it. Elements that still hold any member (usually
      // because a different controller owns it) are kept untouched.
      if (Object.keys(element).length > 1) merged.push(element);
    }
    for (const item of declaredValue) {
      if (liveNames.has(item.name)) continue;
      const element = mergeValue(undefined, item);
      if (Object.keys(element).length > 1) merged.push(element);
    }
    return merged;
  }

  return cloneValue(declaredValue);
}

/**
 * Remove a leaf path, pruning containers that become empty. List elements
 * whose only remaining member is `name` are removed. Returns whether the
 * target existed.
 */
function deleteTreePath(root, segments) {
  return deleteIn(root, segments, 0, false).removed;
}

function deleteIn(node, segments, index, nodeIsListElement) {
  const segment = segments[index];
  const isLast = index === segments.length - 1;

  if (segment.type === 'property') {
    if (!isPlainObject(node) || !Object.hasOwn(node, segment.key)) {
      return { removed: false, empty: isStructurallyEmpty(node, nodeIsListElement) };
    }
    if (isLast) {
      delete node[segment.key];
      return { removed: true, empty: isStructurallyEmpty(node, nodeIsListElement) };
    }
    const result = deleteIn(node[segment.key], segments, index + 1, false);
    if (!result.removed) {
      return { removed: false, empty: isStructurallyEmpty(node, nodeIsListElement) };
    }
    if (result.empty) delete node[segment.key];
    return { removed: true, empty: isStructurallyEmpty(node, nodeIsListElement) };
  }

  if (!Array.isArray(node)) return { removed: false, empty: false };
  const position = node.findIndex((candidate) => isPlainObject(candidate) && candidate.name === segment.name);
  if (position < 0) return { removed: false, empty: node.length === 0 };
  if (isLast) {
    node.splice(position, 1);
    return { removed: true, empty: node.length === 0 };
  }
  const result = deleteIn(node[position], segments, index + 1, true);
  if (!result.removed) return { removed: false, empty: node.length === 0 };
  if (result.empty) node.splice(position, 1);
  return { removed: true, empty: node.length === 0 };
}

function normalizeForce(options) {
  const force = options === undefined || options === null ? undefined : options.force;
  const forcedPaths = new Set();
  let forceAll = false;
  if (force === undefined || force === false || force === null) {
    return { forcedPaths, forceAll };
  }
  if (force === true) return { forcedPaths, forceAll: true };
  if (typeof force === 'string') {
    forcedPaths.add(force);
    return { forcedPaths, forceAll };
  }
  if (Array.isArray(force)) {
    for (const path of force) {
      if (typeof path !== 'string' || path === '') {
        throw new TypeError('force entries must be non-empty path strings');
      }
      forcedPaths.add(path);
    }
    return { forcedPaths, forceAll };
  }
  throw new TypeError('options.force must be a boolean, a path string, or an array of path strings');
}

function cloneOwnership(ownership) {
  const clone = new Map();
  for (const [path, entry] of ownership) {
    clone.set(path, { owners: new Set(entry.owners), segments: entry.segments.slice() });
  }
  return clone;
}

function isKindCompatible(declarationContainer, liveContainer) {
  return declarationContainer.kind === liveContainer.kind;
}

/**
 * Collect the union of owners of every owned leaf strictly beneath `segments`.
 * The applicant is excluded: their own previous claims do not conflict.
 */
function ownersBeneath(segments, ownership, applicant) {
  const owners = new Set();
  for (const [path, entry] of ownership) {
    if (!isPrefix(segments, entry.segments)) continue;
    for (const owner of entry.owners) {
      if (owner !== applicant) owners.add(owner);
    }
  }
  return owners;
}

/**
 * Rendered-path prefix test. The character following the prefix must be a path
 * separator, so `spec.containers[name=a]` is not a prefix of
 * `spec.containers[name=a\.b].image` (escaped names keep their backslash).
 */
function isPathPrefix(prefix, path) {
  if (!path.startsWith(prefix)) return false;
  if (path.length === prefix.length) return true;
  const boundary = path[prefix.length];
  return boundary === '.' || boundary === '[';
}

function detectConflicts({
  liveResource,
  declaration,
  declarationLeaves,
  declarationContainers,
  ownership,
  applicant,
  forcedPaths,
  forceAll,
}) {
  const liveAccumulator = createAccumulator();
  flatten(liveResource, [], liveAccumulator);

  const conflicts = new Map();
  const forcedConflictPaths = new Set();
  // Force matching is done on whole path segments, never on raw string
  // prefixes, so an element named `a.b` can never be mistaken for `a`.
  // Forcing a declared leaf also authorizes the shape collision it creates at
  // an ancestor, and forcing an ancestor covers the collisions below it.
  const isForced = (path) => {
    if (forceAll) return true;
    for (const forcedPath of forcedPaths) {
      if (path === forcedPath) return true;
      if (isPathPrefix(forcedPath, path) || isPathPrefix(path, forcedPath)) return true;
    }
    return false;
  };

  const record = (path, details, ownerSet) => {
    const owners = [...ownerSet].filter((owner) => owner !== applicant).sort();
    if (owners.length === 0) return;
    const forced = isForced(path);
    if (forced) forcedConflictPaths.add(path);
    const existing = conflicts.get(path);
    if (existing) {
      existing.owners = [...new Set([...existing.owners, ...owners])].sort();
      existing.forced = existing.forced || forced;
      return;
    }
    conflicts.set(path, {
      path,
      reason: details.reason,
      currentValue: details.currentValue,
      requestedValue: details.requestedValue,
      owners,
      forced,
    });
  };

  for (const leaf of declarationLeaves.values()) {
    const liveLeaf = liveAccumulator.leaves.get(leaf.path);
    if (liveLeaf) {
      if (!isDeepStrictEqual(liveLeaf.value, leaf.value)) {
        record(
          leaf.path,
          {
            reason: 'value',
            currentValue: cloneValue(liveLeaf.value),
            requestedValue: cloneValue(leaf.value),
          },
          ownership.get(leaf.path)?.owners ?? new Set(),
        );
      }
      continue;
    }

    // A leaf is replacing a live leaf at an ancestor path (type collision).
    const ancestorLeaf = findLiveLeafAncestor(leaf.segments, liveAccumulator.leaves);
    if (ancestorLeaf) {
      record(
        ancestorLeaf.path,
        {
          reason: 'shape',
          currentValue: cloneValue(ancestorLeaf.value),
          requestedValue: cloneValue(resolveValue(declaration, ancestorLeaf.segments).value),
        },
        ownership.get(ancestorLeaf.path)?.owners ?? new Set(),
      );
      continue;
    }

    // A leaf is replacing a structural subtree that has owned descendants.
    const descendantOwners = ownersBeneath(leaf.segments, ownership, applicant);
    if (descendantOwners.size > 0) {
      const live = resolveValue(liveResource, leaf.segments);
      record(
        leaf.path,
        {
          reason: 'shape',
          currentValue: live.found ? cloneValue(live.value) : undefined,
          requestedValue: cloneValue(leaf.value),
        },
        descendantOwners,
      );
    }
  }

  for (const container of declarationContainers.values()) {
    const liveLeaf = liveAccumulator.leaves.get(container.path);
    if (liveLeaf) {
      const requested = resolveValue(declaration, container.segments);
      record(
        container.path,
        {
          reason: 'shape',
          currentValue: cloneValue(liveLeaf.value),
          requestedValue: requested.found ? cloneValue(requested.value) : undefined,
        },
        ownership.get(container.path)?.owners ?? new Set(),
      );
      continue;
    }

    const ancestorLeaf = findLiveLeafAncestor(container.segments, liveAccumulator.leaves);
    if (ancestorLeaf) {
      record(
        ancestorLeaf.path,
        {
          reason: 'shape',
          currentValue: cloneValue(ancestorLeaf.value),
          requestedValue: cloneValue(resolveValue(declaration, ancestorLeaf.segments).value),
        },
        ownership.get(ancestorLeaf.path)?.owners ?? new Set(),
      );
      continue;
    }

    const liveContainer = liveAccumulator.containers.get(container.path);
    if (liveContainer && !isKindCompatible(container, liveContainer)) {
      // object <-> list replacement destroys the live subtree wholesale.
      const descendantOwners = ownersBeneath(container.segments, ownership, applicant);
      if (descendantOwners.size > 0) {
        record(
          container.path,
          {
            reason: 'shape',
            currentValue: cloneValue(liveContainer.value),
            requestedValue: cloneValue(container.value),
          },
          descendantOwners,
        );
      }
    }
  }

  const ordered = [...conflicts.values()].sort((left, right) => left.path.localeCompare(right.path));
  return { conflicts: ordered, forcedConflictPaths };
}

function publicConflicts(conflicts) {
  return conflicts.map(({ path, reason, currentValue, requestedValue, owners }) => ({
    path,
    reason,
    currentValue,
    requestedValue,
    owners,
  }));
}

function commitApply({
  liveResource,
  ownership,
  declaration,
  declarationLeaves,
  declarationContainers,
  applicant,
  forcedConflictPaths,
}) {
  const liveAccumulator = createAccumulator();
  flatten(liveResource, [], liveAccumulator);

  const resource = mergeValue(liveResource, declaration);
  const nextOwnership = cloneOwnership(ownership);
  const changedPaths = new Set();

  const previouslyOwned = [...ownership.entries()]
    .filter(([, entry]) => entry.owners.has(applicant))
    .map(([path]) => path);

  // Forced transfers: the applicant becomes the sole owner of what it
  // declared at the collision point, and the replaced structure loses its
  // old owners.
  for (const path of forcedConflictPaths) {
    const segments = declarationLeaves.get(path)?.segments
      ?? declarationContainers.get(path)?.segments
      ?? ownership.get(path)?.segments;
    if (!segments) continue;
    for (const candidatePath of [...nextOwnership.keys()]) {
      const candidate = nextOwnership.get(candidatePath);
      if (
        candidatePath === path
        || isPrefix(segments, candidate.segments)
        || isPrefix(candidate.segments, segments)
      ) {
        nextOwnership.delete(candidatePath);
      }
    }
    if (declarationLeaves.has(path)) {
      nextOwnership.set(path, {
        owners: new Set([applicant]),
        segments: declarationLeaves.get(path).segments,
      });
    }
  }

  // Omission revokes only the applicant's own share.
  const deletions = [];
  for (const path of previouslyOwned) {
    if (declarationLeaves.has(path)) continue;
    const entry = nextOwnership.get(path);
    if (!entry) continue;
    entry.owners.delete(applicant);
    if (entry.owners.size === 0) {
      nextOwnership.delete(path);
      deletions.push({ path, segments: entry.segments });
    }
  }

  // Declared leaves are claimed by the applicant (co-owned if the value is
  // already held by another controller).
  for (const leaf of declarationLeaves.values()) {
    const entry = nextOwnership.get(leaf.path);
    if (entry) {
      entry.owners.add(applicant);
    } else {
      nextOwnership.set(leaf.path, { owners: new Set([applicant]), segments: leaf.segments });
    }
    const before = liveAccumulator.leaves.get(leaf.path);
    if (!before || !isDeepStrictEqual(before.value, leaf.value)) changedPaths.add(leaf.path);
  }

  // Delete fields whose last owner left, unless the declaration re-establishes
  // something at or above that path.
  const orderedDeletions = deletions.sort((left, right) => left.path.localeCompare(right.path));
  for (const deletion of orderedDeletions) {
    if (declarationContainers.has(deletion.path) || declarationLeaves.has(deletion.path)) continue;
    if (deleteTreePath(resource, deletion.segments)) changedPaths.add(deletion.path);
  }

  for (const [path, entry] of nextOwnership) {
    if (entry.owners.size === 0) nextOwnership.delete(path);
  }

  return { resource, ownership: nextOwnership, changedPaths: [...changedPaths].sort() };
}

export class ResourceStore {
  #resource;
  #ownership;

  constructor(initialResource = {}) {
    if (!isPlainObject(initialResource)) {
      throw new TypeError('initial resource must be a plain object');
    }
    this.#resource = cloneValue(initialResource);
    // Fields present in the initial snapshot are unowned until a controller
    // explicitly claims them.
    this.#ownership = new Map();
  }

  /**
   * Apply a declaration on behalf of `owner`.
   *
   * @param {string} owner
   * @param {Record<string, unknown>} declaration
   * @param {{force?: boolean|string|string[]}} [options]
   * @returns {{resource: object, owners: Array<{path:string,owners:string[]}>, changedPaths: string[]}}
   */
  apply(owner, declaration, options = {}) {
    if (typeof owner !== 'string' || owner.trim() === '') {
      throw new TypeError('owner must be a non-empty string');
    }
    if (!isPlainObject(declaration)) {
      throw new TypeError('declaration must be a plain object');
    }
    const normalizedOptions = options === null || options === undefined ? {} : options;
    if (!isPlainObject(normalizedOptions)) {
      throw new TypeError('options must be a plain object');
    }
    const { forcedPaths, forceAll } = normalizeForce(normalizedOptions);

    const declarationAccumulator = createAccumulator();
    flatten(declaration, [], declarationAccumulator);

    const { conflicts, forcedConflictPaths } = detectConflicts({
      liveResource: this.#resource,
      declaration,
      declarationLeaves: declarationAccumulator.leaves,
      declarationContainers: declarationAccumulator.containers,
      ownership: this.#ownership,
      applicant: owner,
      forcedPaths,
      forceAll,
    });

    const blocking = conflicts.filter((conflict) => !conflict.forced);
    if (blocking.length > 0) {
      // Nothing has been written yet: the store and its ownership map are
      // exactly as they were before this apply.
      throw new OwnershipConflictError(publicConflicts(blocking));
    }

    const result = commitApply({
      liveResource: this.#resource,
      ownership: this.#ownership,
      declaration,
      declarationLeaves: declarationAccumulator.leaves,
      declarationContainers: declarationAccumulator.containers,
      applicant: owner,
      forcedConflictPaths,
    });

    this.#resource = result.resource;
    this.#ownership = result.ownership;

    return {
      resource: cloneValue(this.#resource),
      owners: this.#ownerList(),
      changedPaths: result.changedPaths,
    };
  }

  get resource() {
    return cloneValue(this.#resource);
  }

  get() {
    return cloneValue(this.#resource);
  }

  snapshot() {
    return {
      resource: cloneValue(this.#resource),
      owners: this.#ownerList(),
    };
  }

  ownersOf(path) {
    if (typeof path !== 'string' || path === '') {
      throw new TypeError('path must be a non-empty string');
    }
    const entry = this.#ownership.get(path);
    return entry ? [...entry.owners].sort() : [];
  }

  ownedBy(owner) {
    if (typeof owner !== 'string' || owner === '') {
      throw new TypeError('owner must be a non-empty string');
    }
    const paths = [];
    for (const [path, entry] of this.#ownership) {
      if (entry.owners.has(owner)) paths.push(path);
    }
    return paths.sort();
  }

  #ownerList() {
    return [...this.#ownership.entries()]
      .map(([path, entry]) => ({ path, owners: [...entry.owners].sort() }))
      .sort((left, right) => left.path.localeCompare(right.path));
  }
}

export function createResourceStore(initialResource = {}) {
  return new ResourceStore(initialResource);
}
