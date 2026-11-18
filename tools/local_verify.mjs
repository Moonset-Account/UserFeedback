#!/usr/bin/env node
/**
 * local_verify.mjs — 本地核验入口（运行入口，非独立合格证明）。
 *
 * 用法：
 *   node tools/local_verify.mjs --list
 *   node tools/local_verify.mjs --case C01|C02|C03|C04|ALL
 *
 * 每个用例输出一条 JSON 记录：case、断言总数、passed/failed/skipped、
 * 种子与可重放样本、逐条期望/观察摘要。任一断言失败、入口缺失、
 * 未实现、零断言或整组跳过均以非零退出。
 */
import { isDeepStrictEqual } from 'node:util';
import { EventMatcher, RESULT_STATUS } from '../src/event_matcher.mjs';

const SEED = 20261119;
const T0 = 1_700_000_000_000; // 固定合成基准时间（毫秒），保证可重放

/** 构造合成事件：id、类型、主体(user)、相对事件时间；到达时间默认滞后 500ms。 */
function ev(id, type, user, relMs, arrivalSkewMs = 500) {
  return {
    id,
    type,
    user,
    eventTimeMs: T0 + relMs,
    arrivalTimeMs: T0 + relMs + arrivalSkewMs,
  };
}

const RULE_V1 = {
  subjectKey: ['user'],
  sequence: [{ type: 'sensitive_read' }, { type: 'external_send' }],
  maxIntervalMs: 300_000, // 五分钟
  absence: [{ type: 'approval' }],
  allowedLatenessMs: 60_000,
};

const RULE_V2 = {
  subjectKey: ['user'],
  sequence: [{ type: 'sensitive_read' }, { type: 'external_send' }],
  maxIntervalMs: 100_000, // 收紧间隔
  absence: [{ type: 'approval' }, { type: 'cancel' }], // 新增缺席类型
  allowedLatenessMs: 60_000,
};

function summarize(value) {
  let s;
  try {
    s = JSON.stringify(value);
  } catch {
    s = String(value);
  }
  if (s === undefined) s = String(value);
  return s.length > 240 ? `${s.slice(0, 237)}...` : s;
}

class Checker {
  constructor() {
    this.checks = [];
  }
  check(name, expected, observed) {
    this.checks.push({
      name,
      pass: isDeepStrictEqual(expected, observed),
      expected: summarize(expected),
      observed: summarize(observed),
    });
  }
}

/** 采样记录：仅合成数据，不含任何真实敏感内容。 */
function sampleOf(e) {
  return { id: e.id, type: e.type, user: e.user, eventTimeRelMs: e.eventTimeMs - T0 };
}

