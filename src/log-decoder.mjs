// CRI 日志片段重组解码器。
//
// 容器运行时把一行日志拆成多条带 P(partial) 标记的字节片段，最后一条标 F(full)。
// 本组件供采集器调用：按 (容器, 实例, 流) 维护重组状态，支持跨卷拼行、
// stdout/stderr 交错、UTF-8 字符被切开、快照持久化与恢复去重、实例重启时
// 把旧实例半行作为残缺记录冲刷输出。每条产出行都携带覆盖它的首末记录位置，
// 供下游查询。
//
// 位置模型：卷序号 + 卷内记录号在容器实例维度全局有序（同一卷内 stdout/stderr
// 的记录共享编号序列），因此顺序校验与重送去重挂在实例级，拼行缓冲挂在流级。

const STREAMS = new Set(['stdout', 'stderr']);
const TAGS = new Set(['P', 'F']);

export class LogDecodeError extends Error {
  constructor(message, code) {
    super(message);
    this.name = 'LogDecodeError';
    this.code = code;
  }
}

const decoder = new TextDecoder('utf-8', { fatal: false });

function isUint8Array(value) {
  return value instanceof Uint8Array || Buffer.isBuffer(value);
}

function assertRecord(rec) {
  if (rec === null || typeof rec !== 'object') {
    throw new LogDecodeError('record must be an object', 'ERR_BAD_RECORD');
  }
  if (typeof rec.container !== 'string' || rec.container.length === 0) {
    throw new LogDecodeError('container must be a non-empty string', 'ERR_BAD_RECORD');
  }
  if (typeof rec.instance !== 'string' || rec.instance.length === 0) {
    throw new LogDecodeError('instance must be a non-empty string', 'ERR_BAD_RECORD');
  }
  if (!STREAMS.has(rec.stream)) {
    throw new LogDecodeError(`stream must be one of ${[...STREAMS].join('/')}`, 'ERR_BAD_STREAM');
  }
  if (!Number.isInteger(rec.volume) || rec.volume < 0) {
    throw new LogDecodeError('volume must be a non-negative integer', 'ERR_BAD_POSITION');
  }
  if (!Number.isInteger(rec.recordNo) || rec.recordNo < 0) {
    throw new LogDecodeError('recordNo must be a non-negative integer', 'ERR_BAD_POSITION');
  }
  if (!TAGS.has(rec.tag)) {
    throw new LogDecodeError('tag must be P or F', 'ERR_BAD_TAG');
  }
  if (!isUint8Array(rec.bytes)) {
    throw new LogDecodeError('bytes must be a Uint8Array/Buffer', 'ERR_BAD_RECORD');
  }
}

// 位置比较：先比卷序号，再比卷内记录号。
function comparePos(aVolume, aRecordNo, bVolume, bRecordNo) {
  if (aVolume !== bVolume) return aVolume < bVolume ? -1 : 1;
  if (aRecordNo !== bRecordNo) return aRecordNo < bRecordNo ? -1 : 1;
  return 0;
}

// 严格后继：同卷 recordNo+1，或下一卷 recordNo 0。
function isNextPosition(state, volume, recordNo) {
  if (volume === state.lastVolume) return recordNo === state.lastRecordNo + 1;
  if (volume === state.lastVolume + 1) return recordNo === 0;
  return false;
}

function concatChunks(chunks) {
  let total = 0;
  for (const c of chunks) total += c.length;
  const out = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.length;
  }
  return out;
}

function decodeLine(bytes) {
  let text = decoder.decode(bytes);
  if (text.endsWith('\n')) {
    text = text.slice(0, -1);
    if (text.endsWith('\r')) text = text.slice(0, -1);
  }
  return text;
}

// 单条流的拼行缓冲：半行字节片段 + 半行首末记录位置。
class StreamState {
  constructor() {
    this.chunks = [];
    this.lineStart = null;
    this.lastEnd = null; // 当前半行最近一条片段的位置
  }

  get hasPending() {
    return this.chunks.length > 0;
  }

  toJSON() {
    return {
      pending: this.chunks.map((c) => Buffer.from(c).toString('base64')),
      lineStart: this.lineStart,
      lastEnd: this.lastEnd,
    };
  }

  static fromJSON(raw) {
    const s = new StreamState();
    s.chunks = (raw.pending ?? []).map((b64) => new Uint8Array(Buffer.from(b64, 'base64')));
    s.lineStart = raw.lineStart ?? null;
    s.lastEnd = raw.lastEnd ?? null;
    return s;
  }
}

export class LogDecoder {
  constructor() {
    // container -> { instance, lastVolume, lastRecordNo, streams: Map<stream, StreamState> }
    this.containers = new Map();
  }

