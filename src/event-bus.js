/**
 * 层级主题事件总线（纯本地、无网络、无外部依赖）。
 *
 * 主题规则：
 * - 主题与过滤器均以点号分层，例如 "order.created.eu"。
 * - "*" 匹配且仅匹配一层，例如 "a.*.c" 匹配 "a.b.c"，不匹配 "a.b" 或 "a.x.b.c"。
 * - "#" 只能出现在过滤器末尾（单独作为过滤器或在最后一层），
 *   匹配零层或多层，例如 "a.#" 匹配 "a"、"a.b"、"a.b.c"。
 *
 * 投递规则：
 * - 普通订阅者可挂多条过滤器，一次发布即使多条规则同时命中，也只投一份。
 * - 共享组的一次命中只投给一个有空位的成员；成员按固定顺序，从上次成功者
 *   之后一位开始轮转查找（满员成员跳过），找到第一个有空位的成员，成功后
 *   轮转指针停在该成员。
 * - 一次发布被接受的条件：所有命中的普通订阅者都有空位，且每个命中的共享组
 *   都能选出有空位的成员。任一条件不满足则整次发布暂缓：
 *     1. 不向任何收件箱写入；
 *     2. 任何共享组轮转指针都不前进；
 *     3. 发布按 FIFO 进入暂缓队列，等待 release 触发重试。
 * - 释放慢订阅者（腾出一个收件箱空位）后重试，共享组仍按其“原本应得的轮次”
 *   投递，因为暂缓期间指针从未移动。
 */

'use strict';

/** 过滤器在订阅阶段完成的结构性校验，非法过滤器直接抛错拒绝订阅。 */
function validateFilter(filter) {
  if (typeof filter !== 'string' || filter.length === 0) {
    throw new Error(`非法过滤器: ${String(filter)}（必须是非空字符串）`);
  }
  const levels = filter.split('.');
  for (let i = 0; i < levels.length; i += 1) {
    const level = levels[i];
    if (level === '') {
      throw new Error(`非法过滤器: ${filter}（存在空层级）`);
    }
    // "#" 只能位于最后一层。
    if (level === '#' && i !== levels.length - 1) {
      throw new Error(`非法过滤器: ${filter}（# 只能位于过滤器末尾）`);
    }
  }
  return levels;
}

/** 主题层级匹配；filterLevels 已通过 validateFilter 校验。 */
function matchLevels(topicLevels, filterLevels) {
  const last = filterLevels[filterLevels.length - 1];

  if (last === '#') {
    // 末尾 #：匹配零层或多层，前面的层级必须逐字相等。
    const prefixLength = filterLevels.length - 1;
    if (topicLevels.length < prefixLength) {
      return false;
    }
    for (let i = 0; i < prefixLength; i += 1) {
      if (filterLevels[i] !== topicLevels[i]) {
        return false;
      }
    }
    return true;
  }

  // 无 #：层级数必须相等，逐字匹配或命中单层 "*"。
  if (topicLevels.length !== filterLevels.length) {
    return false;
  }
  for (let i = 0; i < filterLevels.length; i += 1) {
    const f = filterLevels[i];
    if (f !== '*' && f !== topicLevels[i]) {
      return false;
    }
  }
  return true;
}

function validateTopic(topic) {
  if (typeof topic !== 'string' || topic.length === 0) {
    throw new Error(`非法主题: ${String(topic)}（必须是非空字符串）`);
  }
  const levels = topic.split('.');
  for (const level of levels) {
    if (level === '') {
      throw new Error(`非法主题: ${topic}（存在空层级）`);
    }
    if (level === '*' || level === '#') {
      throw new Error(`非法主题: ${topic}（发布主题不得包含通配符）`);
    }
  }
  return levels;
}

function validateId(id, label) {
  if (typeof id !== 'string' || id.length === 0) {
    throw new Error(`非法${label} ID: ${String(id)}`);
  }
  return id;
}

function validateCapacity(capacity, label) {
  if (!Number.isInteger(capacity) || capacity < 1) {
    throw new Error(`非法${label}容量: ${String(capacity)}（必须是 >= 1 的整数）`);
  }
  return capacity;
}

class EventBus {
  #nextMessageId = 1;
  /** 普通订阅者：id -> { id, capacity, filters: string[][], inbox: object[] } */
  #subscribers = new Map();
  /** 共享组：id -> { members, memberOrder, filterLevels, cursor } */
  #groups = new Map();
  /** 暂缓队列（FIFO）：每项为 { message, topicLevels } */
  #pending = [];