// ---------------------------------------------------------------- C01
function caseC01() {
  const c = new Checker();
  const samples = [];
  const engine = new EventMatcher({ allowedLatenessMs: 60_000 });
  engine.publishRule({ ruleId: 'exfil-after-read', version: 1, definition: RULE_V1, publishedAtMs: T0 });
  const feed = (e) => {
    samples.push(sampleOf(e));
    return engine.ingest(e);
  };

  feed(ev('e1', 'sensitive_read', 'u1', 0));
  feed(ev('e2', 'sensitive_read', 'u2', 0));
  feed(ev('e3', 'external_send', 'u1', 100_000)); // u1 在 300s 间隔内完成序列
  feed(ev('e4', 'external_send', 'u2', 400_000)); // u2 超出最大间隔
  feed(ev('e5', 'sensitive_read', 'u3', 0));
  feed(ev('e6', 'approval', 'u3', 50_000)); // u3 区间内出现批准
  feed(ev('e7', 'external_send', 'u3', 100_000)); // u3 候选已被撤销，send 无候选可续
  feed(ev('e8', 'sensitive_read', 'u4', 0));
  feed(ev('e9', 'external_send', 'u4', 100_000)); // u4 先形成暂定匹配
  feed(ev('e10', 'approval', 'u4', 60_000)); // 批准落在 u4 缺席窗口内
  feed(ev('e11', 'approval', 'u1', 150_000)); // 落在 u1 窗口 (0,100s] 之外

  const before = engine.getResults();
  const m1 = before.find((r) => r.subjectKey === 'user=u1');
  const r3 = before.filter((r) => r.subjectKey === 'user=u3');
  const r4 = before.filter((r) => r.subjectKey === 'user=u4');

  c.check('主体键: u1 匹配归属于 user=u1', 'user=u1', m1?.subjectKey);
  c.check('正向序列: u1 匹配参与记录为 [e1,e3]', ['e1', 'e3'], m1?.participations.map((p) => p.eventId));
  c.check('缺席窗口: u1 窗口为 (0, 100000]', { startMs: T0, endMs: T0 + 100_000 }, m1?.window);
  c.check('时间间隔: u2 的 send@400s 超出 300s 最大间隔, 无匹配结果', 0, before.filter((r) => r.subjectKey === 'user=u2').length);
  c.check('时间间隔: u2 候选未闭合前仍活跃', 1, engine.getActiveCandidates().filter((k) => k.subjectKey === 'user=u2').length);
  c.check('缺席区间: u3 候选被批准撤销(部分序列)', ['revoked'], [...new Set(r3.map((r) => r.status))]);
  c.check('缺席区间: u3 撤销者为批准 e6', 'e6', r3[0]?.revokedBy?.eventId);
  c.check('缺席区间: u3 后续 send 不再产生新结果', 1, r3.length);
  c.check('缺席区间: u4 暂定匹配被窗口内批准 e10 撤销', 'revoked', r4[0]?.status);
  c.check('缺席区间: u4 撤销者事件为 e10', 'e10', r4[0]?.revokedBy?.eventId);
  c.check('缺席区间: 窗口外批准 e11 不影响 u1', RESULT_STATUS.TENTATIVE, m1?.status);
  c.check('水位线: 闭合前不存在 confirmed 结果', 0, before.filter((r) => r.status === RESULT_STATUS.CONFIRMED).length);
  c.check('水位线: 闭合前 u1 仅为暂定', RESULT_STATUS.TENTATIVE, m1?.status);

  engine.advanceWatermark(T0 + 300_001);
  const after = engine.getResults();
  const m1After = after.find((r) => r.subjectKey === 'user=u1');
  const r4After = after.find((r) => r.subjectKey === 'user=u4');
  c.check('水位线: 闭合缺席窗口后 u1 定案 confirmed', RESULT_STATUS.CONFIRMED, m1After?.status);
  c.check('水位线: 定案记录含闭合原因', true, m1After?.history.some((h) => h.reason === 'watermark-closed-absence-window'));
  c.check('水位线: 已撤销的 u4 不因水位线翻案', RESULT_STATUS.REVOKED, r4After?.status);
  c.check('候选审计: u2 候选超期过期被记录', true, engine.getCandidateAudit().some((a) => a.subjectKey === 'user=u2' && a.disposition === 'expired'));

  return { checks: c.checks, samples };
}

