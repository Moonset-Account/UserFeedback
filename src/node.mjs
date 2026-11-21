// 集群节点：内置键存储与槽状态机，实现 Redis Cluster 风格的
// MIGRATING（源 A）/ IMPORTING（目标 B）准入与重定向规则。
//
// 槽状态（每个槽在每个节点上互斥）：
//   owner      本节点是槽主，正常服务
//   migrating  本节点是源 A，槽正在逐键搬出；只服务尚留在本地的键
//   importing  本节点是目标 B，槽正在逐键迁入；只凭一次性指示接受请求
//   foreign    槽不归本节点（搬迁前的错误节点，或 A 已显式交出槽主）

import { keySlot } from './hash.mjs';
import {
  AskRedirect,
  AskTicket,
  ImportConflictError,
  ImportGrant,
  MovedRedirect,
  OneTimeTicketRejectedError,
  TryAgain,
  WrongSlotStateError,
} from './protocol.mjs';

export class Node {
  #id;
  #store = new Map();
  #ownSlots = new Set();
  #migrating = new Map(); // slot -> targetNodeId
  #importing = new Map(); // slot -> sourceNodeId
  #emigrated = new Map(); // slot -> Set<key>，已搬出的键（可观测/测试用）
  #imported = new Map(); // slot -> Set<key>，已迁入的键
  #askTickets = new Set();
  #importGrants = new Set();
  #foreignHints = new Map(); // slot -> 槽主节点 id（A 交出槽主后用于 MOVED）
  #resolveOwner = null;

  constructor(id) {
    this.#id = id;
  }

  get id() {
    return this.#id;
  }

  /** 由集群注册时注入槽主查询函数，用于对 foreign 槽给出 MOVED 目标。 */
  bindOwnerResolver(resolver) {
    this.#resolveOwner = resolver;
  }

  /** 向某节点预置数据（仅合成数据/测试用，绕过路由准入，不经过票据）。 */
  putLocal(key, value) {
    this.#store.set(key, value);
  }

  // ---------- 槽生命周期 ----------

  /** 集群把槽初始分配给本节点。 */
  assignSlot(slot) {
    this.#ownSlots.add(slot);
  }

  /** A：进入 MIGRATING，记录目标 B。 */
  beginMigrating(slot, targetNodeId) {
    if (!this.#ownSlots.has(slot)) {
      throw new WrongSlotStateError(`节点 ${this.#id} 不是槽 ${slot} 的槽主，无法开始搬迁`);
    }
    this.#ownSlots.delete(slot);
    this.#migrating.set(slot, targetNodeId);
    this.#emigrated.set(slot, new Set());
  }

  /** B：进入 IMPORTING，记录源 A。 */
  beginImporting(slot, sourceNodeId) {
    if (this.#ownSlots.has(slot) || this.#importing.has(slot) || this.#migrating.has(slot)) {
      throw new WrongSlotStateError(`节点 ${this.#id} 无法对槽 ${slot} 进入 IMPORTING（状态冲突）`);
    }
    this.#importing.set(slot, sourceNodeId);
    this.#imported.set(slot, new Set());
  }

  /** B：最后一个键迁入后，显式接管为槽主。 */
  completeImport(slot) {
    if (!this.#importing.has(slot)) {
      throw new WrongSlotStateError(`节点 ${this.#id} 的槽 ${slot} 不处于 IMPORTING`);
    }
    this.#importing.delete(slot);
    this.#ownSlots.add(slot);
  }

  /** A：确认本槽源键已全部搬走，显式交出槽主身份；此后一律永久重定向。 */
  completeMigration(slot) {
    if (!this.#migrating.has(slot)) {
      throw new WrongSlotStateError(`节点 ${this.#id} 的槽 ${slot} 不处于 MIGRATING`);
    }
    const remaining = this.slotKeyCount(slot);
    if (remaining > 0) {
      throw new WrongSlotStateError(`槽 ${slot} 还有 ${remaining} 个源键未搬迁，不能切换槽主`);
    }
    const target = this.#migrating.get(slot);
    this.#migrating.delete(slot);
    this.#foreignHints.set(slot, target);
  }

