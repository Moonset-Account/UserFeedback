// tests/coverage-gate.test.mjs
//
// 使用 Node.js 内置 node:test + node:assert，无外部依赖。
// 运行：node --test tests/

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import {
  IMPLICIT_EXIT_ID,
  collectAddedLines,
  evaluateCoverageGate,
} from '../src/coverage-gate.js';

const FILE = 'src/auth.js';
const SUMMARY = 'sha-v2';

// 标准差异：新版本第 11 行是替换出来的 if（分支行），第 12 行是新增普通语句，
// 第 13 行是新增注释（不可执行）。第 10 行是未改动的 context 行。
function standardHunk() {
  return {
    oldStart: 10,
    oldCount: 2,
    newStart: 10,
    newCount: 4,
    lines: [
      { type: 'context', text: 'function login(user) {' },
      { type: 'delete', text: '  if (user.admin) {' },
      { type: 'add', text: '  if (user.role === "admin") {' },
      { type: 'add', text: '    grantAdmin(user);' },
      { type: 'add', text: '    // audit marker' },
    ],
  };
}

function standardExecutable() {
  return [
    {
      file: { path: FILE, summary: SUMMARY },
      executableLines: [
        { line: 11, exits: [{ id: 'true' }, { id: 'false' }] },
        { line: 12 }, // 未列出口 → 单一隐式出口
      ],
    },
  ];
}

function baseInput(overrides = {}) {
  return {
    files: [{ path: FILE, summary: SUMMARY }],
    changes: [{ file: { path: FILE, summary: SUMMARY }, hunks: [standardHunk()] }],
    executable: standardExecutable(),
    requiredShardIds: ['shard-a', 'shard-b'],
    shardRecords: [],
    ...overrides,
  };
}

function shardRecord(shardId, attempt, success, files) {
  return {
    shardId,
    attempt,
    success,
    coverage: files === undefined ? { files: [] } : { files },
  };
}

function fileCoverage(path, summary, lines) {
  return { path, summary, lines };
}

describe('collectAddedLines 纯函数：逐行差异行号推进', () => {
  it('context/add 推进新版本行号，delete 不推进，只收集 add 行', () => {
    const hunk = {
      oldStart: 5,
      oldCount: 4,
      newStart: 5,
      newCount: 5,
      lines: [
        { type: 'context', text: 'a' }, // 新行 5
        { type: 'delete', text: 'b' }, // 不占新行
        { type: 'add', text: 'c' }, // 新行 6
        { type: 'add', text: 'd' }, // 新行 7
        { type: 'context', text: 'e' }, // 新行 8
        { type: 'delete', text: 'f' },
        { type: 'add', text: 'g' }, // 新行 9
      ],
    };
    assert.deepEqual(collectAddedLines(hunk), [6, 7, 9]);
  });

  it('空 hunk（纯重命名/无内容差异）返回空数组', () => {
    assert.deepEqual(collectAddedLines({ newStart: 1, lines: [] }), []);
  });

  it('非法结构抛出 TypeError', () => {
    assert.throws(() => collectAddedLines({ lines: [] }), TypeError);
    assert.throws(
      () => collectAddedLines({ newStart: 1, lines: [{ type: 'bogus' }] }),
      TypeError,
    );
  });
});

