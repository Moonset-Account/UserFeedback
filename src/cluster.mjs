// 集群协调器：持有全局槽表，编排 A -> B 的逐键原子搬迁与槽主切换。
// 本模型为单进程内存模拟，「原子」由同步执行保证：B 导入成功之前
// A 不会删除源键，任何失败都让键完整留在 A。

import { Node } from './node.mjs';
import { keySlot, SLOT_COUNT } from './hash.mjs';
import { WrongSlotStateError } from './protocol.mjs';

export class Cluster {
  #nodes = new Map();
  #slotOwner = new Map(); // slot -> nodeId
  #migrationMeta = new Map(); // slot -> {sourceId, targetId, migratedKeys:string[]}

  addNode(node) {
    if (!(node instanceof Node)) throw new TypeError('addNode 需要 Node 实例');
    if (this.#nodes.has(node.id)) throw new Error(`节点 ${node.id} 已存在`);
    this.#nodes.set(node.id, node);
    node.bindOwnerResolver((slot) => this.ownerOf(slot));
    return node;
  }

  getNode(id) {
    const node = this.#nodes.get(id);
    if (!node) throw new Error(`未知节点 ${id}`);
    return node;
  }

  get nodeIds() {
    return [...this.#nodes.keys()];
  }

  assignSlot(slot, nodeId) {
    if (!Number.isInteger(slot) || slot < 0 || slot >= SLOT_COUNT) {
      throw new RangeError(`slot 必须是 [0, ${SLOT_COUNT - 1}] 内的整数`);
    }
    this.getNode(nodeId).assignSlot(slot);
    this.#slotOwner.set(slot, nodeId);
  }

  ownerOf(slot) {
    const owner = this.#slotOwner.get(slot);
    if (!owner) throw new WrongSlotStateError(`槽 ${slot} 尚未分配槽主`);
    return owner;
  }

  slotTableSnapshot() {
    const table = new Map();
    for (const [slot, nodeId] of this.#slotOwner) table.set(slot, nodeId);
    return table;
  }

  /** 向某节点预灌数据（仅测试/合成数据）：本地写入，绕过路由准入。 */
  seed(nodeId, entries) {
    const node = this.getNode(nodeId);
    for (const [key, value] of entries) {
      node.putLocal(key, value);
    }
  }

  /**
   * 开始搬迁：A 进入 MIGRATING、B 进入 IMPORTING。
   * 搬迁期间全局槽表仍指向 A（客户端缓存的旧槽主仍然有效）。
   */
  beginMigration(slot, sourceId, targetId) {
    if (this.ownerOf(slot) !== sourceId) {
      throw new WrongSlotStateError(`槽 ${slot} 当前槽主不是 ${sourceId}`);
    }
    if (sourceId === targetId) {
      throw new WrongSlotStateError('源节点与目标节点不能相同');
    }
    const source = this.getNode(sourceId);
    const target = this.getNode(targetId);
    source.beginMigrating(slot, targetId);
    target.beginImporting(slot, sourceId);
    this.#migrationMeta.set(slot, { sourceId, targetId, migratedKeys: [] });
  }

  /**
   * 原子搬迁单个键：
   * 1) 协调器向 B 申请一次性迁入许可（一次许可只够一个键）；
   * 2) A 导出键：B 凭许可导入成功后，A 才删除源键；
   * 3) 任一步失败，键原样留在 A。
   */
  migrateKey(slot, key) {
    const meta = this.#requireMigration(slot);
    const source = this.getNode(meta.sourceId);
    const target = this.getNode(meta.targetId);
    const grant = target.issueImportGrant(slot);
    const result = source.exportKey(slot, key, (k, value) => {
      target.handle({ op: 'importKey', slot, key: k, value, ticket: grant });
    });
    meta.migratedKeys.push(key);
    return result;
  }

  /** 搬迁 A 上该槽剩余的全部键，逐键原子；返回已搬键列表。 */
  migrateRemainingKeys(slot) {
    const meta = this.#requireMigration(slot);
    const source = this.getNode(meta.sourceId);
    const moved = [];
    for (const key of source.slotKeys(slot)) {
      moved.push(this.migrateKey(slot, key).key);
    }
    return moved;
  }

  /**
   * 显式切换槽主（必须在最后一个源键搬完之后）：
   * A 交出槽主身份 -> B 接管 -> 全局槽表更新为 B。
   * 此后 A 对该槽的一切请求返回永久 MOVED，客户端据此更新缓存。
   */
  completeMigration(slot) {
    const meta = this.#requireMigration(slot);
    const source = this.getNode(meta.sourceId);
    const target = this.getNode(meta.targetId);
    source.completeMigration(slot); // A 端校验：源键必须已清空
    target.completeImport(slot);
    this.#slotOwner.set(slot, meta.targetId);
    this.#migrationMeta.delete(slot);
    return { slot, newOwner: meta.targetId };
  }

  migrationProgress(slot) {
    const meta = this.#migrationMeta.get(slot);
    if (!meta) return null;
    const source = this.getNode(meta.sourceId);
    return {
      slot,
      sourceId: meta.sourceId,
      targetId: meta.targetId,
      remainingOnSource: source.slotKeyCount(slot),
      migrated: [...meta.migratedKeys],
    };
  }

  #requireMigration(slot) {
    const meta = this.#migrationMeta.get(slot);
    if (!meta) {
      throw new WrongSlotStateError(`槽 ${slot} 没有进行中的搬迁`);
    }
    return meta;
  }
}
