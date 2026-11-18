import { GateInputError, ReasonCode, Source } from './errors.js';

/**
 * 字段角色（field role）：决定差异如何被解读，而不是只做“值相等”的朴素比较。
 * - identifier   清单对齐用的业务标识
 * - op           实际操作
 * - target       操作目标
 * - scope        权限范围，仅允许收窄
 * - security     安全选项，无法跨版本映射即阻断
 * - info         其他业务参数，要求与审批一致
 */
export const FieldRole = Object.freeze({
  IDENTIFIER: 'identifier',
  OP: 'op',
  TARGET: 'target',
  SCOPE: 'scope',
  SECURITY: 'security',
  INFO: 'info'
});

const KNOWN_ROLES = new Set(Object.values(FieldRole));
const KNOWN_TYPES = new Set(['string', 'integer', 'boolean', 'enum', 'set']);

function makeFinding(code, source, location, message, extra = {}) {
  return { code, source, location, message, ...extra };
}

/** 按点路径读取嵌套对象，例如 getPath(obj, 'target.resourceId')。 */
export function getPath(obj, path) {
  return path.split('.').reduce((cur, key) => (cur == null ? undefined : cur[key]), obj);
}

/** 收集对象实际出现过的全部点路径（对象嵌套，数组本身不展开）。 */
function collectPaths(value, prefix = '') {
  const out = [];
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    out.push(prefix);
    return out;
  }
  for (const [key, child] of Object.entries(value)) {
    const p = prefix ? `${prefix}.${key}` : key;
    if (child !== null && typeof child === 'object' && !Array.isArray(child)) {
      out.push(...collectPaths(child, p));
    } else {
      out.push(p);
    }
  }
  return out;
}

function conditionHolds(when, effective) {
  if (!when) return true;
  const actual = getPath(effective, when.field);
  if (Object.hasOwn(when, 'equals')) return actual === when.equals;
  if (Object.hasOwn(when, 'in')) return Array.isArray(when.in) && when.in.includes(actual);
  throw new GateInputError(`字段 ${when.field} 的条件缺少 equals/in`);
}

/**
 * 选取首个满足条件的条件默认值；无条件 fallback 排在最后兜底。
 * 匹配是“首个命中”，与语义表的声明顺序保持一致。
 */
export function selectDefault(fieldDef, effective) {
  let fallback;
  for (const candidate of fieldDef.defaults ?? []) {
    if (!candidate.when) {
      fallback = candidate;
      continue;
    }
    if (conditionHolds(candidate.when, effective)) return candidate;
  }
  return fallback;
}

/**
 * 类型校验：按字段声明的类型还原原始值，类型不符产生 TYPE_VIOLATION。
 * set 允许数组或逗号分隔字符串；integer 拒绝布尔与非整数值。
 */
function coerceByType(fieldDef, rawValue) {
  const { path, type } = fieldDef;
  switch (type) {
    case 'string':
      if (typeof rawValue !== 'string') return { error: `字段 ${path} 需要 string，实际为 ${typeof rawValue}` };
      return { value: rawValue };
    case 'integer':
      if (typeof rawValue === 'boolean' || !Number.isInteger(rawValue)) {
        return { error: `字段 ${path} 需要 integer，实际为 ${JSON.stringify(rawValue)}` };
      }
      return { value: rawValue };
    case 'boolean':
      if (typeof rawValue !== 'boolean') return { error: `字段 ${path} 需要 boolean，实际为 ${typeof rawValue}` };
      return { value: rawValue };
    case 'enum':
      if (typeof rawValue !== 'string') return { error: `字段 ${path} 需要 enum(string)` };
      if (fieldDef.enum && !fieldDef.enum.includes(rawValue)) {
        return { error: `字段 ${path} 的值 ${rawValue} 不在枚举 ${fieldDef.enum.join('|')} 内` };
      }
      return { value: rawValue };
    case 'set':
      let values;
      if (Array.isArray(rawValue)) values = rawValue.slice();
      else if (typeof rawValue === 'string') values = rawValue.split(',').map((s) => s.trim()).filter(Boolean);
      else return { error: `字段 ${path} 需要 set(数组或逗号分隔字符串)` };
      if (fieldDef.enum) {
        const bad = values.filter((v) => !fieldDef.enum.includes(v));
        if (bad.length) return { error: `字段 ${path} 含未声明集合值 ${bad.join(', ')}` };
      }
      return { value: [...new Set(values)].sort() };
    default:
      throw new GateInputError(`字段 ${path} 的类型 ${type} 不受支持`);
  }
}

