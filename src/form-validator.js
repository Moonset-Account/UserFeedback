import { Reason } from './reasons.js';

/**
 * 各表单动作的安全策略：
 *  - graceMs: 上一代凭证的宽限窗口（毫秒）。null 表示该动作不享宽限，
 *    一旦凭证代际落后立即拒绝（GENERATION_STALE）。
 *    save-draft（草稿保存）允许宽限，保证两个标签页在轮换间隙仍能各自保存；
 *    submit（最终提交）不允许宽限，旧代际提交一律拒绝。
 *  - fields: 结构化字段规则（type 必填 / 长度上限 / 最小长度）。
 */
export const DEFAULT_POLICIES = Object.freeze({
  'save-draft': Object.freeze({
    graceMs: 5_000,
    fields: Object.freeze({
      content: { required: true, type: 'string', minLength: 1, maxLength: 10_000 },
      title: { required: false, type: 'string', maxLength: 200 },
    }),
  }),
  submit: Object.freeze({
    graceMs: null,
    fields: Object.freeze({
      content: { required: true, type: 'string', minLength: 1, maxLength: 10_000 },
      title: { required: true, type: 'string', minLength: 1, maxLength: 200 },
      comment: { required: false, type: 'string', maxLength: 500 },
    }),
  }),
});

const deny = (reason, extra = {}) => Object.freeze({ allowed: false, reason, ...extra });
const allow = (reason, extra = {}) => Object.freeze({ allowed: true, reason, ...extra });

/**
 * 内存表单校验器。
 *
 * 安全模型：
 *  1. 凭证（令牌）在签发时绑定 { sessionId, action, generation }，由可信桩核验真伪。
 *  2. 校验器逐道检查：令牌真伪 → 会话存在/未撤销 → 会话绑定一致 → 动作绑定一致
 *     → 令牌自身有效期 → 代际（含按动作决定的宽限期）→ 结构化字段。
 *  3. 更换会话（rotateSession）后旧会话立即撤销，其全部凭证失效，无任何宽限。
 *  4. 任何拒绝都不改变内部状态：不推进代际、不写草稿，因此表单失败后可原样重试。
 */
export class FormValidator {
  #clock;
  #tokenStub;
  #policies;
  #sessions; // sessionId -> { generation, rotatedAt, revoked, generations: [{at}] }
  #drafts;   // `${sessionId}::${tabId}` -> 结构化字段快照

  /**
   * @param {object} deps
   * @param {import('./clock.js').Clock} deps.clock 可注入时钟
   * @param {import('./token-stub.js').TrustedTokenStub} deps.tokenStub 可信令牌测试桩
   * @param {object} [deps.policies] 动作策略（默认 DEFAULT_POLICIES）
   */
  constructor({ clock, tokenStub, policies = DEFAULT_POLICIES }) {
    if (!clock || typeof clock.now !== 'function') throw new TypeError('缺少 clock');
    if (!tokenStub || typeof tokenStub.verify !== 'function' || typeof tokenStub.issue !== 'function') {
      throw new TypeError('缺少 tokenStub');
    }
    this.#clock = clock;
    this.#tokenStub = tokenStub;
    this.#policies = policies;
    this.#sessions = new Map();
    this.#drafts = new Map();
  }

  /** 新建会话，返回会话标识（首个代际为 1） */
  createSession(sessionId) {
    if (typeof sessionId !== 'string' || !sessionId) throw new TypeError('sessionId 必须为非空字符串');
    if (this.#sessions.has(sessionId)) throw new Error(`会话已存在: ${sessionId}`);
    this.#sessions.set(sessionId, {
      generation: 1,
      rotatedAt: this.#clock.now(),
      revoked: false,
      generations: [{ at: this.#clock.now() }],
    });
    return sessionId;
  }

  #requireSession(sessionId) {
    const session = this.#sessions.get(sessionId);
    if (!session) throw new Error(`未知会话: ${sessionId}`);
    return session;
  }

