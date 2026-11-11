'use strict';

/**
 * 记录式分配器（Recording Allocator）
 *
 * 不真正持有像素内存，只做容量记账：
 *  - 每一次 allocate / free / releaseTask 尝试都写入不可变审计记录；
 *  - 拒绝的请求同样记录原因，且绝不改变已用容量；
 *  - 支持按任务批量归还（任务取消 / 提前失败时回收全部存活句柄）。
 */
export class RecordingAllocator {
  /**
   * @param {object} opts
   * @param {number} opts.capacityBytes 硬容量上限（并发总预算）
   */
  constructor({ capacityBytes } = {}) {
    if (!Number.isSafeInteger(capacityBytes) || capacityBytes < 0) {
      throw new Error('capacityBytes 必须是非负安全整数');
    }
    this.capacityBytes = capacityBytes;
    this.usedBytes = 0;
    this.peakBytes = 0;
    /** @type {Map<string, {id:string,taskId:string,stage:string,label:string,bytes:number,live:boolean}>} */
    this._handles = new Map();
    /** @type {Map<string, Set<string>>} */
    this._byTask = new Map();
    /** @type {object[]} 审计记录（只追加） */
    this._records = [];
    this._seq = 0;
  }

  get remainingBytes() {
    return this.capacityBytes - this.usedBytes;
  }

  get records() {
    return this._records;
  }

  /**
   * 申请分配。超出容量或参数非法时返回 {ok:false}，并保留现场记录。
   */
  allocate({ taskId, stage, label, bytes } = {}) {
    const seq = this._seq++;
    const base = { seq, event: 'allocate', taskId, stage, label, bytes };

    if (!Number.isSafeInteger(bytes) || bytes <= 0) {
      const record = { ...base, decision: 'deny', reason: 'invalid_size', usedBytes: this.usedBytes };
      this._records.push(record);
      return { ok: false, reason: 'invalid_size', record };
    }

    if (this.usedBytes + bytes > this.capacityBytes) {
      const record = {
        ...base,
        decision: 'deny',
        reason: 'capacity_exceeded',
        usedBytes: this.usedBytes,
        capacityBytes: this.capacityBytes,
      };
      this._records.push(record);
      return { ok: false, reason: 'capacity_exceeded', record };
    }

    const handle = { id: `h${seq}`, taskId, stage, label, bytes, live: true };
    this._handles.set(handle.id, handle);
    if (!this._byTask.has(taskId)) this._byTask.set(taskId, new Set());
    this._byTask.get(taskId).add(handle.id);

    this.usedBytes += bytes;
    this.peakBytes = Math.max(this.peakBytes, this.usedBytes);

    const record = { ...base, decision: 'allow', handleId: handle.id, usedBytes: this.usedBytes };
    this._records.push(record);
    return { ok: true, handle, record };
  }

  /**
   * 释放单个句柄。
   */
  free(handle) {
    const id = handle && handle.id;
    const live = id ? this._handles.get(id) : undefined;
    if (!live) {
      const record = { event: 'free', decision: 'deny', reason: 'unknown_handle', handleId: id };
      this._records.push(record);
      return { ok: false, freedBytes: 0, reason: 'unknown_handle', record };
    }
    live.live = false;
    this._handles.delete(live.id);
    this._byTask.get(live.taskId)?.delete(live.id);
    this.usedBytes -= live.bytes;
    const record = {
      event: 'free',
      decision: 'allow',
      handleId: live.id,
      taskId: live.taskId,
      bytes: live.bytes,
      usedBytes: this.usedBytes,
    };
    this._records.push(record);
    return { ok: true, freedBytes: live.bytes, record };
  }

  /**
   * 释放某任务名下全部存活句柄（取消 / 提前失败路径）。
   * @returns {{freedBytes:number, handleCount:number}}
   */
  releaseTask(taskId, reason = 'completed') {
    const ids = [...(this._byTask.get(taskId) ?? [])];
    let freedBytes = 0;
    for (const id of ids) {
      const handle = this._handles.get(id);
      if (handle && handle.live) {
        handle.live = false;
        this._handles.delete(id);
        freedBytes += handle.bytes;
        this.usedBytes -= handle.bytes;
      }
    }
    this._byTask.delete(taskId);
    const record = {
      event: 'release_task',
      decision: 'allow',
      taskId,
      reason,
      freedBytes,
      handleCount: ids.length,
      usedBytes: this.usedBytes,
    };
    this._records.push(record);
    return { freedBytes, handleCount: ids.length, record };
  }
}
