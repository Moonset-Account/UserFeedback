import { createHash, randomUUID } from 'node:crypto';
import { AdvisoryError } from './errors.js';

/**
 * 段落内容摘要（规范化后取 SHA-256）。
 * 已发送版本永久保留该摘要，用于追溯。
 *
 * @param {string} productId
 * @param {string} body
 * @param {string} fixVersion
 * @returns {string}
 */
export function sectionDigest(productId, body, fixVersion) {
  const canonical = JSON.stringify({
    productId,
    fixVersion,
    body,
  });
  return `sha256:${createHash('sha256').update(canonical).digest('hex')}`;
}

/**
 * 安全通告放行注册表。
 *
 * 领域规则：
 *  1. 段落准入：受影响产品已注册、修复版本已确认、批准绑定在当前修订上；
 *  2. 任何改版（正文或修复版本）都会产生新修订，并清空旧批准与旧确认；
 *  3. 合并通告按段落逐一确认，确认必须落在当前修订上；
 *  4. 合作方只能查看自己获准产品的段落，且需到达约定公开时间；
 *  5. 移除收件人后，其尚未出队的副本一律作废，已交付（delivered）副本保留；
 *  6. 已发送通告冻结所有内容写操作，快照保留内容摘要与原始收件范围。
 */
export class AdvisoryRegistry {
  /**
   * @param {{ now?: () => Date }} [options] 可注入时钟，便于测试公开时间边界
   */
  constructor({ now } = {}) {
    this._now = now ?? (() => new Date());
    /** @type {Map<string, {id:string,name:string}>} */
    this._products = new Map();
    /** @type {Map<string, object>} */
    this._sections = new Map();
    /** @type {Map<string, object>} */
    this._advisories = new Map();
    /** key: `${advisoryId}:${recipientId}:${sectionId}` */
    /** @type {Map<string, object>} */
    this._copies = new Map();
  }

  // ------------------------------------------------------------------ 基础工具

  _timestamp() {
    return this._now().toISOString();
  }

  /**
   * @param {string} value
   * @param {string} label
   */
  static _requireNonEmptyString(value, label) {
    if (typeof value !== 'string' || value.trim() === '') {
      throw new AdvisoryError('INVALID_INPUT', `${label} 必须是非空字符串`, { label });
    }
  }

  /**
   * @param {string} id
   * @param {string} code
   * @param {string} message
   */
  _requireProduct(id, code = 'UNKNOWN_PRODUCT', message = '产品不存在') {
    const product = this._products.get(id);
    if (!product) throw new AdvisoryError(code, message, { productId: id });
    return product;
  }

  _requireSection(sectionId) {
    const section = this._sections.get(sectionId);
    if (!section) {
      throw new AdvisoryError('UNKNOWN_SECTION', '段落不存在', { sectionId });
    }
    return section;
  }

  _requireAdvisory(advisoryId) {
    const advisory = this._advisories.get(advisoryId);
    if (!advisory) {
      throw new AdvisoryError('UNKNOWN_ADVISORY', '通告不存在', { advisoryId });
    }
    return advisory;
  }

  _assertDraft(advisory) {
    if (advisory.status === 'sent') {
      throw new AdvisoryError(
        'ALREADY_SENT',
        '通告已发送，内容与收件范围的写操作已冻结',
        { advisoryId: advisory.id },
      );
    }
  }

  // ------------------------------------------------------------------ 产品注册

  /**
   * 注册受影响产品。
   *
   * @param {string} productId
   * @param {string} name
   * @returns {{id:string,name:string,registeredAt:string}}
   */
  registerProduct(productId, name) {
    AdvisoryRegistry._requireNonEmptyString(productId, 'productId');
    AdvisoryRegistry._requireNonEmptyString(name, 'name');
    if (this._products.has(productId)) {
      throw new AdvisoryError('PRODUCT_EXISTS', '产品已注册', { productId });
    }
    const product = { id: productId, name, registeredAt: this._timestamp() };
    this._products.set(productId, product);
    return { ...product };
  }

  // ------------------------------------------------------------------ 段落管理

