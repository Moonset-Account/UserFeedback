import { canonicalHash, stableStringify } from './digest.js';
import { normalizeEvent, reconstructFields, collectRequestIds, PARSE_STATUS } from './model.js';

/**
 * 问题分类（四类判定，前三类都会阻止提交）：
 *  - attribution_conflict 关联冲突：安全校验结果无法与对应附件一一关联
 *      （典型旧版缺陷：A 附件的校验结论被记到 B 附件上）
 *  - field_missing        字段缺失：一端解析出的字段实例/校验记录在另一端不存在
 *  - truncation           传输截断：片段带 truncated/error 标记，内容不完整
 *  - metadata_conflict    元数据/内容摘要冲突：同一字段实例编号两端对不上
 */
export const ISSUE = Object.freeze({
  ATTRIBUTION_CONFLICT: 'attribution_conflict',
  FIELD_MISSING: 'field_missing',
  TRUNCATION: 'truncation',
  METADATA_CONFLICT: 'metadata_conflict',
});

const GROUP_SEP = ' '; // model.js 中分组键为 "requestId fieldName"

function groupKey(requestId, fieldName) {
  return `${requestId}${GROUP_SEP}${fieldName}`;
}

function getGroup(model, requestId, fieldName) {
  return model.groups.get(groupKey(requestId, fieldName)) ?? null;
}

function getChecks(model, requestId, fieldName) {
  return model.checkGroups.get(groupKey(requestId, fieldName)) ?? [];
}

function uniqSorted(values) {
  return [...new Set(values.filter((v) => v !== undefined && v !== null))].sort();
}

function makeIssue(type, requestId, fieldName, fieldInstance, gatewayRecordIds, businessRecordIds, blockReason) {
  return {
    issueId: null,
    type,
    requestId,
    fieldName,
    fieldInstance,
    gatewayRecordIds: uniqSorted(gatewayRecordIds),
    businessRecordIds: uniqSorted(businessRecordIds),
    blockReason,
  };
}

function instRecordIds(inst) {
  return inst ? inst.recordIds : [];
}

/**
 * 收集一端字段实例上非 ok 的片段（截断 / 解析错误）。
 */
function abnormalFragments(inst) {
  if (!inst) return [];
  return inst.fragments.filter((f) => f.parseStatus !== PARSE_STATUS.OK);
}

/**
 * 核心：对一对（网关、业务层）已解析事件摘要做离线归属检查。
 * 纯函数：同样的输入必然得到同样的报告（reportId / issueId 稳定），
 * 因此重复导入天然幂等，去重逻辑见 store.js。
 */
