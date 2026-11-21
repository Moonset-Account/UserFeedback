// 可控双工字节流（纯本地合成夹具，无网络、无真实负载）。
//
// 同一条流的读端与写端相互独立，便于在测试里扮演“客户端/服务端”端点：
//   - 读端：测试通过 feed() 灌入字节，流按 readStep 步长向中继发 'data'；
//           feedEnd() 排队半关闭，读缓冲排空后发 'end'。
//   - 写端：中继调用 write() 时，按 writeStep 决定本次接受多少字节；
//           发送缓冲满时 write 返回 0（阻塞），flush() 排空后发 'drain'；
//           failOnNextWake() 可制造“正要恢复写入时的硬断开”。
//
// 所有可观察事件都带全局序号写入日志，测试可据此核对写结束发生的次序。

import { EventEmitter } from 'node:events';

let globalSeq = 0;

/** 跨两条流共享的事件时钟：用单调递增序号表达事件先后次序。 */
export class EventClock {
  constructor() {
    this.log = [];
  }

  record(streamName, type, detail = undefined) {
    const seq = globalSeq += 1;
    this.log.push({ seq, stream: streamName, type, detail });
    return seq;
  }

  /** 取某类事件的有序序号列表，例如各端 'writeEnd' 的先后。 */
  sequencesOf(type) {
    return this.log.filter((e) => e.type === type).map((e) => e.seq);
  }

  eventsOf(type) {
    return this.log.filter((e) => e.type === type);
  }
}

export class ControllableDuplex extends EventEmitter {
  /**
   * @param {string} name 端点名（日志标识）
   * @param {object} opts
   * @param {number} [opts.readStep=4] 每次 'data' 最多吐出的字节数
   * @param {number} [opts.writeStep=3] 每次 write() 最多接受的字节数
   * @param {number} [opts.sendCapacity=16] 写端发送缓冲容量（字节）
   * @param {EventClock} [opts.clock] 共享事件时钟
   */
  constructor(name, opts = {}) {
    super();
    this.name = name;
    this.readStep = opts.readStep ?? 4;
    this.writeStep = opts.writeStep ?? 3;
    this.sendCapacity = opts.sendCapacity ?? 16;
    this.clock = opts.clock ?? null;

    // 读端状态
    this._readQueue = []; // Buffer 或 null（null 表示排队的 end）
    this._flowing = false;
    this.readEnded = false;

    // 写端状态
    this.sendBuffer = Buffer.alloc(0);
    this.delivered = Buffer.alloc(0); // 已“落到底层”的字节，供逐字节核对
    this.writeEnded = false;
    this.destroyed = false;
    this.hardError = null;
    this._failOnNextWake = false;
    this._failError = null;
  }

  _log(type, detail) {
    if (this.clock) this.clock.record(this.name, type, detail);
  }

  // ---------------- 读端（中继从这里读） ----------------

  /** 从测试侧灌入待读出的字节。 */
  feed(chunk) {
    if (this.readEnded || this.destroyed) return;
    this._readQueue.push(Buffer.from(chunk));
    this._log('feed', { length: Buffer.byteLength(chunk) });
    this._pumpRead();
  }

  /** 从测试侧安排半关闭：读缓冲排空后触发 'end'。 */
  feedEnd() {
    if (this.readEnded || this.destroyed) return;
    this._readQueue.push(null);
    this._log('feedEnd');
    this._pumpRead();
  }

  pause() {
    this._flowing = false;
    this._log('pause');
    return this;
  }

  resume() {
    if (this.destroyed) return this;
    this._flowing = true;
    this._log('resume');
    this._pumpRead();
    return this;
  }

