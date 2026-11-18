// analyzer.mjs
//
// 面向浏览器扩展消息处理器的来源检查分析器。
// 输入：简化的处理器控制流图（CFG）。节点可以读取消息字段、比较完整
// origin、比较窗口 source、分支、赋值、合并对象或调用敏感接口。
//
// 分析目标：对每个敏感调用，判断所有可达路径是否同时满足该调用登记的
// origin 与 source 条件，且检查必须约束到最终用于调用的同一条消息上。
//
// 方法：
//   - 每个变量携带"字段来源"集合（may 分析，汇合取并集）：来源形如
//     msg:<消息>.<字段>，另有 const（常量）与 unknown（无法解析的调用结果）。
//   - 每个程序点携带"已确立检查"集合（must 分析，汇合取交集）：形如
//     origin:<消息>=<期望值> / source:<消息>=<期望值>，只在 check 节点的
//     pass 边上、且被比较值的来源确为某消息的 origin/source 字段时加入。
//   - 两个格都是有限的（消息、字段、检查期望值均来自 CFG 本身），
//     因此循环上的不动点迭代必然收敛。
//   - 对判定为 violated 的调用，在 (节点, 抽象状态) 乘积图上做 BFS，
//     给出一条具体失败路径及受影响参数；状态空间有限，搜索必然终止。

export const SRC = Object.freeze({ CONST: 'const', UNKNOWN: 'unknown' });

export function msgFieldSrc(message, field) {
  return `msg:${message}.${field}`;
}

export function parseMsgField(src) {
  if (typeof src !== 'string' || !src.startsWith('msg:')) return null;
  const rest = src.slice(4);
  const dot = rest.indexOf('.');
  if (dot <= 0 || dot === rest.length - 1) return null;
  return { message: rest.slice(0, dot), field: rest.slice(dot + 1) };
}

// ---------------------------------------------------------------------------
// 抽象值：标量 { kind:'scalar', sources:Set } 或
//         对象 { kind:'object', fields:Map<field,Set<src>>, base:消息名|null }
// 对象的 base 表示"未显式覆盖的字段回落到某条消息的同名字段"。
// ---------------------------------------------------------------------------

const scalar = (sources) => ({ kind: 'scalar', sources: new Set(sources) });
const objectVal = (fields = new Map(), base = null) => ({ kind: 'object', fields, base });

function unionSources(a, b) {
  const out = new Set(a || []);
  for (const s of b || []) out.add(s);
  return out;
}

function valueSources(v) {
  if (!v) return new Set([SRC.UNKNOWN]);
  if (v.kind === 'scalar') return v.sources.size ? new Set(v.sources) : new Set([SRC.UNKNOWN]);
  const out = new Set();
  for (const ss of v.fields.values()) for (const s of ss) out.add(s);
  if (v.base) out.add(msgFieldSrc(v.base, '*'));
  if (!out.size) out.add(SRC.UNKNOWN);
  return out;
}

function joinValues(a, b) {
  if (!a) return b;
  if (!b) return a;
  if (a.kind === 'scalar' && b.kind === 'scalar') {
    return scalar(unionSources(a.sources, b.sources));
  }
  if (a.kind === 'object' && b.kind === 'object') {
    // base 不一致时放弃回落（退化为未知字段来源），保持保守。
    const base = a.base === b.base ? a.base : null;
    const fields = new Map();
    for (const k of new Set([...a.fields.keys(), ...b.fields.keys()])) {
      fields.set(k, unionSources(a.fields.get(k), b.fields.get(k)));
    }
    return objectVal(fields, base);
  }
  // 标量与对象混合：折叠为标量并补 unknown，保持保守。
  return scalar(unionSources(valueSources(a), unionSources(valueSources(b), [SRC.UNKNOWN])));
}

// 状态：{ vars:Map<var,Value>, facts:Set<fact> }
// vars 是 may（并集汇合），facts 是 must（交集汇合）。
function joinStates(a, b) {
  const vars = new Map();
  for (const k of new Set([...a.vars.keys(), ...b.vars.keys()])) {
    vars.set(k, joinValues(a.vars.get(k), b.vars.get(k)));
  }
  const facts = new Set([...a.facts].filter((f) => b.facts.has(f)));
  return { vars, facts };
}

function canonicalValue(v) {
  if (v.kind === 'scalar') return { kind: 'scalar', sources: [...v.sources].sort() };
  const fields = {};
  for (const [f, ss] of [...v.fields.entries()].sort()) fields[f] = [...ss].sort();
  return { kind: 'object', fields, base: v.base };
}

function canonicalState(s) {
  const vars = {};
  for (const [k, v] of [...s.vars.entries()].sort()) vars[k] = canonicalValue(v);
  return JSON.stringify({ vars, facts: [...s.facts].sort() });
}

// ---------------------------------------------------------------------------
// CFG 校验与后继边
// ---------------------------------------------------------------------------

