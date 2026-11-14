// SEC-VULN-031 — JWT 验签密钥轮换离线审计核心逻辑。
// 输入：颁发方轮换时间表、验证实例缓存版本与刷新记录、验签决策摘要（全部为合成数据）。
// 输出：每个实例在决策时刻应信任的密钥集合推导、误拒/过度信任/证据不足的发现与归因。

const INF = Number.POSITIVE_INFINITY;

export const CATEGORY = Object.freeze({
  FALSE_REJECT: 'false_reject',               // 应接受却被拒绝（含宽限期内误拒）
  OVERTRUST_RETIRED: 'overtrust_retired',     // 退役且宽限期结束后仍接受
  OVERTRUST_UNKNOWN: 'overtrust_unknown',     // 接受该颁发方从未发布（或尚未发布）的密钥
  INSUFFICIENT_EVIDENCE: 'insufficient_evidence', // 快照/实例/颁发方证据缺失，无法定论
});

export const ATTRIBUTION = Object.freeze({
  PUBLISH_CONFIG: 'publish_config',   // 发布配置
  CACHE_REFRESH: 'cache_refresh',     // 缓存刷新
  ISSUER_BINDING: 'issuer_binding',   // 颁发方绑定
});

// 密钥信任窗口：[published_at, retired_at + grace_ms)。未退役则 graceEnd 为无穷。
// 边界语义：t == grace_end 时密钥已不再可信（左闭右开）。
export function keyWindow(key) {
  const publishedAt = key.published_at;
  const retiredAt = key.retired_at ?? null;
  const graceMs = key.grace_ms ?? 0;
  const graceEnd = retiredAt === null ? INF : retiredAt + graceMs;
  return { publishedAt, retiredAt, graceMs, graceEnd };
}

function windowJson(w) {
  return {
    published_at: w.publishedAt,
    retired_at: w.retiredAt,
    grace_ms: w.graceMs,
    grace_end: w.graceEnd === INF ? null : w.graceEnd,
  };
}

// 某一时刻密钥对某颁发方的状态：active | grace | retired | not_published | unknown_key | unknown_issuer
export function trustedState(issuerEntry, kid, t) {
  if (!issuerEntry) return { state: 'unknown_issuer', key: null, window: null };
  const recs = issuerEntry.keys.get(kid);
  if (!recs || recs.length === 0) return { state: 'unknown_key', key: null, window: null };
  const rank = { active: 4, grace: 3, not_published: 2, retired: 1 };
  let best = null;
  for (const key of recs) {
    const w = keyWindow(key);
    let state;
    if (t < w.publishedAt) state = 'not_published';
    else if (w.retiredAt === null || t < w.retiredAt) state = 'active';
    else if (t < w.graceEnd) state = 'grace';
    else state = 'retired';
    if (!best || rank[state] > rank[best.state]) best = { state, key, window: w };
  }
  return best;
}

