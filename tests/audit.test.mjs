import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import {
  audit, trustedState, buildScheduleIndex, expectedTrustedSet,
  CATEGORY, ATTRIBUTION,
} from '../src/audit.mjs';

const load = (name) => JSON.parse(
  readFileSync(new URL(`../fixtures/${name}`, import.meta.url), 'utf8'),
);
const schedule = load('schedule.json');
const cache = load('cache.json');
const decisions = load('decisions.json');
const report = audit({ schedule, cache, decisions, at: 6000 });

const findingsByCase = new Map(report.findings.map((f) => [f.case_id, f]));
const evalByCase = new Map(report.evaluations.map((e) => [e.case_id, e]));
const anomaliesOf = (type) => report.anomalies.filter((a) => a.type === type);

test('新旧密钥在宽限期内合法共存：两条决策均判为正常，不产生发现', () => {
  for (const cid of ['case_grace_coexist_old', 'case_grace_coexist_new']) {
    const ev = evalByCase.get(cid);
    assert.equal(ev.ok, true, `${cid} 应判为正常`);
    assert.equal(ev.expected, 'accept');
    assert.equal(findingsByCase.has(cid), false, `${cid} 不应产生发现`);
  }
  assert.equal(evalByCase.get('case_grace_coexist_old').window, 'grace');
  assert.equal(evalByCase.get('case_grace_coexist_new').window, 'active');
});

test('宽限期结束精确边界：grace_end-1 仍可信，grace_end 起不可信（左闭右开）', () => {
  // a-old: retired 5000 + grace 2000 => grace_end 7000
  assert.equal(evalByCase.get('case_grace_boundary_last').ok, true); // 6999 accept 正常
  assert.equal(evalByCase.get('case_grace_boundary_last').window, 'grace');
  assert.equal(evalByCase.get('case_grace_boundary_end').ok, true); // 7000 reject 正常
  assert.equal(evalByCase.get('case_grace_boundary_end').window, 'retired');
  // 零宽限密钥 b-only：retired 4000 + grace 0 => 4000 起不可信
  assert.equal(evalByCase.get('case_zero_grace_boundary_last').ok, true); // 3999 accept
  assert.equal(evalByCase.get('case_zero_grace_boundary_end').ok, true); // 4000 reject
  // 单元级：恰好在 grace_end 接受 => 过度信任
  const { issuers } = buildScheduleIndex(schedule);
  assert.equal(trustedState(issuers.get('issuer-a'), 'a-old', 6999).state, 'grace');
  assert.equal(trustedState(issuers.get('issuer-a'), 'a-old', 7000).state, 'retired');
  assert.equal(trustedState(issuers.get('issuer-b'), 'b-only', 4000).state, 'retired');
  const boundary = audit({
    schedule, cache,
    decisions: { decisions: [
      { case_id: 'bd', instance_id: 'verifier-1', cache_version: 2, decided_at: 7000, issuer_id: 'issuer-a', kid: 'a-old', result: 'accept' },
    ] },
  });
  const f = boundary.findings.find((x) => x.case_id === 'bd');
  assert.equal(f.category, CATEGORY.OVERTRUST_RETIRED);
  assert.equal(f.key_window.grace_end, 7000);
});

test('宽限期内的误拒被识别并归因到缓存刷新', () => {
  const f = findingsByCase.get('case_false_reject_grace');
  assert.equal(f.category, CATEGORY.FALSE_REJECT);
  assert.equal(f.window, 'grace');
  assert.equal(f.attribution, ATTRIBUTION.CACHE_REFRESH);
  // 可定位：实例、缓存版本、变更时间
  assert.equal(f.instance_id, 'verifier-1');
  assert.equal(f.cache_version, 3);
  assert.equal(f.decided_at, 6950);
  assert.equal(f.key_window.retired_at, 5000);
  assert.equal(f.key_window.grace_end, 7000);
  // 缓存未刷出新密钥导致的误拒（ stale v1 缺少 a-new ）
  const stale = findingsByCase.get('case_false_reject_stale_cache');
  assert.equal(stale.category, CATEGORY.FALSE_REJECT);
  assert.equal(stale.window, 'active');
  assert.equal(stale.attribution, ATTRIBUTION.CACHE_REFRESH);
  assert.equal(stale.evidence.snapshot_has_key, false);
});

test('旧密钥退役且宽限期结束后仍被接受 => 过度信任，归因缓存刷新', () => {
  const f = findingsByCase.get('case_overtrust_after_retire');
  assert.equal(f.category, CATEGORY.OVERTRUST_RETIRED);
  assert.equal(f.attribution, ATTRIBUTION.CACHE_REFRESH);
  assert.equal(f.instance_id, 'verifier-1');
  assert.equal(f.cache_version, 2);
  assert.equal(f.decided_at, 7500);
  assert.equal(f.key_window.grace_end, 7000);
  assert.equal(f.evidence.snapshot_has_key, true); // 旧密钥仍滞留在缓存中
});