export function inspectPair(rawGateway, rawBusiness) {
  const gwEvent = normalizeEvent(rawGateway, 'gateway');
  const bizEvent = normalizeEvent(rawBusiness, 'business');
  if (gwEvent.side !== 'gateway' || bizEvent.side !== 'business') {
    throw new Error('两端事件摘要的 side 必须分别为 gateway 与 business');
  }

  const gw = reconstructFields(gwEvent);
  const biz = reconstructFields(bizEvent);
  const issues = [];

  const requestIds = collectRequestIds(gw, biz);

  // 收集两端出现过的全部 (请求号, 字段名)
  const fieldNamesByRequest = new Map();
  for (const model of [gw, biz]) {
    for (const group of model.groups.values()) {
      if (!fieldNamesByRequest.has(group.requestId)) fieldNamesByRequest.set(group.requestId, new Set());
      fieldNamesByRequest.get(group.requestId).add(group.fieldName);
    }
  }

  const validRequests = [];

  for (const requestId of requestIds) {
    const fieldNames = [...(fieldNamesByRequest.get(requestId) ?? [])].sort();

    for (const fieldName of fieldNames) {
      const gwGroup = getGroup(gw, requestId, fieldName);
      const bizGroup = getGroup(biz, requestId, fieldName);
      const gwInsts = new Map((gwGroup?.instances ?? []).map((i) => [i.fieldInstance, i]));
      const bizInsts = new Map((bizGroup?.instances ?? []).map((i) => [i.fieldInstance, i]));
      const instanceIds = new Set([...gwInsts.keys(), ...bizInsts.keys()]);

      for (const fieldInstance of [...instanceIds].sort((a, b) => a - b)) {
        const gInst = gwInsts.get(fieldInstance) ?? null;
        const bInst = bizInsts.get(fieldInstance) ?? null;

        // 1) 字段缺失：一端有该字段实例，另一端完全没有
        if (!gInst || !bInst) {
          const presentSide = gInst ? '网关' : '业务端';
          const absentSide = gInst ? '业务端' : '网关';
          issues.push(
            makeIssue(
              ISSUE.FIELD_MISSING,
              requestId,
              fieldName,
              fieldInstance,
              instRecordIds(gInst),
              instRecordIds(bInst),
              `${absentSide}缺少${presentSide}已解析的字段实例 ${fieldName}#${fieldInstance}，两端字段序列无法一一对应，需阻止提交`,
            ),
          );
        }

        // 2) 传输截断 / 解析错误：任一端片段状态非 ok
        const gwBad = abnormalFragments(gInst);
        const bizBad = abnormalFragments(bInst);
        if (gwBad.length > 0 || bizBad.length > 0) {
          const detail = [];
          if (gwBad.length > 0) {
            detail.push(
              `网关片段[${gwBad.map((f) => `${f.recordId}(#${f.fragmentIndex}:${f.parseStatus})`).join(', ')}]`,
            );
          }
          if (bizBad.length > 0) {
            detail.push(
              `业务端片段[${bizBad.map((f) => `${f.recordId}(#${f.fragmentIndex}:${f.parseStatus})`).join(', ')}]`,
            );
          }
          issues.push(
            makeIssue(
              ISSUE.TRUNCATION,
              requestId,
              fieldName,
              fieldInstance,
              gwBad.map((f) => f.recordId),
              bizBad.map((f) => f.recordId),
              `字段实例 ${fieldName}#${fieldInstance} 存在截断或解析错误片段：${detail.join('；')}，内容不完整且安全结论不可信，需阻止提交`,
            ),
          );
        }

        // 2b) 片段序列不完整：编号缺口 / 重复编号（状态为 ok 但序列已损坏）
        const seqBroken = (inst) => inst && (inst.hasGap || inst.hasDuplicateIndex);
        if (seqBroken(gInst) || seqBroken(bInst)) {
          const detail = [];
          if (seqBroken(gInst)) {
            detail.push(`网关片段序列[${gInst.fragmentIndexes.join(',')}]${gInst.hasGap ? ' 有编号缺口' : ''}${gInst.hasDuplicateIndex ? ' 有重复编号' : ''}`);
          }
          if (seqBroken(bInst)) {
            detail.push(`业务端片段序列[${bInst.fragmentIndexes.join(',')}]${bInst.hasGap ? ' 有编号缺口' : ''}${bInst.hasDuplicateIndex ? ' 有重复编号' : ''}`);
          }
          issues.push(
            makeIssue(
              ISSUE.TRUNCATION,
              requestId,
              fieldName,
              fieldInstance,
              instRecordIds(gInst),
              instRecordIds(bInst),
              `字段实例 ${fieldName}#${fieldInstance} 片段序列不完整（${detail.join('；')}），无法重建完整附件，需阻止提交`,
            ),
          );
        }

        // 3) 元数据 / 内容摘要冲突（两端实例都存在时才可逐片段比对）
        if (gInst && bInst) {
          compareInstanceMetadata(issues, requestId, fieldName, fieldInstance, gInst, bInst);
        }

        // 4) 安全校验记录关联
        compareChecks(issues, requestId, fieldName, fieldInstance, gInst, bInst, getChecks(gw, requestId, fieldName), getChecks(biz, requestId, fieldName));
      }

      // 校验记录引用了根本不存在的字段实例编号
      flagDanglingChecks(issues, requestId, fieldName, gwInsts, getChecks(gw, requestId, fieldName), '网关');
      flagDanglingChecks(issues, requestId, fieldName, bizInsts, getChecks(biz, requestId, fieldName), '业务端');
    }

    // 合法请求：该请求下没有任何问题
    const requestIssues = issues.filter((i) => i.requestId === requestId);
    if (requestIssues.length === 0) {
      validRequests.push(buildValidRequest(requestId, fieldNamesByRequest.get(requestId) ?? new Set(), gw, biz));
    }
  }

  return finalizeReport(issues, validRequests, gwEvent, bizEvent);
}

function compareInstanceMetadata(issues, requestId, fieldName, fieldInstance, gInst, bInst) {
  const push = (reason) =>
    issues.push(
      makeIssue(
        ISSUE.METADATA_CONFLICT,
        requestId,
        fieldName,
        fieldInstance,
        gInst.recordIds,
        bInst.recordIds,
        reason,
      ),
    );

  if (gInst.fragmentCount !== bInst.fragmentCount) {
    push(
      `字段实例 ${fieldName}#${fieldInstance} 片段数量不一致（网关 ${gInst.fragmentCount} / 业务端 ${bInst.fragmentCount}），字段序列无法对齐，需阻止提交`,
    );
    // 数量都不一致时逐片段比对没有意义，交由其他规则覆盖缺失部分
    return;
  }

  const gwByIdx = new Map(gInst.fragments.map((f) => [f.fragmentIndex, f]));
  const bizByIdx = new Map(bInst.fragments.map((f) => [f.fragmentIndex, f]));
  const allIdx = new Set([...gwByIdx.keys(), ...bizByIdx.keys()]);

  for (const idx of [...allIdx].sort((a, b) => a - b)) {
    const gf = gwByIdx.get(idx);
    const bf = bizByIdx.get(idx);
    if (!gf || !bf) {
      push(
        `字段实例 ${fieldName}#${fieldInstance} 片段编号序列不一致（网关 [${gInst.fragmentIndexes.join(',')}] / 业务端 [${bInst.fragmentIndexes.join(',')}]），需阻止提交`,
      );
      continue;
    }
    if ((gf.contentDigest ?? null) !== (bf.contentDigest ?? null)) {
      push(
        `字段实例 ${fieldName}#${fieldInstance} 的片段 #${idx} 内容摘要不一致（网关 ${gf.contentDigest ?? '缺省'} / 业务端 ${bf.contentDigest ?? '缺省'}），同一字段实例编号对应了不同附件内容，需阻止提交`,
      );
    }
    if (gf.byteLength !== null && bf.byteLength !== null && gf.byteLength !== bf.byteLength) {
      push(
        `字段实例 ${fieldName}#${fieldInstance} 的片段 #${idx} 字节数不一致（网关 ${gf.byteLength} / 业务端 ${bf.byteLength}），需阻止提交`,
      );
    }
  }

  // 兜底：逐片段都相等时字段摘要必然相等；不等则说明重建过程存在异常
  if (gInst.complete && bInst.complete && gInst.fieldDigest !== bInst.fieldDigest) {
    push(`字段实例 ${fieldName}#${fieldInstance} 重建字段摘要不一致，需阻止提交`);
  }
}

function compareChecks(issues, requestId, fieldName, fieldInstance, gInst, bInst, gwChecks, bizChecks) {
  const gCheck = gwChecks.find((c) => c.fieldInstance === fieldInstance) ?? null;
  const bCheck = bizChecks.find((c) => c.fieldInstance === fieldInstance) ?? null;

  // 校验记录缺失（业务端只收到部分校验记录的情形）。
  // 缺少校验的一侧回退带上该实例的片段记录编号，保证问题始终可定位到具体记录。
  if (!gCheck || !bCheck) {
    const missingSide = !gCheck ? '网关' : '业务端';
    const gwIds = [
      ...(gCheck ? [gCheck.recordId] : []),
      ...(!gCheck ? instRecordIds(gInst) : []),
    ];
    const bizIds = [
      ...(bCheck ? [bCheck.recordId] : []),
      ...(!bCheck ? instRecordIds(bInst) : []),
    ];
    issues.push(
      makeIssue(
        ISSUE.FIELD_MISSING,
        requestId,
        fieldName,
        fieldInstance,
        gwIds,
        bizIds,
        `${missingSide}缺少字段实例 ${fieldName}#${fieldInstance} 的安全校验记录，无法证明该附件已通过校验，需阻止提交`,
      ),
    );
    return;
  }

  // 规则 1：校验记录摘要 vs 本端字段重建摘要——不一致即"张冠李戴"的直接证据。
  // 两端各自独立判定，语义不重叠。
  const digestMismatch = (inst, check) => Boolean(inst && inst.complete && check.contentDigest && inst.fieldDigest !== check.contentDigest);
  if (digestMismatch(gInst, gCheck)) {
    issues.push(
      makeIssue(
        ISSUE.ATTRIBUTION_CONFLICT,
        requestId,
        fieldName,
        fieldInstance,
        [gCheck.recordId, ...instRecordIds(gInst)],
        [bCheck.recordId],
        `网关侧字段实例 ${fieldName}#${fieldInstance} 的安全校验记录摘要与字段重建摘要不一致，校验结论可能被记到其他附件上，需阻止提交`,
      ),
    );
  }
  if (digestMismatch(bInst, bCheck)) {
    issues.push(
      makeIssue(
        ISSUE.ATTRIBUTION_CONFLICT,
        requestId,
        fieldName,
        fieldInstance,
        [gCheck.recordId],
        [bCheck.recordId, ...instRecordIds(bInst)],
        `业务端字段实例 ${fieldName}#${fieldInstance} 的安全校验记录摘要与字段重建摘要不一致，校验结论可能被记到其他附件上，需阻止提交`,
      ),
    );
  }

  // 规则 2：跨端错位证据——本端实例 i 的校验摘要与对端实例 j（j≠i）的记录吻合
  const swappedTo = bizChecks.find(
    (c) => c.fieldInstance !== fieldInstance && gCheck.contentDigest && c.contentDigest === gCheck.contentDigest,
  );
  if (swappedTo) {
    issues.push(
      makeIssue(
        ISSUE.ATTRIBUTION_CONFLICT,
        requestId,
        fieldName,
        fieldInstance,
        [gCheck.recordId],
        [swappedTo.recordId, bCheck.recordId],
        `网关 ${fieldName}#${fieldInstance} 的校验摘要与业务端 ${fieldName}#${swappedTo.fieldInstance} 的记录吻合，安全校验结果被错位归属到其他附件，需阻止提交`,
      ),
    );
  }

  // 规则 3：同一实例两端安全结论不一致
  if (gCheck.verdict !== bCheck.verdict) {
    issues.push(
      makeIssue(
        ISSUE.ATTRIBUTION_CONFLICT,
        requestId,
        fieldName,
        fieldInstance,
        [gCheck.recordId],
        [bCheck.recordId],
        `字段实例 ${fieldName}#${fieldInstance} 两端安全结论不一致（网关 ${gCheck.verdict} / 业务端 ${bCheck.verdict}），需阻止提交`,
      ),
    );
  }

  // 规则 4：两端校验记录引用的内容摘要不一致（在规则 1/2 之外独立成立）
  if (gCheck.contentDigest && bCheck.contentDigest && gCheck.contentDigest !== bCheck.contentDigest) {
    issues.push(
      makeIssue(
        ISSUE.ATTRIBUTION_CONFLICT,
        requestId,
        fieldName,
        fieldInstance,
        [gCheck.recordId],
        [bCheck.recordId],
        `字段实例 ${fieldName}#${fieldInstance} 两端校验记录引用的内容摘要不一致，无法确认校验作用于同一附件，需阻止提交`,
      ),
    );
  }
}

function flagDanglingChecks(issues, requestId, fieldName, instMap, checks, side) {
  for (const check of checks) {
    if (!instMap.has(check.fieldInstance)) {
      issues.push(
        makeIssue(
          ISSUE.ATTRIBUTION_CONFLICT,
          requestId,
          fieldName,
          check.fieldInstance,
          side === '网关' ? [check.recordId] : [],
          side === '业务端' ? [check.recordId] : [],
          `${side}安全校验记录 ${check.recordId} 引用了不存在的字段实例 ${fieldName}#${check.fieldInstance}，校验结果无处归属，需阻止提交`,
        ),
      );
    }
  }
}

function buildValidRequest(requestId, fieldNames, gwModel, bizModel) {
  const fields = [];
  for (const fieldName of [...fieldNames].sort()) {
    const gwGroup = getGroup(gwModel, requestId, fieldName);
    const bizGroup = getGroup(bizModel, requestId, fieldName);
    const instances = [];
    const gwInsts = new Map((gwGroup?.instances ?? []).map((i) => [i.fieldInstance, i]));
    const bizInsts = new Map((bizGroup?.instances ?? []).map((i) => [i.fieldInstance, i]));
    for (const instNo of new Set([...gwInsts.keys(), ...bizInsts.keys()])) {
      const g = gwInsts.get(instNo);
      const b = bizInsts.get(instNo);
      instances.push({
        fieldInstance: instNo,
        fieldDigest: g?.fieldDigest ?? b?.fieldDigest ?? null,
        gatewayRecordIds: instRecordIds(g),
        businessRecordIds: instRecordIds(b),
      });
    }
    fields.push({
      fieldName,
      instanceCount: instances.length,
      multiAttachment: instances.length > 1,
      instances,
    });
  }
  return { requestId, fields };
}

/**
 * 确定性收尾：排序 -> 生成稳定 issueId -> 生成稳定 reportId。
 */
function finalizeReport(issues, validRequests, gwEvent, bizEvent) {
  const sortable = issues.map((issue) => ({
    ...issue,
    gatewayRecordIds: [...issue.gatewayRecordIds].sort(),
    businessRecordIds: [...issue.businessRecordIds].sort(),
  }));
  sortable.sort((a, b) =>
    stableStringify([a.type, a.requestId, a.fieldName, a.fieldInstance, a.gatewayRecordIds, a.businessRecordIds, a.blockReason]).localeCompare(
      stableStringify([b.type, b.requestId, b.fieldName, b.fieldInstance, b.gatewayRecordIds, b.businessRecordIds, b.blockReason]),
    ),
  );

  for (const issue of sortable) {
    const { issueId: _ignored, ...rest } = issue;
    issue.issueId = canonicalHash(rest);
  }

  const countsByType = {};
  for (const issue of sortable) countsByType[issue.type] = (countsByType[issue.type] ?? 0) + 1;

  const report = {
    reportId: null,
    generatedAt: null, // 由调用方注入；纯函数内不使用时钟，保证可复现
    blocking: sortable.length > 0,
    summary: {
      issueCount: sortable.length,
      countsByType,
      validRequestCount: validRequests.length,
      gatewayRecordCount: gwEvent.fragments.length + gwEvent.checks.length,
      businessRecordCount: bizEvent.fragments.length + bizEvent.checks.length,
    },
    issues: sortable,
    validRequests: validRequests.sort((a, b) => a.requestId.localeCompare(b.requestId)),
  };
  report.reportId = canonicalHash({
    issues: report.issues,
    validRequests: report.validRequests,
    gatewayRecordCount: report.summary.gatewayRecordCount,
    businessRecordCount: report.summary.businessRecordCount,
  });
  return report;
}
