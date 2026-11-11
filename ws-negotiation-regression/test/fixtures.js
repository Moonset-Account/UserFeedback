/**
 * 离线协商夹具：模拟从生产日志/抓包捕获的客户端握手头（无任何真实网络数据）。
 * 每个用例附带 v1/v2 两版策略的预期矩阵，供 node:test 断言与报告生成共用。
 *
 * 用例编号映射任务要求：
 *   TC-01  从声明集合选择子协议（SP-1）
 *   TC-02  无公共子协议的降级分支（SP-2）
 *   TC-03  客户端未声明子协议（SP-3）
 *   TC-04  可选扩展按策略省略（EX-1，能力表关闭）
 *   TC-05  可选扩展按策略省略（EX-1，客户端未声明）
 *   TC-06  扩展参数与资源限制正确绑定（PM-1..PM-4，v1 配置分支）
 *   TC-07  高窗口位数声明在配置分支被钳制到预算（PM-1/PM-2）
 *   TC-08  ★回归核心：v1 默认分支遗失限制（PM-6 缺失，防护失效）
 *   TC-09  客户端声明不完整 → v2 单列待确认（PM-5）
 *   TC-10  协议级畸形/越界声明 → 拒绝（EX-2）
 */

export const FIXTURES = Object.freeze([
  {
    id: 'TC-01',
    title: '从声明集合按顺序选择子协议（首个不支持，取其后第一个交集）',
    rawHeaders: {
      // soap 不被支持；wamp/mqtt/chat.v2 均支持，按客户端顺序应选 wamp
      // （服务端自身偏好顺序为 mqtt 优先，以此区分两种选择策略）
      secWebSocketProtocol: 'soap, wamp, mqtt, chat.v2',
      secWebSocketExtensions:
        'permessage-deflate; server_max_window_bits=12; client_max_window_bits=12',
    },
    expected: {
      v1Configured: { subprotocol: 'wamp', extensionVerdict: 'ACCEPT' },
      v1Default: { subprotocol: 'wamp', extensionVerdict: 'ACCEPT' },
      v2: { subprotocol: 'wamp', extensionVerdict: 'ACCEPT' },
    },
  },
  {
    id: 'TC-02',
    title: '无公共子协议，省略子协议头（兼容性降级）',
    rawHeaders: {
      secWebSocketProtocol: 'soap, stomp',
      secWebSocketExtensions:
        'permessage-deflate; server_max_window_bits=10; client_max_window_bits=10',
    },
    expected: {
      v1Configured: { subprotocol: null, extensionVerdict: 'ACCEPT' },
      v1Default: { subprotocol: null, extensionVerdict: 'ACCEPT' },
      v2: { subprotocol: null, extensionVerdict: 'ACCEPT' },
    },
  },
  {
    id: 'TC-03',
    title: '客户端未声明子协议，仅回显协议版本',
    rawHeaders: {
      secWebSocketProtocol: '',
      secWebSocketExtensions:
        'permessage-deflate; server_max_window_bits=12; client_max_window_bits=12',
    },
    expected: {
      v1Configured: { subprotocol: null, extensionVerdict: 'ACCEPT' },
      v1Default: { subprotocol: null, extensionVerdict: 'ACCEPT' },
      v2: { subprotocol: null, extensionVerdict: 'ACCEPT' },
    },
  },
  {
    id: 'TC-04',
    title: '客户端请求服务端未启用的扩展，按策略省略',
    rawHeaders: {
      secWebSocketProtocol: 'mqtt',
      secWebSocketExtensions: 'x-supercompress; level=9',
    },
    expected: {
      v1Configured: { subprotocol: 'mqtt', extensionVerdict: 'OMITTED' },
      v1Default: { subprotocol: 'mqtt', extensionVerdict: 'OMITTED' },
      v2: { subprotocol: 'mqtt', extensionVerdict: 'OMITTED' },
    },
  },
  {
    id: 'TC-05',
    title: '客户端未声明任何扩展，扩展槽位省略',
    rawHeaders: {
      secWebSocketProtocol: 'chat.v2',
      secWebSocketExtensions: '',
    },
    expected: {
      v1Configured: { subprotocol: 'chat.v2', extensionVerdict: 'OMITTED' },
      v1Default: { subprotocol: 'chat.v2', extensionVerdict: 'OMITTED' },
      v2: { subprotocol: 'chat.v2', extensionVerdict: 'OMITTED' },
    },
  },
  {
    id: 'TC-06',
    title: '扩展参数与资源限制正确绑定（配置分支基准用例）',
    rawHeaders: {
      secWebSocketProtocol: 'mqtt',
      secWebSocketExtensions:
        'permessage-deflate; server_max_window_bits=12; client_max_window_bits=12',
    },
    expected: {
      v1Configured: { subprotocol: 'mqtt', extensionVerdict: 'ACCEPT' },
      v1Default: { subprotocol: 'mqtt', extensionVerdict: 'ACCEPT' },
      v2: { subprotocol: 'mqtt', extensionVerdict: 'ACCEPT' },
    },
  },
  {
    id: 'TC-07',
    title: '客户端声明 15 位窗口，配置分支钳制到预算 12',
    rawHeaders: {
      secWebSocketProtocol: 'mqtt',
      secWebSocketExtensions:
        'permessage-deflate; server_max_window_bits=15; client_max_window_bits=15; ' +
        'client_no_context_takeover',
    },
    expected: {
      v1Configured: { subprotocol: 'mqtt', extensionVerdict: 'ACCEPT', appliedWindow: 12 },
      v1Default: { subprotocol: 'mqtt', extensionVerdict: 'ACCEPT', appliedWindow: 15 },
      v2: { subprotocol: 'mqtt', extensionVerdict: 'ACCEPT', appliedWindow: 12 },
    },
  },
  {
    id: 'TC-08',
    title: '★回归核心：默认分支遗失全部资源限制（v1 缺陷复现）',
    rawHeaders: {
      secWebSocketProtocol: 'mqtt',
      secWebSocketExtensions:
        'permessage-deflate; server_max_window_bits=15; client_max_window_bits=15',
    },
    expected: {
      v1Configured: { subprotocol: 'mqtt', extensionVerdict: 'ACCEPT', appliedWindow: 12, budgetSafe: true },
      v1Default: { subprotocol: 'mqtt', extensionVerdict: 'ACCEPT', appliedWindow: 15, budgetSafe: false },
      v2: { subprotocol: 'mqtt', extensionVerdict: 'ACCEPT', appliedWindow: 12, budgetSafe: true },
    },
  },
  {
    id: 'TC-09',
    title: '客户端声明不完整（裸标志/缺项），v2 单列待确认',
    rawHeaders: {
      secWebSocketProtocol: 'mqtt',
      secWebSocketExtensions: 'permessage-deflate; client_max_window_bits',
    },
    expected: {
      v1Configured: { subprotocol: 'mqtt', extensionVerdict: 'ACCEPT' },
      v1Default: { subprotocol: 'mqtt', extensionVerdict: 'ACCEPT' },
      v2: { subprotocol: 'mqtt', extensionVerdict: 'PENDING_CONFIRMATION' },
    },
  },
  {
    id: 'TC-10',
    title: '协议级畸形声明（窗口位数越界/非整数），直接拒绝',
    rawHeaders: {
      secWebSocketProtocol: 'mqtt',
      secWebSocketExtensions:
        'permessage-deflate; server_max_window_bits=16; client_max_window_bits=12',
    },
    expected: {
      v1Configured: { subprotocol: 'mqtt', extensionVerdict: 'REJECT' },
      v1Default: { subprotocol: 'mqtt', extensionVerdict: 'REJECT' },
      v2: { subprotocol: 'mqtt', extensionVerdict: 'REJECT' },
    },
  },
]);
