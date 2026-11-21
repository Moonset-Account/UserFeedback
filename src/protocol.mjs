// 路由协议层：定义节点返回给客户端的路由信号，以及目标节点 B 认可的
// 「一次性指示」票据。所有信号都是普通对象/类实例，不涉及任何网络 IO。

import { randomUUID } from 'node:crypto';

/**
 * 一次性指示的基类。
 * 每张票据只对一个槽、一次请求有效；consume() 成功即作废，重复使用抛错。
 */
export class OneTimeTicket {
  #used = false;

  constructor(slot, targetNodeId, kind) {
    this.id = randomUUID();
    this.slot = slot;
    this.targetNodeId = targetNodeId;
    this.kind = kind; // 'ask' | 'import-grant'
  }

  get used() {
    return this.#used;
  }

  /**
   * 校验并消费票据。校验失败或二次使用都抛出异常，B 拒绝请求。
   * @param {number} slot
   * @param {string} targetNodeId
   * @param {string} expectedKind
   */
  consume(slot, targetNodeId, expectedKind) {
    if (this.#used) {
      throw new OneTimeTicketRejectedError(
        `一次性${this.kind === 'ask' ? ' ASK 指示' : '迁入许可'}已被使用，不得复用（票据 ${this.id}）`,
      );
    }
    if (this.kind !== expectedKind || this.slot !== slot || this.targetNodeId !== targetNodeId) {
      this.#used = true; // 错用同样作废，防止拿 A 槽票据反复试探
      throw new OneTimeTicketRejectedError('一次性指示与目标节点/槽/用途不匹配');
    }
    this.#used = true;
  }
}

/**
 * ASK 一次性指示：客户端被 A 重定向到 B 后，随本次请求出示。
 * 只让这一次请求穿透 B 的 IMPORTING 准入限制，不改变槽表。
 */
export class AskTicket extends OneTimeTicket {
  constructor(slot, targetNodeId) {
    super(slot, targetNodeId, 'ask');
  }
}

/**
 * 迁入许可：协调器在一次原子搬键前向 B 申请，B 只凭它接受 importKey。
 * 一次许可对应一个键的迁入，用后即焚。
 */
export class ImportGrant extends OneTimeTicket {
  constructor(slot, targetNodeId) {
    super(slot, targetNodeId, 'import-grant');
  }
}

/** 路由信号基类：携带槽与目标节点，供客户端决定下一步动作。 */
export class RoutingSignal extends Error {
  constructor(message, slot, targetNodeId) {
    super(message);
    this.name = 'RoutingSignal';
    this.slot = slot;
    this.targetNodeId = targetNodeId;
  }
}

/**
 * ASK：一次性重定向。仅对「这一次请求」有效，客户端重试一次，
 * 重试时携带 AskTicket，且不得更新本地槽表。
 */
export class AskRedirect extends RoutingSignal {
  constructor(slot, targetNodeId) {
    super(`ASK slot ${slot} -> 节点 ${targetNodeId}（一次性重定向，重试一次，勿更新槽表）`, slot, targetNodeId);
    this.name = 'AskRedirect';
  }
}

/**
 * MOVED：永久重定向。槽主已显式切换，客户端应更新本地槽表后再重试，
 * 后续请求直接发往新槽主。
 */
export class MovedRedirect extends RoutingSignal {
  constructor(slot, targetNodeId) {
    super(`MOVED slot ${slot} -> 节点 ${targetNodeId}（永久重定向，请更新槽表）`, slot, targetNodeId);
    this.name = 'MovedRedirect';
  }
}

/**
 * TRYAGAIN：多键读的键在搬迁两侧都有归属，暂缓整个请求，不返回任何部分值。
 */
export class TryAgain extends Error {
  constructor(slot, localKeys, remoteKeys) {
    super(`TRYAGAIN slot ${slot}：多键读跨越搬迁两侧（本地 ${localKeys.length} 个 / 对侧 ${remoteKeys.length} 个），暂缓且不返回部分值`);
    this.name = 'TryAgain';
    this.slot = slot;
    this.localKeys = [...localKeys];
    this.remoteKeys = [...remoteKeys];
  }
}

/** B 在 IMPORTING 状态收到没有有效一次性指示的请求。 */
export class OneTimeTicketRejectedError extends Error {
  constructor(message) {
    super(message);
    this.name = 'OneTimeTicketRejectedError';
  }
}

/** 多键操作中的键不属于同一个散列槽。 */
export class CrossSlotError extends Error {
  constructor(slots) {
    super(`CROSSSLOT：多键操作要求所有键位于同一槽，实际涉及槽 ${[...slots].join(', ')}`);
    this.name = 'CrossSlotError';
    this.slots = [...slots];
  }
}

/** 节点在当前槽状态下不允许该操作（例如向非 IMPORTING 节点请求迁入）。 */
export class WrongSlotStateError extends Error {
  constructor(message) {
    super(message);
    this.name = 'WrongSlotStateError';
  }
}

/** 迁入目标已存在同键：导入失败，保证「先删源」不会在失败时发生（原子性）。 */
export class ImportConflictError extends Error {
  constructor(key, nodeId) {
    super(`IMPORT CONFLICT：节点 ${nodeId} 已存在键 ${key}，拒绝迁入`);
    this.name = 'ImportConflictError';
    this.key = key;
    this.nodeId = nodeId;
  }
}
