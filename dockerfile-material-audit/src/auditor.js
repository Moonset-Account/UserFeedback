/**
 * 简化 Dockerfile 外部材料摘要绑定审计器。
 *
 * 审计思路：
 *  1. 解析 Dockerfile，构建阶段（stage）与依赖边（FROM / COPY --from 的内部引用）。
 *  2. 合并全局 ARG、阶段内 ARG 与合成构建参数，解析所有外部镜像引用。
 *     ARG 无法解析 -> 待核实（pending），不做猜测。
 *  3. 从发布目标阶段做可达性分析：
 *     - 不可达阶段（未使用阶段）的材料不参与门禁；
 *     - 可达的测试阶段（名称含 test，大小写不敏感）中的材料仅供测试，不参与门禁；
 *     - 其余可达材料为发布目标真正依赖，参与门禁。
 *  4. 对参与门禁的外部材料，按"来源身份 registry/repository + 内容身份 digest"
 *     对照批准的材料摘要清单判定绑定状态。
 *
 * 状态码：
 *   PINNED_APPROVED        已固定 @digest，且清单存在同源同摘要记录
 *   PINNED_UNAPPROVED      已固定 @digest，但清单无同源同摘要记录
 *   MUTABLE_TAG_APPROVED   可变标签，清单仅有同源标签级记录，无内容摘要绑定
 *   MUTABLE_TAG_OTHER_SOURCE 可变标签，同名材料的审批摘要属于另一来源
 *   MUTABLE_TAG_UNAPPROVED 可变标签，清单中无任何同源记录
 *   PENDING                构建参数无法解析，待人工核实
 */

import { parseDockerfile } from './parser.js';
import { parseImageRef } from './imageRef.js';
import { substitute } from './variable.js';

const TEST_STAGE_RE = /test/i;

function isTestStageName(name) {
  return TEST_STAGE_RE.test(name);
}

/**
 * 主审计入口。
 * @param {object} input
 * @param {string} input.dockerfile Dockerfile 文本
 * @param {string} input.target 发布目标阶段名
 * @param {Record<string, string>} [input.buildParams] 合成构建参数
 * @param {Array<{id?: string, registry: string, repository: string, digest: string, tag?: string}>} [input.approvals]
 * @returns {object} 结构化审计报告
 */