describe('正常场景：分片收齐且全部出口命中 → 放行', () => {
  it('两个分片分别覆盖分支两个出口和普通行，status=pass', () => {
    const input = baseInput({
      shardRecords: [
        shardRecord('shard-a', 1, true, [
          fileCoverage(FILE, SUMMARY, [
            { line: 11, exits: ['true'] },
            { line: 12, executed: true },
          ]),
        ]),
        shardRecord('shard-b', 1, true, [
          fileCoverage(FILE, SUMMARY, [{ line: 11, exits: ['false'] }]),
        ]),
      ],
    });

    const result = evaluateCoverageGate(input);
    assert.equal(result.status, 'pass');
    assert.deepEqual(result.pendingShards, []);
    assert.deepEqual(result.missing, []);
    assert.equal(result.targets.length, 2);
    assert.deepEqual(
      result.targets.map((t) => t.line).sort((a, b) => a - b),
      [11, 12],
    );
  });

  it('多文件多 hunk 交错覆盖：分片各自负责不同文件/出口，合并后放行', () => {
    const file2 = 'src/util.js';
    const summary2 = 'sha-util-1';
    const input = {
      files: [
        { path: FILE, summary: SUMMARY },
        { path: file2, summary: summary2 },
      ],
      changes: [
        { file: { path: FILE, summary: SUMMARY }, hunks: [standardHunk()] },
        {
          file: { path: file2, summary: summary2 },
          hunks: [
            {
              oldStart: 1,
              oldCount: 1,
              newStart: 1,
              newCount: 2,
              lines: [
                { type: 'delete', text: 'const x = 1;' },
                { type: 'add', text: 'const x = 2;' },
                { type: 'add', text: 'const y = x + 1;' },
              ],
            },
          ],
        },
      ],
      executable: [
        ...standardExecutable(),
        {
          file: { path: file2, summary: summary2 },
          executableLines: [
            { line: 1 },
            {
              line: 2,
              exits: [{ id: 'short' }, { id: 'long' }],
            },
          ],
        },
      ],
      requiredShardIds: ['shard-a', 'shard-b'],
      shardRecords: [
        // shard-a 只覆盖 auth.js
        shardRecord('shard-a', 1, true, [
          fileCoverage(FILE, SUMMARY, [
            { line: 11, exits: ['true', 'false'] },
            { line: 12 },
          ]),
        ]),
        // shard-b 只覆盖 util.js
        shardRecord('shard-b', 1, true, [
          fileCoverage(file2, summary2, [
            { line: 1, executed: true },
            { line: 2, exits: ['short', 'long'] },
          ]),
        ]),
      ],
    };

    const result = evaluateCoverageGate(input);
    assert.equal(result.status, 'pass');
    assert.deepEqual(result.missing, []);
    assert.equal(result.targets.length, 4);
  });
});

describe('边界：分片暂缓（pending）', () => {
  it('必需分片完全没有记录 → pending 并列出分片', () => {
    const input = baseInput({
      shardRecords: [
        shardRecord('shard-a', 1, true, [
          fileCoverage(FILE, SUMMARY, [{ line: 11, exits: ['true', 'false'] }]),
        ]),
      ],
    });

    const result = evaluateCoverageGate(input);
    assert.equal(result.status, 'pending');
    assert.deepEqual(result.pendingShards, ['shard-b']);
    assert.deepEqual(result.missing, []); // 暂缓时不下缺测定论
    assert.equal(result.targets.length, 2);
  });

  it('最高尝试失败 → 即使低尝试成功也暂缓，且不合并低尝试命中', () => {
    const input = baseInput({
      shardRecords: [
        shardRecord('shard-a', 1, true, [
          fileCoverage(FILE, SUMMARY, [
            { line: 11, exits: ['true', 'false'] },
            { line: 12 },
          ]),
        ]),
        shardRecord('shard-a', 2, false, []),
        shardRecord('shard-b', 1, true, []),
      ],
    });

    const result = evaluateCoverageGate(input);
    assert.equal(result.status, 'pending');
    assert.deepEqual(result.pendingShards, ['shard-a']);
    // 低尝试的命中不得泄漏进合并结果。
    assert.deepEqual(result.mergedHits, []);
  });

  it('非必需分片的记录不参与判断，只出现在 ignored 中', () => {
    const input = baseInput({
      shardRecords: [
        shardRecord('shard-x', 1, true, [
          fileCoverage(FILE, SUMMARY, [
            { line: 11, exits: ['true', 'false'] },
            { line: 12 },
          ]),
        ]),
      ],
    });

    const result = evaluateCoverageGate(input);
    assert.equal(result.status, 'pending');
    assert.deepEqual(result.pendingShards, ['shard-a', 'shard-b']);
    assert.ok(
      result.ignored.some((i) => i.shardId === 'shard-x' && i.reason === 'not-required'),
    );
    assert.deepEqual(result.mergedHits, []);
  });
});