// ---------------------------------------------------------------- C02
function caseC02() {
  const c = new Checker();
  const samples = [];
  const engine = new EventMatcher({ allowedLatenessMs: 60_000 });
  engine.publishRule({ ruleId: 'exfil-after-read', version: 1, definition: RULE_V1, publishedAtMs: T0 });
  const feed = (e) => {
    samples.push(sampleOf(e));
    return engine.ingest(e);
  };

  feed(ev('a1', 'sensitive_read', 'u1', 0));
  feed(ev('a2', 'external_send', 'u1', 240_000)); // 窗口 (0, 240s]
  feed(ev('b1', 'sensitive_read', 'u2', 0));
  feed(ev('b2', 'external_send', 'u2', 240_000));

  engine.advanceWatermark(T0 + 200_000); // 水位线尚未闭合两个窗口
  const m1 = engine.getResults().find((r) => r.subjectKey === 'user=u1');
  c.check('暂定: 水位线 200s 未闭合 240s 窗口, u1 仍暂定', RESULT_STATUS.TENTATIVE, m1?.status);

  const lateOk = feed(ev('a3', 'approval', 'u1', 180_000, 250_000)); // 迟到 20s ≤ 允许 60s
  c.check('允许迟到: 批准 a3 被接受且标记迟到', { accepted: true, late: true }, { accepted: lateOk.accepted, late: lateOk.late });
  const m1After = engine.getResults().find((r) => r.subjectKey === 'user=u1');
  c.check('允许迟到: a3 撤销 u1 暂定匹配', RESULT_STATUS.REVOKED, m1After?.status);
  c.check('允许迟到: 撤销记录标记迟到来源', true, m1After?.revokedBy?.late === true && m1After?.revokedBy?.eventId === 'a3');
  c.check('允许迟到: 未进入迟到通道', 0, engine.getLateChannel().length);

  const lateNo = feed(ev('b3', 'approval', 'u2', 100_000, 250_000)); // 迟到 100s > 允许 60s
  c.check('迟到通道: 超允许迟到的 b3 不被接受', false, lateNo.accepted);
  c.check('迟到通道: b3 进入迟到通道', 1, engine.getLateChannel().length);
  const lateEntry = engine.getLateChannel()[0];
  c.check('迟到通道: 记录事件 ID 与迟到时长', { eventId: 'b3', latenessMs: 100_000 }, { eventId: lateEntry?.event?.id, latenessMs: lateEntry?.latenessMs });
  const m2 = engine.getResults().find((r) => r.subjectKey === 'user=u2');
  c.check('迟到通道: b3 不影响 u2 暂定匹配', RESULT_STATUS.TENTATIVE, m2?.status);

  engine.advanceWatermark(T0 + 240_001);
  const m2After = engine.getResults().find((r) => r.subjectKey === 'user=u2');
  c.check('迟到通道: 窗口闭合后 u2 定案(迟到批准不生效)', RESULT_STATUS.CONFIRMED, m2After?.status);
  const m1Final = engine.getResults().find((r) => r.subjectKey === 'user=u1');
  c.check('允许迟到: u1 撤销状态不因水位线翻案', RESULT_STATUS.REVOKED, m1Final?.status);

  return { checks: c.checks, samples };
}

