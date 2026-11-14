// HTTP 消息定界配置审计：对齐网关与应用两端的离线结构化记录，
// 判定两端对同一条连接是否作出一致的消息定界决策。
//
// 输入记录字段（每条）：
//   record_id         记录编号（字符串，批次内唯一）
//   side              "gateway" | "app"
//   config_version    配置版本（字符串）
//   connection_id     连接标识（字符串）
//   start_offset      抽象起始偏移（整数 >= 0）
//   end_offset        抽象结束偏移（整数 > start_offset）
//   delimitation      定界结果："ok" | "rejected"
//   reject_reason     拒绝原因（rejected 时必填非空字符串，ok 时须为 null/缺省）
//   connection_closed 连接关闭状态：true | false | null（缺失证据）

const SIDES = new Set(['gateway', 'app']);
const DELIMITATIONS = new Set(['ok', 'rejected']);

// 待检查的配置项（按场景给出，稳定且确定）
export const CONFIG_CHECKS = Object.freeze({
  possibly_reused: Object.freeze([
    'gateway.connection.close_on_reject',
    'gateway.connection.reuse_after_error_response',
    'gateway.http.conflicting_content_length_and_transfer_encoding',
    'app.server.max_keep_alive_requests',
  ]),
  missing_close_evidence: Object.freeze([
    'gateway.logging.record_connection_close',
    'gateway.connection.close_on_reject',
    'app.logging.connection_close_events',
  ]),
  connection_closed: Object.freeze([
    'gateway.connection.close_propagated_to_upstream',
  ]),
  range_mismatch: Object.freeze([
    'gateway.http.message_parser (chunked 与 content-length 优先级)',
    'app.http.message_parser (chunked 与 content-length 优先级)',
    'gateway.http.max_header_size 与 app.http.max_header_size 一致性',
  ]),
  decision_mismatch: Object.freeze([
    'gateway.http.request_smuggling_protection',
    'app.http.request_smuggling_protection',
    'gateway.http.obsolete_line_folding 与 app 端一致性',
  ]),
  insufficient_records: Object.freeze([
    'gateway.logging.emit_delimitation_record',
    'app.logging.emit_delimitation_record',
    'logging.shared_connection_id_correlation',
  ]),
});

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function isNonNegativeInt(v) {
  return Number.isInteger(v) && v >= 0;
}

// 校验单条记录，返回错误列表（空数组表示合法）。
export function validateRecord(rec) {
  const errors = [];
  if (!isPlainObject(rec)) {
    return ['record is not an object'];
  }
  if (typeof rec.record_id !== 'string' || rec.record_id.length === 0) {
    errors.push('record_id must be a non-empty string');
  }
  if (!SIDES.has(rec.side)) {
    errors.push('side must be "gateway" or "app"');
  }
  if (typeof rec.config_version !== 'string' || rec.config_version.length === 0) {
    errors.push('config_version must be a non-empty string');
  }
  if (typeof rec.connection_id !== 'string' || rec.connection_id.length === 0) {
    errors.push('connection_id must be a non-empty string');
  }
  if (!isNonNegativeInt(rec.start_offset)) {
    errors.push('start_offset must be an integer >= 0');
  }
  if (!isNonNegativeInt(rec.end_offset)) {
    errors.push('end_offset must be an integer >= 0');
  }
  if (
    isNonNegativeInt(rec.start_offset) &&
    isNonNegativeInt(rec.end_offset) &&
    rec.end_offset <= rec.start_offset
  ) {
    errors.push('end_offset must be greater than start_offset');
  }
  if (!DELIMITATIONS.has(rec.delimitation)) {
    errors.push('delimitation must be "ok" or "rejected"');
  }
  if (rec.delimitation === 'rejected') {
    if (typeof rec.reject_reason !== 'string' || rec.reject_reason.length === 0) {
      errors.push('reject_reason is required (non-empty string) when delimitation is "rejected"');
    }
  } else if (rec.delimitation === 'ok') {
    if (rec.reject_reason !== undefined && rec.reject_reason !== null) {
      errors.push('reject_reason must be null/absent when delimitation is "ok"');
    }
  }
  if (
    rec.connection_closed !== undefined &&
    rec.connection_closed !== null &&
    typeof rec.connection_closed !== 'boolean'
  ) {
    errors.push('connection_closed must be true, false or null');
  }
  return errors;
}

