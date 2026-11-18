import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { evaluateGate, GateInputError } from '../src/index.js';
import { buildGateInput } from './fixtures/baseline.js';

describe('门禁输入契约', () => {
  it('缺少请求摘要 -> GateInputError（编程错误，区别于业务 BLOCK）', () => {
    const input = buildGateInput();
    delete input.digest;
    assert.throws(() => evaluateGate(input), GateInputError);
  });

  it('解析清单不是数组 -> GateInputError', () => {
    const input = buildGateInput();
    input.execution.parse = { nope: true };
    assert.throws(() => evaluateGate(input), GateInputError);
  });

  it('语义表未声明 identifier 字段 -> GateInputError，无法按业务标识对齐', () => {
    const input = buildGateInput();
    for (const table of input.semanticTables['execution-svc']) {
      table.fields = table.fields.filter((f) => f.role !== 'identifier');
    }
    assert.throws(() => evaluateGate(input), GateInputError);
  });

  it('回执结构不完整 -> GateInputError', () => {
    const input = buildGateInput();
    input.receipts[0].sequence = 'one';
    assert.throws(() => evaluateGate(input), GateInputError);
  });
});