  /**
   * 为会话内某动作签发绑定当前代际的凭证。
   * @returns {{ token: string, attributes: object, generation: number }}
   */
  issueCredential({ sessionId, action, ttlMs = 60_000 }) {
    if (!this.#policies[action]) throw new Error(`未知动作: ${action}`);
    const session = this.#requireSession(sessionId);
    if (session.revoked) throw new Error(`会话已撤销，不能签发凭证: ${sessionId}`);
    const { token, attributes } = this.#tokenStub.issue({
      sessionId,
      action,
      generation: session.generation,
      ttlMs,
    });
    return { token, attributes, generation: session.generation };
  }

  /**
   * 会话内轮换凭证代际（例如重新核验身份后）。
   * 上一代是否仍可用，取决于动作策略里的 graceMs；会话本身保持有效。
   * @returns {number} 新代际号
   */
  rotateGeneration(sessionId) {
    const session = this.#requireSession(sessionId);
    if (session.revoked) throw new Error(`会话已撤销: ${sessionId}`);
    session.generation += 1;
    session.rotatedAt = this.#clock.now();
    session.generations.push({ at: session.rotatedAt });
    return session.generation;
  }

  /**
   * 更换会话：旧会话立即撤销（其全部凭证即刻失效，无宽限），新会话从代际 1 开始。
   * 草稿按会话隔离存储，旧草稿不会进入新会话。
   * @returns {{ oldSessionId: string, newSessionId: string }}
   */
  rotateSession(oldSessionId, newSessionId) {
    const old = this.#requireSession(oldSessionId);
    if (typeof newSessionId !== 'string' || !newSessionId) throw new TypeError('newSessionId 必须为非空字符串');
    if (this.#sessions.has(newSessionId)) throw new Error(`会话已存在: ${newSessionId}`);
    old.revoked = true;
    this.#sessions.set(newSessionId, {
      generation: 1,
      rotatedAt: this.#clock.now(),
      revoked: false,
      generations: [{ at: this.#clock.now() }],
    });
    return { oldSessionId, newSessionId };
  }

  /** 读取某标签页已保存的草稿（不存在返回 null） */
  getDraft(sessionId, tabId) {
    const snap = this.#drafts.get(`${sessionId}::${tabId}`);
    return snap ? structuredClone(snap) : null;
  }

  #validateFields(action, fields) {
    const policy = this.#policies[action];
    const errors = [];
    const rules = policy.fields;
    for (const [name, rule] of Object.entries(rules)) {
      const value = fields ? fields[name] : undefined;
      const present = value !== undefined && value !== null && value !== '';
      if (!present) {
        if (rule.required) errors.push({ field: name, error: 'required' });
        continue;
      }
      if (rule.type === 'string' && typeof value !== 'string') {
        errors.push({ field: name, error: 'type', expected: 'string' });
        continue;
      }
      const len = typeof value === 'string' ? value.length : undefined;
      if (len !== undefined) {
        if (rule.minLength !== undefined && len < rule.minLength) {
          errors.push({ field: name, error: 'minLength', min: rule.minLength });
        }
        if (rule.maxLength !== undefined && len > rule.maxLength) {
          errors.push({ field: name, error: 'maxLength', max: rule.maxLength });
        }
      }
    }
    return errors;
  }

  /**
   * 校验入口：接收结构化字段，返回决定与稳定理由。
   *
   * @param {object} input
   * @param {string} input.sessionId 提交所在会话标识
   * @param {string} input.tabId     匿名标签页标识（仅用于草稿命名空间隔离）
   * @param {string} input.action    表单动作（save-draft | submit）
   * @param {string} input.token     提交凭证（不透明令牌）
   * @param {object} [input.fields]  结构化字段
   * @returns {Readonly<{ allowed: boolean, reason: string, detail?: object }>}
   */
  validate({ sessionId, tabId, action, token, fields = {} }) {
    // 1) 令牌真伪（由可信测试桩核验，回传已核验的绑定属性）
    const verdict = this.#tokenStub.verify(token);
    if (!verdict.valid) {
      return deny(Reason.TOKEN_INVALID, { detail: { stage: 'token-verify' } });
    }
    const attrs = verdict.attributes;

    // 2) 提交会话是否存在且未撤销（更换会话后旧会话立即失效）
    const session = this.#sessions.get(sessionId);
    if (!session) {
      return deny(Reason.SESSION_MISMATCH, {
        detail: { stage: 'session-lookup', submitted: sessionId, bound: attrs.sessionId },
      });
    }
    if (session.revoked) {
      return deny(Reason.SESSION_REVOKED, {
        detail: { stage: 'session-state', sessionId },
      });
    }

    // 3) 凭证绑定的会话必须与提交会话一致（令牌不可跨会话使用）
    if (attrs.sessionId !== sessionId) {
      return deny(Reason.SESSION_MISMATCH, {
        detail: { stage: 'session-binding', submitted: sessionId, bound: attrs.sessionId },
      });
    }

    // 4) 凭证绑定的动作必须与提交动作一致（令牌不可跨动作使用）
    if (attrs.action !== action) {
      return deny(Reason.ACTION_MISMATCH, {
        detail: { stage: 'action-binding', submitted: action, bound: attrs.action },
      });
    }
    const policy = this.#policies[action];
    if (!policy) {
      return deny(Reason.ACTION_MISMATCH, { detail: { stage: 'action-policy', action } });
    }

    // 5) 令牌自身有效期（独立于代际宽限期的第二道时间检查）
    if (this.#clock.now() >= attrs.exp) {
      return deny(Reason.TOKEN_EXPIRED, {
        detail: { stage: 'token-expiry', now: this.#clock.now(), exp: attrs.exp },
      });
    }

    // 6) 代际检查：当前代际放行；上一代按动作策略决定是否享有宽限期
    const age = session.generation - attrs.generation;
    if (age < 0) {
      // 凭证代际比会话还新——不可能来自合法轮换，拒绝
      return deny(Reason.GENERATION_STALE, {
        detail: { stage: 'generation', current: session.generation, bound: attrs.generation },
      });
    }
    let generationReason = Reason.ALLOWED;
    if (age === 1) {
      if (policy.graceMs === null) {
        return deny(Reason.GENERATION_STALE, {
          detail: {
            stage: 'generation',
            current: session.generation,
            bound: attrs.generation,
            grace: 'not-applicable',
          },
        });
      }
      const graceDeadline = session.rotatedAt + policy.graceMs;
      if (this.#clock.now() > graceDeadline) {
        return deny(Reason.GRACE_EXPIRED, {
          detail: {
            stage: 'generation-grace',
            current: session.generation,
            bound: attrs.generation,
            graceDeadline,
            now: this.#clock.now(),
          },
        });
      }
      generationReason = Reason.ALLOWED_GRACE;
    } else if (age > 1) {
      // 落后两代及以上：即使有宽限也只惠及“上一代”，一律拒绝
      return deny(Reason.GENERATION_STALE, {
        detail: {
          stage: 'generation',
          current: session.generation,
          bound: attrs.generation,
          grace: 'previous-generation-only',
        },
      });
    }

    // 7) 结构化字段校验。放在授权检查之后；失败不改变任何状态，允许原样重试。
    const fieldErrors = this.#validateFields(action, fields);
    if (fieldErrors.length > 0) {
      return deny(Reason.FORM_INVALID, { detail: { stage: 'fields', errors: fieldErrors } });
    }

    // 8) 授权通过且字段合法：save-draft 写入按会话+标签页隔离的草稿快照
    if (action === 'save-draft') {
      if (typeof tabId !== 'string' || !tabId) {
        return deny(Reason.FORM_INVALID, { detail: { stage: 'tab', error: 'tabId-required' } });
      }
      this.#drafts.set(`${sessionId}::${tabId}`, structuredClone(fields));
    }

    return allow(generationReason, {
      detail: {
        generation: attrs.generation,
        currentGeneration: session.generation,
        tabId: tabId ?? null,
      },
    });
  }
}