// 建立颁发方索引，并检测发布配置异常（退役早于发布、负宽限、同 kid 窗口重叠）。
export function buildScheduleIndex(schedule) {
  const issuers = new Map();
  const anomalies = [];
  const kidToIssuers = new Map();
  for (const iss of schedule.issuers ?? []) {
    const entry = { issuer_id: iss.issuer_id, keys: new Map() };
    for (const k of iss.keys ?? []) {
      const w = keyWindow(k);
      if (w.retiredAt !== null && w.retiredAt < w.publishedAt) {
        anomalies.push({
          type: 'schedule_anomaly', subtype: 'retired_before_publish',
          attribution: ATTRIBUTION.PUBLISH_CONFIG,
          issuer_id: iss.issuer_id, kid: k.kid, key_window: windowJson(w),
          message: `发布配置异常：颁发方 ${iss.issuer_id} 的密钥 ${k.kid} 退役时间 ${w.retiredAt} 早于发布时间 ${w.publishedAt}`,
        });
      }
      if (w.graceMs < 0) {
        anomalies.push({
          type: 'schedule_anomaly', subtype: 'negative_grace',
          attribution: ATTRIBUTION.PUBLISH_CONFIG,
          issuer_id: iss.issuer_id, kid: k.kid, key_window: windowJson(w),
          message: `发布配置异常：颁发方 ${iss.issuer_id} 的密钥 ${k.kid} 宽限期为负（${w.graceMs}ms）`,
        });
      }
      const existing = entry.keys.get(k.kid) ?? [];
      for (const prev of existing) {
        const pw = keyWindow(prev);
        if (w.publishedAt < pw.graceEnd && pw.publishedAt < w.graceEnd) {
          anomalies.push({
            type: 'schedule_anomaly', subtype: 'overlapping_windows',
            attribution: ATTRIBUTION.PUBLISH_CONFIG,
            issuer_id: iss.issuer_id, kid: k.kid, key_window: windowJson(w),
            message: `发布配置异常：颁发方 ${iss.issuer_id} 的密钥 ${k.kid} 存在多条信任窗口重叠的记录`,
          });
        }
      }
      existing.push(k);
      entry.keys.set(k.kid, existing);
      if (!kidToIssuers.has(k.kid)) kidToIssuers.set(k.kid, new Set());
      kidToIssuers.get(k.kid).add(iss.issuer_id);
    }
    issuers.set(iss.issuer_id, entry);
  }
  return { issuers, anomalies, kidToIssuers };
}

// 推导某时刻应信任的密钥集合（可按颁发方子集过滤）。
export function expectedTrustedSet(issuers, t, issuerIds = null) {
  const out = [];
  for (const [issuerId, entry] of issuers) {
    if (issuerIds && !issuerIds.has(issuerId)) continue;
    for (const kid of entry.keys.keys()) {
      const st = trustedState(entry, kid, t);
      if (st.state === 'active' || st.state === 'grace') out.push({ issuer_id: issuerId, kid });
    }
  }
  out.sort((a, b) => a.issuer_id.localeCompare(b.issuer_id) || a.kid.localeCompare(b.kid));
  return out;
}

// 缓存刷新事件按应用时间排序后，t 时刻生效的缓存版本（应用顺序优先于版本号）。
export function activeVersionAt(events, t) {
  let v = null;
  for (const ev of events) {
    if (ev.applied_at <= t) v = ev.cache_version;
    else break;
  }
  return v;
}

// 构建实例视图：快照索引、刷新时间线、乱序/缺失快照/快照内容异常。
export function buildInstanceView(instance, issuers) {
  const snapshots = new Map();
  for (const s of instance.snapshots ?? []) {
    const keys = s.keys ?? [];
    snapshots.set(s.cache_version, {
      cache_version: s.cache_version,
      keys,
      set: new Set(keys.map((k) => `${k.issuer_id}${k.kid}`)),
    });
  }
  const events = [...(instance.refresh_events ?? [])].sort((a, b) => a.applied_at - b.applied_at);
  const anomalies = [];
  let maxVersion = -INF;
  for (const ev of events) {
    if (ev.cache_version < maxVersion) {
      anomalies.push({
        type: 'out_of_order_refresh',
        attribution: ATTRIBUTION.CACHE_REFRESH,
        instance_id: instance.instance_id, cache_version: ev.cache_version, at: ev.applied_at,
        message: `缓存刷新乱序：实例 ${instance.instance_id} 在 ${ev.applied_at} 应用了较旧的缓存版本 v${ev.cache_version}（此前已应用 v${maxVersion}），生效缓存被回退`,
      });
    }
    if (ev.cache_version > maxVersion) maxVersion = ev.cache_version;
    if (!snapshots.has(ev.cache_version)) {
      anomalies.push({
        type: 'missing_snapshot',
        attribution: ATTRIBUTION.CACHE_REFRESH,
        instance_id: instance.instance_id, cache_version: ev.cache_version, at: ev.applied_at,
        message: `证据缺失：实例 ${instance.instance_id} 的刷新记录引用了没有快照的缓存版本 v${ev.cache_version}`,
      });
    }
  }
  // 快照内容审计：以实例出现过的颁发方为范围，对比刷新时刻的应信集合。
  const scoped = new Set();
  for (const s of snapshots.values()) for (const k of s.keys) scoped.add(k.issuer_id);
  for (const ev of events) {
    const snap = snapshots.get(ev.cache_version);
    if (!snap) continue;
    const expected = expectedTrustedSet(issuers, ev.applied_at, scoped);
    const expectedSet = new Set(expected.map((k) => `${k.issuer_id}${k.kid}`));
    for (const exp of expected) {
      if (!snap.set.has(`${exp.issuer_id}${exp.kid}`)) {
        anomalies.push({
          type: 'snapshot_missing_key',
          attribution: ATTRIBUTION.CACHE_REFRESH,
          instance_id: instance.instance_id, cache_version: ev.cache_version, at: ev.applied_at,
          issuer_id: exp.issuer_id, kid: exp.kid,
          message: `缓存刷新不完整：实例 ${instance.instance_id} 的 v${ev.cache_version}（${ev.applied_at} 应用）缺少当时应信任的密钥 ${exp.issuer_id}/${exp.kid}`,
        });
      }
    }
    for (const k of snap.keys) {
      if (!expectedSet.has(`${k.issuer_id}${k.kid}`)) {
        const st = trustedState(issuers.get(k.issuer_id), k.kid, ev.applied_at);
        anomalies.push({
          type: 'snapshot_unexpected_key',
          attribution: ATTRIBUTION.CACHE_REFRESH,
          instance_id: instance.instance_id, cache_version: ev.cache_version, at: ev.applied_at,
          issuer_id: k.issuer_id, kid: k.kid, key_state: st.state,
          message: `缓存内容异常：实例 ${instance.instance_id} 的 v${ev.cache_version}（${ev.applied_at} 应用）持有当时不可信的密钥 ${k.issuer_id}/${k.kid}（状态 ${st.state}）`,
        });
      }
    }
  }
  return { instance_id: instance.instance_id, snapshots, events, anomalies };
}

