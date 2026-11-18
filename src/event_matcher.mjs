/**
 * event_matcher.mjs
 *
 * 关联规则事件匹配引擎（如“敏感读取后五分钟内发生外传，且两者之间没有有效批准”）。
 *
 * 核心语义：
 * - 规则定义：主体键 subjectKey、正向事件序列 sequence、最大间隔 maxIntervalMs、
 *   区间内不应出现的事件 absence，以及规则级允许迟到 allowedLatenessMs。
 * - 输入事件携带：事件时间 eventTimeMs、到达时间 arrivalTimeMs、事件 ID id；
 *   规则通过独立的发布记录 publishRule 进入引擎，按版本管理。
 * - 显式水位线 advanceWatermark 决定哪些结果可以定案（confirmed）；
 *   缺席区间被水位线闭合之前，匹配结果只能是暂定（tentative）。
 * - 允许迟到范围内到达的缺席事件（如批准）会撤销暂定匹配；
 *   超过水位线 + 允许迟到的记录进入单独的迟到通道，不再生效。
 * - 一个事件可参与多个尚未闭合的候选；重复 ID 不产生新候选；
 *   同 ID 内容冲突的记录被隔离，不参与匹配。
 * - 规则更新后，已有候选仍按其创建时的旧版本续接；新候选只按当前版本创建。
 * - 状态预算不足时产生“未能判断”(undetermined) 结果或丢弃(dropped)记录，
 *   均附参与记录，绝不能被当作“无风险”。
 */

export const RESULT_STATUS = Object.freeze({
  TENTATIVE: 'tentative',
  CONFIRMED: 'confirmed',
  REVOKED: 'revoked',
  UNDETERMINED: 'undetermined',
  DROPPED: 'dropped',
});