// ---------------------------------------------------------------- C03
function caseC03() {
  const c = new Checker();
  const samples = [];
  const engine = new EventMatcher({ allowedLatenessMs: 60_000 });
  engine.publishRule({ ruleId: 'exfil-after-read', version: 1, definition: RULE_V1, publishedAtMs: T0 });
  const feed = (e) => {
    samples.push(sampleOf(e));
    return engine.ingest(e);
  };

  feed(ev('r1', 'sensitive_read', 'u1', 0)); // 候选 A (v1)
  const dup = feed(ev('r1', 'sensitive_read', 'u1', 0)); // 完全重复
  c.check('重复 ID: 相同内容重投被拒绝且标记重复', { accepted: false, duplicate: true }, { accepted: dup.accepted, duplicate: dup.duplicate === true });
  c.check('重复 ID: 不产生新候选', 1, engine.getActiveCandidates().length);

  const conflict = feed({ id: 'r1', type: 'sensitive_read', user: 'u1', eventTimeMs: T0 + 5_000, arrivalTimeMs: T0 + 5_500 });
  c.check('冲突 ID: 同 ID 不同内容被隔离', { accepted: false, conflict: true }, { accepted: conflict.accepted, conflict: conflict.conflict === true });
  c.check('冲突 ID: 隔离记录可审计', 1, engine.getConflicts().length);
  c.check('冲突 ID: 冲突内容不产生新候选', 1, engine.getActiveCandidates().length);

  engine.publishRule({ ruleId: 'exfil-after-read', version: 2, definition: RULE_V2, publishedAtMs: T0 + 1_000 });
  feed(ev('r2', 'sensitive_read', 'u1', 10_000)); // 候选 B —— 应按当前版本 v2 创建
  const candB = engine.getActiveCandidates().find((k) => k.participations.some((p) => p.eventId === 'r2'));
  c.check('规则版本: 更新后新候选按当前版本 v2 创建', 2, candB?.ruleVersion);

  feed(ev('s1', 'external_send', 'u1', 150_000)); // 距 A 首步 150s: v1(300s) 可续接, v2(100s) 不可
  const resA = engine.getResults().find((r) => r.participations.some((p) => p.eventId === 'r1'));
  c.check('旧规则续接: 候选 A 按 v1 在 150s 处完成', 1, resA?.ruleVersion);
  c.check('旧规则续接: A 的窗口右端为 150s(超出 v2 的 100s 限制)', T0 + 150_000, resA?.window?.endMs);
  c.check('旧规则续接: 候选 B(v2) 不被 150s 的 send 完成', 0, engine.getResults().filter((r) => r.participations.some((p) => p.eventId === 'r2')).length);

  feed(ev('s2', 'external_send', 'u1', 105_000)); // 距 B 首步 95s ≤ v2 的 100s
  const resB = engine.getResults().find((r) => r.participations.some((p) => p.eventId === 'r2'));
  c.check('新规则候选: B 按 v2 在 95s 处完成', 2, resB?.ruleVersion);
  c.check('新规则候选: B 的参与记录为 [r2,s2]', ['r2', 's2'], resB?.participations.map((p) => p.eventId));

  // 多候选共享事件
  feed(ev('r3', 'sensitive_read', 'u2', 0));
  feed(ev('r4', 'sensitive_read', 'u2', 5_000));
  feed(ev('s3', 'external_send', 'u2', 20_000)); // 同时完成两个未闭合候选
  const shared = engine.getResults().filter((r) => r.subjectKey === 'user=u2');
  c.check('共享事件: 一个 send 完成两个未闭合候选', 2, shared.length);
  c.check('共享事件: 两个结果都包含 s3 参与记录', [true, true], shared.map((r) => r.participations.some((p) => p.eventId === 's3')));
  c.check('共享事件: 两个结果均为暂定(窗口未闭合)', [RESULT_STATUS.TENTATIVE, RESULT_STATUS.TENTATIVE], shared.map((r) => r.status).sort());

  const versions = new Set(engine.getResults().map((r) => r.ruleVersion));
  c.check('规则版本: 结果只来自 v1(旧候选续接)与 v2(新候选)', [1, 2], [...versions].sort((a, b) => a - b));

  return { checks: c.checks, samples };
}

// ---------------------------------------------------------------- C04
function caseC04() {
  const c = new Checker();
  const samples = [];

  // reject 策略：预算不足 => 未能判断(undetermined)，保留参与记录
  const e1 = new EventMatcher({ maxActiveCandidates: 2, onBudgetExceeded: 'reject' });
  e1.publishRule({ ruleId: 'exfil-after-read', version: 1, definition: RULE_V1, publishedAtMs: T0 });
  const feed1 = (e) => {
    samples.push(sampleOf(e));
    return e1.ingest(e);
  };
  feed1(ev('r1', 'sensitive_read', 'u1', 0));
  feed1(ev('r2', 'sensitive_read', 'u2', 0));
  feed1(ev('r3', 'sensitive_read', 'u3', 0)); // 预算已满，无法建候选
  const und = e1.getResults().filter((r) => r.status === RESULT_STATUS.UNDETERMINED);
  c.check('预算不足: 产生未能判断(undetermined)结果', 1, und.length);
  c.check('预算不足: 未能判断结果携带参与记录 r3', ['r3'], und[0]?.participations.map((p) => p.eventId));
  c.check('预算不足: 未能判断结果归属 user=u3', 'user=u3', und[0]?.subjectKey);
  c.check('预算不足: 活跃候选仍占满预算', 2, e1.getActiveCandidates().length);
  const sum1 = e1.getRiskSummary();
  c.check('预算不足: 风险汇总计为 riskUnknown=1', 1, sum1.riskUnknown);
  c.check('预算不足: 结论不是无风险', 'risk-unknown', sum1.conclusion);

  feed1(ev('s1', 'external_send', 'u1', 100_000)); // 释放一个候选位
  feed1(ev('r4', 'sensitive_read', 'u3', 1_000)); // 现在可以为 u3 建候选
  c.check('预算恢复: 释放后可为 u3 建新候选', 2, e1.getActiveCandidates().length);
  c.check('预算恢复: u1 完成序列进入暂定', 1, e1.getResults().filter((r) => r.subjectKey === 'user=u1' && r.status === RESULT_STATUS.TENTATIVE).length);

  // evict-oldest 策略：丢弃最旧候选 => dropped 记录，不等同无风险
  const e2 = new EventMatcher({ maxActiveCandidates: 1, onBudgetExceeded: 'evict-oldest' });
  e2.publishRule({ ruleId: 'exfil-after-read', version: 1, definition: RULE_V1, publishedAtMs: T0 });
  const feed2 = (e) => {
    samples.push(sampleOf(e));
    return e2.ingest(e);
  };
  feed2(ev('a1', 'sensitive_read', 'u1', 0));
  feed2(ev('a2', 'sensitive_read', 'u2', 10_000)); // 驱逐 u1 候选
  const dropped = e2.getResults().filter((r) => r.status === RESULT_STATUS.DROPPED);
  c.check('丢弃候选: 驱逐产生 dropped 记录', 1, dropped.length);
  c.check('丢弃候选: dropped 记录保留参与记录 a1', ['a1'], dropped[0]?.participations.map((p) => p.eventId));
  const sum2 = e2.getRiskSummary();
  c.check('丢弃候选: 计入 riskUnknown 而非无风险', 1, sum2.riskUnknown);
  c.check('丢弃候选: 结论为 risk-unknown', 'risk-unknown', sum2.conclusion);
  c.check('丢弃候选: 汇总明确提示不得当作无风险', true, /must not be treated as no-risk/.test(sum2.note));

  return { checks: c.checks, samples };
}

