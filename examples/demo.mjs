import { evaluateGate } from '../src/index.js';
import { buildGateInput } from '../tests/fixtures/baseline.js';

/**
 * 演示入口（非测试代码）：展示一次 ALLOW 与一次 BLOCK 的真实门禁输出。
 * 运行：node examples/demo.mjs
 */
const allowed = evaluateGate(buildGateInput());
console.log('=== 场景一：范围收窄 + 安全选项可映射（预期 ALLOW）===');
console.log(JSON.stringify({ decision: allowed.decision, findings: allowed.findings, effective: allowed.effective }, null, 2));

const blockedInput = buildGateInput();
// 执行侧把范围扩大到 admin，且回执来自已淘汰的 v2
blockedInput.execution.parse[0].permissions = ['read', 'write', 'admin'];
blockedInput.receipts[1].versionLabel = 'v2';
const blocked = evaluateGate(blockedInput);
console.log('\n=== 场景二：范围扩大 + 旧版本回执迟到（预期 BLOCK，含差异定位）===');
console.log(JSON.stringify({ decision: blocked.decision, findings: blocked.findings }, null, 2));
