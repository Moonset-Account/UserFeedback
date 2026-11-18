/**
 * 发布完整性判定模块。
 *
 * 输入：
 *  - manifest：脚本清单，记录页面入口、地区分支、回退条件与批准摘要
 *  - buildRoot：构建导出目录，存放实际脚本字节
 *
 * 判定：枚举每个入口实际启用的分支，沿「主脚本 -> 逐级回退脚本」枚举运行时
 *  可达的全部引用，逐个核算 sha256 并与批准摘要对照。
 *
 * 违规码：
 *  - SCRIPT_MISSING     可达引用在构建产物中缺件
 *  - APPROVAL_MISSING   脚本存在但清单中没有任何批准记录（例如只批了地区脚本、漏批回退脚本）
 *  - DIGEST_MISMATCH    存在批准记录但实际字节摘要不一致（同名脚本换内容）
 *  - PATH_TRAVERSAL     引用路径越出构建目录
 */

import { createHash } from 'node:crypto';
import path from 'node:path';
import defaultFs from 'node:fs/promises';

const HEX_SHA256 = /^[0-9a-f]{64}$/i;
const MAX_FALLBACK_DEPTH = 10;
const VALID_STATUS = new Set(['published', 'preview']);

/** 清单结构错误：属于输入本身不合法，与内容层面的违规（缺件/缺批准/摘要不符）区分。 */
export class ManifestError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ManifestError';
  }
}

function requireScriptRef(value, where) {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new ManifestError(`${where} 必须是非空字符串脚本路径`);
  }
}

/**
 * 校验清单结构。结构不合法直接抛 ManifestError；
 * 资源缺件、缺批准、摘要不符等内容问题不在此处理，而在分析结果中逐条报告。
 */
export function validateManifest(manifest) {
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
    throw new ManifestError('manifest 必须是对象');
  }
  if (!Array.isArray(manifest.entries)) {
    throw new ManifestError('manifest.entries 必须是数组');
  }

  const entryIds = new Set();
  manifest.entries.forEach((entry, i) => {
    const where = `entries[${i}]`;
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
      throw new ManifestError(`${where} 必须是对象`);
    }
    if (typeof entry.id !== 'string' || entry.id.trim() === '') {
      throw new ManifestError(`${where}.id 必须是非空字符串`);
    }
    if (entryIds.has(entry.id)) {
      throw new ManifestError(`入口 id 重复: ${entry.id}`);
    }
    entryIds.add(entry.id);

    const status = entry.status ?? 'published';
    if (!VALID_STATUS.has(status)) {
      throw new ManifestError(`${where}.status 非法: ${String(status)}（允许 published | preview）`);
    }
    if (!Array.isArray(entry.branches) || entry.branches.length === 0) {
      throw new ManifestError(`${where}.branches 必须是非空数组`);
    }

    const branchIds = new Set();
    entry.branches.forEach((branch, j) => {
      const bwhere = `${where}.branches[${j}]`;
      if (!branch || typeof branch !== 'object' || Array.isArray(branch)) {
        throw new ManifestError(`${bwhere} 必须是对象`);
      }
      if (typeof branch.id !== 'string' || branch.id.trim() === '') {
        throw new ManifestError(`${bwhere}.id 必须是非空字符串`);
      }
      if (branchIds.has(branch.id)) {
        throw new ManifestError(`入口 ${entry.id} 下分支 id 重复: ${branch.id}`);
      }
      branchIds.add(branch.id);

      if (branch.enabled !== undefined && typeof branch.enabled !== 'boolean') {
        throw new ManifestError(`${bwhere}.enabled 必须是布尔值`);
      }

      requireScriptRef(branch.script, `${bwhere}.script`);

      let fallback = branch.fallback;
      let depth = 0;
      while (fallback !== undefined && fallback !== null) {
        if (typeof fallback !== 'object' || Array.isArray(fallback)) {
          throw new ManifestError(`${bwhere}.fallback 必须是对象`);
        }
        requireScriptRef(fallback.script, `${bwhere}.fallback.script`);
        fallback = fallback.fallback;
        depth += 1;
        if (depth > MAX_FALLBACK_DEPTH) {
          throw new ManifestError(`${bwhere} 回退链深度超过 ${MAX_FALLBACK_DEPTH} 层`);
        }
      }
    });
  });

  if (manifest.approvals !== undefined && !Array.isArray(manifest.approvals)) {
    throw new ManifestError('manifest.approvals 必须是数组');
  }
  (manifest.approvals ?? []).forEach((approval, i) => {
    const where = `approvals[${i}]`;
    if (!approval || typeof approval !== 'object' || Array.isArray(approval)) {
      throw new ManifestError(`${where} 必须是对象`);
    }
    if (typeof approval.script !== 'string' || approval.script.trim() === '') {
      throw new ManifestError(`${where}.script 必须是非空字符串`);
    }
    if (typeof approval.sha256 !== 'string' || !HEX_SHA256.test(approval.sha256)) {
      throw new ManifestError(`${where}.sha256 必须是 64 位十六进制 sha256 摘要`);
    }
  });
}

/** 从磁盘读取并解析清单文件。JSON 不合法时包装为 ManifestError。 */
export async function loadManifest(manifestPath, fileSystem = defaultFs) {
  let raw;
  try {
    raw = await fileSystem.readFile(manifestPath, 'utf8');
  } catch (err) {
    if (err && err.code === 'ENOENT') {
      throw new ManifestError(`清单文件不存在: ${manifestPath}`);
    }
    throw err;
  }
  try {
    return JSON.parse(raw);
  } catch (err) {
    throw new ManifestError(`清单不是合法 JSON: ${err.message}`);
  }
}

