'use strict';

/**
 * 网关准入模块：普通表单与文档上传共用入口。
 *
 * - 按路由声明（route）同时跟踪「上传字节」（原始请求流）与「解码输出字节」。
 * - 输入为分块请求流，解码器可替换（route.decoder 为工厂函数，每个会话独立实例）。
 * - 文档先流入待定区（pending），整份请求读完且通过限额后才对下游可见（commit）。
 * - 解码晚些超限、客户端取消、下游写入失败，都会清掉待定状态。
 * - 同一业务请求（requestId）失败后可用同一 requestId 重试；并行请求各自独立预算。
 */

export class AdmissionError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'AdmissionError';
    this.code = code;
  }
}

export const CODES = Object.freeze({
  UNKNOWN_ROUTE: 'UNKNOWN_ROUTE',
  DUPLICATE_REQUEST: 'DUPLICATE_REQUEST',
  SESSION_CLOSED: 'SESSION_CLOSED',
  UPLOAD_LIMIT: 'UPLOAD_LIMIT',
  DECODED_LIMIT: 'DECODED_LIMIT',
  DECODE_ERROR: 'DECODE_ERROR',
  DOWNSTREAM_FAILURE: 'DOWNSTREAM_FAILURE',
  ABORTED: 'ABORTED',
});

function byteLength(chunk) {
  if (typeof chunk === 'string') return Buffer.byteLength(chunk);
  if (chunk == null) return 0;
  return chunk.length;
}

/**
 * @param {object} options
 * @param {Record<string, {
 *   maxUploadBytes: number,
 *   maxDecodedBytes: number,
 *   decoder: () => { update(chunk): (Buffer|string|Uint8Array|null|undefined), finalize?(): any, destroy?(): void },
 *   sink?: { write(requestId: string, buffers: Buffer[]): (void|Promise<void>) },
 * }>} options.routes 路由声明表
 */
export function createAdmissionGateway({ routes } = {}) {
  if (!routes || typeof routes !== 'object') {
    throw new TypeError('routes declaration is required');
  }

  /** 待定区：requestId -> 会话内部状态。未 commit 前对外不可见。 */
  const pending = new Map();
  /** 已提交、对下游可见的文档：requestId -> { route, uploadBytes, decodedBytes, body } */
  const visible = new Map();

  function cleanup(requestId, state) {
    state.settled = true;
    pending.delete(requestId);
    if (state.decoder && typeof state.decoder.destroy === 'function') {
      try {
        state.decoder.destroy();
      } catch {
        /* 清理阶段的解码器销毁失败不遮蔽原始错误 */
      }
    }
    state.chunks.length = 0;
  }

  function admit(routeName, requestId) {
    const route = routes[routeName];
    if (!route) {
      throw new AdmissionError(CODES.UNKNOWN_ROUTE, `unknown route: ${routeName}`);
    }
    if (typeof route.decoder !== 'function') {
      throw new TypeError(`route ${routeName} must declare a decoder factory`);
    }
    if (pending.has(requestId)) {
      throw new AdmissionError(
        CODES.DUPLICATE_REQUEST,
        `request ${requestId} is already in flight`,
      );
    }

    const state = {
      route,
      decoder: route.decoder(),
      uploadBytes: 0,
      decodedBytes: 0,
      chunks: [],
      settled: false,
    };
    pending.set(requestId, state);

    function fail(code, message, cause) {
      const err = new AdmissionError(code, message);
      if (cause !== undefined) err.cause = cause;
      cleanup(requestId, state);
      return err;
    }

    function trackDecoded(output) {
      if (output == null) return;
      const buf = Buffer.isBuffer(output) ? output : Buffer.from(output);
      state.decodedBytes += buf.length;
      if (state.decodedBytes > route.maxDecodedBytes) {
        throw fail(
          CODES.DECODED_LIMIT,
          `decoded output exceeds ${route.maxDecodedBytes} bytes for route ${routeName}`,
        );
      }
      state.chunks.push(buf);
    }

    const session = {
      requestId,
      route: routeName,

      /** 当前待定区统计（仅供观测，不代表已可见）。 */
      stats() {
        return {
          uploadBytes: state.uploadBytes,
          decodedBytes: state.decodedBytes,
          settled: state.settled,
        };
      },

      /**
       * 写入一个分块。上传字节与解码输出字节同步记账；
       * 任一限额超限即拒绝并清理待定状态。
       */
      write(chunk) {
        if (state.settled) {
          throw new AdmissionError(CODES.SESSION_CLOSED, `session ${requestId} is closed`);
        }
        state.uploadBytes += byteLength(chunk);
        if (state.uploadBytes > route.maxUploadBytes) {
          throw fail(
            CODES.UPLOAD_LIMIT,
            `upload exceeds ${route.maxUploadBytes} bytes for route ${routeName}`,
          );
        }
        let output;
        try {
          output = state.decoder.update(chunk);
        } catch (cause) {
          throw fail(CODES.DECODE_ERROR, `decoder rejected chunk for ${requestId}`, cause);
        }
        trackDecoded(output);
      },

      /**
       * 整份请求读完：先让解码器 finalize（解码可能到此刻才超限），
       * 再写下游；全部通过才把文档从待定区提交为可见。
       * 任一步失败都会清掉待定状态。
       */
      async end() {
        if (state.settled) {
          throw new AdmissionError(CODES.SESSION_CLOSED, `session ${requestId} is closed`);
        }
        if (typeof state.decoder.finalize === 'function') {
          let tail;
          try {
            tail = state.decoder.finalize();
          } catch (cause) {
            throw fail(CODES.DECODE_ERROR, `decoder finalize failed for ${requestId}`, cause);
          }
          trackDecoded(tail);
        }
        const body = Buffer.concat(state.chunks);
        if (route.sink && typeof route.sink.write === 'function') {
          try {
            await route.sink.write(requestId, [body]);
          } catch (cause) {
            throw fail(
              CODES.DOWNSTREAM_FAILURE,
              `downstream write failed for ${requestId}`,
              cause,
            );
          }
        }
        // 提交：从待定区移到可见区。
        state.settled = true;
        pending.delete(requestId);
        visible.set(requestId, {
          route: routeName,
          uploadBytes: state.uploadBytes,
          decodedBytes: state.decodedBytes,
          body,
        });
        return { requestId, uploadBytes: state.uploadBytes, decodedBytes: state.decodedBytes };
      },

      /** 客户端取消 / 主动中止：清掉待定状态，之后可用同一 requestId 重试。 */
      abort(reason = 'client cancelled') {
        if (state.settled) return;
        cleanup(requestId, state);
        state.abortReason = reason;
      },
    };

    return session;
  }

  return {
    admit,
    /** 待定区是否持有该请求（调试/断言用）。 */
    isPending: (requestId) => pending.has(requestId),
    /** 已提交且可见的文档；未提交一律返回 undefined。 */
    getVisible: (requestId) => visible.get(requestId),
    pendingCount: () => pending.size,
    visibleCount: () => visible.size,
  };
}