// ---------------------------------------------------------------- 入口
const CASES = {
  C01: { description: '主体键/正向序列/时间间隔/缺席区间; 水位线闭合前仅暂定', run: caseC01 },
  C02: { description: '允许迟到的批准撤销暂定匹配; 超允许迟到记录进入迟到通道', run: caseC02 },
  C03: { description: '重复/冲突 ID、多候选共享事件、旧候选按旧规则续接与新版建候选', run: caseC03 },
  C04: { description: '状态预算不足: 未能判断与参与记录保留, 丢弃候选不等同无风险', run: caseC04 },
};

function printJson(obj) {
  process.stdout.write(`${JSON.stringify(obj, null, 2)}\n`);
}

function runOne(id) {
  const def = CASES[id];
  try {
    const { checks, samples } = def.run();
    const passed = checks.filter((k) => k.pass).length;
    const failed = checks.length - passed;
    return {
      case: id,
      description: def.description,
      seed: SEED,
      assertions: checks.length,
      passed,
      failed,
      skipped: 0,
      samples,
      checks,
      ok: checks.length > 0 && failed === 0,
    };
  } catch (err) {
    return {
      case: id,
      description: def.description,
      seed: SEED,
      assertions: 0,
      passed: 0,
      failed: 0,
      skipped: 0,
      samples: [],
      checks: [],
      error: String((err && err.message) || err),
      ok: false,
    };
  }
}

const args = process.argv.slice(2);
if (args.includes('--list')) {
  printJson({ cases: Object.entries(CASES).map(([id, d]) => ({ case: id, description: d.description })) });
  process.exit(0);
}
const ci = args.indexOf('--case');
if (ci === -1 || !args[ci + 1]) {
  printJson({ error: 'usage: node tools/local_verify.mjs --list | --case C01|C02|C03|C04|ALL' });
  process.exit(2);
}
const target = String(args[ci + 1]).toUpperCase();
const ids = target === 'ALL' ? Object.keys(CASES) : [target];
if (ids.some((id) => !CASES[id])) {
  printJson({ error: `unknown case: ${target}`, known: Object.keys(CASES) });
  process.exit(2);
}
const reports = ids.map(runOne);
for (const r of reports) printJson(r);
const bad = reports.some((r) => !r.ok || r.assertions === 0 || r.failed > 0 || r.skipped >= r.assertions);
process.exit(bad ? 1 : 0);