  /**
   * 订阅普通订阅者。订阅者不存在时按 capacity 创建（默认容量 1）；
   * 已存在时为其追加一条过滤器。同一订阅者可挂多条过滤器。
   */
  subscribe(subscriberId, filter, capacity = 1) {
    validateId(subscriberId, '订阅者');
    const levels = validateFilter(filter);
    let subscriber = this.#subscribers.get(subscriberId);
    if (!subscriber) {
      validateCapacity(capacity, '订阅者');
      subscriber = {
        id: subscriberId,
        capacity,
        filters: [],
        inbox: [],
      };
      this.#subscribers.set(subscriberId, subscriber);
    }
    subscriber.filters.push(levels);
    return this;
  }

  /**
   * 创建共享组。
   * @param {string} groupId
   * @param {Array<{ id: string, capacity?: number }>} members 成员固定顺序即数组顺序；
   *        容量可统一用 defaultCapacity，或在各成员上单独给出。
   * @param {number} defaultCapacity 成员未显式给出容量时使用的默认值（默认 1）。
   */
  createSharedGroup(groupId, members, defaultCapacity = 1) {
    validateId(groupId, '共享组');
    if (this.#groups.has(groupId)) {
      throw new Error(`共享组已存在: ${groupId}`);
    }
    if (!Array.isArray(members) || members.length === 0) {
      throw new Error(`共享组 ${groupId} 至少需要一个成员`);
    }
    validateCapacity(defaultCapacity, '默认成员');
    const memberMap = new Map();
    for (const member of members) {
      if (!member || typeof member.id !== 'string' || member.id.length === 0) {
        throw new Error(`共享组 ${groupId} 存在非法成员定义`);
      }
      if (memberMap.has(member.id)) {
        throw new Error(`共享组 ${groupId} 成员 ID 重复: ${member.id}`);
      }
      const capacity = member.capacity === undefined ? defaultCapacity : member.capacity;
      validateCapacity(capacity, `成员 ${member.id}`);
      memberMap.set(member.id, { id: member.id, capacity, inbox: [] });
    }
    this.#groups.set(groupId, {
      id: groupId,
      members: memberMap,
      memberOrder: members.map((m) => m.id),
      filterLevels: null,
      // -1 表示尚无成功记录，首次投递从 cursor+1 即首位成员开始。
      cursor: -1,
    });
    return this;
  }

  /** 为共享组挂载过滤器（共享组按组持有一份订阅规则）。 */
  subscribeSharedGroup(groupId, filter) {
    validateId(groupId, '共享组');
    const group = this.#groups.get(groupId);
    if (!group) {
      throw new Error(`共享组不存在: ${groupId}`);
    }
    group.filterLevels = validateFilter(filter);
    return this;
  }

  /**
   * 发布消息。
   * 所有发布（含当场可接受的）统一进入 FIFO 队列后尝试投递：
   * 队首可接受才真正写入收件箱并出队；否则整次暂缓，状态原地不动。
   * @returns {{ status: 'accepted'|'deferred', messageId: number, queueLength: number, reason?: string }}
   */
  publish(topic, payload = null) {
    const topicLevels = validateTopic(topic);
    const message = {
      id: this.#nextMessageId++,
      topic,
      payload,
    };
    this.#pending.push({ message, topicLevels });
    const result = this.#tryHead();
    return {
      status: result.accepted ? 'accepted' : 'deferred',
      messageId: message.id,
      queueLength: this.#pending.length,
      ...(result.accepted ? {} : { reason: result.reason }),
    };
  }

  /**
   * 尝试投递队首发布。先完成全部可行性计算，再统一提交，保证原子性：
   * 任何一个命中方没有空位时，不写入任何收件箱、不移动任何轮转指针。
   */
  #tryHead() {
    if (this.#pending.length === 0) {
      return { accepted: true };
    }
    const { message, topicLevels } = this.#pending[0];
    const deliveries = []; // { owner: 收件箱对象(含 capacity/inbox) }
    const reasonParts = [];

    // 1) 普通订阅者：多条过滤器命中只算一份。
    for (const subscriber of this.#subscribers.values()) {
      const hit = subscriber.filters.some((filterLevels) =>
        matchLevels(topicLevels, filterLevels),
      );
      if (!hit) {
        continue;
      }
      if (subscriber.inbox.length >= subscriber.capacity) {
        reasonParts.push(`普通订阅者 ${subscriber.id} 无空位`);
        continue;
      }
      deliveries.push({ owner: subscriber });
    }

    // 2) 共享组：命中的组必须能从固定顺序中选出一个有空位的成员。
    const groupChoices = []; // { group, member, index }
    for (const group of this.#groups.values()) {
      if (!group.filterLevels || !matchLevels(topicLevels, group.filterLevels)) {
        continue;
      }
      const count = group.memberOrder.length;
      let chosen = null;
      // 从上次成功者“之后一位”（cursor + 1）开始，按固定顺序环形扫描，
      // 选出第一个有空位的成员；全部满员则本次命中无法投递。
      for (let step = 1; step <= count; step += 1) {
        const index = (group.cursor + step) % count;
        const member = group.members.get(group.memberOrder[index]);
        if (member.inbox.length < member.capacity) {
          chosen = { member, index };
          break;
        }
      }
      if (!chosen) {
        reasonParts.push(`共享组 ${group.id} 无可用成员`);
        continue;
      }
      groupChoices.push({ group, member: chosen.member, index: chosen.index });
      deliveries.push({ owner: chosen.member });
    }

    // 3) 存在任何受阻命中方：整次暂缓，所有状态保持原样。
    if (reasonParts.length > 0) {
      return { accepted: false, reason: reasonParts.join('; ') };
    }

    // 4) 全部可行：统一提交（普通订阅者去重天然保证每人至多一份）。
    for (const item of deliveries) {
      item.owner.inbox.push(message);
    }
    for (const choice of groupChoices) {
      choice.group.cursor = choice.index;
    }
    this.#pending.shift();
    return { accepted: true };
  }

  /**
   * 按 FIFO 重试暂缓队列；队首仍受阻则停止（保留后续发布的原始顺序）。
   * release 操作会自动调用，也可手动调用。
   * @returns {{ processed: number, remaining: number }}
   */
  retryPending() {
    let processed = 0;
    while (this.#pending.length > 0) {
      const result = this.#tryHead();
      if (!result.accepted) {
        break;
      }
      processed += 1;
    }
    return { processed, remaining: this.#pending.length };
  }

  /** 释放（确认）普通订阅者最旧的一条在途消息，腾出一个空位并触发重试。 */
  release(subscriberId) {
    validateId(subscriberId, '订阅者');
    const subscriber = this.#subscribers.get(subscriberId);
    if (!subscriber) {
      throw new Error(`订阅者不存在: ${subscriberId}`);
    }
    const message = subscriber.inbox.shift() ?? null;
    const retry = this.retryPending();
    return { released: message, retry };
  }

  /** 释放共享组成员最旧的一条在途消息，腾出一个空位并触发重试。 */
  releaseGroupMember(groupId, memberId) {
    validateId(groupId, '共享组');
    validateId(memberId, '成员');
    const group = this.#groups.get(groupId);
    if (!group) {
      throw new Error(`共享组不存在: ${groupId}`);
    }
    const member = group.members.get(memberId);
    if (!member) {
      throw new Error(`共享组 ${groupId} 中不存在成员: ${memberId}`);
    }
    const message = member.inbox.shift() ?? null;
    const retry = this.retryPending();
    return { released: message, retry };
  }

  // ---- 只读检查接口（便于验收与诊断，不改变任何状态）----

  /** 普通订阅者收件箱快照（按入箱顺序的消息 ID 列表）。 */
  inboxOf(subscriberId) {
    const subscriber = this.#subscribers.get(subscriberId);
    if (!subscriber) {
      throw new Error(`订阅者不存在: ${subscriberId}`);
    }
    return subscriber.inbox.map((m) => m.id);
  }

  /** 共享组成员收件箱快照。 */
  memberInboxOf(groupId, memberId) {
    const group = this.#groups.get(groupId);
    if (!group) {
      throw new Error(`共享组不存在: ${groupId}`);
    }
    const member = group.members.get(memberId);
    if (!member) {
      throw new Error(`共享组 ${groupId} 中不存在成员: ${memberId}`);
    }
    return member.inbox.map((m) => m.id);
  }

  /** 共享组当前轮转指针（上次成功成员的下标；-1 表示尚无成功记录）。 */
  cursorOf(groupId) {
    const group = this.#requireGroup(groupId);
    return group.cursor;
  }

  /** 共享组固定成员顺序。 */
  memberOrderOf(groupId) {
    const group = this.#requireGroup(groupId);
    return [...group.memberOrder];
  }

  /** 暂缓队列中的消息 ID（FIFO 顺序）。 */
  pendingMessageIds() {
    return this.#pending.map((entry) => entry.message.id);
  }

  #requireGroup(groupId) {
    const group = this.#groups.get(groupId);
    if (!group) {
      throw new Error(`共享组不存在: ${groupId}`);
    }
    return group;
  }
}

export { EventBus, matchLevels, validateFilter };