function stableStringify(value) {
  if (value === undefined) return 'undefined';
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`;
  const keys = Object.keys(value).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${stableStringify(value[k])}`).join(',')}}`;
}

function isNonNegativeFinite(n) {
  return typeof n === 'number' && Number.isFinite(n) && n >= 0;
}

function validateDefinition(definition) {
  if (!definition || typeof definition !== 'object') {
    throw new Error('rule definition must be an object');
  }
  const { subjectKey, sequence, maxIntervalMs, absence, allowedLatenessMs } = definition;
  if (!Array.isArray(subjectKey) || subjectKey.length === 0 || subjectKey.some((f) => typeof f !== 'string' || f.length === 0)) {
    throw new Error('definition.subjectKey must be a non-empty array of field names');
  }
  if (!Array.isArray(sequence) || sequence.length === 0 || sequence.some((s) => !s || typeof s.type !== 'string' || s.type.length === 0)) {
    throw new Error('definition.sequence must be a non-empty array of {type}');
  }
  if (!isNonNegativeFinite(maxIntervalMs)) {
    throw new Error('definition.maxIntervalMs must be a non-negative finite number');
  }
  if (!Array.isArray(absence) || absence.some((a) => !a || typeof a.type !== 'string' || a.type.length === 0)) {
    throw new Error('definition.absence must be an array of {type} (possibly empty)');
  }
  if (allowedLatenessMs !== undefined && !isNonNegativeFinite(allowedLatenessMs)) {
    throw new Error('definition.allowedLatenessMs must be a non-negative finite number when present');
  }
}

export class EventMatcher {
  #nextCandidateId = 1;
  #nextResultId = 1;

  /**
   * @param {object} [options]
   * @param {number} [options.maxActiveCandidates=1000] 状态预算：最大并发活跃候选数
   * @param {'reject'|'evict-oldest'} [options.onBudgetExceeded='reject'] 预算不足策略
   * @param {number} [options.allowedLatenessMs=0] 引擎级允许迟到（与规则级取最大）
   * @param {number} [options.initialWatermarkMs=-Infinity] 初始水位线
   */
  constructor(options = {}) {
    const {
      maxActiveCandidates = 1000,
      onBudgetExceeded = 'reject',
      allowedLatenessMs = 0,
      initialWatermarkMs = Number.NEGATIVE_INFINITY,
    } = options;
    if (!Number.isInteger(maxActiveCandidates) || maxActiveCandidates < 1) {
      throw new Error('maxActiveCandidates must be a positive integer');
    }
    if (onBudgetExceeded !== 'reject' && onBudgetExceeded !== 'evict-oldest') {
      throw new Error("onBudgetExceeded must be 'reject' or 'evict-oldest'");
    }
    if (!isNonNegativeFinite(allowedLatenessMs)) {
      throw new Error('allowedLatenessMs must be a non-negative finite number');
    }
    this.maxActiveCandidates = maxActiveCandidates;
    this.onBudgetExceeded = onBudgetExceeded;
    this.allowedLatenessMs = allowedLatenessMs;
    this.watermarkMs = initialWatermarkMs;

    this.ruleVersions = new Map(); // ruleId -> Map(version -> definition snapshot)
    this.currentVersions = new Map(); // ruleId -> 当前版本号（取已发布最大版本）
    this.eventsById = new Map(); // eventId -> { event, fingerprint, disposition }
    this.conflicts = []; // 同 ID 内容冲突的隔离记录
    this.invalidRecords = []; // 缺主体键字段等无法键控的记录
    this.lateChannel = []; // 超过水位线+允许迟到的记录
    this.candidates = []; // 活跃候选
    this.results = []; // 匹配/撤销/未能判断/丢弃结果
    this.candidateAudit = []; // 候选审计（如超时过期）
    this.postFinalizationNotices = []; // 定案后到达的相关缺席事件（仅审计，不改判）
  }

  /** 发布规则版本。同 ruleId 下当前版本取已发布的最大版本号。 */
  publishRule(record) {
    if (!record || typeof record !== 'object') throw new Error('rule record must be an object');
    const { ruleId, version, definition } = record;
    if (typeof ruleId !== 'string' || ruleId.length === 0) throw new Error('ruleId must be a non-empty string');
    if (!Number.isInteger(version) || version < 1) throw new Error('version must be a positive integer');
    validateDefinition(definition);
    if (!this.ruleVersions.has(ruleId)) this.ruleVersions.set(ruleId, new Map());
    const versions = this.ruleVersions.get(ruleId);
    if (versions.has(version)) {
      const existing = versions.get(version);
      if (stableStringify(existing) !== stableStringify(definition)) {
        throw new Error(`rule ${ruleId} v${version} already published with a different definition`);
      }
      return { ruleId, version, currentVersion: this.currentVersions.get(ruleId), republished: true };
    }
    const snapshot = JSON.parse(JSON.stringify(definition));
    versions.set(version, snapshot);
    const current = this.currentVersions.get(ruleId) ?? 0;
    if (version > current) this.currentVersions.set(ruleId, version);
    return { ruleId, version, currentVersion: this.currentVersions.get(ruleId), republished: false };
  }

  /** 推进水位线（单调不减），闭合缺席区间并定案，同时过期不可能完成的候选。 */
  advanceWatermark(tsMs) {
    if (typeof tsMs !== 'number' || !Number.isFinite(tsMs)) {
      throw new Error('watermark must be a finite number');
    }
    if (tsMs < this.watermarkMs) {
      throw new Error(`watermark must be monotonic: ${tsMs} < ${this.watermarkMs}`);
    }
    this.watermarkMs = tsMs;
    for (const res of this.results) {
      if (res.status === RESULT_STATUS.TENTATIVE && this.watermarkMs > res.window.endMs) {
        res.status = RESULT_STATUS.CONFIRMED;
        res.history.push({ status: res.status, reason: 'watermark-closed-absence-window', watermarkMs: tsMs });
      }
    }
    for (const cand of [...this.candidates]) {
      if (this.watermarkMs > cand.lastStepTimeMs + cand.definition.maxIntervalMs) {
        this.#removeCandidate(cand);
        this.candidateAudit.push({
          candidateId: cand.id,
          ruleId: cand.ruleId,
          ruleVersion: cand.ruleVersion,
          subjectKey: cand.subjectKey,
          disposition: 'expired',
          participations: cand.participations.map((p) => ({ ...p })),
          watermarkMs: tsMs,
        });
      }
    }
    return { watermarkMs: this.watermarkMs };
  }

  /**
   * 摄入事件。返回接收回执；重复 ID 不产生新候选，冲突内容被隔离，
   * 超过允许迟到的记录进入迟到通道。
   */
  ingest(event) {
    this.#validateEvent(event);
    const { arrivalTimeMs, ...content } = event; // 到达时间不参与内容指纹（重投允许到达时间不同）
    const fingerprint = stableStringify(content);
    const prior = this.eventsById.get(event.id);
    if (prior) {
      if (prior.fingerprint === fingerprint) {
        return { accepted: false, duplicate: true, eventId: event.id };
      }
      this.conflicts.push({
        eventId: event.id,
        reason: 'conflicting-content-same-id',
        first: prior.event,
        rejected: event,
      });
      return { accepted: false, conflict: true, eventId: event.id };
    }

    if (event.eventTimeMs <= this.watermarkMs) {
      const allowed = this.#effectiveAllowedLateness();
      const latenessMs = this.watermarkMs - event.eventTimeMs;
      if (latenessMs > allowed) {
        this.lateChannel.push({
          event,
          latenessMs,
          watermarkMs: this.watermarkMs,
          allowedLatenessMs: allowed,
          reason: 'beyond-allowed-lateness',
        });
        this.eventsById.set(event.id, { event, fingerprint, disposition: 'late-channel' });
        return { accepted: false, late: true, latenessMs, eventId: event.id };
      }
      this.eventsById.set(event.id, { event, fingerprint, disposition: 'processed-late' });
      this.#process(event, { late: true, latenessMs });
      return { accepted: true, late: true, latenessMs, eventId: event.id };
    }

    this.eventsById.set(event.id, { event, fingerprint, disposition: 'processed' });
    this.#process(event, { late: false });
    return { accepted: true, late: false, eventId: event.id };
  }

  #validateEvent(event) {
    if (!event || typeof event !== 'object') throw new Error('event must be an object');
    if (typeof event.id !== 'string' || event.id.length === 0) throw new Error('event.id must be a non-empty string');
    if (typeof event.type !== 'string' || event.type.length === 0) throw new Error('event.type must be a non-empty string');
    if (typeof event.eventTimeMs !== 'number' || !Number.isFinite(event.eventTimeMs)) throw new Error('event.eventTimeMs must be a finite number');
    if (typeof event.arrivalTimeMs !== 'number' || !Number.isFinite(event.arrivalTimeMs)) throw new Error('event.arrivalTimeMs must be a finite number');
  }

  #effectiveAllowedLateness() {
    let allowed = this.allowedLatenessMs;
    for (const [ruleId, version] of this.currentVersions) {
      const def = this.ruleVersions.get(ruleId)?.get(version);
      if (def && typeof def.allowedLatenessMs === 'number' && def.allowedLatenessMs > allowed) {
        allowed = def.allowedLatenessMs;
      }
    }
    return allowed;
  }

  #keyOf(event, def) {
    const parts = [];
    for (const field of def.subjectKey) {
      const v = event[field];
      if (v === undefined || v === null) return null;
      parts.push(`${field}=${String(v)}`);
    }
    return parts.join('|');
  }

  #definitionFor(ruleId, version) {
    return this.ruleVersions.get(ruleId)?.get(version) ?? null;
  }

  #removeCandidate(cand) {
    const i = this.candidates.indexOf(cand);
    if (i >= 0) this.candidates.splice(i, 1);
  }

  #emitResult(partial) {
    const result = {
      resultId: this.#nextResultId++,
      ruleId: partial.ruleId,
      ruleVersion: partial.ruleVersion,
      subjectKey: partial.subjectKey,
      status: partial.status,
      phase: partial.phase, // 'complete' 序列完成 | 'partial' 候选未完成
      window: partial.window, // 缺席窗口 (startMs, endMs]
      participations: partial.participations.map((p) => ({ ...p })),
      history: [{ status: partial.status, reason: partial.reason, watermarkMs: this.watermarkMs }],
    };
    if (partial.revokedBy) result.revokedBy = partial.revokedBy;
    this.results.push(result);
    return result;
  }

  #process(event, ctx) {
    // 1) 缺席条件：命中活跃候选 => 保守撤销（事件时间晚于候选首步即落在任何未来完成窗口内）
    for (const cand of [...this.candidates]) {
      const def = cand.definition;
      if (!def.absence.some((a) => a.type === event.type)) continue;
      const key = this.#keyOf(event, def);
      if (key === null || key !== cand.subjectKey) continue;
      if (event.eventTimeMs > cand.firstStepTimeMs) {
        this.#removeCandidate(cand);
        this.#emitResult({
          ruleId: cand.ruleId,
          ruleVersion: cand.ruleVersion,
          subjectKey: cand.subjectKey,
          status: RESULT_STATUS.REVOKED,
          phase: 'partial',
          window: { startMs: cand.firstStepTimeMs, endMs: cand.lastStepTimeMs },
          participations: cand.participations,
          reason: 'absence-event-before-completion',
          revokedBy: { eventId: event.id, eventTimeMs: event.eventTimeMs, late: ctx.late },
        });
      }
    }
    // 2) 缺席条件：命中暂定结果 => 撤销；命中已定案结果 => 仅审计记录，不改判
    for (const res of this.results) {
      const def = this.#definitionFor(res.ruleId, res.ruleVersion);
      if (!def || !def.absence.some((a) => a.type === event.type)) continue;
      const key = this.#keyOf(event, def);
      if (key === null || key !== res.subjectKey) continue;
      const inWindow = event.eventTimeMs > res.window.startMs && event.eventTimeMs <= res.window.endMs;
      if (!inWindow) continue;
      if (res.status === RESULT_STATUS.TENTATIVE) {
        res.status = RESULT_STATUS.REVOKED;
        res.revokedBy = { eventId: event.id, eventTimeMs: event.eventTimeMs, late: ctx.late };
        res.history.push({ status: res.status, reason: 'absence-event-in-window', eventId: event.id, late: ctx.late, watermarkMs: this.watermarkMs });
      } else if (res.status === RESULT_STATUS.CONFIRMED) {
        this.postFinalizationNotices.push({
          resultId: res.resultId,
          eventId: event.id,
          note: 'absence-event-after-finalization',
          late: ctx.late,
        });
      }
    }
    // 3) 正向序列：推进已有候选（按其创建时的规则版本；一个事件可推进多个未闭合候选）
    for (const cand of [...this.candidates]) {
      const def = cand.definition;
      const nextStep = def.sequence[cand.nextStepIndex];
      if (!nextStep || nextStep.type !== event.type) continue;
      const key = this.#keyOf(event, def);
      if (key === null || key !== cand.subjectKey) continue;
      if (event.eventTimeMs <= cand.lastStepTimeMs) continue; // 序列事件时间必须递增
      if (event.eventTimeMs - cand.lastStepTimeMs > def.maxIntervalMs) continue; // 超出最大间隔
      cand.participations.push({ eventId: event.id, type: event.type, eventTimeMs: event.eventTimeMs, role: `step-${cand.nextStepIndex + 1}` });
      cand.lastStepTimeMs = event.eventTimeMs;
      cand.nextStepIndex += 1;
      if (cand.nextStepIndex >= def.sequence.length) {
        this.#removeCandidate(cand);
        this.#emitResult({
          ruleId: cand.ruleId,
          ruleVersion: cand.ruleVersion,
          subjectKey: cand.subjectKey,
          status: RESULT_STATUS.TENTATIVE,
          phase: 'complete',
          window: { startMs: cand.firstStepTimeMs, endMs: cand.lastStepTimeMs },
          participations: cand.participations,
          reason: 'sequence-complete-absence-window-open',
        });
      }
    }
    // 4) 正向序列：仅按当前版本创建新候选
    for (const [ruleId, version] of this.currentVersions) {
      const def = this.ruleVersions.get(ruleId).get(version);
      if (def.sequence[0].type !== event.type) continue;
      const key = this.#keyOf(event, def);
      if (key === null) {
        this.invalidRecords.push({ event, ruleId, reason: 'missing-subject-field' });
        continue;
      }
      this.#createCandidate(ruleId, version, key, def, event);
    }
  }

  #createCandidate(ruleId, version, key, def, event) {
    if (this.candidates.length >= this.maxActiveCandidates) {
      if (this.onBudgetExceeded === 'evict-oldest') {
        const oldest = this.candidates.shift();
        this.#emitResult({
          ruleId: oldest.ruleId,
          ruleVersion: oldest.ruleVersion,
          subjectKey: oldest.subjectKey,
          status: RESULT_STATUS.DROPPED,
          phase: 'partial',
          window: { startMs: oldest.firstStepTimeMs, endMs: oldest.lastStepTimeMs },
          participations: oldest.participations,
          reason: 'evicted-state-budget-exhausted',
        });
      } else {
        // 预算不足：不建候选，但保留“未能判断”结果与参与记录，绝不当作无风险
        this.#emitResult({
          ruleId,
          ruleVersion: version,
          subjectKey: key,
          status: RESULT_STATUS.UNDETERMINED,
          phase: 'partial',
          window: { startMs: event.eventTimeMs, endMs: event.eventTimeMs },
          participations: [{ eventId: event.id, type: event.type, eventTimeMs: event.eventTimeMs, role: 'step-1-rejected' }],
          reason: 'state-budget-exhausted',
        });
        return;
      }
    }
    const participations = [{ eventId: event.id, type: event.type, eventTimeMs: event.eventTimeMs, role: 'step-1' }];
    if (def.sequence.length === 1) {
      this.#emitResult({
        ruleId,
        ruleVersion: version,
        subjectKey: key,
        status: RESULT_STATUS.TENTATIVE,
        phase: 'complete',
        window: { startMs: event.eventTimeMs, endMs: event.eventTimeMs },
        participations,
        reason: 'sequence-complete-absence-window-open',
      });
      return;
    }
    this.candidates.push({
      id: this.#nextCandidateId++,
      ruleId,
      ruleVersion: version,
      subjectKey: key,
      definition: def, // 规则版本快照：后续事件按旧规则续接
      nextStepIndex: 1,
      firstStepTimeMs: event.eventTimeMs,
      lastStepTimeMs: event.eventTimeMs,
      participations,
    });
  }

  /** 全部结果（可选按 status/subjectKey/ruleId 过滤），返回深拷贝快照。 */
  getResults(filter = {}) {
    const { status, subjectKey, ruleId } = filter;
    const out = this.results.filter(
      (r) =>
        (status === undefined || r.status === status) &&
        (subjectKey === undefined || r.subjectKey === subjectKey) &&
        (ruleId === undefined || r.ruleId === ruleId),
    );
    return structuredClone(out);
  }

  getActiveCandidates() {
    return structuredClone(
      this.candidates.map((c) => ({
        candidateId: c.id,
        ruleId: c.ruleId,
        ruleVersion: c.ruleVersion,
        subjectKey: c.subjectKey,
        nextStepIndex: c.nextStepIndex,
        firstStepTimeMs: c.firstStepTimeMs,
        lastStepTimeMs: c.lastStepTimeMs,
        participations: c.participations,
      })),
    );
  }

  getLateChannel() {
    return structuredClone(this.lateChannel);
  }

  getConflicts() {
    return structuredClone(this.conflicts);
  }

  getInvalidRecords() {
    return structuredClone(this.invalidRecords);
  }

  getCandidateAudit() {
    return structuredClone(this.candidateAudit);
  }

  getPostFinalizationNotices() {
    return structuredClone(this.postFinalizationNotices);
  }

  /**
   * 风险汇总：undetermined/dropped 计入 riskUnknown，
   * 只要存在未能完成判断的候选，结论就是 risk-unknown，绝不输出 no-risk。
   */
  getRiskSummary() {
    const byStatus = {};
    for (const r of this.results) byStatus[r.status] = (byStatus[r.status] ?? 0) + 1;
    const riskUnknown = (byStatus[RESULT_STATUS.UNDETERMINED] ?? 0) + (byStatus[RESULT_STATUS.DROPPED] ?? 0);
    return {
      byStatus,
      riskUnknown,
      activeCandidates: this.candidates.length,
      lateChannelSize: this.lateChannel.length,
      conflicts: this.conflicts.length,
      conclusion: riskUnknown > 0 ? 'risk-unknown' : 'complete',
      note:
        riskUnknown > 0
          ? 'undetermined/dropped candidates must not be treated as no-risk'
          : 'all candidates fully evaluated',
    };
  }
}

export function createEngine(options) {
  return new EventMatcher(options);
}
