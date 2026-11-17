// 名称规范化：严格按生态配置给出的步骤顺序执行。
// 每一步都携带原始码点位置，保证后续可以回溯混淆字符在原始名称中的位置。

/**
 * 把字符串拆成 { char, index } 码点序列（按 code point 而非 UTF-16 单元计数）。
 */
export function toCodePoints(spec) {
  const out = [];
  // 使用迭代器天然按码点展开，避免代理对错位。
  let i = 0;
  for (const ch of String(spec)) {
    out.push({ char: ch, index: i });
    i += ch.length;
  }
  return out;
}

/**
 * 解析包规格，拆出作用域与裸名（npm 风格 @scope/name；其他生态无作用域）。
 */
export function parseSpec(spec) {
  const s = String(spec);
  const m = /^@([^/@\s]+)\/([^@\s]+)$/.exec(s);
  if (m) {
    const scope = m[1];
    const name = m[2];
    return {
      scope,
      name,
      // 裸名在原字符串中的起始位置（"@scope/" 的长度）
      nameStart: scope.length + 2,
    };
  }
  return { scope: null, name: s, nameStart: 0 };
}

const DEFAULT_SEPARATORS = ['-', '_', '.'];

/**
 * 按规则集的有序步骤规范化一个包名。
 *
 * 支持的操作（op）：
 *  - { op: 'scope', mode: 'keep' | 'drop' }   保留 / 删除作用域
 *  - { op: 'case-fold' }                       Unicode 大小写折叠（这里用 toLowerCase）
 *  - { op: 'strip-separators', chars?: [...] } 删除分隔符，默认 - _ .
 *
 * 规则：scope 步骤只允许出现一次且必须位于首位；冲突处理在 rules.js 中单独识别。
 * 返回 { normalized, positions, droppedPositions, applied, unknownOps }
 *  - positions[i]        规范化后第 i 个码点在“裸名”中的原始索引
 *  - droppedPositions    被删除字符的原始索引（含作用域字符与分隔符）
 */
export function normalizeName(spec, ruleSet) {
  const { scope, name, nameStart } = parseSpec(spec);
  let tokens = toCodePoints(name).map((t) => ({ char: t.char, index: t.index }));
  const droppedPositions = [];
  const applied = [];
  const unknownOps = [];

  let scopeMode = 'keep';
  const ordered = Array.isArray(ruleSet?.steps) ? ruleSet.steps : [];

  for (const step of ordered) {
    if (!step || typeof step !== 'object' || typeof step.op !== 'string') {
      unknownOps.push(step);
      continue;
    }
    switch (step.op) {
      case 'scope': {
        // 作用域不是裸名 token 的一部分；drop 时把 "@scope/" 全部标记为丢弃。
        scopeMode = step.mode === 'drop' ? 'drop' : 'keep';
        if (scopeMode === 'drop' && scope !== null) {
          for (let i = 0; i < nameStart; i++) droppedPositions.push(i);
        }
        applied.push({ op: 'scope', mode: scopeMode });
        break;
      }
      case 'case-fold': {
        tokens = tokens.map((t) => ({ char: t.char.toLowerCase(), index: t.index }));
        applied.push({ op: 'case-fold' });
        break;
      }
      case 'strip-separators': {
        const chars = Array.isArray(step.chars) && step.chars.length
          ? step.chars
          : DEFAULT_SEPARATORS;
        const set = new Set(chars);
        const kept = [];
        for (const t of tokens) {
          if (set.has(t.char)) {
            droppedPositions.push(t.index);
          } else {
            kept.push(t);
          }
        }
        tokens = kept;
        applied.push({ op: 'strip-separators', chars: [...set] });
        break;
      }
      default:
        unknownOps.push(step);
    }
  }

  const normalized = (scopeMode === 'keep' && scope !== null
    ? `@${scope}/${tokens.map((t) => t.char).join('')}`
    : tokens.map((t) => t.char).join(''));

  // 若保留作用域，positions 只描述裸名部分；作用域部分不参与骨架比较。
  const positions = tokens.map((t) => t.index);
  return {
    normalized,
    positions,
    droppedPositions: [...droppedPositions].sort((a, b) => a - b),
    applied,
    unknownOps,
    scopeKept: scopeMode === 'keep' && scope !== null,
  };
}
