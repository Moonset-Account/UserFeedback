// shared-write-check.mjs
//
// 核验库：宿主把数据副本发送给低权限插件前，检查双方对象图之间是否存在
// 可写的共享对象身份（object identity）。
//
// 设计要点（对应题面约束）：
//   1. 只沿「普通对象（plain object）与数组」的【自有数据属性】递归遍历。
//      普通对象 = 原型为 Object.prototype 或 null 的非代理对象；数组为普通数组。
//      遇到其它内建/类实例对象（Map、Date、类实例……）不再继续展开。
//   2. 比较的是实际对象身份（同一个引用），内容相同的独立对象可以通过。
//   3. 共享节点只有在「自身 + 全部可达后代」都冻结、且整条子图都是普通
//      数据（普通对象/数组 + 数据属性 + 普通数据值，无访问器、无代理、
//      无异构对象）时才可接受。
//   4. 访问器属性 / 代理：把该分支标记为「无法核验（unverifiable）」，
//      绝不执行 getter，也绝不触发任何代理回调（trap）。
//   5. 循环引用：每个根各自维护 Visited 集合，BFS 天然收敛终止。
//   6. 输出每个可写（不可接受）共享对象从两侧可达的一对【最短路径】。
//
// 代理识别说明：
//   ECMAScript 本身没有「无陷阱检测 Proxy」的标准手段（任何 get/isFrozen
//   等观测都会触发代理 trap）。本库运行于 Node.js，使用 node:util 的
//   util.types.isProxy —— 它由引擎层直接读取内部槽，不执行任何代理回调，
//   从而满足「不触发代理回调」的要求。

import { types as nodeUtilTypes } from 'node:util';

const isProxy = (value) => {
  try {
    return nodeUtilTypes.isProxy(value);
  } catch {
    // 理论上不会抛出；保守起见按「无法确认」处理为代理。
    return true;
  }
};

/**
 * 判断是否为可展开的普通容器（普通对象或数组），且不是代理。
 * 不执行任何业务代码：
 *   - Array.isArray 读取对象内部槽，不触发代理 trap；
 *   - Object.getPrototypeOf 同样是内部槽操作。
 */