  /**
   * 新建通告段落。修复版本可在后续确认后通过 setFixVersion 绑定。
   *
   * @param {string} productId
   * @param {string} body
   * @returns {object} 段落快照
   */
  createSection(productId, body) {
    this._requireProduct(productId);
    AdvisoryRegistry._requireNonEmptyString(body, 'body');
    const id = `sec_${randomUUID()}`;
    const now = this._timestamp();
    const section = {
      id,
      productId,
      body,
      fixVersion: null,
      revision: 1,
      approvals: [],
      createdAt: now,
      updatedAt: now,
    };
    this._sections.set(id, section);
    return this._sectionView(section);
  }

  /**
   * 确认修复版本。修复版本变更视为改版：修订号 +1，旧批准全部清空。
   *
   * @param {string} sectionId
   * @param {string} fixVersion
   * @returns {object}
   */
  setFixVersion(sectionId, fixVersion) {
    AdvisoryRegistry._requireNonEmptyString(fixVersion, 'fixVersion');
    const section = this._requireSection(sectionId);
    if (section.fixVersion === fixVersion) return this._sectionView(section);
    section.fixVersion = fixVersion;
    section.revision += 1;
    section.approvals = [];
    section.updatedAt = this._timestamp();
    return this._sectionView(section);
  }

  /**
   * 修订段落正文。正文变更视为改版：修订号 +1，旧批准全部清空。
   *
   * @param {string} sectionId
   * @param {string} body
   * @returns {object}
   */
  reviseSection(sectionId, body) {
    AdvisoryRegistry._requireNonEmptyString(body, 'body');
    const section = this._requireSection(sectionId);
    if (section.body === body) return this._sectionView(section);
    section.body = body;
    section.revision += 1;
    section.approvals = [];
    section.updatedAt = this._timestamp();
    return this._sectionView(section);
  }

  /**
   * 为段落绑定批准。批准绑定在“当前修订号”上，改版后自动过期，必须重新绑定。
   *
   * @param {string} sectionId
   * @param {string} approver
   * @param {string} [note]
   * @returns {object} 批准记录
   */
  bindApproval(sectionId, approver, note = '') {
    AdvisoryRegistry._requireNonEmptyString(approver, 'approver');
    const section = this._requireSection(sectionId);
    if (!section.fixVersion) {
      throw new AdvisoryError(
        'FIX_VERSION_REQUIRED',
        '修复版本尚未确认，无法绑定批准',
        { sectionId, productId: section.productId },
      );
    }
    const approval = {
      approvalId: `apr_${randomUUID()}`,
      approver,
      note,
      revision: section.revision,
      boundAt: this._timestamp(),
    };
    section.approvals.push(approval);
    return { ...approval };
  }

  /**
   * @param {object} section
   * @returns {object} 段落只读视图
   */
  _sectionView(section) {
    return {
      id: section.id,
      productId: section.productId,
      body: section.body,
      fixVersion: section.fixVersion,
      revision: section.revision,
      approvals: section.approvals.map((a) => ({ ...a })),
      createdAt: section.createdAt,
      updatedAt: section.updatedAt,
      digest: section.fixVersion
        ? sectionDigest(section.productId, section.body, section.fixVersion)
        : null,
    };
  }

  getSection(sectionId) {
    return this._sectionView(this._requireSection(sectionId));
  }

  // ------------------------------------------------------------------ 通告管理