  // ---------- 一次性指示的签发 ----------

  /** A 对一个不归它服务的请求签发 ASK：只让客户端在 B 试这一次。 */
  issueAskTicket(slot) {
    if (!this.#migrating.has(slot)) {
      throw new WrongSlotStateError(`节点 ${this.#id} 不在搬迁槽 ${slot}，不能签发 ASK 指示`);
    }
    const ticket = new AskTicket(slot, this.#migrating.get(slot));
    this.#askTickets.add(ticket);
    return ticket;
  }

  /** B 向协调器签发一次性迁入许可，一次许可只够搬一个键。 */
  issueImportGrant(slot) {
    if (!this.#importing.has(slot)) {
      throw new WrongSlotStateError(`节点 ${this.#id} 不在导入槽 ${slot}，不能签发迁入许可`);
    }
    const grant = new ImportGrant(slot, this.#id);
    this.#importGrants.add(grant);
    return grant;
  }

  // ---------- 请求处理 ----------

  /**
   * 统一请求入口。
   * @param {{op:'get'|'set'|'delete'|'mget'|'importKey', slot:number, key?:string,
   *          keys?:string[], value?:unknown, ticket?:OneTimeTicket}} request
   * @returns {object} 正常结果信封；路由信号以 throw 形式返回
   */
  handle(request) {
    const { op, slot } = request;
    switch (this.#stateOf(slot)) {
      case 'foreign':
        throw new MovedRedirect(slot, this.#ownerOf(slot));
      case 'importing':
        return this.#handleImporting(request);
      case 'migrating':
        return this.#handleMigrating(request);
      case 'owner':
        return this.#handleOwner(request);
      default:
        throw new WrongSlotStateError('未知槽状态');
    }
  }

  #handleOwner(request) {
    const { op } = request;
    if (op === 'importKey') {
      throw new WrongSlotStateError(`节点 ${this.#id} 已是槽主，不接受迁入请求`);
    }
    return this.#executeLocal(request);
  }

  #handleMigrating(request) {
    const { op, slot, key, keys } = request;
    const target = this.#migrating.get(slot);

    if (op === 'importKey') {
      throw new WrongSlotStateError(`节点 ${this.#id} 是搬迁源，不接受迁入请求`);
    }

    if (op === 'mget') {
      const localKeys = keys.filter((k) => this.#store.has(k));
      const remoteKeys = keys.filter((k) => !this.#store.has(k));
      if (remoteKeys.length === 0) {
        return this.#executeLocal(request); // 全部还在 A，正常服务
      }
      if (localKeys.length === 0) {
        throw new AskRedirect(slot, target); // 全部归 B：一次性重定向
      }
      // 两侧都有归属：暂缓整组请求，绝不返回部分值
      throw new TryAgain(slot, localKeys, remoteKeys);
    }

    // 单键读 / 新建 / 删除：键还在 A 才由 A 服务，
    // 已搬走（含搬走后又删除）或全新的键一律归 B。
    if (this.#store.has(key)) {
      return this.#executeLocal(request);
    }
    throw new AskRedirect(slot, target);
  }

  #handleImporting(request) {
    const { op, slot, ticket } = request;

    if (op === 'importKey') {
      // B 只凭一次性迁入许可接受导入，且一次许可只对应一次导入
      this.#consume(ticket, slot, 'import-grant');
      const { key, value } = request;
      if (this.#store.has(key)) {
        throw new ImportConflictError(key, this.#id);
      }
      this.#store.set(key, value);
      this.#imported.get(slot)?.add(key);
      return { op, slot, nodeId: this.#id, key, value, imported: true };
    }

    // 普通客户端请求：IMPORTING 期间 B 不主动服务，
    // 只接受携带一次性 ASK 指示的那一次重试。
    this.#consume(ticket, slot, 'ask');
    return this.#executeLocal(request);
  }

  #executeLocal(request) {
    const { op, slot, key, keys, value } = request;
    switch (op) {
      case 'get':
        return {
          op,
          slot,
          nodeId: this.#id,
          key,
          exists: this.#store.has(key),
          value: this.#store.get(key) ?? null,
        };
      case 'set':
        this.#store.set(key, value);
        return { op, slot, nodeId: this.#id, key, value, stored: true };
      case 'delete': {
        const existed = this.#store.delete(key);
        return { op, slot, nodeId: this.#id, key, existed };
      }
      case 'mget':
        return {
          op,
          slot,
          nodeId: this.#id,
          values: keys.map((k) => ({
            key: k,
            exists: this.#store.has(k),
            value: this.#store.get(k) ?? null,
          })),
        };
      default:
        throw new WrongSlotStateError(`不支持的操作 ${op}`);
    }
  }

  /**
   * 集群协调器调用的原子搬键：从 A 本地读取 → B 导入 → A 删除。
   * B 导入失败时 A 绝不删除，键原样留在 A，保证原子性。
   */
  exportKey(slot, key, importOnTarget) {
    if (!this.#migrating.has(slot)) {
      throw new WrongSlotStateError(`节点 ${this.#id} 的槽 ${slot} 不处于 MIGRATING`);
    }
    if (!this.#store.has(key)) {
      throw new WrongSlotStateError(`键 ${key} 不在源节点 ${this.#id} 上（可能已搬迁）`);
    }
    const value = this.#store.get(key);
    // 先在 B 落地；任何异常都会向外抛，中断后续删除
    importOnTarget(key, value);
    this.#store.delete(key);
    this.#emigrated.get(slot)?.add(key);
    return { slot, key, migrated: true };
  }

  // ---------- 内部工具 ----------

  #consume(ticket, slot, expectedKind) {
    if (!ticket) {
      throw new OneTimeTicketRejectedError(
        `节点 ${this.#id} 的槽 ${slot} 处于 ${expectedKind === 'ask' ? 'IMPORTING，普通请求必须携带一次性 ASK 指示' : '导入态，迁入必须携带一次性迁入许可'}`,
      );
    }
    ticket.consume(slot, this.#id, expectedKind);
    if (expectedKind === 'ask') this.#askTickets.delete(ticket);
    else this.#importGrants.delete(ticket);
  }

  #stateOf(slot) {
    if (this.#ownSlots.has(slot)) return 'owner';
    if (this.#migrating.has(slot)) return 'migrating';
    if (this.#importing.has(slot)) return 'importing';
    return 'foreign';
  }

  #ownerOf(slot) {
    const hint = this.#foreignHints.get(slot);
    if (hint) return hint;
    if (this.#resolveOwner) return this.#resolveOwner(slot);
    throw new WrongSlotStateError(`节点 ${this.#id} 不知道槽 ${slot} 的槽主`);
  }

  // ---------- 可观测性（测试与协调器用） ----------

  stateOf(slot) {
    return this.#stateOf(slot);
  }

  hasKey(key) {
    return this.#store.has(key);
  }

  slotKeyCount(slot) {
    let n = 0;
    for (const key of this.#store.keys()) {
      if (keySlot(key) === slot) n += 1;
    }
    return n;
  }

  /** 列出本节点上属于某槽的键（协调器逐键搬迁时遍历，快照副本）。 */
  slotKeys(slot) {
    return [...this.#store.keys()].filter((k) => keySlot(k) === slot);
  }

  emigratedKeys(slot) {
    return [...(this.#emigrated.get(slot) ?? [])];
  }

  importedKeys(slot) {
    return [...(this.#imported.get(slot) ?? [])];
  }

  get storeSize() {
    return this.#store.size;
  }
}