function validateSemanticTable(table) {
  if (!table || typeof table !== 'object') throw new GateInputError('语义表必须为对象');
  if (typeof table.service !== 'string' || !table.service) throw new GateInputError('语义表缺少 service');
  if (!Number.isInteger(table.versionOrdinal)) throw new GateInputError(`服务 ${table.service} 的语义表缺少整数 versionOrdinal`);
  if (typeof table.versionLabel !== 'string') throw new GateInputError(`服务 ${table.service} 的语义表缺少 versionLabel`);
  if (!Array.isArray(table.fields)) throw new GateInputError(`服务 ${table.service} 的语义表缺少 fields 数组`);
  const paths = new Set();
  for (const f of table.fields) {
    if (!f || typeof f.path !== 'string') throw new GateInputError('语义表字段缺少 path');
    if (paths.has(f.path)) throw new GateInputError(`服务 ${table.service} 字段 ${f.path} 重复声明`);
    paths.add(f.path);
    if (!KNOWN_TYPES.has(f.type)) throw new GateInputError(`字段 ${f.path} 类型 ${f.type} 不受支持`);
    if (!KNOWN_ROLES.has(f.role)) throw new GateInputError(`字段 ${f.path} 角色 ${f.role} 不受支持`);
    if (f.defaults !== undefined && !Array.isArray(f.defaults)) throw new GateInputError(`字段 ${f.path} 的 defaults 必须为数组`);
  }
}

/**
 * 按语义表把一条原始解析记录还原为“有效参数”。
 *
 * 处理顺序：
 * 1. 未在语义表声明的原始字段 -> UNDECLARED_FIELD（门禁失败关闭，不静默忽略）；
 * 2. 显式给出的字段做类型/枚举校验；
 * 3. 缺失字段按条件默认值做定点迭代填充，依赖环 -> DEFAULT_CYCLE；
 * 4. 仍缺失且 required -> MISSING_FIELD。
 *
 * @returns {{record: object, appliedDefaults: Array, findings: Array}}
 */
export function restoreOne(rawRecord, semanticTable, source) {
  validateSemanticTable(semanticTable);
  const findings = [];
  const appliedDefaults = [];
  const fieldByPath = new Map(semanticTable.fields.map((f) => [f.path, f]));

  if (rawRecord === null || typeof rawRecord !== 'object' || Array.isArray(rawRecord)) {
    findings.push(makeFinding(ReasonCode.TYPE_VIOLATION, source, '$', '解析记录必须为对象'));
    return { record: {}, appliedDefaults, findings };
  }

  // 1. 未声明字段（嵌套路径逐点比对）
  for (const actualPath of collectPaths(rawRecord)) {
    if (!fieldByPath.has(actualPath)) {
      findings.push(
        makeFinding(ReasonCode.UNDECLARED_FIELD, source, actualPath, `字段 ${actualPath} 未出现在 ${semanticTable.service} v${semanticTable.versionLabel} 的语义表中`)
      );
    }
  }

  // 2. 显式字段类型还原
  const record = {};
  for (const fieldDef of semanticTable.fields) {
    const rawValue = getPath(rawRecord, fieldDef.path);
    if (rawValue === undefined) continue;
    const result = coerceByType(fieldDef, rawValue);
    if (result.error) {
      findings.push(makeFinding(ReasonCode.TYPE_VIOLATION, source, fieldDef.path, result.error));
    } else {
      assignPath(record, fieldDef.path, result.value);
    }
  }

  // 3. 条件默认值定点迭代：每轮至少填充一个字段；整轮无进展则剩余项存在循环/缺失依赖
  const pending = semanticTable.fields.filter((f) => getPath(record, f.path) === undefined && f.defaults && f.defaults.length);
  let progressed = true;
  while (pending.length && progressed) {
    progressed = false;
    for (let i = pending.length - 1; i >= 0; i--) {
      const fieldDef = pending[i];
      const selected = selectDefault(fieldDef, record);
      if (!selected) {
        // 仅声明了条件默认值且当前无一命中：视为依赖未就绪，留在 pending 参与收敛检测
        continue;
      }
      // 依赖字段尚未就绪：本轮跳过，等待后续轮次
      if (selected.when && getPath(record, selected.when.field) === undefined) continue;
      const coerced = coerceByType(fieldDef, selected.value);
      if (coerced.error) {
        findings.push(makeFinding(ReasonCode.TYPE_VIOLATION, source, fieldDef.path, `条件默认值类型错误：${coerced.error}`));
      } else {
        assignPath(record, fieldDef.path, coerced.value);
        appliedDefaults.push({
          path: fieldDef.path,
          role: fieldDef.role,
          value: coerced.value,
          reason: selected.reason ?? (selected.when ? `when ${selected.when.field}` : 'fallback')
        });
      }
      pending.splice(i, 1);
      progressed = true;
    }
  }
  for (const stuck of pending) {
    findings.push(makeFinding(ReasonCode.DEFAULT_CYCLE, source, stuck.path, `字段 ${stuck.path} 的默认值依赖无法收敛`));
  }

  // 4. 必填校验
  for (const fieldDef of semanticTable.fields) {
    if (fieldDef.required && getPath(record, fieldDef.path) === undefined) {
      findings.push(makeFinding(ReasonCode.MISSING_FIELD, source, fieldDef.path, `必填字段 ${fieldDef.path} 缺失且无可用默认值`));
    }
  }

  return { record, appliedDefaults, findings };
}