test('缓存更新乱序：旧版本后应用被检测为异常，相关误拒归因缓存刷新', () => {
  const ooo = anomaliesOf('out_of_order_refresh');
  assert.equal(ooo.length, 1);
  assert.equal(ooo[0].instance_id, 'verifier-2');
  assert.equal(ooo[0].cache_version, 1);
  assert.equal(ooo[0].at, 5200);
  assert.equal(ooo[0].attribution, ATTRIBUTION.CACHE_REFRESH);
  const f = findingsByCase.get('case_out_of_order_stale');
  assert.equal(f.category, CATEGORY.FALSE_REJECT);
  assert.equal(f.attribution, ATTRIBUTION.CACHE_REFRESH);
  assert.equal(f.cache_version, 1);
});

test('缺失快照 => 证据不足，不做强行归因', () => {
  const f = findingsByCase.get('case_missing_snapshot');
  assert.equal(f.category, CATEGORY.INSUFFICIENT_EVIDENCE);
  assert.equal(f.attribution, null);
  assert.equal(f.evidence.reason, 'missing_snapshot');
  assert.equal(f.expected, 'accept'); // 仍给出按时间表推导的期望供参考
  const ms = anomaliesOf('missing_snapshot');
  assert.equal(ms.length, 1);
  assert.equal(ms[0].instance_id, 'verifier-3');
  assert.equal(ms[0].cache_version, 2);
});

test('跨颁发方标识复用：同一 kid 按颁发方独立判定，绑定错误归因 issuer_binding', () => {
  // issuer-a 的 shared-kid 合法接受
  assert.equal(evalByCase.get('case_xiss_ok_a').ok, true);
  // issuer-b 也发布了 shared-kid，但缓存只绑定了 issuer-a => 误拒，归因颁发方绑定
  const fr = findingsByCase.get('case_xiss_false_reject_b');
  assert.equal(fr.category, CATEGORY.FALSE_REJECT);
  assert.equal(fr.attribution, ATTRIBUTION.ISSUER_BINDING);
  assert.equal(fr.evidence.snapshot_has_kid_under_other_issuer, true);
  // issuer-b 从未发布 a-old，接受 => 过度信任未知密钥，归因颁发方绑定
  const ot = findingsByCase.get('case_xiss_overtrust_b');
  assert.equal(ot.category, CATEGORY.OVERTRUST_UNKNOWN);
  assert.equal(ot.attribution, ATTRIBUTION.ISSUER_BINDING);
  assert.deepEqual(ot.evidence.other_issuers_with_kid, ['issuer-a']);
});

test('缓存恢复：刷新纠正后，同一实例后续决策不再产生发现', () => {
  assert.equal(evalByCase.get('case_cache_recovery_reject').ok, true);
  assert.equal(evalByCase.get('case_cache_recovery_accept').ok, true);
  // 宽限期结束（7000）后，verifier-1 使用最新缓存 v3 的决策全部正常
  const late = report.findings.filter(
    (f) => f.instance_id === 'verifier-1' && f.cache_version === 3 && f.decided_at >= 7000,
  );
  assert.equal(late.length, 0, 'verifier-1 恢复后（v3，7000 起）不应再有发现');
});

test('发布配置问题：异常时间表被检测，快照正确仍误拒归因 publish_config', () => {
  const sa = anomaliesOf('schedule_anomaly');
  assert.equal(sa.length, 1);
  assert.equal(sa[0].subtype, 'retired_before_publish');
  assert.equal(sa[0].issuer_id, 'issuer-c');
  assert.equal(sa[0].kid, 'c-bad');
  assert.equal(sa[0].attribution, ATTRIBUTION.PUBLISH_CONFIG);
  const f = findingsByCase.get('case_false_reject_publish_config');
  assert.equal(f.category, CATEGORY.FALSE_REJECT);
  assert.equal(f.attribution, ATTRIBUTION.PUBLISH_CONFIG);
  assert.equal(f.evidence.snapshot_has_key, true); // 缓存证据显示密钥在场
});