function canonical(rec) {
  return JSON.stringify({
    record_id: rec.record_id,
    side: rec.side,
    config_version: rec.config_version,
    connection_id: rec.connection_id,
    start_offset: rec.start_offset,
    end_offset: rec.end_offset,
    delimitation: rec.delimitation,
    reject_reason: rec.reject_reason ?? null,
    connection_closed: rec.connection_closed ?? null,
  });
}

function normalize(rec) {
  return {
    record_id: rec.record_id,
    side: rec.side,
    config_version: rec.config_version,
    connection_id: rec.connection_id,
    start_offset: rec.start_offset,
    end_offset: rec.end_offset,
    delimitation: rec.delimitation,
    reject_reason: rec.reject_reason ?? null,
    connection_closed: rec.connection_closed ?? null,
  };
}

function byRangeThenId(a, b) {
  if (a.start_offset !== b.start_offset) return a.start_offset - b.start_offset;
  if (a.end_offset !== b.end_offset) return a.end_offset - b.end_offset;
  return a.record_id < b.record_id ? -1 : a.record_id > b.record_id ? 1 : 0;
}

function rangesOverlap(a, b) {
  return Math.max(a.start_offset, b.start_offset) < Math.min(a.end_offset, b.end_offset);
}

// 网关拒绝后的连接关闭状态分析。
function closeAnalysis(rec) {
  if (rec.connection_closed === true) {
    return {
      close_status: 'connection_closed',
      pending_config_checks: [...CONFIG_CHECKS.connection_closed],
    };
  }
  if (rec.connection_closed === false) {
    return {
      close_status: 'possibly_reused',
      pending_config_checks: [...CONFIG_CHECKS.possibly_reused],
    };
  }
  return {
    close_status: 'missing_close_evidence',
    pending_config_checks: [...CONFIG_CHECKS.missing_close_evidence],
  };
}

function pairBase(connId, g, a) {
  return {
    connection_id: connId,
    gateway_record_id: g.record_id,
    app_record_id: a.record_id,
    range: [g.start_offset, g.end_offset],
    config_versions: { gateway: g.config_version, app: a.config_version },
  };
}

function classifyPair(connId, g, a) {
  const base = pairBase(connId, g, a);
  if (g.delimitation === 'ok' && a.delimitation === 'ok') {
    return {
      case: 'normal_forward',
      ...base,
      connection_closed: { gateway: g.connection_closed, app: a.connection_closed },
    };
  }
  if (g.delimitation === 'rejected' && a.delimitation === 'rejected') {
    return {
      case: 'gateway_reject',
      ...base,
      reject_reason: g.reject_reason,
      ...closeAnalysis(g),
    };
  }
  // 同一字节区间，一端放行一端拒绝：定界决策分歧。
  return {
    case: 'boundary_divergence',
    divergence: 'decision_mismatch',
    ...base,
    gateway_delimitation: g.delimitation,
    app_delimitation: a.delimitation,
    gateway_reject_reason: g.reject_reason,
    app_reject_reason: a.reject_reason,
    pending_config_checks: [...CONFIG_CHECKS.decision_mismatch],
  };
}

function gatewayRejectFinding(connId, g) {
  return {
    case: 'gateway_reject',
    connection_id: connId,
    gateway_record_id: g.record_id,
    app_record_id: null,
    range: [g.start_offset, g.end_offset],
    config_versions: { gateway: g.config_version, app: null },
    reject_reason: g.reject_reason,
    ...closeAnalysis(g),
  };
}

function insufficientFinding(connId, g, a, missing) {
  const rec = g ?? a;
  return {
    case: 'insufficient_records',
    missing,
    connection_id: connId,
    gateway_record_id: g ? g.record_id : null,
    app_record_id: a ? a.record_id : null,
    range: [rec.start_offset, rec.end_offset],
    config_versions: {
      gateway: g ? g.config_version : null,
      app: a ? a.config_version : null,
    },
    pending_config_checks: [...CONFIG_CHECKS.insufficient_records],
  };
}