  _pumpRead() {
    // 同步排空读队列；被 pause 后停止，等 resume 继续。
    while (this._flowing && this._readQueue.length > 0 && !this.destroyed) {
      const head = this._readQueue[0];
      if (head === null) {
        this._readQueue.shift();
        this._flowing = false;
        this.readEnded = true;
        this._log('end');
        this.emit('end');
        return;
      }
      const piece = head.subarray(0, Math.min(this.readStep, head.length));
      if (head.length > this.readStep) {
        this._readQueue[0] = head.subarray(this.readStep);
      } else {
        this._readQueue.shift();
      }
      this._log('data', { length: piece.length });
      this.emit('data', Buffer.from(piece));
    }
  }

  // ---------------- 写端（中继往这里写） ----------------

  /**
   * @returns {number} 本次实际接受的字节数；0 表示发送缓冲已满（阻塞）
   */
  write(chunk) {
    if (this.destroyed) {
      // 硬断开后任何写入都抛错，语义与失效 socket 一致。
      throw this.hardError ?? new Error(`${this.name} 已销毁`);
    }
    const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    if (buf.length === 0) return 0;

    const room = this.sendCapacity - this.sendBuffer.length;
    if (room <= 0) {
      this._log('writeBlocked', { wanted: buf.length });
      return 0;
    }
    const accept = Math.min(buf.length, this.writeStep, room);
    this.sendBuffer = Buffer.concat([this.sendBuffer, buf.subarray(0, accept)]);
    this._log('writeAccept', { accept, wanted: buf.length, buffered: this.sendBuffer.length });
    return accept;
  }

  /** 半关闭：中继把一个方向的字节全部写完后调用。 */
  end() {
    if (this.destroyed || this.writeEnded) return;
    this.writeEnded = true;
    this._log('writeEnd', {
      delivered: this.delivered.length,
      sendBuffered: this.sendBuffer.length,
    });
    this.emit('finish');
  }

  /** 测试侧把发送缓冲“送达底层”，腾出空间；若此前满阻塞则补发 'drain'。 */
  flush() {
    if (this.destroyed) return;
    const wasFull = this.sendBuffer.length >= this.sendCapacity;
    if (this.sendBuffer.length > 0) {
      this.delivered = Buffer.concat([this.delivered, this.sendBuffer]);
      this.sendBuffer = Buffer.alloc(0);
    }
    this._log('flush', { delivered: this.delivered.length });
    if (wasFull && !this.writeEnded) {
      this._wake();
    }
  }

  /**
   * 安排硬断开：下一次本应发 'drain'（正要恢复写入）时改发 'error'。
   * 对应题面“也可能在正要恢复写入时报告硬断开”。
   */
  failOnNextWake(message = 'ECONNRESET') {
    this._failOnNextWake = true;
    this._failError = new Error(`${this.name}: ${message}`);
    this._log('armFailOnNextWake');
  }

  /** 立即报告硬断开（不等 drain）。 */
  triggerHardError(message = 'ECONNRESET') {
    this._hardFail(new Error(`${this.name}: ${message}`));
  }

  _wake() {
    if (this._failOnNextWake) {
      this._failOnNextWake = false;
      this._hardFail(this._failError ?? new Error(`${this.name}: ECONNRESET`));
      return;
    }
    this._log('drain');
    this.emit('drain');
  }

  _hardFail(err) {
    if (this.destroyed) return;
    this.destroyed = true;
    this.hardError = err;
    this._flowing = false;
    this._log('error', { message: err.message });
    this.emit('error', err);
  }

  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    this._flowing = false;
    this._log('destroy');
    this.emit('close');
  }

  /** 已送达底层的全部字节（逐字节核对用）。 */
  receivedContent() {
    return Buffer.concat([this.delivered, this.sendBuffer]);
  }

  /** 当前仍滞留在发送缓冲、尚未“送达底层”的字节数。 */
  get sendBufferedBytes() {
    return this.sendBuffer.length;
  }

  /** 当前读端排队等待发给中继的字节数。 */
  get readQueuedBytes() {
    return this._readQueue.reduce((n, item) => n + (item ? item.length : 0), 0);
  }
}
