import { computeFieldDigest } from './digest.js';

/**
 * 解析状态取值：
 *  - ok       片段完整解析
 *  - error    该片段解析失败（语法/编码错误等）
 *  - truncated 收到截断标记（连接中断、Content-Length 不足等）
 */
export const PARSE_STATUS = Object.freeze({
  OK: 'ok',
  ERROR: 'error',
  TRUNCATED: 'truncated',
});

export const SIDE = Object.freeze({
  GATEWAY: 'gateway',
  BUSINESS: 'business',
});

const FRAGMENT_REQUIRED = ['recordId', 'requestId', 'fieldName', 'fieldInstance', 'fragmentIndex', 'parseStatus'];
const CHECK_REQUIRED = ['recordId', 'requestId', 'fieldName', 'fieldInstance', 'verdict'];

function requireFields(record, required, kind) {
  const missing = required.filter((f) => record[f] === undefined || record[f] === null || record[f] === '');
  if (missing.length > 0) {
    throw new Error(`${kind} 记录 ${record.recordId ?? '(无 recordId)'} 缺少必填字段: ${missing.join(', ')}`);
  }
}

function normalizeSide(rawSide, fallback) {
  if (!rawSide) return fallback;
  if (rawSide !== SIDE.GATEWAY && rawSide !== SIDE.BUSINESS) {
    throw new Error(`未知数据来源 side=${rawSide}，仅支持 gateway / business`);
  }
  return rawSide;
}

function normalizeFragment(raw, side) {
  requireFields(raw, FRAGMENT_REQUIRED, '片段');
  const status = String(raw.parseStatus);
  if (!Object.values(PARSE_STATUS).includes(status)) {
    throw new Error(`片段记录 ${raw.recordId} 的 parseStatus=${raw.parseStatus} 非法`);
  }
  const fragmentIndex = Number(raw.fragmentIndex);
  if (!Number.isInteger(fragmentIndex) || fragmentIndex < 0) {
    throw new Error(`片段记录 ${raw.recordId} 的 fragmentIndex 必须是非负整数`);
  }
  return {
    kind: 'fragment',
    recordId: String(raw.recordId),
    side: normalizeSide(raw.side, side),
    requestId: String(raw.requestId),
    fieldName: String(raw.fieldName),
    fieldInstance: Number(raw.fieldInstance),
    fragmentIndex,
    parseStatus: status,
    // 内容摘要允许缺省（缺省本身也是一种可被检测到的元数据异常）
    contentDigest: raw.contentDigest === undefined || raw.contentDigest === null ? null : String(raw.contentDigest),
    byteLength: raw.byteLength === undefined || raw.byteLength === null ? null : Number(raw.byteLength),
  };
}

function normalizeCheck(raw, side) {
  requireFields(raw, CHECK_REQUIRED, '安全校验');
  return {
    kind: 'check',
    recordId: String(raw.recordId),
    side: normalizeSide(raw.side, side),
    requestId: String(raw.requestId),
    fieldName: String(raw.fieldName),
    fieldInstance: Number(raw.fieldInstance),
    verdict: String(raw.verdict),
    contentDigest: raw.contentDigest === undefined || raw.contentDigest === null ? null : String(raw.contentDigest),
  };
}

/**
 * 归一化一份已解析事件摘要。
 * 输入结构（两端同构）：
 * {
 *   side: 'gateway' | 'business',
 *   fragments: [{ recordId, requestId, fieldName, fieldInstance,
 *                  fragmentIndex, parseStatus, contentDigest?, byteLength? }],
 *   checks:    [{ recordId, requestId, fieldName, fieldInstance,
 *                  verdict, contentDigest? }]
 * }
 */
export function normalizeEvent(rawEvent, defaultSide) {
  if (!rawEvent || typeof rawEvent !== 'object') {
    throw new Error('事件摘要必须是对象');
  }
  const side = normalizeSide(rawEvent.side, defaultSide);
  const fragments = (rawEvent.fragments ?? []).map((r) => normalizeFragment(r, side));
  const checks = (rawEvent.checks ?? []).map((r) => normalizeCheck(r, side));

  const seen = new Set();
  for (const r of [...fragments, ...checks]) {
    const uid = `${r.side}:${r.recordId}`;
    if (seen.has(uid)) {
      throw new Error(`记录编号重复: ${uid}`);
    }
    seen.add(uid);
  }
  return { side, fragments, checks };
}

function groupBy(records, keyFn) {
  const map = new Map();
  for (const r of records) {
    const key = keyFn(r);
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(r);
  }
  return map;
}

/**
 * 按 (请求号, 字段名) 分组，并在组内按字段实例编号重建字段序列。
 * 每个字段实例下再按片段编号排序，重建该实例的片段序列与字段级摘要。
 *
 * 同名附件字段出现多次时，它们是同一字段名下不同的 fieldInstance，
 * 重建结果是一个有序实例序列（即题目要求的"字段序列"）。
 */
export function reconstructFields(event) {
  const groups = new Map();
  const fragmentGroups = groupBy(event.fragments, (r) => `${r.requestId} ${r.fieldName}`);

  for (const [groupKey, frags] of fragmentGroups) {
    const { requestId, fieldName } = frags[0];
    const byInstance = groupBy(frags, (r) => r.fieldInstance);
    const instances = [];

    for (const [instanceRaw, instanceFrags] of byInstance) {
      const fieldInstance = Number(instanceRaw);
      const ordered = [...instanceFrags].sort((a, b) => a.fragmentIndex - b.fragmentIndex);
      const recordIds = ordered.map((f) => f.recordId);
      const statuses = ordered.map((f) => f.parseStatus);
      const indexes = ordered.map((f) => f.fragmentIndex);

      const hasDuplicateIndex = new Set(indexes).size !== indexes.length;
      const hasGap = indexes.some((idx, i) => idx !== i);
      const hasError = statuses.includes(PARSE_STATUS.ERROR);
      const hasTruncated = statuses.includes(PARSE_STATUS.TRUNCATED);
      // 截断或错误片段没有可信内容，摘要只在全部 ok 时重建
      const complete = !hasDuplicateIndex && !hasGap && !hasError && !hasTruncated;
      const fieldDigest = complete ? computeFieldDigest(ordered.map((f) => f.contentDigest ?? '')) : null;
      instances.push({
        fieldInstance,
        recordIds,
        fragmentCount: ordered.length,
        fragmentIndexes: indexes,
        statuses,
        hasDuplicateIndex,
        hasGap,
        hasError,
        hasTruncated,
        complete,
        fieldDigest,
        fragments: ordered,
      });
    }

    instances.sort((a, b) => a.fieldInstance - b.fieldInstance);
    groups.set(groupKey, { requestId, fieldName, instances });
  }

  // 安全校验记录也按同样的键归位，供后续一一关联
  const checkGroups = groupBy(event.checks, (r) => `${r.requestId} ${r.fieldName}`);
  for (const [, checks] of checkGroups) {
    checks.sort((a, b) => a.fieldInstance - b.fieldInstance || a.recordId.localeCompare(b.recordId));
  }

  return { side: event.side, groups, checkGroups };
}

export function collectRequestIds(...models) {
  const ids = new Set();
  for (const model of models) {
    for (const g of model.groups.values()) ids.add(g.requestId);
    for (const checks of model.checkGroups.values()) {
      for (const c of checks) ids.add(c.requestId);
    }
  }
  return [...ids].sort();
}