function alignConnection(connId, records) {
  const gw = records.filter((r) => r.side === 'gateway').sort(byRangeThenId);
  const app = records.filter((r) => r.side === 'app').sort(byRangeThenId);
  const findings = [];
  const appMatched = new Set();

  const appByRange = new Map();
  for (const a of app) {
    const key = `${a.start_offset}:${a.end_offset}`;
    if (!appByRange.has(key)) appByRange.set(key, []);
    appByRange.get(key).push(a);
  }

  for (const g of gw) {
    const key = `${g.start_offset}:${g.end_offset}`;
    const exact = (appByRange.get(key) ?? []).find((a) => !appMatched.has(a.record_id));
    if (exact) {
      appMatched.add(exact.record_id);
      findings.push(classifyPair(connId, g, exact));
      continue;
    }
    const overlap = app.find((a) => !appMatched.has(a.record_id) && rangesOverlap(g, a));
    if (overlap) {
      appMatched.add(overlap.record_id);
      findings.push({
        case: 'boundary_divergence',
        divergence: 'range_mismatch',
        connection_id: connId,
        gateway_record_id: g.record_id,
        app_record_id: overlap.record_id,
        gateway_range: [g.start_offset, g.end_offset],
        app_range: [overlap.start_offset, overlap.end_offset],
        config_versions: { gateway: g.config_version, app: overlap.config_version },
        pending_config_checks: [...CONFIG_CHECKS.range_mismatch],
      });
      continue;
    }
    if (g.delimitation === 'rejected') {
      // 网关直接拒绝且应用端无记录：符合预期，进入关闭状态分析。
      findings.push(gatewayRejectFinding(connId, g));
    } else {
      findings.push(insufficientFinding(connId, g, null, 'missing_app_record'));
    }
  }

  for (const a of app) {
    if (!appMatched.has(a.record_id)) {
      findings.push(insufficientFinding(connId, null, a, 'missing_gateway_record'));
    }
  }
  return findings;
}

function findingSortKey(f) {
  const range = f.range ?? f.gateway_range ?? f.app_range ?? [0, 0];
  return [
    f.connection_id,
    range[0],
    range[1],
    f.gateway_record_id ?? '',
    f.app_record_id ?? '',
  ];
}

function compareFindings(a, b) {
  const ka = findingSortKey(a);
  const kb = findingSortKey(b);
  for (let i = 0; i < ka.length; i += 1) {
    if (ka[i] < kb[i]) return -1;
    if (ka[i] > kb[i]) return 1;
  }
  return 0;
}

// 分析一个批次。input: { batch_id?, records: [...] }
// 输出确定性报告：同一批记录重复导入（含完全重复的记录）结论稳定。
export function analyzeBatch(input) {
  if (!isPlainObject(input) || !Array.isArray(input.records)) {
    throw new Error('input must be an object with a "records" array');
  }

  const invalidRecords = [];
  const conflicts = [];
  const seen = new Map();
  let duplicatesIgnored = 0;
  const valid = [];

  input.records.forEach((rec, index) => {
    const errors = validateRecord(rec);
    if (errors.length > 0) {
      invalidRecords.push({
        index,
        record_id: isPlainObject(rec) ? rec.record_id ?? null : null,
        errors,
      });
      return;
    }
    const canon = canonical(rec);
    if (seen.has(rec.record_id)) {
      if (seen.get(rec.record_id) === canon) {
        duplicatesIgnored += 1; // 完全重复：忽略，结论不变
      } else {
        conflicts.push({
          record_id: rec.record_id,
          detail: 'same record_id imported with different content; first occurrence kept',
        });
      }
      return;
    }
    seen.set(rec.record_id, canon);
    valid.push(normalize(rec));
  });

  const byConnection = new Map();
  for (const rec of valid) {
    if (!byConnection.has(rec.connection_id)) byConnection.set(rec.connection_id, []);
    byConnection.get(rec.connection_id).push(rec);
  }

  const findings = [];
  for (const connId of [...byConnection.keys()].sort()) {
    findings.push(...alignConnection(connId, byConnection.get(connId)));
  }
  findings.sort(compareFindings);

  const summary = {
    batch_id: typeof input.batch_id === 'string' ? input.batch_id : null,
    total_input_records: input.records.length,
    valid_records: valid.length,
    duplicates_ignored: duplicatesIgnored,
    invalid_records: invalidRecords.length,
    conflicts: conflicts.length,
    connections: byConnection.size,
    normal_forward: 0,
    gateway_reject: 0,
    boundary_divergence: 0,
    insufficient_records: 0,
    close_status: { connection_closed: 0, possibly_reused: 0, missing_close_evidence: 0 },
  };
  for (const f of findings) {
    summary[f.case] += 1;
    if (f.case === 'gateway_reject') {
      summary.close_status[f.close_status] += 1;
    }
  }

  return { summary, findings, invalid_records: invalidRecords, conflicts };
}

