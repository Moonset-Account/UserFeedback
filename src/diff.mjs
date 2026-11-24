import { isDeepStrictEqual } from 'node:util';
import { formatPath } from './paths.mjs';
import { isPlainObject } from './paths.mjs';
import { getIdentities, jsonClone } from './document.mjs';

function equalVisible(a, b) {
  return isDeepStrictEqual(a, b);
}

function nodeAtPath(root, segments) {
  let current = root;
  for (const segment of segments) current = current?.[segment];
  return current;
}

/**
 * Identity-aware structural diff.
 *
 * Array correspondence comes from hidden identities. A reordered identity emits
 * one arrayTransform whose order is expressed in INITIAL indices; new values are
 * net constants. This intentionally does not copy an entire witness preimage:
 * unchanged subtrees are referenced by initial index/path, not embedded.
 */
export function diffDocuments(before, after, pathSegments = [], options = {}) {
  const ops = [];
  const path = formatPath(pathSegments);
  const resetPaths = new Set(options.resetPaths ?? []);
  const isReset = resetPaths.has(path);

  if (isReset && after !== undefined) {
    return [{ kind: 'replace', op: 'replace', path, value: jsonClone(after) }];
  }

  if (Array.isArray(before) && Array.isArray(after)) {
    const beforeIds = getIdentities(before);
    const afterIds = getIdentities(after);
    const sameOrder = beforeIds.length === afterIds.length && beforeIds.every((id, i) => id === afterIds[i]);

    if (sameOrder) {
      for (let i = 0; i < before.length; i += 1) {
        ops.push(...diffDocuments(before[i], after[i], [...pathSegments, String(i)], options));
      }
      return ops;
    }

    // Diff matched elements at their INITIAL positions before reorder transform.
    for (let afterIndex = 0; afterIndex < after.length; afterIndex += 1) {
      const id = afterIds[afterIndex];
      const beforeIndex = beforeIds.indexOf(id);
      if (beforeIndex >= 0) {
        const elementPath = formatPath([...pathSegments, String(beforeIndex)]);
        if (resetPaths.has(elementPath)) {
          // Opaque first-transaction replacement: carry the final constant below.
        } else {
          ops.push(...diffDocuments(
            before[beforeIndex],
            after[afterIndex],
            [...pathSegments, String(beforeIndex)],
            options,
          ));
        }
      } else if (options.identityPaths?.has(id)) {
        const initialSegments = options.identityPaths.get(id);
        const initialPointer = formatPath(initialSegments);
        if (!resetPaths.has(initialPointer)) {
          const sourceNode = nodeAtPath(options.initialRoot ?? before, initialSegments);
          ops.push(...diffDocuments(
            sourceNode,
            after[afterIndex],
            initialSegments,
            options,
          ));
        }
      }
    }

    const order = afterIds.map((id, afterIndex) => {
      const beforeIndex = beforeIds.indexOf(id);
      if (beforeIndex >= 0) {
        const elementPath = formatPath([...pathSegments, String(beforeIndex)]);
        if (resetPaths.has(elementPath)) {
          return { kind: 'initialReset', index: beforeIndex, value: jsonClone(after[afterIndex]) };
        }
        return { kind: 'initial', index: beforeIndex };
      }
      if (options.identityPaths?.has(id)) {
        return { kind: 'globalInitial', initialPath: formatPath(options.identityPaths.get(id)) };
      }
      return { kind: 'new', value: jsonClone(after[afterIndex]) };
    });

    ops.push({
      kind: 'arrayTransform',
      op: 'arrayTransform',
      path,
      arrayPath: path,
      order,
      initialLength: before.length,
      finalLength: after.length,
    });
    return ops;
  }

  if (isPlainObject(before) && isPlainObject(after)) {
    const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
    for (const key of keys) {
      const childPath = [...pathSegments, key];
      const childPointer = formatPath(childPath);
      const inBefore = Object.hasOwn(before, key);
      const inAfter = Object.hasOwn(after, key);
      if (inBefore && !inAfter) {
        ops.push({ kind: 'remove', op: 'remove', path: childPointer });
      } else if (!inBefore && inAfter) {
        ops.push({
          kind: 'insertKey',
          op: 'insert',
          path: childPointer,
          parentPath: path,
          key,
          value: jsonClone(after[key]),
        });
      } else {
        ops.push(...diffDocuments(before[key], after[key], childPath, options));
      }
    }
    return ops;
  }

  if (!equalVisible(before, after)) {
    if (pathSegments.length === 0) {
      ops.push({ kind: 'replace', op: 'replace', path: '', value: jsonClone(after) });
    } else {
      ops.push({ kind: 'replace', op: 'replace', path, value: jsonClone(after) });
    }
  }
  return ops;
}