function indexApprovals(approvals) {
  const index = new Map();
  for (const approval of approvals) {
    const list = index.get(approval.script) ?? [];
    list.push(approval);
    index.set(approval.script, list);
  }
  return index;
}

/** 评估单个脚本引用：路径安全 -> 字节存在 -> 摘要 -> 批准。 */
async function evaluateNode(scriptRef, root, approvalIndex, fileSystem) {
  const node = {
    script: scriptRef,
    safe: false,
    exists: false,
    sha256: null,
    approved: false,
    approval: null,
    violations: [],
  };

  const fullPath = path.resolve(root, scriptRef);
  const insideRoot = fullPath === root || fullPath.startsWith(root + path.sep);
  if (!insideRoot) {
    node.violations.push({
      code: 'PATH_TRAVERSAL',
      script: scriptRef,
      message: `脚本引用越出构建目录: ${scriptRef}`,
    });
    return node;
  }
  node.safe = true;

  let bytes;
  try {
    bytes = await fileSystem.readFile(fullPath);
  } catch (err) {
    if (err && (err.code === 'ENOENT' || err.code === 'EISDIR')) {
      node.violations.push({
        code: 'SCRIPT_MISSING',
        script: scriptRef,
        message: `构建产物中缺少可达脚本: ${scriptRef}`,
      });
      return node;
    }
    throw err;
  }
  node.exists = true;

  const digest = createHash('sha256').update(bytes).digest('hex');
  node.sha256 = digest;

  const approvals = approvalIndex.get(scriptRef) ?? [];
  if (approvals.length === 0) {
    node.violations.push({
      code: 'APPROVAL_MISSING',
      script: scriptRef,
      actualSha256: digest,
      message: `脚本缺少批准记录: ${scriptRef}`,
    });
    return node;
  }

  const matched = approvals.find((a) => a.sha256.toLowerCase() === digest);
  if (!matched) {
    node.violations.push({
      code: 'DIGEST_MISMATCH',
      script: scriptRef,
      actualSha256: digest,
      approvedSha256: approvals.map((a) => a.sha256.toLowerCase()),
      message: `脚本实际字节与批准摘要不一致（同名脚本内容已更换）: ${scriptRef}`,
    });
    return node;
  }

  node.approved = true;
  node.approval = {
    sha256: matched.sha256.toLowerCase(),
    approvedBy: matched.approvedBy ?? null,
    approvedAt: matched.approvedAt ?? null,
    summary: matched.summary ?? null,
  };
  return node;
}

/**
 * 完整性分析主入口。
 *
 * @param {object} options
 * @param {object} options.manifest  清单对象（也可用 loadManifest 从文件读取）
 * @param {string} options.buildRoot 构建导出目录（实际脚本字节）
 * @param {import('node:fs/promises')} [options.fileSystem] 可选文件系统抽象
 * @returns {Promise<object>} 结构化判定结果；ok=false 表示正式发布必须被阻断
 */
export async function analyzeIntegrity({ manifest, buildRoot, fileSystem = defaultFs }) {
  validateManifest(manifest);
  if (typeof buildRoot !== 'string' || buildRoot.trim() === '') {
    throw new TypeError('buildRoot 必须是非空字符串路径');
  }

  const root = path.resolve(buildRoot);
  const approvalIndex = indexApprovals(manifest.approvals ?? []);

  const result = {
    ok: true,
    buildRoot: root,
    entries: [],
    violations: [],
    previewViolations: [],
    skippedEntries: [],
  };

  for (const entry of manifest.entries) {
    const status = entry.status ?? 'published';
    const blocking = status === 'published';
    const entryReport = {
      id: entry.id,
      route: entry.route ?? null,
      status,
      blocking,
      enabledBranchCount: 0,
      branches: [],
      violations: [],
    };

    for (const branch of entry.branches) {
      if (branch.enabled === false) {
        // 显式禁用的分支不会成为运行时允许的分支，不参与核算
        continue;
      }
      entryReport.enabledBranchCount += 1;

      const branchReport = {
        id: branch.id,
        region: branch.region ?? '*',
        chain: [],
        violations: [],
      };

      let current = {
        script: branch.script,
        condition: branch.condition ?? null,
        fallback: branch.fallback,
      };
      let kind = 'primary';
      while (current) {
        const node = await evaluateNode(current.script, root, approvalIndex, fileSystem);
        const nodeReport = {
          kind,
          condition: current.condition ?? null,
          script: node.script,
          safe: node.safe,
          exists: node.exists,
          sha256: node.sha256,
          approved: node.approved,
          approval: node.approval,
        };
        branchReport.chain.push(nodeReport);

        for (const violation of node.violations) {
          branchReport.violations.push({
            ...violation,
            entryId: entry.id,
            branchId: branch.id,
            region: branchReport.region,
            kind,
          });
        }

        current = current.fallback ?? null;
        kind = 'fallback';
      }

      entryReport.branches.push(branchReport);
      entryReport.violations.push(...branchReport.violations);
    }

    if (entryReport.enabledBranchCount === 0) {
      result.skippedEntries.push({ id: entry.id, reason: 'no-enabled-branch' });
    }

    if (blocking) {
      result.violations.push(...entryReport.violations);
    } else {
      // 预览入口仍完整评估并留痕，但其违规不阻塞正式入口
      result.previewViolations.push(...entryReport.violations);
    }
    result.entries.push(entryReport);
  }

  result.ok = result.violations.length === 0;
  return result;
}
