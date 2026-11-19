/**
 * 事件响应桌面演练 · 线索投放器
 *
 * 核心概念：
 *  - Card   线索卡定义：可见席位、解锁所需决定、前置阅知要求
 *  - Branch 演练分支：有序事件日志（投放 / 决定 / 阅知）
 *  - Fork   从某次决定之前另开分支，继承当时已发生的投放与阅知
 *
 * 可见性规则（席位视角）：
 *  一张卡对席位 S 可见，当且仅当：
 *    1. 该卡已在当前分支投放；
 *    2. S 在卡的可见席位列表中；
 *    3. 卡声明的每一个解锁决定（含选项限定）都已在当前分支发生；
 *    4. 若卡声明了前置阅知要求，指定组已在当前分支确认看过指定卡。
 *  未满足条件的卡（包括因其他组隐藏决定而未解锁的卡）不会以任何形式
 *  （含卡名）出现在席位查询结果中。
 */

export class ClueDropperError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'ClueDropperError';
    this.code = code;
  }
}

const EV_DROP = 'drop';
const EV_DECISION = 'decision';
const EV_ACK = 'ack';

export class ClueDropper {
  #cards = new Map();
  #branches = new Map();

  /**
   * 登记线索卡。
   * @param {object} def
   * @param {string} def.id           卡 ID（唯一）
   * @param {string} def.name         卡名
   * @param {string[]} def.seats      可见席位（非空）
   * @param {Array<string|{decision:string, option?:string}>} [def.requiresDecisions]
   *        解锁所需决定；字符串形式只要求决定发生，对象形式可限定选项
   * @param {{group:string, card:string}} [def.requiresAck]
   *        前置阅知：指定组须已确认看过指定卡
   */
  addCard(def) {
    const { id, name, seats, requiresDecisions = [], requiresAck = null } = def ?? {};
    if (typeof id !== 'string' || id.length === 0) {
      throw new ClueDropperError('INVALID_CARD', 'card id must be a non-empty string');
    }
    if (this.#cards.has(id)) {
      throw new ClueDropperError('DUPLICATE_CARD', `card already registered: ${id}`);
    }
    if (typeof name !== 'string' || name.length === 0) {
      throw new ClueDropperError('INVALID_CARD', `card ${id}: name must be a non-empty string`);
    }
    if (!Array.isArray(seats) || seats.length === 0 || seats.some((s) => typeof s !== 'string' || s.length === 0)) {
      throw new ClueDropperError('INVALID_CARD', `card ${id}: seats must be a non-empty string array`);
    }
    const normalizedReqs = requiresDecisions.map((r) => {
      if (typeof r === 'string' && r.length > 0) return { decision: r, option: undefined };
      if (r && typeof r.decision === 'string' && r.decision.length > 0) {
        return { decision: r.decision, option: r.option };
      }
      throw new ClueDropperError('INVALID_CARD', `card ${id}: invalid requiresDecisions entry`);
    });
    if (requiresAck !== null) {
      if (typeof requiresAck.group !== 'string' || typeof requiresAck.card !== 'string') {
        throw new ClueDropperError('INVALID_CARD', `card ${id}: requiresAck needs {group, card}`);
      }
    }
    this.#cards.set(id, Object.freeze({
      id,
      name,
      seats: Object.freeze([...seats]),
      requiresDecisions: Object.freeze(normalizedReqs),
      requiresAck: requiresAck === null ? null : Object.freeze({ ...requiresAck }),
    }));
    return this;
  }

  /** 创建初始分支。 */
  createBranch(branchId) {
    if (this.#branches.has(branchId)) {
      throw new ClueDropperError('DUPLICATE_BRANCH', `branch already exists: ${branchId}`);
    }
    this.#branches.set(branchId, { id: branchId, events: [] });
    return this;
  }

  /**
   * 从某次决定之前另开分支。
   * 新分支继承分叉点之前已发生的全部事件（投放、阅知、更早的决定），
   * 不含该决定本身及其后的任何事件；之后两条分支各自推进。
   */
  fork(sourceBranchId, newBranchId, { beforeDecision }) {
    const source = this.#branch(sourceBranchId);
    if (this.#branches.has(newBranchId)) {
      throw new ClueDropperError('DUPLICATE_BRANCH', `branch already exists: ${newBranchId}`);
    }
    const cut = source.events.findIndex((e) => e.type === EV_DECISION && e.decision === beforeDecision);
    if (cut === -1) {
      throw new ClueDropperError('DECISION_NOT_FOUND', `branch ${sourceBranchId} has no decision: ${beforeDecision}`);
    }
    this.#branches.set(newBranchId, {
      id: newBranchId,
      events: source.events.slice(0, cut).map((e) => ({ ...e })),
    });
    return this;
  }

  /** 导演在指定分支投放一张卡。 */
  drop(branchId, cardId) {
    const branch = this.#branch(branchId);
    const card = this.#card(cardId);
    if (branch.events.some((e) => e.type === EV_DROP && e.card === card.id)) {
      throw new ClueDropperError('DUPLICATE_DROP', `card ${card.id} already dropped on branch ${branchId}`);
    }
    branch.events.push({ type: EV_DROP, card: card.id });
    return this;
  }

  /**
   * 某组在指定分支作出决定。同一分支上同一决定只能作出一次，
   * 不同分支可以作出不同选择（如 isolate=host 与 isolate=network）。
   */
  decide(branchId, decision, option, { by } = {}) {
    const branch = this.#branch(branchId);
    if (branch.events.some((e) => e.type === EV_DECISION && e.decision === decision)) {
      throw new ClueDropperError('DECISION_ALREADY_MADE', `decision ${decision} already made on branch ${branchId}`);
    }
    branch.events.push({ type: EV_DECISION, decision, option, by: by ?? null });
    return this;
  }

  /**
   * 某组确认看过一张卡（阅知）。
   * 仅当该卡当前对该组真实可见时才接受，防止伪造阅知来解锁下游卡。
   */
  ack(branchId, group, cardId) {
    const branch = this.#branch(branchId);
    const card = this.#card(cardId);
    if (branch.events.some((e) => e.type === EV_ACK && e.group === group && e.card === card.id)) {
      throw new ClueDropperError('DUPLICATE_ACK', `group ${group} already acked card ${card.id} on branch ${branchId}`);
    }
    if (!this.#visibleTo(branch, card, group)) {
      throw new ClueDropperError('ACK_REJECTED', `card is not currently visible to group ${group}; ack rejected`);
    }
    branch.events.push({ type: EV_ACK, group, card: card.id });
    return this;
  }

  /**
   * 席位查询：只返回本组已投放且已解锁的卡。
   * 未解锁的卡（含因其他组隐藏决定而锁定的卡）连卡名都不会出现。
   * @returns {Array<{id:string, name:string}>} 按投放顺序
   */
  visibleCards(branchId, seat) {
    const branch = this.#branch(branchId);
    const out = [];
    for (const e of branch.events) {
      if (e.type !== EV_DROP) continue;
      const card = this.#cards.get(e.card);
      if (this.#visibleTo(branch, card, seat)) {
        out.push({ id: card.id, name: card.name });
      }
    }
    return out;
  }

  /**
   * 席位重放：按每张卡对该席位真正变得可见的时间顺序回放，
   * 得到该席位当时真正掌握的线索序列。
   * @returns {Array<{id:string, name:string, visibleAt:number}>}
   */
  replay(branchId, seat) {
    const branch = this.#branch(branchId);
    const out = [];
    branch.events.forEach((e, dropIndex) => {
      if (e.type !== EV_DROP) return;
      const card = this.#cards.get(e.card);
      if (!this.#visibleTo(branch, card, seat)) return;
      out.push({ id: card.id, name: card.name, visibleAt: this.#visibleAt(branch, card, dropIndex) });
    });
    out.sort((a, b) => a.visibleAt - b.visibleAt);
    return out;
  }

  // ---- 内部 ----

  #branch(branchId) {
    const branch = this.#branches.get(branchId);
    if (!branch) throw new ClueDropperError('UNKNOWN_BRANCH', `unknown branch: ${branchId}`);
    return branch;
  }

  #card(cardId) {
    const card = this.#cards.get(cardId);
    if (!card) throw new ClueDropperError('UNKNOWN_CARD', `unknown card: ${cardId}`);
    return card;
  }

  /** 卡当前是否对席位可见（已投放由调用方保证或另行判断）。 */
  #visibleTo(branch, card, seat) {
    if (!card.seats.includes(seat)) return false;
    if (!branch.events.some((e) => e.type === EV_DROP && e.card === card.id)) return false;
    return this.#unlocked(branch, card);
  }

  /** 卡的解锁条件（决定 + 前置阅知）是否在当前分支全部满足。 */
  #unlocked(branch, card) {
    for (const req of card.requiresDecisions) {
      const met = branch.events.some((e) =>
        e.type === EV_DECISION &&
        e.decision === req.decision &&
        (req.option === undefined || e.option === req.option));
      if (!met) return false;
    }
    if (card.requiresAck !== null) {
      const { group, card: prevCard } = card.requiresAck;
      const met = branch.events.some((e) =>
        e.type === EV_ACK && e.group === group && e.card === prevCard);
      if (!met) return false;
    }
    return true;
  }

  /** 卡对席位变得可见的事件序号：投放、所需决定、所需阅知中最晚者。 */
  #visibleAt(branch, card, dropIndex) {
    let at = dropIndex;
    for (const req of card.requiresDecisions) {
      const idx = branch.events.findIndex((e) =>
        e.type === EV_DECISION &&
        e.decision === req.decision &&
        (req.option === undefined || e.option === req.option));
      if (idx > at) at = idx;
    }
    if (card.requiresAck !== null) {
      const { group, card: prevCard } = card.requiresAck;
      const idx = branch.events.findIndex((e) =>
        e.type === EV_ACK && e.group === group && e.card === prevCard);
      if (idx > at) at = idx;
    }
    return at;
  }
}

export function createClueDropper() {
  return new ClueDropper();
}
