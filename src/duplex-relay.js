// 双向双工字节流中继。
//
// 接入两个“socket 风格”的双工字节流，分别在两个方向上建立中继：
//   方向 aToB：从 a 读、向 b 写
//   方向 bToA：从 b 读、向 a 写
//
// 每个方向独立维护“已接收但尚未写出”的待写缓冲，并设置字节上限：
//   - 缓冲达到上限时暂停对应源端的读取；
//   - 对端（目的端）排空缓冲后再恢复源端；
//   - 两个方向互不阻塞：一个方向背压不影响另一个方向继续转发。
//
// 半关闭语义：源端写结束后，不立即向目的端转发 end，而是等待该方向
// 已接收的全部字节写出，再调用目的端的 end()。
//
// 硬断开语义：任一方向在写入（含“正要恢复写入”）时收到 'error'，立即
// 结束整个中继，拆除全部监听，并在结果里报告两个方向各自尚未写出的字节数。

import { EventEmitter } from 'node:events';

/**
 * 单个方向的中继状态机。
 */
class Direction {
  /**
   * @param {object} source 源双工流（读端）
   * @param {object} destination 目的双工流（写端）
   * @param {string} label 方向名，用于报告
   * @param {number} bufferLimit 待写缓冲字节上限（正整数）
   * @param {(err: Error, dir: Direction) => void} onHardError 硬断开回调
   * @param {(dir: Direction) => void} onFinished 方向干净完成回调
   */
  constructor(source, destination, label, bufferLimit, onHardError, onFinished) {
    if (!Number.isInteger(bufferLimit) || bufferLimit <= 0) {
      throw new TypeError(`bufferLimit(${bufferLimit}) 必须是正整数`);
    }
    this.source = source;
    this.destination = destination;
    this.label = label;
    this.bufferLimit = bufferLimit;
    this._onHardError = onHardError;
    this._onFinished = onFinished;

    /** @type {Buffer[]} 等待写出的字节块 */
    this.queue = [];
    /** 队列中的总字节数（尚未写出） */
    this.bufferedBytes = 0;
    /** 运行期间待写缓冲达到过的最大字节数 */
    this.peakBufferedBytes = 0;
    /** 源端是否已写结束（半关闭到达） */
    this.sourceEnded = false;
    /** 本方向是否已完成（end 已传递 / 已随硬断开终止） */
    this.done = false;
    /** 是否因缓冲达到上限而暂停了源端 */
    this.paused = false;
    /** 目的端是否处于写阻塞（write 返回 0，等待 drain） */
    this.writeBlocked = false;
    /** 从源端累计读取的字节数 */
    this.bytesRead = 0;
    /** 向目的端累计写出的字节数 */
    this.bytesWritten = 0;
    /** 结束本方向时仍未写出的字节数（正常完成为 0） */
    this.unwrittenAtEnd = 0;

    this._attached = false;
    this._onData = (chunk) => this._handleData(chunk);
    this._onEnd = () => this._handleSourceEnd();
    this._onDrain = () => this._handleDrain();
    // 方向自身收敛两端错误，以便定位是哪个方向触发的硬断开；
    // 中继级 error 监听仅作兜底。
    this._onSourceError = (err) => this._fail(err);
    this._onDestinationError = (err) => this._fail(err);
  }

  attach() {
    if (this._attached || this.done) return;
    this.source.on('data', this._onData);
    this.source.on('end', this._onEnd);
    this.source.on('error', this._onSourceError);
    this.destination.on('drain', this._onDrain);
    this.destination.on('error', this._onDestinationError);
    this._attached = true;
  }

  /** 中继整体终止时拆除本方向监听（幂等）。 */
  detach() {
    if (!this._attached) return;
    this.source.off('data', this._onData);
    this.source.off('end', this._onEnd);
    this.source.off('error', this._onSourceError);
    this.destination.off('drain', this._onDrain);
    this.destination.off('error', this._onDestinationError);
    this._attached = false;
  }