const KINDS = new Set([
  'nop', 'const', 'assign', 'read', 'merge',
  'checkOrigin', 'checkSource', 'branch', 'call', 'unknownCall',
]);

function successors(node) {
  switch (node.kind) {
    case 'checkOrigin':
    case 'checkSource':
      return [['pass', node.pass], ['fail', node.fail]];
    case 'branch':
      return [['true', node.true], ['false', node.false]];
    default:
      return node.next != null ? [['next', node.next]] : [];
  }
}

function normalizeCfg(cfg) {
  if (!cfg || typeof cfg !== 'object') throw new Error('cfg must be an object');
  const { nodes, entry } = cfg;
  if (!nodes || typeof nodes !== 'object') throw new Error('cfg.nodes must be an object');
  if (!nodes[entry]) throw new Error(`cfg.entry "${entry}" not found in nodes`);
  for (const [id, n] of Object.entries(nodes)) {
    if (!n || !KINDS.has(n.kind)) throw new Error(`node ${id}: unknown kind "${n && n.kind}"`);
    for (const [, succ] of successors(n)) {
      if (succ != null && !nodes[succ]) throw new Error(`node ${id}: missing successor "${succ}"`);
    }
  }
  return { nodes, entry, messageSet: new Set(cfg.messages || []) };
}

// ---------------------------------------------------------------------------
// 转移函数
// ---------------------------------------------------------------------------

function readField(state, cfg, objectName, field) {
  // 直接读消息变量的字段：来源就是该消息的该字段。
  if (cfg.messageSet.has(objectName) && !state.vars.has(objectName)) {
    return scalar([msgFieldSrc(objectName, field)]);
  }
  const obj = state.vars.get(objectName);
  if (!obj) return scalar([SRC.UNKNOWN]);
  if (obj.kind === 'object') {
    const f = obj.fields.get(field);
    if (f) return scalar(f);
    if (obj.base) return scalar([msgFieldSrc(obj.base, field)]);
    return scalar([SRC.UNKNOWN]);
  }
  return scalar([SRC.UNKNOWN]);
}

function transfer(node, state, cfg, edge) {
  const vars = new Map(state.vars);
  const facts = new Set(state.facts);
  switch (node.kind) {
    case 'nop':
    case 'branch':
    case 'call':
      break;
    case 'const':
      vars.set(node.target, scalar([SRC.CONST]));
      break;
    case 'assign':
      vars.set(node.target, state.vars.get(node.from) || scalar([SRC.UNKNOWN]));
      break;
    case 'read':
      vars.set(node.target, readField(state, cfg, node.object, node.field));
      break;
    case 'merge': {
      // 合并对象：被覆盖字段的来源重新追踪为覆盖值的来源，
      // 未覆盖字段保留 base 对象（或 base 消息）的来源。
      let baseVal;
      if (cfg.messageSet.has(node.base) && !state.vars.has(node.base)) {
        baseVal = objectVal(new Map(), node.base);
      } else {
        const b = state.vars.get(node.base);
        baseVal = b && b.kind === 'object' ? b : objectVal(new Map(), null);
      }
      const fields = new Map(baseVal.fields);
      for (const [f, varName] of Object.entries(node.overrides || {})) {
        fields.set(f, valueSources(state.vars.get(varName)));
      }
      vars.set(node.target, objectVal(fields, baseVal.base));
      break;
    }
    case 'checkOrigin':
    case 'checkSource': {
      if (edge === 'pass') {
        const field = node.kind === 'checkOrigin' ? 'origin' : 'source';
        const v = state.vars.get(node.value);
        const srcs = v && v.kind === 'scalar' ? v.sources : new Set([SRC.UNKNOWN]);
        // 只有被比较值确实来自某消息的 origin/source 字段，
        // pass 边才能为该消息登记对应事实；检查别的消息不算保护。
        for (const s of srcs) {
          const mf = parseMsgField(s);
          if (mf && mf.field === field) facts.add(`${field}:${mf.message}=${node.expected}`);
        }
      }
      break;
    }
    case 'unknownCall':
      // 无法解析的调用结果：保留未知。
      vars.set(node.target, scalar([SRC.UNKNOWN]));
      break;
    default:
      throw new Error(`unhandled node kind "${node.kind}"`);
  }
  return { vars, facts };
}

// ---------------------------------------------------------------------------
// 调用点求值
// ---------------------------------------------------------------------------

