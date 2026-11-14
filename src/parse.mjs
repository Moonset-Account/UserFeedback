// 输入解析与校验：只接受离线合成 JSON，不做任何网络访问。
// 时间统一解析为毫秒时间戳，原始 ISO 字符串保留用于报告输出。

const ALERT_REQUIRED = [
  'alert_id',
  'project_id',
  'rule_id',
  'rule_version',
  'location_id',
  'vuln_type',
  'evidence_revision',
];

const EXEMPTION_REQUIRED = [
  'exemption_id',
  'project_id',
  'rule_id',
  'rule_version',
  'location_id',
  'evidence_revision',
  'valid_from',
  'valid_until',
];

function fail(message) {
  const err = new Error(message);
  err.code = 'INPUT_INVALID';
  throw err;
}

function assertNonEmptyString(record, field, kind, index) {
  const value = record[field];
  if (typeof value !== 'string' || value.trim() === '') {
    fail(`输入错误：${kind}[${index}] 的字段 "${field}" 必须是非空字符串`);
  }
}

function parseInstant(value, label) {
  if (typeof value !== 'string' || value.trim() === '') {
    fail(`输入错误：${label} 必须是 ISO 8601 时间字符串`);
  }
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) {
    fail(`输入错误：${label} 无法解析为时间：${value}`);
  }
  return ms;
}

export function parseAuditTime(value) {
  return parseInstant(value, '核验时间 --at');
}

export function validateAlerts(input) {
  const alerts = Array.isArray(input) ? input : input?.alerts;
  if (!Array.isArray(alerts)) {
    fail('输入错误：告警文件必须是数组，或包含 alerts 数组的对象');
  }
  const seen = new Set();
  return alerts.map((raw, index) => {
    if (raw === null || typeof raw !== 'object') {
      fail(`输入错误：alerts[${index}] 必须是对象`);
    }
    for (const field of ALERT_REQUIRED) {
      assertNonEmptyString(raw, field, 'alerts', index);
    }
    if (seen.has(raw.alert_id)) {
      fail(`输入错误：alert_id 重复：${raw.alert_id}`);
    }
    seen.add(raw.alert_id);

    const evidenceStatus = raw.evidence_status ?? 'active';
    if (evidenceStatus !== 'active' && evidenceStatus !== 'withdrawn') {
      fail(
        `输入错误：alerts[${index}].evidence_status 只允许 active 或 withdrawn，实际为 ${String(
          evidenceStatus,
        )}`,
      );
    }

    return {
      alert_id: raw.alert_id,
      project_id: raw.project_id,
      rule_id: raw.rule_id,
      rule_version: raw.rule_version,
      location_id: raw.location_id,
      vuln_type: raw.vuln_type,
      evidence_revision: raw.evidence_revision,
      evidence_status: evidenceStatus,
    };
  });
}

export function validateExemptions(input) {
  const exemptions = Array.isArray(input) ? input : input?.exemptions;
  if (!Array.isArray(exemptions)) {
    fail('输入错误：豁免文件必须是数组，或包含 exemptions 数组的对象');
  }
  const seen = new Set();
  return exemptions.map((raw, index) => {
    if (raw === null || typeof raw !== 'object') {
      fail(`输入错误：exemptions[${index}] 必须是对象`);
    }
    for (const field of EXEMPTION_REQUIRED) {
      assertNonEmptyString(raw, field, 'exemptions', index);
    }
    if (seen.has(raw.exemption_id)) {
      fail(`输入错误：exemption_id 重复：${raw.exemption_id}`);
    }
    seen.add(raw.exemption_id);

    const validFromString = raw.valid_from;
    const validUntilString = raw.valid_until;
    const valid_from_ms = parseInstant(validFromString, `exemptions[${index}].valid_from`);
    const valid_until_ms = parseInstant(validUntilString, `exemptions[${index}].valid_until`);
    if (valid_from_ms > valid_until_ms) {
      fail(
        `输入错误：豁免 ${raw.exemption_id} 的 valid_from 晚于 valid_until（${validFromString} > ${validUntilString}）`,
      );
    }

    const reconfirmed = raw.reconfirmed_rule_version;
    if (reconfirmed !== undefined && reconfirmed !== null && typeof reconfirmed !== 'string') {
      fail(`输入错误：豁免 ${raw.exemption_id} 的 reconfirmed_rule_version 必须是字符串或 null`);
    }

    return {
      exemption_id: raw.exemption_id,
      project_id: raw.project_id,
      rule_id: raw.rule_id,
      rule_version: raw.rule_version,
      location_id: raw.location_id,
      evidence_revision: raw.evidence_revision,
      valid_from: validFromString,
      valid_until: validUntilString,
      valid_from_ms,
      valid_until_ms,
      reconfirmed_rule_version: reconfirmed ?? null,
      rationale: typeof raw.rationale === 'string' ? raw.rationale : '',
      granted_by: typeof raw.granted_by === 'string' ? raw.granted_by : '',
    };
  });
}

export async function loadJsonFile(path, fs) {
  let text;
  try {
    text = await fs.readFile(path, 'utf8');
  } catch (err) {
    fail(`输入错误：无法读取文件 ${path}：${err.message}`);
  }
  try {
    return JSON.parse(text);
  } catch (err) {
    fail(`输入错误：文件 ${path} 不是合法 JSON：${err.message}`);
  }
}
