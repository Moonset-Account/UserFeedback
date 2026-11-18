// 封存、最小化披露导出与独立复核的主流程。
import {
  leafDigest,
  generateSalt,
  DIGEST_ALGORITHM,
  SALT_BYTES,
  DIGEST_BYTES,
} from './digest.js';
import { buildLevels, buildProof, verifyProof } from './tree.js';

export const FORMAT_VERSION = 1;

function validateEntries(entries) {
  if (!Array.isArray(entries) || entries.length === 0) {
    throw new TypeError('entries 必须是非空数组');
  }
  entries.forEach((entry, index) => {
    if (!entry || typeof entry !== 'object') {
      throw new TypeError(`第 ${index} 项不是对象`);
    }
    if (typeof entry.name !== 'string' || entry.name.length === 0) {
      throw new TypeError(`第 ${index} 项缺少非空 name`);
    }
    if (typeof entry.content !== 'string' && !Buffer.isBuffer(entry.content)) {
      throw new TypeError(`第 ${index} 项的 content 必须是字符串或 Buffer`);
    }
  });
}

function validatePositionList(positions, leafCount, label) {
  if (!Array.isArray(positions)) {
    throw new TypeError(`${label} 必须是数组`);
  }
  const seen = new Set();
  for (const position of positions) {
    if (!Number.isInteger(position) || position < 0 || position >= leafCount) {
      throw new RangeError(`${label} 中的位置 ${String(position)} 超出范围 [0, ${leafCount - 1}]`);
    }
    if (seen.has(position)) {
      throw new RangeError(`${label} 中的位置 ${position} 重复`);
    }
    seen.add(position);
  }
}

/**
 * 按有序清单封存：
 * 1. 每个条目生成独立随机盐；
 * 2. 将 位置 || 盐 || 名称 || 内容 承诺为叶子摘要；
 * 3. 构建有序摘要树，可信根写入封存记录。
 *
 * @returns {{ sealedRecord: object, sealedPackage: object }}
 */
export function sealManifest(entries, options = {}) {
  validateEntries(entries);

  const leaves = entries.map((entry, position) => {
    const salt = Buffer.isBuffer(entry.salt) && entry.salt.length === SALT_BYTES
      ? Buffer.from(entry.salt)
      : generateSalt();
    const digest = leafDigest({ position, salt, name: entry.name, content: entry.content });
    return { position, salt, name: entry.name, content: entry.content, digest };
  });

  const levels = buildLevels(leaves.map((leaf) => leaf.digest));
  const root = levels.at(-1)[0];

  const sealedAt = typeof options.sealedAt === 'string' ? options.sealedAt : new Date().toISOString();
  const sealedRecord = {
    formatVersion: FORMAT_VERSION,
    algorithm: DIGEST_ALGORITHM,
    leafCount: leaves.length,
    root,
    sealedAt,
  };

  // 封存包由保管方持有，包含全部盐与内容，用于日后生成披露副本。
  const sealedPackage = {
    record: sealedRecord,
    levels,
    entries: leaves,
  };

  return { sealedRecord, sealedPackage };
}

/**
 * 生成披露副本：只包含批准位置的名称、内容、盐、位置与包含证明。
 * 导出结果严格按位置升序排列；被遮盖条目的名称、内容、盐均不出现在副本中。
 * 批准位置列表会被校验（范围、重复），乱序提供不影响结果（内部排序）。
 */
export function createDisclosure(sealedPackage, approvedPositions) {
  if (!sealedPackage || !Array.isArray(sealedPackage.entries) || !Array.isArray(sealedPackage.levels)) {
    throw new TypeError('sealedPackage 结构非法');
  }
  const { entries, levels, record } = sealedPackage;
  validatePositionList(approvedPositions, entries.length, 'approvedPositions');

  const sortedPositions = [...approvedPositions].sort((a, b) => a - b);
  const disclosedEntries = sortedPositions.map((position) => {
    const entry = entries[position];
    if (!entry || entry.position !== position) {
      throw new Error(`封存包位置 ${position} 数据缺失或错位`);
    }
    return {
      position,
      name: entry.name,
      content: entry.content,
      salt: entry.salt,
      digest: entry.digest,
      proof: buildProof(levels, position),
    };
  });

  return {
    formatVersion: FORMAT_VERSION,
    algorithm: DIGEST_ALGORITHM,
    leafCount: entries.length,
    entries: disclosedEntries,
  };
}

function failure(code, reason, position = null) {
  const result = { ok: false, code, reason };
  if (position !== null) result.position = position;
  return result;
}

/**
 * 复核端独立验证。复核端只持有：
 * - 独立取得的可信封存记录（含 root、leafCount）；
 * - 独立取得的批准位置清单；
 * - 待验披露副本。
 *
 * 验证：批准集合完整且无多余、副本严格按位置升序、逐项重算叶子与包含证明并比对可信根。
 */