export function audit({ dockerfile, target, buildParams = {}, approvals = [] }) {
  const instructions = parseDockerfile(dockerfile);
  const normalizedApprovals = approvals.map(normalizeApproval);

  // ---- 1. 构建阶段与 ARG 作用域 -------------------------------------------
  const globalArgs = new Map(); // name -> default|null（第一个 FROM 之前）
  const stages = [];
  const aliasIndex = new Map(); // 小写别名 -> stage
  let current = null;
  let seenFrom = false;

  for (const instr of instructions) {
    if (instr.kind === 'ARG' && !seenFrom) {
      globalArgs.set(instr.name, instr.default);
      continue;
    }
    if (instr.kind === 'FROM') {
      seenFrom = true;
      const stage = {
        name: instr.alias || `stage-${stages.length + 1}`,
        fromLine: instr.line,
        fromRaw: instr.image,
        platform: instr.platform,
        localArgs: new Map(),
        edges: [], // {stage, line, via}
        externalRefs: [],
      };
      stages.push(stage);
      current = stage;
      if (instr.alias) {
        aliasIndex.set(instr.alias.toLowerCase(), stage);
      }
      continue;
    }
    if (instr.kind === 'ARG' && current) {
      current.localArgs.set(instr.name, instr.default);
      continue;
    }
    if (instr.kind === 'COPY_FROM' && current) {
      current._pendingCopy = current._pendingCopy || [];
      current._pendingCopy.push(instr);
    }
  }

  const targetStage = stages.find(
    (s) => s.name.toLowerCase() === String(target).toLowerCase(),
  );
  if (!targetStage) {
    throw new Error(`发布目标阶段不存在: ${target}`);
  }

  // ---- 2. 变量解析 + 引用分类（内部阶段 / 外部材料 / 待核实） ---------------
  const resolveRef = (raw, line, owner, via) => {
    const vars = buildVarMap(globalArgs, owner.localArgs, buildParams);
    const { value, unresolved } = substitute(raw, vars);
    if (unresolved.length > 0) {
      owner.externalRefs.push({
        kind: via,
        line,
        raw,
        resolved: null,
        unresolved,
        status: 'PENDING',
      });
      return;
    }
    const internal = aliasIndex.get(value.trim().toLowerCase());
    if (internal) {
      owner.edges.push({ stage: internal, line, via });
      return;
    }
    const ref = parseImageRef(value);
    owner.externalRefs.push({
      kind: via,
      line,
      raw,
      resolved: value,
      image: ref,
      status: null,
    });
  };

  for (const stage of stages) {
    resolveRef(stage.fromRaw, stage.fromLine, stage, 'FROM');
    for (const copy of stage._pendingCopy || []) {
      resolveRef(copy.from, copy.line, stage, 'COPY --from');
    }
    delete stage._pendingCopy;
  }

  // ---- 3. 目标可达性分析（BFS，记录依赖路径） ------------------------------
  const reachable = new Set();
  const parent = new Map(); // stage -> {prev, edge}
  const queue = [targetStage];
  reachable.add(targetStage);
  while (queue.length > 0) {
    const node = queue.shift();
    for (const edge of node.edges) {
      if (!reachable.has(edge.stage)) {
        reachable.add(edge.stage);
        parent.set(edge.stage, { prev: node, edge });
        queue.push(edge.stage);
      }
    }
  }

  const pathOf = (stage) => {
    const chain = [stage];
    let p = parent.get(stage);
    while (p) {
      chain.unshift(p.prev);
      p = parent.get(p.prev);
    }
    return chain.map((s) => s.name);
  };

  // ---- 4. 逐条外部材料对照审批清单 -----------------------------------------
  const findings = [];
  for (const stage of stages) {
    const reachableFlag = reachable.has(stage);
    const testStage = isTestStageName(stage.name);
    const gating = reachableFlag && !testStage;

    for (const ref of stage.externalRefs) {
      const base = {
        line: ref.line,
        instruction: ref.kind === 'FROM' ? `FROM ${ref.raw}` : `COPY --from=${ref.raw}`,
        ownerStage: stage.name,
        via: ref.kind,
        reachable: reachableFlag,
        testStage,
        gating,
        dependencyPath: reachableFlag ? pathOf(stage) : null,
      };

      if (ref.status === 'PENDING') {
        findings.push({
          ...base,
          status: 'PENDING',
          material: null,
          unresolvedArgs: ref.unresolved,
          message: `构建参数 ${ref.unresolved.map((n) => `$${n}`).join('、')} 无法解析，材料身份待核实`,
          applicableApproval: null,
        });
        continue;
      }

      const image = ref.image;
      const material = {
        registry: image.registry,
        repository: image.repository,
        tag: image.tag,
        digest: image.digest,
        source: image.sourceKey,
        rawReference: image.raw,
        contentPinned: image.pinned,
      };
      const decision = decide(image, normalizedApprovals);
      findings.push({
        ...base,
        status: decision.status,
        material,
        unresolvedArgs: [],
        message: decision.message,
        applicableApproval: decision.approval,
      });
    }
  }

  // ---- 5. 汇总结论 ---------------------------------------------------------
  const gatingFindings = findings.filter((f) => f.gating);
  const violations = gatingFindings.filter((f) =>
    ['PINNED_UNAPPROVED', 'MUTABLE_TAG_APPROVED', 'MUTABLE_TAG_OTHER_SOURCE', 'MUTABLE_TAG_UNAPPROVED'].includes(f.status),
  );
  const pending = gatingFindings.filter((f) => f.status === 'PENDING');

  let decision = 'PASS';
  if (violations.length > 0) {
    decision = 'FAIL';
  } else if (pending.length > 0) {
    decision = 'INCONCLUSIVE';
  }

  return {
    target: targetStage.name,
    decision,
    summary: {
      totalExternalMaterials: findings.length,
      gatingMaterials: gatingFindings.length,
      violations: violations.length,
      pending: pending.length,
      testOnlyMaterials: findings.filter((f) => f.testStage).length,
      unusedStageMaterials: findings.filter((f) => !f.reachable).length,
      stages: {
        total: stages.length,
        reachable: [...reachable].map((s) => s.name),
        testStages: stages.filter((s) => isTestStageName(s.name)).map((s) => s.name),
        unused: stages.filter((s) => !reachable.has(s)).map((s) => s.name),
      },
    },
    findings,
  };
}