  /**
   * 创建合并通告。sectionIds 即合并进同一通告的产品段落；
   * recipients 为受控收件范围，每个收件人只能覆盖该通告包含的产品。
   *
   * @param {{ title:string; publishAt:string|Date; sectionIds:string[]; recipients:Array<{id:string, productScope:string[]}> }} input
   * @returns {object}
   */
  createAdvisory({ title, publishAt, sectionIds, recipients }) {
    AdvisoryRegistry._requireNonEmptyString(title, 'title');
    if (Array.isArray(sectionIds) === false || sectionIds.length === 0) {
      throw new AdvisoryError('INVALID_INPUT', 'sectionIds 必须是非空数组');
    }
    if (Array.isArray(recipients) === false || recipients.length === 0) {
      throw new AdvisoryError('INVALID_INPUT', 'recipients 必须是非空数组');
    }

    const publishTime = new Date(publishAt);
    if (Number.isNaN(publishTime.getTime())) {
      throw new AdvisoryError('INVALID_INPUT', 'publishAt 不是合法时间');
    }

    const sections = [];
    const seen = new Set();
    for (const sectionId of sectionIds) {
      if (seen.has(sectionId)) {
        throw new AdvisoryError('DUPLICATE_SECTION', '合并通告中段落不得重复', { sectionId });
      }
      seen.add(sectionId);
      sections.push(this._requireSection(sectionId));
    }

    const advisoryProducts = new Set(sections.map((s) => s.productId));
    const recipientMap = new Map();
    for (const recipient of recipients) {
      if (!recipient || typeof recipient.id !== 'string' || recipient.id.trim() === '') {
        throw new AdvisoryError('INVALID_INPUT', '收件人 id 必须是非空字符串');
      }
      if (!Array.isArray(recipient.productScope) || recipient.productScope.length === 0) {
        throw new AdvisoryError('INVALID_INPUT', '收件人 productScope 必须是非空数组', {
          recipientId: recipient.id,
        });
      }
      for (const pid of recipient.productScope) {
        this._requireProduct(pid);
        if (!advisoryProducts.has(pid)) {
          throw new AdvisoryError(
            'RECIPIENT_OUT_OF_SCOPE',
            '收件范围包含通告未覆盖的产品',
            { recipientId: recipient.id, productId: pid },
          );
        }
      }
      if (recipientMap.has(recipient.id)) {
        throw new AdvisoryError('RECIPIENT_EXISTS', '收件人重复', {
          recipientId: recipient.id,
        });
      }
      recipientMap.set(recipient.id, [...new Set(recipient.productScope)]);
    }

    const id = `adv_${randomUUID()}`;
    const now = this._timestamp();
    const advisory = {
      id,
      title,
      publishAt: publishTime.toISOString(),
      sectionIds: [...sectionIds],
      recipients: [...recipientMap.entries()].map(([rid, scope]) => ({
        id: rid,
        productScope: scope,
        addedAt: now,
      })),
      // sectionId -> 段落级确认（绑定在当时修订号上）
      confirmations: new Map(),
      status: 'draft',
      createdAt: now,
      sentAt: null,
    };
    this._advisories.set(id, advisory);
    return this._advisoryView(advisory);
  }

  /**
   * 追加受控收件人（仅草稿状态）。
   *
   * @param {string} advisoryId
   * @param {string} recipientId
   * @param {string[]} productScope
   * @returns {object}
   */
  addRecipient(advisoryId, recipientId, productScope) {
    AdvisoryRegistry._requireNonEmptyString(recipientId, 'recipientId');
    if (!Array.isArray(productScope) || productScope.length === 0) {
      throw new AdvisoryError('INVALID_INPUT', 'productScope 必须是非空数组');
    }
    const advisory = this._requireAdvisory(advisoryId);
    this._assertDraft(advisory);
    if (advisory.recipients.some((r) => r.id === recipientId)) {
      throw new AdvisoryError('RECIPIENT_EXISTS', '收件人已在通告范围内', { recipientId });
    }
    const advisoryProducts = new Set(
      advisory.sectionIds.map((sid) => this._requireSection(sid).productId),
    );
    for (const pid of productScope) {
      this._requireProduct(pid);
      if (!advisoryProducts.has(pid)) {
        throw new AdvisoryError(
          'RECIPIENT_OUT_OF_SCOPE',
          '收件范围包含通告未覆盖的产品',
          { recipientId, productId: pid },
        );
      }
    }
    advisory.recipients.push({
      id: recipientId,
      productScope: [...new Set(productScope)],
      addedAt: this._timestamp(),
    });
    return this._advisoryView(advisory);
  }

  /**
   * 移除收件人：
   *  - 草稿阶段：从受控收件范围移除，发送时不再为其生成副本；
   *  - 已发送阶段：收件范围快照保留在 sentSnapshot 中，
   *    其尚未出队（queued）的副本立即作废，已 delivered 的副本保留。
   *
   * @param {string} advisoryId
   * @param {string} recipientId
   * @returns {{advisory:object, voidedCopies:Array<object>}}
   */
  removeRecipient(advisoryId, recipientId) {
    const advisory = this._requireAdvisory(advisoryId);
    const index = advisory.recipients.findIndex((r) => r.id === recipientId);
    if (index === -1) {
      throw new AdvisoryError(
        'RECIPIENT_NOT_FOUND',
        '收件人不在通告范围内',
        { advisoryId, recipientId },
      );
    }

    if (advisory.status === 'draft') {
      advisory.recipients.splice(index, 1);
      return { advisory: this._advisoryView(advisory), voidedCopies: [] };
    }

    // 已发送：活动收件范围移除，但 sentSnapshot 保留原始范围
    advisory.recipients.splice(index, 1);
    const voided = [];
    for (const [key, copy] of this._copies) {
      if (copy.advisoryId !== advisoryId || copy.recipientId !== recipientId) continue;
      if (copy.status === 'queued') {
        copy.status = 'voided';
        copy.voidedAt = this._timestamp();
        copy.voidReason = 'recipient_removed';
        voided.push({ ...copy });
      }
    }
    return { advisory: this._advisoryView(advisory), voidedCopies: voided };
  }

