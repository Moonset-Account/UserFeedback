'use strict';

// 模拟 TLS 1.3 连接适配器。
// 真实环境中绑定字节由 SSL_export_keying_material(RFC 8446) 按
// "EXPORTER-Channel-Binding"（RFC 9266）导出；测试里以确定性字节模拟，
// 每个连接拥有独立且可区分的导出值。

const { ScramError, CODES } = require('./errors');
const { TLS_EXPORTER } = require('./messages');

class TlsConnectionAdapter {
  /**
   * @param {object} opts
   * @param {Buffer|string} opts.exportedBinding 该连接导出的 channel-binding 字节
   * @param {string} [opts.connectionId] 连接标识，用于“原连接”核对
   * @param {string} [opts.protocol] 协议版本，默认 TLSv1.3
   */
  constructor({ exportedBinding, connectionId, protocol = 'TLSv1.3' }) {
    if (exportedBinding === undefined || exportedBinding === null) {
      throw new ScramError(
        CODES.CHANNEL_BINDING_UNSUPPORTED,
        'connection adapter cannot provide tls-exporter binding bytes',
      );
    }
    this.connectionId = connectionId ?? `conn-${Math.random().toString(36).slice(2, 10)}`;
    this.protocol = protocol;
    this._binding = Buffer.isBuffer(exportedBinding)
      ? Buffer.from(exportedBinding)
      : Buffer.from(exportedBinding, 'utf8');
  }

  /**
   * 返回本次连接的 tls-exporter 绑定字节。
   * 同一连接多次导出必须返回同一字节；不同连接互不相同。
   */
  exportChannelBinding(label = TLS_EXPORTER) {
    if (label !== TLS_EXPORTER) {
      throw new ScramError(
        CODES.CHANNEL_BINDING_UNSUPPORTED,
        `unsupported channel binding label: ${label}`,
      );
    }
    return Buffer.from(this._binding);
  }
}

module.exports = { TlsConnectionAdapter };
