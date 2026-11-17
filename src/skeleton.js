// 混淆骨架：把规范化名称中的混淆字符替换成本地映射里的骨架字符，
// 同时保留每个输出码点到原始名称码点位置的映射，便于报告“发生混淆的字符位置”。

import { normalizeName, toCodePoints } from './normalization.js';

/**
 * 规范化本地混淆映射。
 * 输入形态：{ "а": "a", "е": "e", ... } 或 { "а": { skeleton: "a", name: "cyrillic a" } }
 * 统一成 Map<混淆字符, { skeleton, name? }>。
 */
export function loadConfusableMap(raw) {
  const map = new Map();
  if (!raw || typeof raw !== 'object') return map;
  for (const [ch, val] of Object.entries(raw)) {
    // 键可能是多码点？本地映射只接受单码点键，否则忽略并交由调用方在样本里说明。
    const cps = toCodePoints(ch);
    if (cps.length !== 1) continue;
    if (typeof val === 'string') {
      if (toCodePoints(val).length >= 1) map.set(ch, { skeleton: val, name: undefined });
    } else if (val && typeof val === 'object' && typeof val.skeleton === 'string') {
      map.set(ch, { skeleton: val.skeleton, name: val.name });
    }
  }
  return map;
}

/**
 * 基于规范化结果构造混淆骨架。
 *
 * 返回：
 * {
 *   skeleton: string,
 *   spans: [{ from, to, originalIndex, char, mappedTo, kind }]
 * }
 *  - 普通字符 kind='plain'；命中映射的字符 kind='confusable'，并记录 mappedTo。
 *  - originalIndex 是相对“裸名”的码点索引（与 normalization.positions 同一坐标系）。
 */
export function buildSkeleton(normalizedResult, confusableMap) {
  const { normalized, positions, scopeKept } = normalizedResult;
  const parsed = normalized.startsWith('@') && scopeKept
    ? normalized.slice(normalized.indexOf('/') + 1)
    : normalized;
  const offset = scopeKept ? normalized.indexOf('/') + 1 : 0;

  const chars = toCodePoints(parsed);
  const spans = [];
  let out = '';
  chars.forEach((cp, i) => {
    const originalIndex = positions[i];
    const hit = confusableMap.get(cp.char);
    if (hit) {
      out += hit.skeleton;
      spans.push({
        from: offset + cp.index,
        to: offset + cp.index + cp.char.length,
        originalIndex,
        char: cp.char,
        mappedTo: hit.skeleton,
        kind: 'confusable',
        name: hit.name,
      });
    } else {
      out += cp.char;
      spans.push({
        from: offset + cp.index,
        to: offset + cp.index + cp.char.length,
        originalIndex,
        char: cp.char,
        mappedTo: cp.char,
        kind: 'plain',
      });
    }
  });

  return { skeleton: out, spans };
}

/**
 * 一步完成：规范化 + 骨架。
 */
export function normalizeAndSkeleton(spec, ruleSet, confusableMap) {
  const norm = normalizeName(spec, ruleSet);
  const skel = buildSkeleton(norm, confusableMap);
  return { ...norm, ...skel };
}

/**
 * 比较两个骨架结果，返回命中的混淆字符明细（以左侧声明侧为基准）。
 * 仅在 skeleton 字符串完全相等时才有意义；同时排除“规范化名也完全相等”的纯同名情况。
 */
export function diffConfusables(declSkel, internalSkel) {
  const hits = [];
  const n = Math.min(declSkel.spans.length, internalSkel.spans.length);
  for (let i = 0; i < n; i++) {
    const a = declSkel.spans[i];
    const b = internalSkel.spans[i];
    if (a.kind === 'confusable' || b.kind === 'confusable') {
      hits.push({
        position: a.originalIndex,
        declarationChar: a.char,
        internalChar: b.char,
        mappedTo: a.kind === 'confusable' ? a.mappedTo : b.mappedTo,
      });
    }
  }
  return hits;
}