  /**
   * 按段落确认合并通告。确认必须落在段落当前修订上；
   * 段落改版或批准失效后，旧确认不再满足放行条件。
   *
   * @param {string} advisoryId
   * @param {string} sectionId
   * @param {string} confirmer
   * @returns {object}
   */
  confirmSection(advisoryId, sectionId, confirmer) {
    AdvisoryRegistry._requireNonEmptyString(confirmer, 'confirmer');
    const advisory = this._requireAdvisory(advisoryId);
    this._assertDraft(advisory);
    if (!advisory.sectionIds.includes(sectionId)) {
      throw new AdvisoryError(
        'SECTION_NOT_IN_ADVISORY',
        '段落不属于该通告，无法确认',
        { advisoryId, sectionId },
      );
    }
    const section = this._requireSection(sectionId);
    if (!section.fixVersion) {
      throw new AdvisoryError(
        'FIX_VERSION_REQUIRED',
        '修复版本尚未确认，段落无法确认',
        { sectionId },
      );
    }
    const currentApproval = section.approvals.find((a) => a.revision === section.revision);
    if (!currentApproval) {
      throw new AdvisoryError(
        'APPROVAL_REQUIRED',
        '当前修订未绑定批准，确认前须先重新绑定批准',
        { sectionId, revision: section.revision },
      );
    }
    advisory.confirmations.set(sectionId, {
      sectionId,
      revision: section.revision,
      confirmer,
      confirmedAt: this._timestamp(),
    });
    return { ...advisory.confirmations.get(sectionId) };
  }

  // ------------------------------------------------------------------ 放行评估

  /**
   * 非抛出式放行评估：汇总所有阻断原因，供调用方展示。
   *
   * @param {string} advisoryId
   * @returns {{releasable:boolean, reasons:Array<{code:string,message:string,sectionId?:string}>, at:string}}
   */
  evaluateRelease(advisoryId) {
    const advisory = this._requireAdvisory(advisoryId);
    const reasons = [];

    if (advisory.status === 'sent') {
      return {
        releasable: true,
        reasons: [],
        at: this._timestamp(),
        note: '通告已发送',
      };
    }

    for (const sectionId of advisory.sectionIds) {
      const section = this._requireSection(sectionId);
      if (!section.fixVersion) {
        reasons.push({
          code: 'FIX_VERSION_REQUIRED',
          message: `段落 ${sectionId} 的修复版本尚未确认`,
          sectionId,
        });
      }
      const hasCurrentApproval = section.approvals.some(
        (a) => a.revision === section.revision,
      );
      if (section.fixVersion && !hasCurrentApproval) {
        reasons.push({
          code: 'APPROVAL_REQUIRED',
          message: `段落 ${sectionId} 的当前修订 ${section.revision} 未绑定批准（可能已改版）`,
          sectionId,
        });
      }
      const confirmation = advisory.confirmations.get(sectionId);
      if (!confirmation) {
        reasons.push({
          code: 'SECTION_CONFIRMATION_REQUIRED',
          message: `合并通告段落 ${sectionId} 尚未逐段确认`,
          sectionId,
        });
      } else if (confirmation.revision !== section.revision) {
        reasons.push({
          code: 'SECTION_CONFIRMATION_REQUIRED',
          message: `段落 ${sectionId} 的确认基于修订 ${confirmation.revision}，当前为 ${section.revision}，须重新确认`,
          sectionId,
        });
      }
    }

    if (this._now().getTime() < new Date(advisory.publishAt).getTime()) {
      reasons.push({
        code: 'PUBLIC_TIME_NOT_REACHED',
        message: `尚未到达公开时间 ${advisory.publishAt}`,
      });
    }

    if (advisory.recipients.length === 0) {
      reasons.push({
        code: 'NO_ALLOWED_RECIPIENTS',
        message: '受控收件范围为空，没有任何可送达的收件人',
      });
    }

    return { releasable: reasons.length === 0, reasons, at: this._timestamp() };
  }