function isPlainContainer(value) {
  if (value === null || typeof value !== 'object') return false;
  if (isProxy(value)) return false;
  if (Array.isArray(value)) return true;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/**
 * 对单个根做 BFS 遍历，产出：
 *   map:       Map<object, { path: Segment[], kind: 'object'|'array'|'exotic' }>
 *              每个【可达对象身份】-> 一条最短路径（BFS 首次到达即最短）。
 *   unverifiable: Array<{ path: Segment[], reason: 'accessor'|'proxy'|'function'|'root-proxy' }>
 *
 * 路径段 Segment：字符串键（数组下标/普通键）或 symbol 键，保持原始类型。
 * 访问器与代理分支只记录、不取值、不展开。
 */
function walkGraph(root) {
  const map = new Map();
  const unverifiable = [];

  // 根不是对象：没有任何可达对象身份。
  if (root === null || (typeof root !== 'object' && typeof root !== 'function')) {
    return { map, unverifiable };
  }

  // 根本身是代理或函数：整根无法核验，不触发任何 trap、不执行函数。
  if (typeof root === 'function' || isProxy(root)) {
    unverifiable.push({
      path: [],
      reason: typeof root === 'function' && !isProxy(root) ? 'function' : 'root-proxy',
    });
    return { map, unverifiable };
  }

  // 根是对象但不是普通容器（Map/Date/类实例……）：记录其身份但不展开，
  // 交由共享判定处理（非普通对象共享一律不可接受）。
  if (!isPlainContainer(root)) {
    map.set(root, { path: [], kind: 'exotic' });
    return { map, unverifiable };
  }

  const rootKind = Array.isArray(root) ? 'array' : 'object';
  map.set(root, { path: [], kind: rootKind });

  const queue = [root];
  const visited = new Set([root]);

  while (queue.length > 0) {
    const node = queue.shift();
    const nodeEntry = map.get(node);

    let keys;
    try {
      // Reflect.ownKeys = 字符串键 + symbol 键（含不可枚举属性），
      // 只依赖对象内部槽，不触发 getter。
      keys = Reflect.ownKeys(node);
    } catch {
      // 普通对象/数组不会抛；保守标记为无法核验。
      unverifiable.push({ path: nodeEntry.path, reason: 'proxy' });
      continue;
    }

    for (const key of keys) {
      let descriptor;
      try {
        descriptor = Object.getOwnPropertyDescriptor(node, key);
      } catch {
        unverifiable.push({ path: [...nodeEntry.path, key], reason: 'proxy' });
        continue;
      }

      // 不存在（理论上不会）或为访问器属性：不取值、不触发 getter。
      if (!descriptor || 'get' in descriptor || 'set' in descriptor) {
        unverifiable.push({ path: [...nodeEntry.path, key], reason: 'accessor' });
        continue;
      }

      // 数据属性才沿其值继续。
      const value = descriptor.value;
      if (value === null || (typeof value !== 'object' && typeof value !== 'function')) {
        continue; // 原始值 / null：无对象身份，跳过
      }

      // 函数（含代理函数）：不调用、不展开，标记无法核验。
      if (typeof value === 'function') {
        unverifiable.push({
          path: [...nodeEntry.path, key],
          reason: isProxy(value) ? 'proxy' : 'function',
        });
        continue;
      }
      if (isProxy(value)) {
        unverifiable.push({ path: [...nodeEntry.path, key], reason: 'proxy' });
        continue;
      }

      if (!isPlainContainer(value)) {
        // 内建异构对象 / 类实例：记录身份，不展开；共享时一律不可接受。
        if (!map.has(value)) {
          map.set(value, { path: [...nodeEntry.path, key], kind: 'exotic' });
        }
        continue;
      }

      if (visited.has(value)) {
        // 循环引用 / 重复引用：已在图中，路径保持 BFS 首次到达的最短路径。
        continue;
      }
      visited.add(value);

      const kind = Array.isArray(value) ? 'array' : 'object';
      const path = [...nodeEntry.path, key];
      map.set(value, { path, kind });
      queue.push(value);
    }
  }

  return { map, unverifiable };
}

/**
 * 校验一个【共享】普通容器子图是否可接受：
 * 自身及全部可达后代都冻结，且全部为普通数据（无访问器、无代理、无异构对象）。
 *
 * 这里的遍历同样不执行 getter、不触发代理回调。
 *
 * @returns {{ ok: true } | { ok: false, reason: string, relativePath: Segment[] }}
 */
function checkFrozenPlainGraph(shared) {
  if (!Object.isFrozen(shared)) {
    return { ok: false, reason: 'not-frozen', relativePath: [] };
  }

  const visited = new Set([shared]);
  const stack = [{ node: shared, relPath: [] }];

  while (stack.length > 0) {
    const { node, relPath } = stack.pop();

    let keys;
    try {
      keys = Reflect.ownKeys(node);
    } catch {
      return { ok: false, reason: 'proxy-descendant', relativePath: relPath };
    }

    for (const key of keys) {
      let descriptor;
      try {
        descriptor = Object.getOwnPropertyDescriptor(node, key);
      } catch {
        return { ok: false, reason: 'proxy-descendant', relativePath: [...relPath, key] };
      }

      if (!descriptor || 'get' in descriptor || 'set' in descriptor) {
        return { ok: false, reason: 'accessor-descendant', relativePath: [...relPath, key] };
      }

      const value = descriptor.value;
      if (value === null || (typeof value !== 'object' && typeof value !== 'function')) {
        continue;
      }

      if (typeof value === 'function') {
        return {
          ok: false,
          reason: isProxy(value) ? 'proxy-descendant' : 'function-descendant',
          relativePath: [...relPath, key],
        };
      }
      if (isProxy(value)) {
        return { ok: false, reason: 'proxy-descendant', relativePath: [...relPath, key] };
      }

      if (!isPlainContainer(value)) {
        return { ok: false, reason: 'non-plain-descendant', relativePath: [...relPath, key] };
      }

      if (!Object.isFrozen(value)) {
        return { ok: false, reason: 'not-frozen', relativePath: [...relPath, key] };
      }

      if (visited.has(value)) continue; // 循环引用：已检查过，收敛
      visited.add(value);
      stack.push({ node: value, relPath: [...relPath, key] });
    }
  }

  return { ok: true };
}

/**
 * 格式化单个路径段。
 * 数组下标 / 标识符风格的字符串键用点号或方括号呈现，其余字符串加引号，
 * symbol 以 [Symbol(...)] 呈现。
 */
export function formatSegment(segment) {
  if (typeof segment === 'symbol') {
    return `[${String(segment)}]`;
  }
  if (/^(0|[1-9][0-9]*)$/.test(segment)) {
    return `[${segment}]`;
  }
  if (/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(segment)) {
    return `.${segment}`;
  }
  return `[${JSON.stringify(segment)}]`;
}

/** 把路径段数组渲染为人类可读的路径字符串，根为空路径时渲染为 "<root>"。 */
export function formatPath(segments) {
  if (!segments || segments.length === 0) return '<root>';
  let out = '<root>';
  for (const segment of segments) {
    out += formatSegment(segment);
  }
  return out;
}

/**
 * 主入口：核验宿主对象与拟发送副本之间的可写共享身份。
 *
 * @param {*} host 宿主侧对象图根
 * @param {*} copy 拟发送给插件的副本对象图根
 * @returns {object} 核验报告
 *   ok:            true 表示不存在可写共享身份，且没有无法核验的分支
 *   violations:    不可接受的共享对象，每条含两侧最短路径与原因
 *   accepted:      可接受的「全冻结普通数据」共享对象
 *   unverifiable:  无法核验的分支（访问器 / 代理），含所在侧与路径
 *   hostPaths/copyPaths: 内部最短路径表（段数组，供程序化消费）
 */
export function verifySharedWritable(host, copy) {
  const hostWalk = walkGraph(host);
  const copyWalk = walkGraph(copy);

  const unverifiable = [
    ...hostWalk.unverifiable.map((u) => ({ side: 'host', ...u })),
    ...copyWalk.unverifiable.map((u) => ({ side: 'copy', ...u })),
  ];

  const violations = [];
  const accepted = [];

  // 身份交集：只有两侧都到达的同一引用才算共享。
  for (const [identity, hostEntry] of hostWalk.map) {
    const copyEntry = copyWalk.map.get(identity);
    if (!copyEntry) continue;

    const pair = {
      identity,
      hostPath: hostEntry.path,
      copyPath: copyEntry.path,
      hostPathString: formatPath(hostEntry.path),
      copyPathString: formatPath(copyEntry.path),
    };

    // 异构对象 / 类实例等非普通容器：即使冻结也不符合「都是普通数据」。
    if (hostEntry.kind === 'exotic' || copyEntry.kind === 'exotic') {
      violations.push({ ...pair, reason: 'non-plain-object' });
      continue;
    }

    const check = checkFrozenPlainGraph(identity);
    if (check.ok) {
      accepted.push(pair);
    } else {
      violations.push({
        ...pair,
        reason: check.reason,
        relativePath: check.relativePath,
        relativePathString: formatPath(check.relativePath),
      });
    }
  }

  return {
    ok: violations.length === 0 && unverifiable.length === 0,
    violations,
    accepted,
    unverifiable,
    hostPaths: hostWalk.map,
    copyPaths: copyWalk.map,
  };
}

export default verifySharedWritable;
