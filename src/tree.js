// 有序摘要树（Merkle 树）：按清单顺序排列叶子，奇数节点向上提升，
// 证明步骤显式携带 level 与 side，复核端按 position 推导期望路径并校验，
// 因此换序、错层、错误方向都会被拒绝。
import { nodeDigest, DIGEST_BYTES } from './digest.js';

function validateLeaves(leaves) {
  if (!Array.isArray(leaves) || leaves.length === 0) {
    throw new TypeError('leaves 必须是非空数组');
  }
  for (const [index, leaf] of leaves.entries()) {
    if (!Buffer.isBuffer(leaf) || leaf.length !== DIGEST_BYTES) {
      throw new TypeError(`第 ${index} 个叶子不是 ${DIGEST_BYTES} 字节摘要`);
    }
  }
}

export function buildLevels(leaves) {
  validateLeaves(leaves);
  const levels = [leaves.slice()];
  while (levels.at(-1).length > 1) {
    const current = levels.at(-1);
    const next = [];
    for (let i = 0; i < current.length; i += 2) {
      const left = current[i];
      const right = i + 1 < current.length ? current[i + 1] : left; // 奇数提升
      next.push(nodeDigest(left, right));
    }
    levels.push(next);
  }
  return levels;
}

export function computeRoot(leaves) {
  const levels = buildLevels(leaves);
  return levels.at(-1)[0];
}

/** 深度 = 从叶子层到根的层数；单叶子树深度为 0。 */
export function treeDepth(leafCount) {
  if (!Number.isInteger(leafCount) || leafCount <= 0) {
    throw new TypeError('leafCount 必须是正整数');
  }
  let depth = 0;
  let span = 1;
  while (span < leafCount) {
    span *= 2;
    depth += 1;
  }
  return depth;
}

/**
 * 生成叶子的包含证明。
 * 每步显式记录 { level, side, digest }：
 * - level：该步起始层（0 为叶子层）
 * - side：兄弟节点相对当前节点的方向
 */
export function buildProof(levels, position) {
  const leafCount = levels[0].length;
  if (!Number.isInteger(position) || position < 0 || position >= leafCount) {
    throw new RangeError(`position ${position} 超出清单范围 [0, ${leafCount - 1}]`);
  }
  const proof = [];
  let index = position;
  for (let level = 0; level < levels.length - 1; level += 1) {
    const layer = levels[level];
    if (index % 2 === 0) {
      const siblingIndex = index + 1;
      if (siblingIndex < layer.length) {
        proof.push({ level, side: 'right', digest: layer[siblingIndex] });
      }
      // 奇数提升：本层无兄弟，直接进入上一层，不产生证明步骤。
    } else {
      proof.push({ level, side: 'left', digest: layer[index - 1] });
    }
    index = Math.floor(index / 2);
  }
  return proof;
}

/**
 * 独立复核：从叶子承诺与证明重算根。
 * 不信任证明里的方向与层级，全部按 position 推导并逐字段比对。
 */
export function verifyProof({ leaf, position, proof, expectedRoot, leafCount }) {
  if (!Buffer.isBuffer(leaf) || leaf.length !== DIGEST_BYTES) {
    return { ok: false, code: 'PROOF_MALFORMED', reason: '叶子摘要格式非法' };
  }
  if (!Number.isInteger(leafCount) || leafCount <= 0) {
    return { ok: false, code: 'PROOF_MALFORMED', reason: 'leafCount 非法' };
  }
  if (!Number.isInteger(position) || position < 0 || position >= leafCount) {
    return { ok: false, code: 'POSITION_OUT_OF_RANGE', reason: '位置超出封存清单范围' };
  }
  if (!Buffer.isBuffer(expectedRoot) || expectedRoot.length !== DIGEST_BYTES) {
    return { ok: false, code: 'PROOF_MALFORMED', reason: '可信根格式非法' };
  }
  if (!Array.isArray(proof)) {
    return { ok: false, code: 'PROOF_MALFORMED', reason: '证明不是数组' };
  }

  const depth = treeDepth(leafCount);
  let current = leaf;
  let index = position;
  let stepCursor = 0;

  for (let level = 0; level < depth; level += 1) {
    const spanAtLevel = Math.max(1, Math.ceil(leafCount / 2 ** level));
    const isLastIndex = index === spanAtLevel - 1;

    if (index % 2 === 0) {
      if (isLastIndex) {
        // 奇数提升：构建时该节点与自身配对，无外部兄弟，不产生证明步骤。
        current = nodeDigest(current, current);
      } else {
        // 与右兄弟配对，证明步骤必须显式声明 side='right'。
        const step = proof[stepCursor];
        stepCursor += 1;
        if (!step || typeof step !== 'object'
          || step.level !== level || step.side !== 'right'
          || !Buffer.isBuffer(step.digest) || step.digest.length !== DIGEST_BYTES) {
          return { ok: false, code: 'PROOF_MALFORMED', reason: `第 ${level} 层需要有效的 right 证明步骤` };
        }
        current = nodeDigest(current, step.digest);
      }
    } else {
      // 与左兄弟配对，证明步骤必须显式声明 side='left'。
      const step = proof[stepCursor];
      stepCursor += 1;
      if (!step || typeof step !== 'object'
        || step.level !== level || step.side !== 'left'
        || !Buffer.isBuffer(step.digest) || step.digest.length !== DIGEST_BYTES) {
        return { ok: false, code: 'PROOF_MALFORMED', reason: `第 ${level} 层需要有效的 left 证明步骤` };
      }
      current = nodeDigest(step.digest, current);
    }

    index = Math.floor(index / 2);
  }

  if (stepCursor !== proof.length) {
    return { ok: false, code: 'PROOF_MALFORMED', reason: '证明包含多余步骤' };
  }
  if (!current.equals(expectedRoot)) {
    return { ok: false, code: 'ROOT_MISMATCH', reason: '重算根与可信根不一致' };
  }
  return { ok: true };
}