describe('核心规则：整份替换 + 分支出口维度统计', () => {
  it('重跑撤掉某分支唯一命中：同行仍被其他分片执行，缺测仍列出该分支', () => {
    // 第一阶段：两个分片凑齐两个出口 → 放行。
    const firstRun = baseInput({
      shardRecords: [
        shardRecord('shard-a', 1, true, [
          fileCoverage(FILE, SUMMARY, [
            { line: 11, exits: ['true'] },
            { line: 12 },
          ]),
        ]),
        shardRecord('shard-b', 1, true, [
          fileCoverage(FILE, SUMMARY, [{ line: 11, exits: ['false'] }]),
        ]),
      ],
    });
    assert.equal(evaluateCoverageGate(firstRun).status, 'pass');

    // 第二阶段：shard-b 重跑（更高尝试号、成功），新版本不再命中 false 出口。
    const rerun = baseInput({
      shardRecords: [
        shardRecord('shard-a', 1, true, [
          fileCoverage(FILE, SUMMARY, [
            { line: 11, exits: ['true'] }, // 第 11 行仍被该分片执行
            { line: 12 },
          ]),
        ]),
        shardRecord('shard-b', 2, true, [
          fileCoverage(FILE, SUMMARY, [{ line: 11, exits: [] }]),
        ]),
      ],
    });

    const result = evaluateCoverageGate(rerun);
    assert.equal(result.status, 'missing');
    assert.equal(result.missing.length, 1);
    const item = result.missing[0];
    assert.equal(item.path, FILE);
    assert.equal(item.line, 11);
    assert.equal(item.kind, 'branch');
    assert.deepEqual(item.missingExits, ['false']);
    assert.deepEqual(item.coveredExits, ['true']); // 行本身仍被执行过

    // 合并结果里第 11 行只剩 true 出口，false 已随旧尝试整份消失。
    const line11 = result.mergedHits.find((h) => h.path === FILE && h.line === 11);
    assert.deepEqual(line11.exits, ['true']);
  });

  it('同分片高尝试整份替换低尝试：不跨尝试累积出口', () => {
    // 记录顺序故意颠倒，验证按 attempt 而非数组顺序选择。
    const input = baseInput({
      shardRecords: [
        shardRecord('shard-a', 2, true, [
          fileCoverage(FILE, SUMMARY, [{ line: 11, exits: ['true'] }]),
        ]),
        shardRecord('shard-a', 1, true, [
          fileCoverage(FILE, SUMMARY, [
            { line: 11, exits: ['true', 'false'] },
            { line: 12 },
          ]),
        ]),
        shardRecord('shard-b', 1, true, [fileCoverage(FILE, SUMMARY, [{ line: 12 }])]),
      ],
    });

    const result = evaluateCoverageGate(input);
    assert.equal(result.status, 'missing');
    assert.deepEqual(
      result.missing.map((m) => ({ line: m.line, exits: m.missingExits })),
      [{ line: 11, exits: ['false'] }],
    );
  });

  it('普通可执行行完全未命中：按隐式出口报 line 级缺测', () => {
    const input = baseInput({
      shardRecords: [
        shardRecord('shard-a', 1, true, [
          fileCoverage(FILE, SUMMARY, [{ line: 11, exits: ['true', 'false'] }]),
        ]),
        shardRecord('shard-b', 1, true, []), // 成功但零覆盖
      ],
    });

    const result = evaluateCoverageGate(input);
    assert.equal(result.status, 'missing');
    assert.equal(result.missing.length, 1);
    assert.deepEqual(result.missing[0], {
      path: FILE,
      line: 12,
      kind: 'line',
      missingExits: [IMPLICIT_EXIT_ID],
      coveredExits: [],
    });
  });
});

