/**
 * 离线协商回归入口：
 *  1) 对全部夹具依次运行 v1(配置分支) / v1(默认分支) / v2 三套协商；
 *  2) 用独立审计器对照资源预算核验每条响应决策；
 *  3) 生成 Markdown 对照表与 JSON 决策记录到 artifacts/；
 *  4) 以退出码作为回归门禁：v2 必须全部预算安全，v1 默认分支必须复现失效。
 *
 * 运行：npm run report  （不发起任何网络请求）
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { parseClientOffer } from '../src/parser.js';
import { negotiateV1 } from '../src/policy-v1.js';
import { negotiateV2 } from '../src/policy-v2.js';
import { auditDecision } from '../src/auditor.js';
import {
  CAPABILITY_V1,
  CAPABILITY_V2,
  RESOURCE_BUDGET,
  POLICY_WINDOW_RANGE,
} from '../src/capabilities.js';
import {
  RULE,
  RULE_ID_TO_KEY,
  SOURCE_LABELS,
  VERDICT,
  VERDICT_LABELS,
  PMD,
} from '../src/constants.js';
import { FIXTURES } from '../test/fixtures.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const ARTIFACTS_DIR = join(HERE, '..', 'artifacts');

const COLUMN_DEFS = [
  { key: 'v1Configured', title: 'v1 配置分支', run: (p) => negotiateV1(p, CAPABILITY_V1, { route: 'configured' }) },
  { key: 'v1Default', title: 'v1 默认分支(遗留)', run: (p) => negotiateV1(p, CAPABILITY_V1, { route: 'default' }) },
  { key: 'v2', title: 'v2 修正策略', run: (p) => negotiateV2(p, CAPABILITY_V2) },
];

function cell(value) {
  return value === null || value === undefined ? '—' : String(value).replaceAll('|', '\\|');
}

function extOf(decision) {
  return decision.extensions[0];
}

function verdictText(ext) {
  const base = VERDICT_LABELS[ext.verdict] ?? ext.verdict;
  if (ext.verdict === VERDICT.REJECT) return `${base}（${ext.rejectReason}）`;
  if (ext.verdict === VERDICT.PENDING_CONFIRMATION) return `${base}（缺项 ${ext.completeness.missing.join('、')}）`;
  return base;
}

function appliedCell(ext, paramKey) {
  const p = ext.parameters.find((x) => x.key === paramKey);
  if (!p) return '—（缺失）';
  return `${p.appliedValue ?? '未回应'} · ${SOURCE_LABELS[p.source]}`;
}

function buildMatrixRows(results) {
  return results.map((r) => {
    const row = [r.fixture.id, r.fixture.title];
    for (const col of COLUMN_DEFS) {
      const d = r.decisions[col.key];
      const audit = r.audits[col.key];
      const sp = d.subprotocol.selected ? `${d.subprotocol.selected}(${d.subprotocol.rule})` : `无(${d.subprotocol.rule})`;
      const safe = d.extensions[0].verdict === VERDICT.ACCEPT
        ? (audit.budgetSafe ? '预算安全✓' : '预算失效✗')
        : '不适用';
      row.push(`${sp}<br>${verdictText(d.extensions[0])}<br>${safe}`);
    }
    return row;
  });
}

function buildProvenanceTable(results, fixtureId) {
  const fx = results.find((r) => r.fixture.id === fixtureId);
  const paramOrder = [
    PMD.SERVER_MAX_WINDOW_BITS,
    PMD.CLIENT_MAX_WINDOW_BITS,
    PMD.SERVER_NO_CONTEXT_TAKEOVER,
    PMD.CLIENT_NO_CONTEXT_TAKEOVER,
  ];
  const lines = [];
  lines.push(`#### ${fixtureId} ${fx.fixture.title}`);
  lines.push('');
  lines.push(
    '| 参数 | 客户端声明值 | v1 配置分支（生效值·来源） | v1 默认分支（生效值·来源） | v2（生效值·来源） | RFC 合法范围 | 策略合法范围 |',
  );
  lines.push('| --- | --- | --- | --- | --- | --- | --- |');

  const v1c = extOf(fx.decisions.v1Configured);
  const v1d = extOf(fx.decisions.v1Default);
  const v2 = extOf(fx.decisions.v2);
  const parsed = parseClientOffer(fx.fixture.rawHeaders);
  const offer = parsed.extensionOffers.find((o) => o.name === 'permessage-deflate');

  for (const key of paramOrder) {
    const declared = offer?.params?.[key];
    const declaredText = !declared?.present
      ? '未声明'
      : declared.value === null
        ? '裸标志（无值）'
        : String(declared.value);
    const anyParam =
      v1c.parameters.find((p) => p.key === key) ??
      v2.parameters.find((p) => p.key === key);
    const isWindow = key.endsWith('_max_window_bits');
    const rfc = !anyParam
      ? '—'
      : isWindow
        ? `[${anyParam.rfcRange.min}, ${anyParam.rfcRange.max}]（整数）`
        : '布尔标志（RFC：存在即生效）';
    const policy = !anyParam
      ? '—'
      : isWindow
        ? `[${anyParam.policyRange.min}, ${anyParam.policyRange.max}]（RFC ∩ 预算）`
        : '必须为 true（PM-3 强制注入）';
    lines.push(
      `| ${cell(anyParam?.label ?? key)} | ${cell(declaredText)} | ${cell(appliedCell(v1c, key))} | ${cell(appliedCell(v1d, key))} | ${cell(appliedCell(v2, key))} | ${rfc} | ${policy} |`,
    );
  }
  lines.push('');
  return lines.join('\n');
}

const RULE_MATRIX = [
  { rule: 'SP-1', v1c: '✓', v1d: '✓', v2: '✓', note: '正常选择，无降级' },
  { rule: 'SP-2', v1c: '✓', v1d: '✓', v2: '✓', note: '兼容性降级：无公共子协议时不回应子协议头，回退裸 WebSocket' },
  { rule: 'SP-3', v1c: '✓', v1d: '✓', v2: '✓', note: '客户端未声明，仅回显协议版本' },
  { rule: 'EX-1', v1c: '✓', v1d: '✓', v2: '✓', note: '兼容性降级：可选扩展按能力表开关省略，连接不启用压缩' },
  { rule: 'EX-2', v1c: '✓', v1d: '✓', v2: '✓', note: '畸形声明拒绝，协议级防护两代一致' },
  { rule: 'PM-1', v1c: '✓', v1d: '✗ 缺失', v2: '✓', note: 'v1 默认分支原样回显 15，未与预算上限 12 取 min，窗口内存超配 8 倍（2^15/2^12）' },
  { rule: 'PM-2', v1c: '✓', v1d: '✗ 缺失', v2: '✓', note: 'v1 默认分支合法区间被放宽为 RFC [8,15]，预算交集 [8,12] 失效' },
  { rule: 'PM-3', v1c: '✓', v1d: '✗ 缺失', v2: '✓', note: 'v1 默认分支不强制 no_context_takeover，跨消息压缩状态不受预算约束' },
  { rule: 'PM-4', v1c: '✓', v1d: '✗ 缺失', v2: '✓', note: `v1 默认分支不绑定解压上限 ${RESOURCE_BUDGET.maxDecompressedMessageBytes} 字节，解压炸弹无上限` },
  { rule: 'PM-5', v1c: '— 无闸门', v1d: '— 无闸门', v2: '✓', note: 'v1 对不完整声明静默接受（隐含 RFC 缺省 15）；v2 单列待确认，禁止未确认参数投产' },
  { rule: 'PM-6', v1c: '（仅配置分支）', v1d: '✗ 根因缺失', v2: '✓', note: '根因规则：v1 默认分支整体豁免 PM-1..PM-4；v2 统一预算路径后该豁免消失' },
];

function buildRuleTable() {
  const lines = [
    '| 策略条目 | 规则内容 | v1 配置分支 | v1 默认分支 | v2 修正策略 | 导致的降级 / 防护失效 |',
    '| --- | --- | --- | --- | --- | --- |',
  ];
  for (const r of RULE_MATRIX) {
    lines.push(
      `| ${r.rule} | ${cell(RULE[RULE_ID_TO_KEY[r.rule]])} | ${r.v1c} | ${r.v1d} | ${r.v2} | ${cell(r.note)} |`,
    );
  }
  return lines.join('\n');
}

function main() {
  mkdirSync(ARTIFACTS_DIR, { recursive: true });

  const results = FIXTURES.map((fixture) => {
    const parsed = parseClientOffer(fixture.rawHeaders);
    const decisions = {};
    const audits = {};
    for (const col of COLUMN_DEFS) {
      decisions[col.key] = col.run(parsed);
      audits[col.key] = auditDecision(decisions[col.key]);
    }
    return { fixture, parsed, decisions, audits };
  });

  // ---- 回归门禁断言 ----
  const failures = [];
  for (const r of results) {
    const fx = r.fixture;
    for (const col of COLUMN_DEFS) {
      const ext = extOf(r.decisions[col.key]);
      const expected = fx.expected[col.key];
      if (ext.verdict !== expected.extensionVerdict) {
        failures.push(`${fx.id}/${col.key}: 判定 ${ext.verdict} ≠ 预期 ${expected.extensionVerdict}`);
      }
      if (r.decisions[col.key].subprotocol.selected !== expected.subprotocol) {
        failures.push(`${fx.id}/${col.key}: 子协议不符`);
      }
      if (expected.budgetSafe !== undefined && r.audits[col.key].budgetSafe !== expected.budgetSafe) {
        failures.push(`${fx.id}/${col.key}: 预算安全结论不符`);
      }
    }
    // v2 所有 ACCEPT 决策必须预算安全
    if (extOf(r.decisions.v2).verdict === VERDICT.ACCEPT && !r.audits.v2.budgetSafe) {
      failures.push(`${fx.id}/v2: 修正策略仍存在预算失效`);
    }
  }
  // 核心回归：TC-08 的 v1 默认分支必须复现防护失效，v2 必须修复
  const tc08 = results.find((r) => r.fixture.id === 'TC-08');
  if (tc08.audits.v1Default.budgetSafe) failures.push('TC-08: v1 默认分支未复现预算失效，回归失效');
  if (!tc08.audits.v2.budgetSafe) failures.push('TC-08: v2 未修复默认分支缺陷');

  // ---- Markdown 报告 ----
  const md = [];
  md.push('# WebSocket 扩展协商离线回归报告');
  md.push('');
  md.push(`生成时间：${new Date().toISOString()} ｜ 运行方式：纯离线，无网络请求 ｜ 夹具数：${FIXTURES.length}`);
  md.push('');
  md.push('## 1. 资源预算与策略合法范围（服务端消息处理预设）');
  md.push('');
  md.push('| 预算项 | 值 | 说明 |');
  md.push('| --- | --- | --- |');
  md.push(`| maxWindowBits | ${RESOURCE_BUDGET.maxWindowBits} | 单压缩上下文 LZ77 窗口上限，策略窗口合法区间 [${POLICY_WINDOW_RANGE.min}, ${POLICY_WINDOW_RANGE.max}] = RFC [8,15] ∩ 预算 |`);
  md.push(`| maxDecompressedMessageBytes | ${RESOURCE_BUDGET.maxDecompressedMessageBytes}（1 MiB） | 单消息解压字节上限，扩展启用即必须绑定（PM-4） |`);
  md.push(`| enforceNoContextTakeover | ${RESOURCE_BUDGET.enforceNoContextTakeover} | 双向禁止跨消息上下文接管（PM-3） |`);
  md.push('');
  md.push('## 2. 用例 × 策略响应决策矩阵');
  md.push('');
  md.push('| 用例 | 场景 | v1 配置分支（子协议/扩展判定/预算） | v1 默认分支（子协议/扩展判定/预算） | v2 修正策略（子协议/扩展判定/预算） |');
  md.push('| --- | --- | --- | --- | --- |');
  for (const row of buildMatrixRows(results)) md.push(`| ${row.map(cell).join(' | ')} |`);
  md.push('');
  md.push('## 3. 每项参数的来源与策略规定的合法范围');
  md.push('');
  for (const id of ['TC-06', 'TC-07', 'TC-08']) md.push(buildProvenanceTable(results, id));
  md.push('## 4. 策略条目对照：实际选择、适用限制与失效定位');
  md.push('');
  md.push(buildRuleTable());
  md.push('');
  md.push('## 5. 回归结论');
  md.push('');
  md.push('- **防护失效根因（v1）**：默认分支缺失 PM-6，整体豁免 PM-1..PM-4。TC-08 中对等端声明的 15 位窗口被原样回显，超出服务端预算 12（压缩上下文内存超配 8 倍），且无上下文接管限制、无 1 MiB 解压预算绑定。审计双轨结论显示该决策 `policyConformant=true`（忠实执行了缺限制的旧能力表）但 `budgetSafe=false`，证明缺陷在策略层而非实现偏差。');
  md.push('- **兼容性降级的具体规则**：SP-2（无公共子协议 → 不回应子协议头）与 EX-1（能力表关闭/未声明 → 省略扩展），两者均为策略允许的合法降级，不构成防护缺口。');
  md.push('- **v2 修复验证**：统一预算路径使 TC-08 的窗口钳制到 12、强制双向 no_context_takeover、绑定解压预算，全部审计检查通过；PM-5 闸门把不完整声明（TC-09）单列为 PENDING_CONFIRMATION，杜绝 v1 的静默 RFC 缺省套用。');
  md.push('');
  md.push(`回归门禁：${failures.length === 0 ? '**全部通过 ✓**' : `**${failures.length} 项失败 ✗**`}`);
  if (failures.length > 0) md.push('', ...failures.map((f) => `- ${f}`));
  md.push('');

  writeFileSync(join(ARTIFACTS_DIR, 'negotiation-report.md'), md.join('\n'), 'utf8');

  // ---- JSON 决策记录（机器可消费） ----
  const jsonPayload = {
    generatedAt: new Date().toISOString(),
    resourceBudget: RESOURCE_BUDGET,
    policyWindowRange: POLICY_WINDOW_RANGE,
    results: results.map((r) => ({
      fixtureId: r.fixture.id,
      title: r.fixture.title,
      rawHeaders: r.fixture.rawHeaders,
      parsedOffer: r.parsed,
      decisions: r.decisions,
      audits: r.audits,
    })),
    gateFailures: failures,
  };
  writeFileSync(
    join(ARTIFACTS_DIR, 'negotiation-report.json'),
    JSON.stringify(jsonPayload, null, 2),
    'utf8',
  );

  console.log(`报告已生成：artifacts/negotiation-report.md / .json（${results.length} 个夹具 × 3 策略列）`);
  if (failures.length > 0) {
    console.error('回归门禁失败：');
    for (const f of failures) console.error(` - ${f}`);
    process.exit(1);
  }
  console.log('回归门禁通过：v1 默认分支缺陷已复现，v2 修正策略全部预算安全。');
}

main();
