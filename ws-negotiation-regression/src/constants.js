/**
 * 协商域常量与术语表。
 *
 * 背景：RFC 6455（WebSocket 协议）的 Sec-WebSocket-Extensions 握手 +
 * RFC 7692（permessage-deflate 扩展）。本套件完全离线运行，不发起任何网络请求，
 * 仅对“解析后的客户端声明 / 服务端能力表 / 响应决策记录”三份数据做回归审计。
 */

/** 协商最终判定（verdict） */
export const VERDICT = Object.freeze({
  /** 接受：响应参数全部落在策略声明的合法范围内，并与资源预算绑定 */
  ACCEPT: 'ACCEPT',
  /** 拒绝：声明畸形或参数越界，按策略不得回应该扩展 */
  REJECT: 'REJECT',
  /** 省略：服务端按策略决定不启用该可选扩展（合法降级，非错误） */
  OMITTED: 'OMITTED',
  /** 待确认：客户端声明不完整，v2 完整性闸门要求补全后再协商 */
  PENDING_CONFIRMATION: 'PENDING_CONFIRMATION',
});

/** 参数来源（provenance）——记录每个生效参数“从哪里来” */
export const SOURCE = Object.freeze({
  /** 客户端在 Sec-WebSocket-Extensions 中显式声明 */
  CLIENT_OFFER: 'client_offer',
  /** 服务端能力表给出的预算/默认值 */
  SERVER_CAPABILITY: 'server_capability',
  /** 策略在客户端缺省时注入的默认值 */
  POLICY_DEFAULT: 'policy_default',
  /** 策略按资源预算钳制后的计算值 */
  POLICY_CLAMP: 'policy_clamp',
  /** 策略强制注入（如 no_context_takeover） */
  POLICY_MANDATE: 'policy_mandate',
  /** RFC 7692 定义的协议级缺省 */
  RFC_DEFAULT: 'rfc_default',
  /** 旧策略默认分支的遗留回退（无策略约束，直接回显对端值） */
  LEGACY_FALLBACK: 'legacy_fallback',
});

export const SOURCE_LABELS = Object.freeze({
  [SOURCE.CLIENT_OFFER]: '客户端声明',
  [SOURCE.SERVER_CAPABILITY]: '服务端能力表',
  [SOURCE.POLICY_DEFAULT]: '策略默认',
  [SOURCE.POLICY_CLAMP]: '策略按预算钳制',
  [SOURCE.POLICY_MANDATE]: '策略强制注入',
  [SOURCE.RFC_DEFAULT]: 'RFC 协议缺省',
  [SOURCE.LEGACY_FALLBACK]: '旧策略遗留回显',
});

/** 策略条目编号（decision.policyRules 引用这些稳定 ID） */
export const RULE = Object.freeze({
  SP1: '子协议候选顺序选择（按客户端声明序取首个交集）',
  SP2: '无公共子协议则不回应子协议头',
  SP3: '无候选时仅回显协议版本',
  EX1: '可选扩展按能力表开关省略',
  EX2: '畸形扩展声明直接拒绝',
  PM1: '窗口位数取客户端声明与预算上限的较小值',
  PM2: '窗口位数合法范围取 RFC 与服务端预算交集',
  PM3: '无上下文接管由服务端强制注入',
  PM4: '扩展启用时必须绑定解压消息预算',
  PM5: '声明缺项进入待确认，禁止静默套用 RFC 缺省',
  PM6: '默认分支同样适用 PM-1..PM-4（v1 曾在此遗失限制）',
});

/** permessage-deflate 的四个已识别参数键 */
export const PMD = Object.freeze({
  SERVER_NO_CONTEXT_TAKEOVER: 'server_no_context_takeover',
  CLIENT_NO_CONTEXT_TAKEOVER: 'client_no_context_takeover',
  SERVER_MAX_WINDOW_BITS: 'server_max_window_bits',
  CLIENT_MAX_WINDOW_BITS: 'client_max_window_bits',
});

/** RFC 7692 第 7.1.1/7.1.2 节规定的窗口位数协议合法区间 */
export const RFC_WINDOW_BITS_MIN = 8;
export const RFC_WINDOW_BITS_MAX = 15;

/** 判定的中文释义（报告用） */
export const VERDICT_LABELS = Object.freeze({
  [VERDICT.ACCEPT]: '接受',
  [VERDICT.REJECT]: '拒绝',
  [VERDICT.OMITTED]: '省略（兼容性降级）',
  [VERDICT.PENDING_CONFIRMATION]: '待确认（声明不完整）',
});

/** 规则短 ID（带连字符）→ RULE 常量键，供报告表显式查找 */
export const RULE_ID_TO_KEY = Object.freeze({
  'SP-1': 'SP1',
  'SP-2': 'SP2',
  'SP-3': 'SP3',
  'EX-1': 'EX1',
  'EX-2': 'EX2',
  'PM-1': 'PM1',
  'PM-2': 'PM2',
  'PM-3': 'PM3',
  'PM-4': 'PM4',
  'PM-5': 'PM5',
  'PM-6': 'PM6',
});