  /**
   * 从 snapshot() 的产物恢复解码器。恢复后上游重送已保存的记录会被去重，
   * 未完成的半行可继续补完且只产出一次。
   */
  static restore(snapshot) {
    const d = new LogDecoder();
    const raw = typeof snapshot === 'string' ? JSON.parse(snapshot) : snapshot;
    if (raw === null || typeof raw !== 'object' || raw.version !== 1) {
      throw new LogDecodeError('unsupported snapshot', 'ERR_BAD_SNAPSHOT');
    }
    for (const [container, cState] of Object.entries(raw.containers ?? {})) {
      const streams = new Map();
      for (const [stream, sState] of Object.entries(cState.streams ?? {})) {
        streams.set(stream, StreamState.fromJSON(sState));
      }
      d.containers.set(container, {
        instance: cState.instance,
        lastVolume: cState.lastVolume,
        lastRecordNo: cState.lastRecordNo,
        streams,
      });
    }
    return d;
  }

  /** 导出 JSON 安全的快照（可直接 JSON.stringify）。 */
  snapshot() {
    const containers = {};
    for (const [container, cState] of this.containers) {
      const streams = {};
      for (const [stream, sState] of cState.streams) {
        streams[stream] = sState.toJSON();
      }
      containers[container] = {
        instance: cState.instance,
        lastVolume: cState.lastVolume,
        lastRecordNo: cState.lastRecordNo,
        streams,
      };
    }
    return { version: 1, containers };
  }

  /**
   * 摄入一条记录，返回本次产出的行数组（可能为空；实例切换时可能先带出
   * 旧实例的残缺记录）。重复（已消费位置）的记录被静默跳过；位置跳跃等
   * 非法输入抛 LogDecodeError。
   */
  ingest(rec) {
    assertRecord(rec);
    const emitted = [];
    let cState = this.containers.get(rec.container);

    // 同一容器换了实例：先把旧实例各流的半行作为残缺记录冲刷，再开新实例。
    if (cState && cState.instance !== rec.instance) {
      emitted.push(...this.#flushContainer(rec.container, cState));
      this.containers.delete(rec.container);
      cState = undefined;
    }
    if (!cState) {
      cState = { instance: rec.instance, lastVolume: -1, lastRecordNo: -1, streams: new Map() };
      this.containers.set(rec.container, cState);
    }

    if (cState.lastVolume >= 0) {
      const cmp = comparePos(rec.volume, rec.recordNo, cState.lastVolume, cState.lastRecordNo);
      if (cmp <= 0) {
        return emitted; // 上游重送的已保存记录：去重，不重复产出
      }
      if (!isNextPosition(cState, rec.volume, rec.recordNo)) {
        throw new LogDecodeError(
          `record position gap at volume=${rec.volume} recordNo=${rec.recordNo}, ` +
            `last consumed volume=${cState.lastVolume} recordNo=${cState.lastRecordNo}`,
          'ERR_POSITION_GAP',
        );
      }
    }

    let sState = cState.streams.get(rec.stream);
    if (!sState) {
      sState = new StreamState();
      cState.streams.set(rec.stream, sState);
    }
    if (!sState.hasPending) {
      sState.lineStart = { volume: rec.volume, recordNo: rec.recordNo };
    }
    sState.chunks.push(new Uint8Array(rec.bytes));
    sState.lastEnd = { volume: rec.volume, recordNo: rec.recordNo };
    cState.lastVolume = rec.volume;
    cState.lastRecordNo = rec.recordNo;

    if (rec.tag === 'F') {
      emitted.push(this.#emitLine(rec.container, cState, rec.stream, sState, false));
    }
    return emitted;
  }

  #emitLine(container, cState, stream, sState, partial) {
    const end = sState.lastEnd ?? { volume: cState.lastVolume, recordNo: cState.lastRecordNo };
    const line = {
      container,
      instance: cState.instance,
      stream,
      text: decodeLine(concatChunks(sState.chunks)),
      partial,
      start: sState.lineStart ?? end,
      end,
    };
    sState.chunks = [];
    sState.lineStart = null;
    sState.lastEnd = null;
    return line;
  }

  #flushContainer(container, cState) {
    const emitted = [];
    for (const [stream, sState] of cState.streams) {
      if (sState.hasPending) {
        emitted.push(this.#emitLine(container, cState, stream, sState, true));
      }
    }
    return emitted;
  }

  /** 冲刷所有容器未完成的半行（如采集器停止时），返回残缺记录数组。 */
  flushAll() {
    const emitted = [];
    for (const [container, cState] of this.containers) {
      emitted.push(...this.#flushContainer(container, cState));
    }
    return emitted;
  }
}