function fmtRange(f) {
  const r = f.range ?? null;
  if (r) return `[${r[0]}, ${r[1]})`;
  return `gw=[${f.gateway_range[0]}, ${f.gateway_range[1]}) app=[${f.app_range[0]}, ${f.app_range[1]})`;
}

// 文本报告：每条结论可定位到连接、记录编号与配置版本。
export function renderText(report) {
  const lines = [];
  const s = report.summary;
  lines.push(`HTTP 消息定界配置审计报告 (batch: ${s.batch_id ?? 'n/a'})`);
  lines.push(
    `记录: 输入 ${s.total_input_records}, 有效 ${s.valid_records}, ` +
      `重复忽略 ${s.duplicates_ignored}, 非法 ${s.invalid_records}, 冲突 ${s.conflicts}; 连接数 ${s.connections}`,
  );
  lines.push(
    `分类: 正常转发 ${s.normal_forward}, 网关拒绝 ${s.gateway_reject}, ` +
      `边界分歧 ${s.boundary_divergence}, 记录不足 ${s.insufficient_records}`,
  );
  lines.push(
    `网关拒绝关闭状态: 已关闭 ${s.close_status.connection_closed}, ` +
      `可能复用 ${s.close_status.possibly_reused}, 缺少关闭证据 ${s.close_status.missing_close_evidence}`,
  );
  lines.push('');
  for (const f of report.findings) {
    const cv = f.config_versions;
    const cvStr = `config(gateway=${cv.gateway ?? 'n/a'}, app=${cv.app ?? 'n/a'})`;
    const recStr = `records(gateway=${f.gateway_record_id ?? 'n/a'}, app=${f.app_record_id ?? 'n/a'})`;
    lines.push(`- [${f.case}] conn=${f.connection_id} ${fmtRange(f)} ${recStr} ${cvStr}`);
    if (f.case === 'gateway_reject') {
      lines.push(`    reject_reason=${f.reject_reason} close_status=${f.close_status}`);
    }
    if (f.case === 'boundary_divergence') {
      lines.push(`    divergence=${f.divergence}`);
    }
    if (f.case === 'insufficient_records') {
      lines.push(`    missing=${f.missing}`);
    }
    if (Array.isArray(f.pending_config_checks) && f.pending_config_checks.length > 0) {
      lines.push(`    待检查配置项:`);
      for (const c of f.pending_config_checks) lines.push(`      - ${c}`);
    }
  }
  if (report.invalid_records.length > 0) {
    lines.push('');
    lines.push('非法记录:');
    for (const inv of report.invalid_records) {
      lines.push(`- index=${inv.index} record_id=${inv.record_id ?? 'n/a'}: ${inv.errors.join('; ')}`);
    }
  }
  if (report.conflicts.length > 0) {
    lines.push('');
    lines.push('冲突记录:');
    for (const c of report.conflicts) {
      lines.push(`- record_id=${c.record_id}: ${c.detail}`);
    }
  }
  return lines.join('\n');
}
