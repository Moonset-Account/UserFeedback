import { parsePath, isPlainObject } from './paths.mjs';
import { TransactionError } from './errors.mjs';

export const IDENTITIES = Symbol('structural-patch.element-identities');

let identityCounter = 0;
export function createIdentity(prefix = 'el') {
  identityCounter += 1;
  return `${prefix}_${identityCounter.toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

export function resetIdentityCounter() {
  identityCounter = 0;
}

function defineIdentities(array, ids) {
  Object.defineProperty(array, IDENTITIES, {
    value: ids,
    writable: true,
    enumerable: false,
    configurable: true,
  });
  return array;
}

export function getIdentityList(array) {
  if (!Array.isArray(array)) return null;
  const ids = array[IDENTITIES];
  if (Array.isArray(ids) && ids.length === array.length) return ids;
  const fresh = array.map(() => createIdentity());
  defineIdentities(array, fresh);
  return fresh;
}

/**
 * Deep-clone a JSON document and attach hidden identity vectors to arrays.
 * Existing vectors are preserved only when preserve is true and length matches.
 */
export function materialize(value, options = {}) {
  const preserve = options.preserve ?? true;
  const createId = options.createId ?? createIdentity;
  if (Array.isArray(value)) {
    const result = [];
    let inherited = preserve ? value[IDENTITIES] : null;
    if (!Array.isArray(inherited) || inherited.length !== value.length) inherited = null;
    const ids = value.map((_, index) => inherited?.[index] ?? createId());
    defineIdentities(result, ids);
    for (const item of value) result.push(materialize(item, options));
    return result;
  }
  if (isPlainObject(value)) {
    const result = {};
    for (const [key, child] of Object.entries(value)) {
      result[key] = materialize(child, options);
    }
    return result;
  }
  if (value === undefined) return undefined;
  if (typeof value === 'bigint' || typeof value === 'function' || typeof value === 'symbol') {
    throw new TransactionError('UnsupportedValue', `Unsupported JSON value type: ${typeof value}`);
  }
  return value;
}

export function initializeIdentities(value, createId = createIdentity) {
  if (Array.isArray(value)) {
    const existing = value[IDENTITIES];
    if (!Array.isArray(existing) || existing.length !== value.length) {
      defineIdentities(value, value.map(() => createId()));
    }
    for (const child of value) initializeIdentities(child, createId);
  } else if (isPlainObject(value)) {
    for (const child of Object.values(value)) initializeIdentities(child, createId);
  }
  return value;
}

export function cloneDocument(doc) {
  return materialize(doc, { preserve: true });
}

export function getIdentities(doc, path = []) {
  const segments = parsePath(path);
  let current = doc;
  for (const seg of segments) {
    if (current == null || typeof current !== 'object') {
      throw new TransactionError('PathNotFound', `Cannot resolve ${seg} while reading identities`);
    }
    current = current[seg];
  }
  if (!Array.isArray(current)) {
    throw new TransactionError('NotAnArray', 'Identity list requires an array path');
  }
  return [...getIdentityList(current)];
}

export function getIdentityAt(doc, path) {
  const segments = parsePath(path);
  if (segments.length === 0) {
    throw new TransactionError('InvalidPath', 'Root has no element identity');
  }
  const parentPath = segments.slice(0, -1);
  const last = segments.at(-1);
  let parent = doc;
  for (const seg of parentPath) parent = parent?.[seg];
  if (!Array.isArray(parent)) return undefined;
  const idx = Number(last);
  if (!Number.isInteger(idx) || idx < 0 || idx >= parent.length) return undefined;
  return getIdentityList(parent)[idx];
}

export function findIdentityPath(root, id) {
  function walk(node, path) {
    if (Array.isArray(node)) {
      const ids = getIdentityList(node);
      for (let i = 0; i < node.length; i += 1) {
        if (ids[i] === id) return [...path, String(i)];
        const found = walk(node[i], [...path, String(i)]);
        if (found) return found;
      }
    } else if (isPlainObject(node)) {
      for (const [key, child] of Object.entries(node)) {
        const found = walk(child, [...path, key]);
        if (found) return found;
      }
    }
    return null;
  }
  return walk(root, []);
}

export function findIdentityInArray(array, id) {
  const ids = getIdentityList(array);
  return ids.indexOf(id);
}

export function assertUniqueIdentities(doc) {
  const seen = new Set();
  const duplicates = [];
  function walk(node) {
    if (Array.isArray(node)) {
      for (const id of getIdentityList(node)) {
        if (seen.has(id)) duplicates.push(id);
        seen.add(id);
      }
      node.forEach(walk);
    } else if (isPlainObject(node)) {
      Object.values(node).forEach(walk);
    }
  }
  walk(doc);
  if (duplicates.length) {
    throw new TransactionError('DuplicateIdentity', 'Duplicate hidden element identities', { duplicates });
  }
  return true;
}

export function collectIdentityPaths(root, pathSegments = [], map = new Map()) {
  if (Array.isArray(root)) {
    const ids = getIdentityList(root);
    ids.forEach((id, index) => {
      const childPath = [...pathSegments, String(index)];
      map.set(id, childPath);
      collectIdentityPaths(root[index], childPath, map);
    });
  } else if (isPlainObject(root)) {
    for (const [key, value] of Object.entries(root)) {
      collectIdentityPaths(value, [...pathSegments, key], map);
    }
  }
  return map;
}

export function captureIdentityTree(node) {
  if (Array.isArray(node)) {
    return {
      ids: [...getIdentityList(node)],
      children: node.map(captureIdentityTree),
    };
  }
  if (isPlainObject(node)) {
    const children = {};
    for (const [key, value] of Object.entries(node)) {
      const child = captureIdentityTree(value);
      if (child) children[key] = child;
    }
    return { children };
  }
  return null;
}

export function restoreIdentityTree(node, tree) {
  if (!tree || node == null || typeof node !== 'object') return node;
  if (Array.isArray(node) && Array.isArray(tree.ids) && tree.ids.length === node.length) {
    Object.defineProperty(node, IDENTITIES, {
      value: [...tree.ids],
      writable: true,
      enumerable: false,
      configurable: true,
    });
    node.forEach((child, i) => restoreIdentityTree(child, tree.children?.[i]));
  } else if (isPlainObject(node) && tree.children) {
    for (const [key, childTree] of Object.entries(tree.children)) {
      if (Object.hasOwn(node, key)) restoreIdentityTree(node[key], childTree);
    }
  }
  return node;
}

export function jsonClone(value) {
  return JSON.parse(JSON.stringify(value));
}