  /**
   * 计算每个段落当前可送达的收件人（仍在活动收件范围内且获准该产品）。
   *
   * @param {object} advisory
   * @param {object} section
   * @returns {Array<object>}
   */
  _allowedRecipientsFor(advisory, section) {
    return advisory.recipients
      .filter((r) => r.productScope.includes(section.productId))
      .map((r) => ({ ...r }));
  }

  /**
   * 构建可发送通告包；存在任一阻断原因时抛出 RELEASE_BLOCKED。
   *
   * @param {string} advisoryId
   * @returns {{advisoryId:string,title:string,publishAt:string,generatedAt:string,paragraphs:Array<object>,recipientCount:number}}
   */
  buildReleasePackage(advisoryId) {
    const advisory = this._requireAdvisory(advisoryId);
    const evaluation = this.evaluateRelease(advisoryId);
    if (!evaluation.releasable) {
      throw new AdvisoryError('RELEASE_BLOCKED', '通告放行评估未通过', {
        advisoryId,
        reasons: evaluation.reasons,
      });
    }

    const paragraphs = advisory.sectionIds.map((sectionId) => {
      const section = this._requireSection(sectionId);
      const allowed = this._allowedRecipientsFor(advisory, section);
      if (allowed.length === 0) {
        throw new AdvisoryError(
          'NO_ALLOWED_RECIPIENTS',
          `段落 ${sectionId} 没有任何获准查看该产品的有效收件人`,
          { sectionId, productId: section.productId },
        );
      }
      return {
        sectionId,
        productId: section.productId,
        fixVersion: section.fixVersion,
        revision: section.revision,
        digest: sectionDigest(section.productId, section.body, section.fixVersion),
        recipientIds: allowed.map((r) => r.id),
      };
    });

    return {
      advisoryId: advisory.id,
      title: advisory.title,
      publishAt: advisory.publishAt,
      generatedAt: this._timestamp(),
      paragraphs,
      recipientCount: advisory.recipients.length,
    };
  }

  /**
   * 发送通告：通过放行评估后为每个“收件人 × 获准段落”生成 queued 副本，
   * 并冻结通告、记录追溯快照（内容摘要 + 原始收件范围）。
   *
   * @param {string} advisoryId
   * @returns {{advisory:object, releasePackage:object, enqueuedCopies:Array<object>}}
   */
  send(advisoryId) {
    const advisory = this._requireAdvisory(advisoryId);
    this._assertDraft(advisory);
    const releasePackage = this.buildReleasePackage(advisoryId);

    const enqueued = [];
    for (const sectionId of advisory.sectionIds) {
      const section = this._requireSection(sectionId);
      for (const recipient of this._allowedRecipientsFor(advisory, section)) {
        const key = `${advisory.id}:${recipient.id}:${sectionId}`;
        const copy = {
          copyId: `cpy_${randomUUID()}`,
          key,
          advisoryId: advisory.id,
          sectionId,
          recipientId: recipient.id,
          productId: section.productId,
          status: 'queued',
          digest: sectionDigest(section.productId, section.body, section.fixVersion),
          enqueuedAt: this._timestamp(),
          deliveredAt: null,
          voidedAt: null,
          voidReason: null,
        };
        this._copies.set(key, copy);
        enqueued.push({ ...copy });
      }
    }

    advisory.status = 'sent';
    advisory.sentAt = this._timestamp();
    advisory.sentSnapshot = {
      title: advisory.title,
      publishAt: advisory.publishAt,
      sentAt: advisory.sentAt,
      // 原始收件范围快照：后续移除收件人不会改写这里
      recipientScope: advisory.recipients.map((r) => ({
        id: r.id,
        productScope: [...r.productScope],
      })),
      sections: advisory.sectionIds.map((sectionId) => {
        const section = this._requireSection(sectionId);
        return {
          sectionId,
          productId: section.productId,
          body: section.body,
          fixVersion: section.fixVersion,
          revision: section.revision,
          digest: sectionDigest(section.productId, section.body, section.fixVersion),
        };
      }),
    };

    return {
      advisory: this._advisoryView(advisory),
      releasePackage,
      enqueuedCopies: enqueued,
    };
  }