export function verifyDisclosure(bundle, sealedRecord, approvedPositions) {
  try {
    if (!bundle || typeof bundle !== 'object') {
      return failure('BUNDLE_MALFORMED', '披露副本结构非法');
    }
    if (!sealedRecord || typeof sealedRecord !== 'object') {
      return failure('RECORD_MALFORMED', '封存记录结构非法');
    }
    if (!Array.isArray(approvedPositions)) {
      return failure('APPROVED_LIST_MALFORMED', '批准位置清单必须是数组');
    }
    if (sealedRecord.algorithm !== DIGEST_ALGORITHM || bundle.algorithm !== DIGEST_ALGORITHM) {
      return failure('ALGORITHM_MISMATCH', `只支持 ${DIGEST_ALGORITHM}`);
    }
    if (sealedRecord.formatVersion !== FORMAT_VERSION || bundle.formatVersion !== FORMAT_VERSION) {
      return failure('FORMAT_MISMATCH', `格式版本必须为 ${FORMAT_VERSION}`);
    }
    if (!Number.isInteger(sealedRecord.leafCount) || sealedRecord.leafCount <= 0) {
      return failure('RECORD_MALFORMED', '封存记录 leafCount 非法');
    }
    if (!Buffer.isBuffer(sealedRecord.root) || sealedRecord.root.length !== DIGEST_BYTES) {
      return failure('RECORD_MALFORMED', '封存记录可信根格式非法');
    }
    if (bundle.leafCount !== sealedRecord.leafCount) {
      return failure('LEAF_COUNT_MISMATCH', '副本与封存记录的条目总数不一致');
    }

    const approvedSet = new Set();
    for (const position of approvedPositions) {
      if (!Number.isInteger(position) || position < 0 || position >= sealedRecord.leafCount) {
        return failure('APPROVED_LIST_MALFORMED',
          `批准位置 ${String(position)} 超出封存范围 [0, ${sealedRecord.leafCount - 1}]`);
      }
      if (approvedSet.has(position)) {
        return failure('APPROVED_LIST_MALFORMED', `批准位置 ${position} 重复`);
      }
      approvedSet.add(position);
    }

    if (!Array.isArray(bundle.entries)) {
      return failure('BUNDLE_MALFORMED', '副本 entries 必须是数组');
    }

    // 1. 副本必须严格按位置升序，且位置合法、不重复。
    let previous = -1;
    for (const entry of bundle.entries) {
      if (!entry || typeof entry !== 'object') {
        return failure('BUNDLE_MALFORMED', '副本条目结构非法');
      }
      const { position } = entry;
      if (!Number.isInteger(position) || position < 0 || position >= sealedRecord.leafCount) {
        return failure('POSITION_OUT_OF_RANGE', `副本位置 ${String(position)} 超出封存范围`, position);
      }
      if (position <= previous) {
        return failure('ORDER_VIOLATION', '副本条目未严格按位置升序排列', position);
      }
      previous = position;
    }

    const bundlePositions = new Set();
    for (const entry of bundle.entries) bundlePositions.add(entry.position);

    // 2. 批准集合完整性：缺少获批条目即失败。
    for (const position of approvedSet) {
      if (!bundlePositions.has(position)) {
        return failure('MISSING_POSITION', `批准披露的位置 ${position} 未出现在副本中`, position);
      }
    }
    // 3. 最小披露：出现未批准条目即失败。
    for (const position of bundlePositions) {
      if (!approvedSet.has(position)) {
        return failure('UNAPPROVED_POSITION', `副本包含未批准披露的位置 ${position}`, position);
      }
    }

    // 4. 逐项重算叶子承诺并验证包含证明。
    for (const entry of bundle.entries) {
      if (typeof entry.name !== 'string' || entry.name.length === 0) {
        return failure('BUNDLE_MALFORMED', `位置 ${entry.position} 的名称非法`, entry.position);
      }
      if (typeof entry.content !== 'string' && !Buffer.isBuffer(entry.content)) {
        return failure('BUNDLE_MALFORMED', `位置 ${entry.position} 的内容非法`, entry.position);
      }
      if (!Buffer.isBuffer(entry.salt) || entry.salt.length !== SALT_BYTES) {
        return failure('BUNDLE_MALFORMED', `位置 ${entry.position} 的盐非法`, entry.position);
      }

      let recomputedLeaf;
      try {
        recomputedLeaf = leafDigest({
          position: entry.position,
          salt: entry.salt,
          name: entry.name,
          content: entry.content,
        });
      } catch (error) {
        return failure('BUNDLE_MALFORMED', `位置 ${entry.position} 的叶子输入非法: ${error.message}`, entry.position);
      }

      if (Buffer.isBuffer(entry.digest) && !entry.digest.equals(recomputedLeaf)) {
        return failure('LEAF_DIGEST_MISMATCH', '副本携带的叶子摘要与内容/盐/位置重算结果不一致', entry.position);
      }

      const proofResult = verifyProof({
        leaf: recomputedLeaf,
        position: entry.position,
        proof: entry.proof,
        expectedRoot: sealedRecord.root,
        leafCount: sealedRecord.leafCount,
      });
      if (!proofResult.ok) {
        return failure(proofResult.code, `位置 ${entry.position} 证明失败: ${proofResult.reason}`, entry.position);
      }
    }

    return { ok: true, verifiedCount: bundle.entries.length, leafCount: sealedRecord.leafCount };
  } catch (error) {
    return failure('BUNDLE_MALFORMED', `验证过程发生异常: ${error.message}`);
  }
}