function attributeFalseReject({ snapHasExact, snapHasKidOther }) {
  if (!snapHasExact && snapHasKidOther) return ATTRIBUTION.ISSUER_BINDING;
  if (!snapHasExact) return ATTRIBUTION.CACHE_REFRESH;
  return ATTRIBUTION.PUBLISH_CONFIG;
}

function attributeOvertrustRetired({ snapHasExact, kidTrustedElsewhere }) {
  if (snapHasExact) return ATTRIBUTION.CACHE_REFRESH;
  if (kidTrustedElsewhere) return ATTRIBUTION.ISSUER_BINDING;
  return ATTRIBUTION.PUBLISH_CONFIG;
}

function attributeOvertrustUnknown({ snapHasExact }) {
  return snapHasExact ? ATTRIBUTION.CACHE_REFRESH : ATTRIBUTION.ISSUER_BINDING;
}

const WINDOW_LABEL = {
  active: '有效期内', grace: '宽限期内', retired: '宽限期结束后',
  not_published: '发布前', unknown_key: '未知密钥', unknown_issuer: '未知颁发方',
};

// 主审计入口。decisions/at 均为合成输入；返回确定性报告（不含墙钟时间）。
export function audit({ schedule, cache, decisions, at = null }) {
  const { issuers, anomalies: schedAnomalies, kidToIssuers } = buildScheduleIndex(schedule);
  const instances = new Map();
  const anomalies = [...schedAnomalies];
  for (const inst of cache.instances ?? []) {
    const view = buildInstanceView(inst, issuers);
    instances.set(view.instance_id, view);
    anomalies.push(...view.anomalies);
  }

  const evaluations = [];
  const findings = [];
  const skewSeen = new Set();

  const pushInsufficient = (d, expected, window, reason, extra = {}) => {
    const finding = {
      finding_id: null,
      case_id: d.case_id ?? null,
      category: CATEGORY.INSUFFICIENT_EVIDENCE,
      attribution: null,
      instance_id: d.instance_id, cache_version: d.cache_version, decided_at: d.decided_at,
      issuer_id: d.issuer_id, kid: d.kid,
      expected, actual: d.result, window,
      key_window: extra.key_window ?? null,
      active_cache_version: extra.active_cache_version ?? null,
      evidence: { reason },
      message: `证据不足（${reason}）：无法完整核验实例 ${d.instance_id} 在 ${d.decided_at} 对 ${d.issuer_id}/${d.kid} 的 ${d.result} 决策`,
    };
    findings.push(finding);
    evaluations.push({
      case_id: d.case_id ?? null,
      instance_id: d.instance_id, cache_version: d.cache_version, decided_at: d.decided_at,
      issuer_id: d.issuer_id, kid: d.kid,
      window, expected, actual: d.result, ok: false, finding_id: null,
    });
  };

  for (const d of decisions.decisions ?? []) {
    const issuerEntry = issuers.get(d.issuer_id);
    const inst = instances.get(d.instance_id);
    const st = issuerEntry
      ? trustedState(issuerEntry, d.kid, d.decided_at)
      : { state: 'unknown_issuer', key: null, window: null };
    const expected = st.state === 'active' || st.state === 'grace'
      ? 'accept'
      : st.state === 'unknown_issuer' ? null : 'reject';

    // 版本错位：决策声称的缓存版本与按刷新时间线推导的生效版本不一致。
    let activeVersion = null;
    if (inst && inst.events.length > 0) {
      activeVersion = activeVersionAt(inst.events, d.decided_at);
      if (activeVersion !== null && activeVersion !== d.cache_version) {
        const key = `${d.instance_id}|${d.decided_at}|${d.cache_version}|${activeVersion}`;
        if (!skewSeen.has(key)) {
          skewSeen.add(key);
          anomalies.push({
            type: 'version_skew',
            attribution: ATTRIBUTION.CACHE_REFRESH,
            instance_id: d.instance_id, cache_version: d.cache_version, at: d.decided_at,
            expected_cache_version: activeVersion,
            message: `缓存版本错位：实例 ${d.instance_id} 在 ${d.decided_at} 的决策声称使用 v${d.cache_version}，但按刷新记录当时生效的应为 v${activeVersion}`,
          });
        }
      }
    }

    if (d.result !== 'accept' && d.result !== 'reject') {
      pushInsufficient(d, expected, st.state, 'invalid_result');
      continue;
    }
    if (!issuerEntry) {
      pushInsufficient(d, expected, st.state, 'unknown_issuer');
      continue;
    }
    if (!inst) {
      pushInsufficient(d, expected, st.state, 'unknown_instance');
      continue;
    }
    const snap = inst.snapshots.get(d.cache_version);
    if (!snap) {
      pushInsufficient(d, expected, st.state, 'missing_snapshot', {
        key_window: st.window ? windowJson(st.window) : null,
        active_cache_version: activeVersion,
      });
      continue;
    }

    const base = {
      case_id: d.case_id ?? null,
      instance_id: d.instance_id, cache_version: d.cache_version, decided_at: d.decided_at,
      issuer_id: d.issuer_id, kid: d.kid,
      window: st.state, expected, actual: d.result,
    };

    if (expected === d.result) {
      evaluations.push({ ...base, ok: true, finding_id: null });
      continue;
    }

    // 期望与实际不一致：分类并归因。
    const snapHasExact = snap.set.has(`${d.issuer_id}${d.kid}`);
    const snapKidIssuers = snap.keys.filter((k) => k.kid === d.kid).map((k) => k.issuer_id);
    const snapHasKidOther = snapKidIssuers.some((x) => x !== d.issuer_id);
    const otherIssuers = [...(kidToIssuers.get(d.kid) ?? [])].filter((x) => x !== d.issuer_id);
    const kidTrustedElsewhere = otherIssuers.some((o) => {
      const st2 = trustedState(issuers.get(o), d.kid, d.decided_at);
      return st2.state === 'active' || st2.state === 'grace';
    });
    const evidence = {
      snapshot_has_key: snapHasExact,
      snapshot_has_kid_under_other_issuer: snapHasKidOther,
      snapshot_kid_issuers: snapKidIssuers,
      other_issuers_with_kid: otherIssuers,
      kid_trusted_elsewhere: kidTrustedElsewhere,
    };

    let category; let attribution; let message;
    if (expected === 'accept') {
      category = CATEGORY.FALSE_REJECT;
      attribution = attributeFalseReject({ snapHasExact, snapHasKidOther });
      message = `误拒：密钥 ${d.issuer_id}/${d.kid} 在 ${d.decided_at} 处于${WINDOW_LABEL[st.state]}，实例 ${d.instance_id}（缓存 v${d.cache_version}）却拒绝`;
    } else if (st.state === 'retired') {
      category = CATEGORY.OVERTRUST_RETIRED;
      attribution = attributeOvertrustRetired({ snapHasExact, kidTrustedElsewhere });
      message = `过度信任：密钥 ${d.issuer_id}/${d.kid} 已于 ${st.window.retiredAt} 退役、宽限期 ${st.window.graceEnd} 结束，实例 ${d.instance_id}（缓存 v${d.cache_version}）在 ${d.decided_at} 仍接受`;
    } else {
      category = CATEGORY.OVERTRUST_UNKNOWN;
      attribution = attributeOvertrustUnknown({ snapHasExact });
      message = `过度信任：密钥 ${d.issuer_id}/${d.kid} 在 ${d.decided_at} 对该颁发方${WINDOW_LABEL[st.state]}，实例 ${d.instance_id}（缓存 v${d.cache_version}）却接受`;
    }

    const finding = {
      finding_id: null,
      case_id: d.case_id ?? null,
      category, attribution,
      instance_id: d.instance_id, cache_version: d.cache_version, decided_at: d.decided_at,
      issuer_id: d.issuer_id, kid: d.kid,
      expected, actual: d.result, window: st.state,
      key_window: st.window ? windowJson(st.window) : null,
      active_cache_version: activeVersion,
      evidence,
      message,
    };
    findings.push(finding);
    evaluations.push({ ...base, ok: false, finding_id: null });
  }

  findings.forEach((f, i) => { f.finding_id = `F-${String(i + 1).padStart(4, '0')}`; });
  anomalies.forEach((a, i) => { a.anomaly_id = `A-${String(i + 1).padStart(4, '0')}`; });
  const findingByCase = new Map();
  for (const f of findings) {
    const ev = evaluations.find((e) => !e.ok && e.finding_id === null
      && e.instance_id === f.instance_id && e.decided_at === f.decided_at
      && e.issuer_id === f.issuer_id && e.kid === f.kid && e.cache_version === f.cache_version);
    if (ev) ev.finding_id = f.finding_id;
    if (f.case_id) findingByCase.set(f.case_id, f);
  }

  const byCategory = {};
  const byAttribution = {};
  for (const f of findings) {
    byCategory[f.category] = (byCategory[f.category] ?? 0) + 1;
    if (f.attribution) byAttribution[f.attribution] = (byAttribution[f.attribution] ?? 0) + 1;
  }
  const anomaliesByType = {};
  for (const a of anomalies) anomaliesByType[a.type] = (anomaliesByType[a.type] ?? 0) + 1;

  const report = {
    tool: 'jwt-rotation-audit',
    version: 1,
    summary: {
      decisions_total: (decisions.decisions ?? []).length,
      evaluations_ok: evaluations.filter((e) => e.ok).length,
      findings_total: findings.length,
      by_category: byCategory,
      by_attribution: byAttribution,
      anomalies_total: anomalies.length,
      anomalies_by_type: anomaliesByType,
    },
    anomalies,
    findings,
    evaluations,
  };

  if (at !== null && at !== undefined) {
    report.trusted_sets = {
      at,
      issuers: [...issuers.keys()].sort().map((id) => ({
        issuer_id: id,
        trusted_kids: expectedTrustedSet(issuers, at, new Set([id])).map((k) => k.kid),
      })),
      instances: [...instances.values()].map((v) => {
        const av = activeVersionAt(v.events, at);
        const snap = av === null ? null : v.snapshots.get(av);
        return {
          instance_id: v.instance_id,
          active_cache_version: av,
          snapshot_missing: av !== null && !snap,
          cached_keys: snap ? snap.keys : [],
        };
      }),
    };
  }
  return report;
}