  /**
   * 副本出队（标记 delivered）。已作废副本拒绝出队。
   *
   * @param {string} advisoryId
   * @param {string} recipientId
   * @param {string} sectionId
   * @returns {object}
   */
  dequeueCopy(advisoryId, recipientId, sectionId) {
    const key = `${advisoryId}:${recipientId}:${sectionId}`;
    const copy = this._copies.get(key);
    if (!copy) {
      throw new AdvisoryError('UNKNOWN_COPY', '副本不存在', {
        advisoryId, recipientId, sectionId,
      });
    }
    if (copy.status === 'delivered') {
      throw new AdvisoryError('COPY_NOT_QUEUED', '副本已出队，不能重复出队', {
        copyId: copy.copyId,
      });
    }
    if (copy.status === 'voided') {
      throw new AdvisoryError('COPY_VOIDED', '副本已作废，禁止出队', {
        copyId: copy.copyId,
      });
    }
    copy.status = 'delivered';
    copy.deliveredAt = this._timestamp();
    return { ...copy };
  }

  /**
   * 查看控制：合作方在约定时间内只能看获准的产品段落。
   *
   * @param {string} advisoryId
   * @param {string} recipientId
   * @param {string} sectionId
   * @returns {{allowed:boolean, reason?:string, sectionId:string, recipientId:string}}
   */
  canViewSection(advisoryId, recipientId, sectionId) {
    const advisory = this._requireAdvisory(advisoryId);
    if (!advisory.sectionIds.includes(sectionId)) {
      throw new AdvisoryError('SECTION_NOT_IN_ADVISORY', '段落不属于该通告', {
        advisoryId, sectionId,
      });
    }
    const section = this._requireSection(sectionId);
    const recipient = advisory.recipients.find((r) => r.id === recipientId);
    if (!recipient) {
      return {
        allowed: false,
        reason: '收件人不在通告受控范围内（可能已被移除）',
        sectionId,
        recipientId,
      };
    }
    if (!recipient.productScope.includes(section.productId)) {
      return {
        allowed: false,
        reason: '越权访问：该收件人未获准查看此产品段落',
        sectionId,
        recipientId,
      };
    }
    if (this._now().getTime() < new Date(advisory.publishAt).getTime()) {
      return {
        allowed: false,
        reason: '尚未到达约定公开时间',
        sectionId,
        recipientId,
      };
    }
    return { allowed: true, sectionId, recipientId };
  }

  /**
   * 已发送版本追溯快照：原内容摘要与原始收件范围。
   *
   * @param {string} advisoryId
   * @returns {object}
   */
  getSentSnapshot(advisoryId) {
    const advisory = this._requireAdvisory(advisoryId);
    if (advisory.status !== 'sent' || !advisory.sentSnapshot) {
      throw new AdvisoryError('NOT_SENT', '通告尚未发送，无追溯快照', { advisoryId });
    }
    return JSON.parse(JSON.stringify(advisory.sentSnapshot));
  }

  /**
   * 列出某通告下的全部副本（含 queued / delivered / voided 状态）。
   *
   * @param {string} advisoryId
   * @returns {Array<object>}
   */
  listCopies(advisoryId) {
    this._requireAdvisory(advisoryId);
    return [...this._copies.values()]
      .filter((c) => c.advisoryId === advisoryId)
      .map((c) => ({ ...c }));
  }

  /**
   * @param {object} advisory
   * @returns {object} 通告只读视图（Map 转为普通结构，避免泄漏内部对象）
   */
  _advisoryView(advisory) {
    return {
      id: advisory.id,
      title: advisory.title,
      publishAt: advisory.publishAt,
      status: advisory.status,
      sectionIds: [...advisory.sectionIds],
      recipients: advisory.recipients.map((r) => ({
        id: r.id,
        productScope: [...r.productScope],
        addedAt: r.addedAt,
      })),
      confirmations: [...advisory.confirmations.entries()].map(([sid, c]) => ({ ...c })),
      createdAt: advisory.createdAt,
      sentAt: advisory.sentAt,
    };
  }

  getAdvisory(advisoryId) {
    return this._advisoryView(this._requireAdvisory(advisoryId));
  }
}