  /** 启动源端读取。 */
  resumeSource() {
    if (this.done || this.paused || this.sourceEnded) return;
    this.source.resume();
  }

  _handleData(chunk) {
    if (this.done) return;
    const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    if (buf.length === 0) return;

    this.bytesRead += buf.length;
    this.queue.push(buf);
    this.bufferedBytes += buf.length;
    if (this.bufferedBytes > this.peakBufferedBytes) {
      this.peakBufferedBytes = this.bufferedBytes;
    }

    // 待写缓冲达到上限：暂停对应源端。目的端排空后在 _pump 中恢复。
    if (!this.paused && this.bufferedBytes >= this.bufferLimit) {
      this.paused = true;
      this.source.pause();
    }
    this._pump();
  }

  _handleSourceEnd() {
    if (this.done || this.sourceEnded) return;
    this.sourceEnded = true;
    // 队列已空：已接收字节全部写出，直接向对端传递写结束。
    if (this.bufferedBytes === 0) {
      this._finish();
    }
    // 否则等 _pump 排空后再 _finish。
  }

  _handleDrain() {
    if (this.done) return;
    this.writeBlocked = false;
    this._pump();
  }

  /**
   * 尽可能把队列字节写给目的端。
   * 底层一次可能只接受部分字节（write 返回 < chunk.length，返回 0 表示阻塞）。
   */
  _pump() {
    if (this.done || this.writeBlocked) return;

    while (this.queue.length > 0 && !this.writeBlocked && !this.done) {
      const head = this.queue[0];
      let accepted;
      try {
        accepted = this.destination.write(head);
      } catch (err) {
        // 同步抛出同样视为硬断开（例如对端已销毁）。
        this._fail(err);
        return;
      }

      if (typeof accepted !== 'number' || accepted < 0 || accepted > head.length) {
        this._fail(new Error(`非法的 write 返回值: ${String(accepted)}`));
        return;
      }

      if (accepted === 0) {
        // 底层暂时一个字节都不接受，等待 'drain'。
        this.writeBlocked = true;
        return;
      }

      this.bytesWritten += accepted;
      this.bufferedBytes -= accepted;

      if (accepted < head.length) {
        // 部分接受：保留剩余字节，继续尝试（下一次可能又部分接受或阻塞）。
        this.queue[0] = head.subarray(accepted);
      } else {
        this.queue.shift();
      }
    }

    if (this.done) return;

    // 两个条件必须在同一帧一起判断：source.resume() 可能同步重入读取，
    // 嵌套帧会让 sourceEnded 与 bufferedBytes 同时变化，分开判断会在外层
    // 帧残留字节时错误地提前 _finish()。
    if (this.bufferedBytes === 0) {
      // 已排空：若此前因上限暂停了源端，现在恢复。
      if (this.paused) {
        this.paused = false;
        this.source.resume();
      }
      // 源端已半关闭且字节全部写出：向对端传递写结束。
      if (!this.done && this.sourceEnded && this.bufferedBytes === 0) {
        this._finish();
      }
    }
  }

  _finish() {
    if (this.done) return;
    this.done = true;
    this.unwrittenAtEnd = 0;
    try {
      this.destination.end();
    } catch (err) {
      this.detach();
      this._onHardError(err, this);
      return;
    }
    this.detach();
    this._onFinished(this);
  }

  _fail(err) {
    if (this.done) return;
    this.done = true;
    this.unwrittenAtEnd = this.bufferedBytes;
    this.detach();
    this._onHardError(err, this);
  }

  /** 中继整体终止时调用：固化未写字节数。 */
  terminate() {
    if (this.done) return;
    this.done = true;
    this.unwrittenAtEnd = this.bufferedBytes;
    this.detach();
  }