function evaluateCall(nodeId, node, state) {
  const require = node.require || {};
  const paramSources = {};
  const usedMessages = new Set();
  const unknownParams = [];
  for (const [arg, varName] of Object.entries(node.args || {})) {
    const srcs = valueSources(state.vars.get(varName));
    paramSources[arg] = [...srcs].sort();
    let hasUnknown = false;
    for (const s of srcs) {
      if (s === SRC.UNKNOWN) hasUnknown = true;
      const mf = parseMsgField(s);
      if (mf) usedMessages.add(mf.message);
    }
    if (hasUnknown) unknownParams.push(arg);
  }
  const missing = [];
  for (const m of [...usedMessages].sort()) {
    if (require.origin != null && !state.facts.has(`origin:${m}=${require.origin}`)) {
      missing.push({ message: m, condition: 'origin', expected: require.origin });
    }
    if (require.source != null && !state.facts.has(`source:${m}=${require.source}`)) {
      missing.push({ message: m, condition: 'source', expected: require.source });
    }
  }
  const missingMessages = new Set(missing.map((x) => x.message));
  const affectedParams = Object.entries(paramSources)
    .filter(([, ss]) => ss.some((s) => {
      const mf = parseMsgField(s);
      return mf && missingMessages.has(mf.message);
    }))
    .map(([a]) => a);
  let verdict;
  if (missing.length) verdict = 'violated';
  else if (unknownParams.length) verdict = 'unknown';
  else verdict = 'protected';
  return {
    nodeId,
    api: node.api ?? null,
    verdict,
    require: { origin: require.origin ?? null, source: require.source ?? null },
    usedMessages: [...usedMessages].sort(),
    missing,
    affectedParams,
    unknownParams,
    paramSources,
    witness: null,
  };
}

// ---------------------------------------------------------------------------
// 失败路径见证：在 (节点, 抽象状态) 乘积图上 BFS，状态空间有限故必然终止。
// ---------------------------------------------------------------------------

function findWitness(cfg, callInfo, options) {
  const cap = options.witnessStateCap ?? 200000;
  const start = { vars: new Map(), facts: new Set() };
  const queue = [{ id: cfg.entry, state: start, path: [{ node: cfg.entry, edgeIn: null }] }];
  const visited = new Set([`${cfg.entry}|${canonicalState(start)}`]);
  let explored = 0;
  while (queue.length) {
    if (++explored > cap) return { error: 'witness-search-capped', explored };
    const { id, state, path } = queue.shift();
    if (id === callInfo.nodeId) {
      const ev = evaluateCall(id, cfg.nodes[id], state);
      if (ev.missing.length) {
        return {
          path,
          missing: ev.missing,
          affectedParams: ev.affectedParams,
          paramSources: ev.paramSources,
        };
      }
    }
    for (const [edge, succId] of successors(cfg.nodes[id])) {
      if (succId == null) continue;
      const out = transfer(cfg.nodes[id], state, cfg, edge);
      const key = `${succId}|${canonicalState(out)}`;
      if (visited.has(key)) continue;
      visited.add(key);
      queue.push({ id: succId, state: out, path: [...path, { node: succId, edgeIn: edge }] });
    }
  }
  return { error: 'no-violating-path-found' };
}

// ---------------------------------------------------------------------------
// 主入口
// ---------------------------------------------------------------------------

export function analyze(cfg, options = {}) {
  const norm = normalizeCfg(cfg);
  const { nodes, entry } = norm;
  const ids = Object.keys(nodes);

  // must 分析的事实全集：所有消息 × 所有出现过的检查期望值。
  const universe = new Set();
  for (const n of Object.values(nodes)) {
    if (n.kind === 'checkOrigin') {
      for (const m of norm.messageSet) universe.add(`origin:${m}=${n.expected}`);
    }
    if (n.kind === 'checkSource') {
      for (const m of norm.messageSet) universe.add(`source:${m}=${n.expected}`);
    }
  }

  const inState = new Map();
  for (const id of ids) inState.set(id, { vars: new Map(), facts: new Set(universe) });
  inState.set(entry, { vars: new Map(), facts: new Set() });

  const iterationCap = options.iterationCap ?? 100000;
  const worklist = [...ids];
  const inQueue = new Set(worklist);
  let iterations = 0;
  while (worklist.length) {
    if (++iterations > iterationCap) break;
    const id = worklist.shift();
    inQueue.delete(id);
    const node = nodes[id];
    const st = inState.get(id);
    for (const [edge, succId] of successors(node)) {
      if (succId == null) continue;
      const out = transfer(node, st, norm, edge);
      const prev = inState.get(succId);
      const joined = joinStates(prev, out);
      if (canonicalState(joined) !== canonicalState(prev)) {
        inState.set(succId, joined);
        if (!inQueue.has(succId)) {
          worklist.push(succId);
          inQueue.add(succId);
        }
      }
    }
  }
  const converged = worklist.length === 0;

  const calls = [];
  for (const id of [...ids].sort()) {
    const node = nodes[id];
    if (node.kind !== 'call') continue;
    calls.push(evaluateCall(id, node, inState.get(id)));
  }
  for (const c of calls) {
    if (c.verdict === 'violated') c.witness = findWitness(norm, c, options);
  }

  return {
    calls,
    stats: { converged, iterations, nodeCount: ids.length },
  };
}