function assignPath(target, path, value) {
  const keys = path.split('.');
  let cur = target;
  for (let i = 0; i < keys.length - 1; i++) {
    cur[keys[i]] ??= {};
    cur = cur[keys[i]];
  }
  cur[keys.at(-1)] = value;
}

/**
 * 把一个值从源版本词汇表映射到目标版本词汇表。
 * set 逐元素映射；任一元素无法映射时整体失败，并返回首个坏值。
 *
 * @returns {{ok: true, value: *} | {ok: false, badValue: *}}
 */
export function mapValueAcrossVersions(value, fieldDef, targetTable) {
  const mapping = (fieldDef.mappings ?? []).find((m) => m.targetService === targetTable.service && m.targetVersion === targetTable.versionLabel);
  if (!mapping) return { ok: true, value };
  if (fieldDef.type === 'set') {
    const mapped = [];
    for (const item of value) {
      if (!Object.hasOwn(mapping.values, item)) return { ok: false, badValue: item };
      mapped.push(mapping.values[item]);
    }
    return { ok: true, value: [...new Set(mapped)].sort() };
  }
  if (!Object.hasOwn(mapping.values, value)) return { ok: false, badValue: value };
  return { ok: true, value: mapping.values[value] };
}

/**
 * 把一条已还原记录的全部可映射字段转换到目标版本词汇表。
 * 安全选项无法映射 -> SECURITY_UNMAPPED；其他枚举无法映射 -> ENUM_UNMAPPED。
 */
export function mapRecordToVersion(record, sourceTable, targetTable, source) {
  const mapped = structuredClone(record);
  const findings = [];
  for (const fieldDef of sourceTable.fields) {
    const current = getPath(mapped, fieldDef.path);
    if (current === undefined) continue;
    const result = mapValueAcrossVersions(current, fieldDef, targetTable);
    if (!result.ok) {
      const code = fieldDef.role === FieldRole.SECURITY ? ReasonCode.SECURITY_UNMAPPED : ReasonCode.ENUM_UNMAPPED;
      findings.push(
        makeFinding(
          code,
          source,
          fieldDef.path,
          `字段 ${fieldDef.path} 的值 ${result.badValue} 无法从 ${sourceTable.service} v${sourceTable.versionLabel} 映射到 ${targetTable.service} v${targetTable.versionLabel}`,
          { badValue: result.badValue, fieldRole: fieldDef.role }
        )
      );
      continue;
    }
    assignPath(mapped, fieldDef.path, result.value);
  }
  return { record: mapped, findings };
}

/**
 * 权限范围子集判定：
 * - set 类型：执行侧集合必须是审批侧集合的子集；
 * - 声明了 rank 的 enum/string：数值越大权限越大，执行侧 rank 必须 <= 审批侧 rank；
 * - 未声明 rank 的标量：必须严格相等。
 */
export function isScopeNarrower(approvalValue, executionValue, fieldDef) {
  if (fieldDef.type === 'set') {
    const allowed = new Set(approvalValue);
    return executionValue.every((v) => allowed.has(v));
  }
  if (fieldDef.rank && typeof fieldDef.rank === 'object') {
    const a = fieldDef.rank[approvalValue];
    const e = fieldDef.rank[executionValue];
    if (a === undefined || e === undefined) return executionValue === approvalValue;
    return e <= a;
  }
  return executionValue === approvalValue;
}

export { makeFinding };