test('汇总计数与分类分布正确', () => {
  const s = report.summary;
  assert.equal(s.decisions_total, 18);
  assert.equal(s.findings_total, 9);
  assert.deepEqual(s.by_category, {
    false_reject: 5,
    overtrust_retired: 2,
    overtrust_unknown: 1,
    insufficient_evidence: 1,
  });
  assert.deepEqual(s.by_attribution, {
    cache_refresh: 5,
    issuer_binding: 2,
    publish_config: 1,
  });
  assert.equal(s.anomalies_total, 9);
  assert.deepEqual(s.anomalies_by_type, {
    schedule_anomaly: 1,
    out_of_order_refresh: 1,
    missing_snapshot: 1,
    snapshot_missing_key: 4,
    snapshot_unexpected_key: 1,
    version_skew: 1,
  });
});

test('分别推导每个实例/颁发方在指定时刻应信任的密钥集合', () => {
  const ts = report.trusted_sets;
  assert.equal(ts.at, 6000);
  const byIssuer = new Map(ts.issuers.map((i) => [i.issuer_id, i.trusted_kids]));
  assert.deepEqual(byIssuer.get('issuer-a'), ['a-new', 'a-old', 'shared-kid']); // 宽限期共存
  assert.deepEqual(byIssuer.get('issuer-b'), ['shared-kid']); // b-only 已退役(零宽限)
  assert.deepEqual(byIssuer.get('issuer-c'), []);
  assert.deepEqual(byIssuer.get('issuer-d'), ['d-1']);
  const v1 = ts.instances.find((i) => i.instance_id === 'verifier-1');
  assert.equal(v1.active_cache_version, 2); // 6000 时生效 v2（5100 应用）
  const v3 = ts.instances.find((i) => i.instance_id === 'verifier-3');
  assert.equal(v3.active_cache_version, 2);
  assert.equal(v3.snapshot_missing, true); // v2 快照缺失
});

test('缓存版本错位异常：决策声称版本与刷新时间线推导不一致', () => {
  const skew = anomaliesOf('version_skew');
  assert.equal(skew.length, 1);
  assert.equal(skew[0].instance_id, 'verifier-1');
  assert.equal(skew[0].cache_version, 2);
  assert.equal(skew[0].expected_cache_version, 3);
  assert.equal(skew[0].at, 7500);
});

test('所有发现均可定位到实例、缓存版本和变更时间', () => {
  for (const f of report.findings) {
    assert.equal(typeof f.instance_id, 'string');
    assert.equal(typeof f.cache_version, 'number');
    assert.equal(typeof f.decided_at, 'number');
    assert.ok(f.finding_id.startsWith('F-'));
  }
});

test('expectedTrustedSet 单元：宽限期共存与边界', () => {
  const { issuers } = buildScheduleIndex(schedule);
  const at6000 = expectedTrustedSet(issuers, 6000, new Set(['issuer-a']));
  assert.deepEqual(at6000.map((k) => k.kid), ['a-new', 'a-old', 'shared-kid']);
  const at7000 = expectedTrustedSet(issuers, 7000, new Set(['issuer-a']));
  assert.deepEqual(at7000.map((k) => k.kid), ['a-new', 'shared-kid']); // 边界起旧密钥退出
});

test('CLI 端到端：app.mjs 读取合成输入并产出可定位报告', () => {
  const root = fileURLToPath(new URL('..', import.meta.url));
  const outDir = mkdtempSync(path.join(tmpdir(), 'sec-vuln-031-'));
  const outPath = path.join(outDir, 'report.json');
  const r = spawnSync(process.execPath, [
    path.join(root, 'app.mjs'),
    '--schedule', path.join(root, 'fixtures', 'schedule.json'),
    '--cache', path.join(root, 'fixtures', 'cache.json'),
    '--decisions', path.join(root, 'fixtures', 'decisions.json'),
    '--at', '6000',
    '--out', outPath,
  ], { encoding: 'utf8', timeout: 30000 });
  assert.equal(r.status, 0, `CLI 应退出 0，stderr: ${r.stderr}`);
  const cliReport = JSON.parse(readFileSync(outPath, 'utf8'));
  assert.equal(cliReport.summary.findings_total, 9);
  assert.equal(cliReport.summary.decisions_total, 18);
  assert.equal(cliReport.trusted_sets.at, 6000);
  const cliCases = new Set(cliReport.findings.map((f) => f.case_id));
  for (const cid of ['case_false_reject_grace', 'case_overtrust_after_retire', 'case_missing_snapshot', 'case_xiss_overtrust_b']) {
    assert.ok(cliCases.has(cid), `CLI 报告应包含 ${cid}`);
  }
});

test('CLI 参数错误退出码为 2', () => {
  const root = fileURLToPath(new URL('..', import.meta.url));
  const r = spawnSync(process.execPath, [path.join(root, 'app.mjs'), '--schedule', 'x.json'], { encoding: 'utf8', timeout: 30000 });
  assert.equal(r.status, 2);
});