  stats() {
    return {
      label: this.label,
      bufferLimit: this.bufferLimit,
      bytesRead: this.bytesRead,
      bytesWritten: this.bytesWritten,
      peakBufferedBytes: this.peakBufferedBytes,
      bufferedBytes: this.bufferedBytes,
      unwrittenAtEnd: this.unwrittenAtEnd,
      sourceEnded: this.sourceEnded,
      paused: this.paused,
      writeBlocked: this.writeBlocked,
      done: this.done,
    };
  }
}

/**
 * 双向双工字节流中继。
 *
 * 流接口约定（Node net.Socket 的子集，便于用可控流驱动测试）：
 *   事件：'data'(Buffer)、'end'、'drain'、'error'
 *   方法：write(chunk) -> number
 *           返回本次被底层接受的字节数；0 表示完全阻塞，后续须发 'drain'；
 *           允许 0 < n < chunk.length 表示部分接受。
 *         end()        传递写结束（半关闭）
 *         pause()/resume()  流控
 */
export class DuplexRelay extends EventEmitter {
  /**
   * @param {object} a 双工字节流 A
   * @param {object} b 双工字节流 B
   * @param {object} [options]
   * @param {number} [options.aToBLimit=65536] a->b 待写缓冲上限
   * @param {number} [options.bToALimit=65536] b->a 待写缓冲上限
   */
  constructor(a, b, options = {}) {
    super();
    this.a = a;
    this.b = b;
    const aToBLimit = options.aToBLimit ?? 65536;
    const bToALimit = options.bToALimit ?? 65536;

    this._terminated = false;
    this._finishedCount = 0;
    this._failure = null;

    this.aToB = new Direction(
      a, b, 'aToB', aToBLimit,
      (err, dir) => this._handleHardError(err, dir),
      () => this._handleDirectionFinished(),
    );
    this.bToA = new Direction(
      b, a, 'bToA', bToALimit,
      (err, dir) => this._handleHardError(err, dir),
      () => this._handleDirectionFinished(),
    );
  }

  /** 挂载监听并启动两个方向，返回 this。 */
  start() {
    if (this._terminated) return this;
    this.aToB.attach();
    this.bToA.attach();
    // 错误事件由中继统一收敛，避免流自身抛未监听错误。
    this._onAError = (err) => this._handleHardError(err, null);
    this._onBError = (err) => this._handleHardError(err, null);
    this.a.on('error', this._onAError);
    this.b.on('error', this._onBError);
    this.aToB.resumeSource();
    this.bToA.resumeSource();
    return this;
  }

  /** 启动并返回在中继结束时结算的 Promise。 */
  run() {
    this.start();
    if (!this._donePromise) {
      this._donePromise = new Promise((resolve) => {
        this._resolveDone = resolve;
      });
    }
    return this._donePromise;
  }

  _handleDirectionFinished() {
    if (this._terminated) return;
    this._finishedCount += 1;
    if (this._finishedCount === 2) {
      this._teardown();
      this._resolve(this._report(true, null));
    }
  }

  _handleHardError(err, direction) {
    if (this._terminated) return;
    this._failure = {
      message: err && err.message ? err.message : String(err),
      direction: direction ? direction.label : null,
      error: err instanceof Error ? err : new Error(String(err)),
    };
    // 结束整个中继：两个方向都停止，并固化各自未写出的字节数。
    this.aToB.terminate();
    this.bToA.terminate();
    this._teardown();
    this._resolve(this._report(false, this._failure));
  }

  _teardown() {
    this._terminated = true;
    if (this._onAError) this.a.off('error', this._onAError);
    if (this._onBError) this.b.off('error', this._onBError);
    this.aToB.detach();
    this.bToA.detach();
  }

  _resolve(report) {
    this.emit('terminate', report);
    if (this._resolveDone) this._resolveDone(report);
  }

  _report(clean, failure) {
    return {
      clean,
      failure,
      directions: {
        aToB: this.aToB.stats(),
        bToA: this.bToA.stats(),
      },
    };
  }
}

export default DuplexRelay;