// ---------------------------------------------------------------------------

function buildVarMap(globalArgs, localArgs, buildParams) {
  const vars = {};
  for (const [name, def] of globalArgs) {
    if (Object.prototype.hasOwnProperty.call(buildParams, name)) {
      vars[name] = String(buildParams[name]);
    } else if (def !== null) {
      vars[name] = def;
    }
  }
  for (const [name, def] of localArgs) {
    if (Object.prototype.hasOwnProperty.call(buildParams, name)) {
      vars[name] = String(buildParams[name]);
    } else if (def !== null) {
      vars[name] = def;
    }
  }
  return vars;
}

function normalizeApproval(a) {
  // 与 imageRef 保持同一归一化口径：docker.io 单段仓库补 library/
  let { registry, repository } = a;
  registry = registry || 'docker.io';
  if (registry === 'docker.io' && !repository.includes('/')) {
    repository = `library/${repository}`;
  }
  return {
    id: a.id || null,
    registry,
    repository,
    digest: a.digest,
    tag: a.tag || null,
    sourceKey: `${registry}/${repository}`,
  };
}

/**
 * 对照清单判定单条外部材料。
 */
function decide(image, approvals) {
  const sameSource = approvals.filter((a) => a.sourceKey === image.sourceKey);
  const sameNameOtherSource = approvals.filter(
    (a) => a.repository === image.repository && a.sourceKey !== image.sourceKey,
  );

  if (image.pinned) {
    const exact = sameSource.find((a) => a.digest === image.digest);
    if (exact) {
      return {
        status: 'PINNED_APPROVED',
        approval: publicApproval(exact),
        message: `已固定摘要 ${image.digest}，与审批记录 ${labelOf(exact)} 的同源同摘要匹配`,
      };
    }
    return {
      status: 'PINNED_UNAPPROVED',
      approval: sameSource[0] ? publicApproval(sameSource[0]) : null,
      message: `引用固定为 ${image.digest}，但审批清单中不存在该来源的匹配摘要`,
    };
  }

  // 可变标签（无 @digest）
  if (sameSource.length > 0) {
    const tagApproval = image.tag
      ? sameSource.find((a) => a.tag === image.tag)
      : sameSource[0];
    return {
      status: 'MUTABLE_TAG_APPROVED',
      approval: publicApproval(tagApproval || sameSource[0]),
      message: `仍以可变标签 ${image.tag || '(无标签)'} 引用，审批记录 ${labelOf(tagApproval || sameSource[0])} 只覆盖标签/其他摘要，未绑定当前引用的内容摘要`,
    };
  }

  if (sameNameOtherSource.length > 0) {
    const other = sameNameOtherSource[0];
    return {
      status: 'MUTABLE_TAG_OTHER_SOURCE',
      approval: publicApproval(other),
      message: `审批摘要 ${labelOf(other)} 对应另一来源 ${other.sourceKey}，与实际来源 ${image.sourceKey} 不符（同名材料不同来源）`,
    };
  }

  return {
    status: 'MUTABLE_TAG_UNAPPROVED',
    approval: null,
    message: `可变标签 ${image.tag || '(无标签)'} 引用，审批清单中无同源材料记录`,
  };
}

function publicApproval(a) {
  return {
    id: a.id,
    registry: a.registry,
    repository: a.repository,
    digest: a.digest,
    tag: a.tag,
  };
}

function labelOf(a) {
  return a.id ? `${a.id}` : `${a.sourceKey}@${a.digest}`;
}