describe('边界：摘要匹配与候选范围', () => {
  it('文件摘要不匹配的命中整份忽略并给出原因', () => {
    const input = baseInput({
      shardRecords: [
        shardRecord('shard-a', 1, true, [
          fileCoverage(FILE, 'sha-stale', [
            { line: 11, exits: ['true', 'false'] },
            { line: 12 },
          ]),
        ]),
        shardRecord('shard-b', 1, true, []),
      ],
    });

    const result = evaluateCoverageGate(input);
    assert.equal(result.status, 'missing');
    assert.ok(
      result.ignored.some(
        (i) => i.shardId === 'shard-a' && i.path === FILE && i.reason === 'summary-mismatch',
      ),
    );
    assert.deepEqual(result.mergedHits, []);
    assert.equal(result.missing.length, 2);
  });

  it('覆盖了候选清单之外的路径：忽略并标注 unknown-path', () => {
    const input = baseInput({
      shardRecords: [
        shardRecord('shard-a', 1, true, [
          fileCoverage('src/other.js', 'whatever', [{ line: 1 }]),
        ]),
        shardRecord('shard-b', 1, true, []),
      ],
    });

    const result = evaluateCoverageGate(input);
    assert.ok(
      result.ignored.some((i) => i.reason === 'unknown-path' && i.path === 'src/other.js'),
    );
    assert.equal(result.status, 'missing');
  });
});

describe('待测行范围：只考查新增/替换的可执行行', () => {
  it('新增但不可执行的行（注释）不进入 targets', () => {
    const result = evaluateCoverageGate(baseInput());
    assert.deepEqual(
      result.targets.map((t) => t.line),
      [11, 12],
    );
  });

  it('未改动的 context 可执行行即使无覆盖也不算缺测', () => {
    const executable = standardExecutable();
    executable[0].executableLines.push({ line: 10 }); // context 行，可执行但未改动
    const result = evaluateCoverageGate(baseInput({ executable }));
    assert.ok(!result.targets.some((t) => t.line === 10));
  });

  it('纯重命名（零 hunk）不产生待测行，无需分片即可放行', () => {
    const input = baseInput({
      changes: [{ file: { path: FILE, summary: SUMMARY }, hunks: [] }],
      requiredShardIds: [],
      shardRecords: [],
    });
    const result = evaluateCoverageGate(input);
    assert.equal(result.status, 'pass');
    assert.deepEqual(result.targets, []);
    assert.deepEqual(result.missing, []);
  });

  it('没有必需分片但有待测行 → missing', () => {
    const input = baseInput({ requiredShardIds: [] });
    const result = evaluateCoverageGate(input);
    assert.equal(result.status, 'missing');
    assert.equal(result.missing.length, 2);
  });
});

describe('输入校验', () => {
  it('差异引用候选清单之外的文件抛 TypeError', () => {
    const input = baseInput();
    input.changes[0].file.path = 'src/ghost.js';
    assert.throws(() => evaluateCoverageGate(input), /不在候选清单/);
  });

  it('编译器数据摘要与候选文件不一致抛 TypeError', () => {
    const input = baseInput();
    input.executable[0].file.summary = 'sha-other';
    assert.throws(() => evaluateCoverageGate(input), TypeError);
  });

  it('同分片同尝试号重复记录抛 TypeError', () => {
    const input = baseInput({
      shardRecords: [
        shardRecord('shard-a', 1, true, []),
        shardRecord('shard-a', 1, true, []),
        shardRecord('shard-b', 1, true, []),
      ],
    });
    assert.throws(() => evaluateCoverageGate(input), /重复记录/);
  });

  it('非法 attempt / success / 结构抛 TypeError', () => {
    assert.throws(
      () => evaluateCoverageGate(baseInput({ shardRecords: [{ shardId: 's', attempt: 0 }] })),
      TypeError,
    );
    assert.throws(
      () =>
        evaluateCoverageGate(
          baseInput({ shardRecords: [{ shardId: 's', attempt: 1, success: 'yes' }] }),
        ),
      TypeError,
    );
    assert.throws(() => evaluateCoverageGate(baseInput({ files: 'nope' })), TypeError);
  });
});
