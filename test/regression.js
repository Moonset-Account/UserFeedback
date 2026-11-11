'use strict';

/**
 * 子进程接口回归套件。
 *
 * 退出码（位掩码，供 CI 区分失败类别）：
 *   0  全部通过
 *   1  其他失败（策略 / 审计 / 基础设施）
 *   2  参数边界丢失（arg-boundary）
 *   4  环境变量继承异常（env-inheritance）
 *   组合时按位或，例如 6 = 参数边界 + 环境继承同时失败。
 */

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

const { InMemoryAdapter } = require('../src/adapter');
const { AuditLog } = require('../src/audit');
const {
  PolicyViolation,
  loadJson,
  invoke,
} = require('../src/invoker');

const ROOT = path.resolve(__dirname, '..');
const POLICY = loadJson(path.join(ROOT, 'config', 'policy.json'));

const EXIT_BIT = {
  other: 1,
  'arg-boundary': 2,
  'env-inheritance': 4,
};

const tests = [];
function test(category, name, fn) {
  tests.push({ category, name, fn });
}

function loadTask(name) {
  return loadJson(path.join(ROOT, 'tasks', name));
}

function freshContext() {
  return { adapter: new InMemoryAdapter(), policy: POLICY, baseDir: ROOT };
}

// ---------- 参数边界 ----------

test('arg-boundary', '含空格的合法参数作为单个数组元素传入', () => {
  const ctx = freshContext();
  invoke(loadTask('task-1001.json'), ctx);
  const call = ctx.adapter.lastCall;

  assert.equal(call.program, 'batch-worker', '程序标识应原样传递');
  assert.equal(call.argsIsArray, true, '参数必须以数组形式传递，禁止拼接为字符串');
  assert.ok(
    call.args.includes('daily report 2026-11-11.csv'),
    `含空格的文件名应作为单个元素存在，实际 args=${JSON.stringify(call.args)}`
  );
  // 若边界丢失，文件名会被拆成 4 段，数组长度随之膨胀
  assert.deepEqual(call.args, [
    '--format', 'json',
    '--input', 'daily report 2026-11-11.csv',
    '--note', '',
  ]);
});

test('arg-boundary', '空字符串与缺失值可以区分', () => {
  const ctx = freshContext();
  invoke(loadTask('task-1001.json'), ctx);
  const { args } = ctx.adapter.lastCall;

  const noteIdx = args.indexOf('--note');
  assert.notEqual(noteIdx, -1, '空字符串参数的 flag 应保留');
  assert.equal(args[noteIdx + 1], '', '空字符串应作为独立元素下发');
  assert.ok(
    Object.prototype.hasOwnProperty.call(args, noteIdx + 1) ||
      noteIdx + 1 < args.length,
    '空字符串元素不得被丢弃'
  );
  assert.ok(
    !args.includes('--optional-comment'),
    '缺失值（null）的参数应整体省略，flag 不得出现'
  );
});

// ---------- 策略：不可变参数与目录限制 ----------

test('other', '不可变参数固定在最前且不可被任务覆盖', () => {
  const ctx = freshContext();
  invoke(loadTask('task-1001.json'), ctx);
  const { args } = ctx.adapter.lastCall;
  assert.deepEqual(args.slice(0, 2), ['--format', 'json'], '不可变参数应位于最前');

  assert.throws(
    () => invoke(loadTask('task-1003.json'), freshContext()),
    PolicyViolation,
    '任务试图覆盖 --format 应被拒绝'
  );
});

test('other', '目录限制生效：工作目录必须落在允许的根目录内', () => {
  assert.throws(
    () => invoke(loadTask('task-1002.json'), freshContext()),
    PolicyViolation,
    'workdir 越出允许根目录应被拒绝'
  );

  // 合法目录正常解析
  const ctx = freshContext();
  invoke(loadTask('task-1001.json'), ctx);
  assert.equal(
    ctx.adapter.lastCall.cwd,
    path.join(ROOT, 'workdirs', 'alpha'),
    '合法 workdir 应解析为绝对路径'
  );
});

// ---------- 环境变量继承 ----------

test('env-inheritance', '只下发批准的变量，不继承 process.env，未批准键被过滤', () => {
  process.env.LEAKED_SECRET = 'should-not-propagate';
  process.env.BATCH_PROFILE = 'spoofed-from-parent';
  try {
    const ctx = freshContext();
    invoke(loadTask('task-1001.json'), ctx);
    const { env } = ctx.adapter.lastCall;

    assert.equal(env.LEAKED_SECRET, undefined, '父进程环境变量不得泄漏给子进程');
    assert.equal(
      env.BATCH_PROFILE, 'nightly',
      '批准变量的取值必须来自任务记录，而非 process.env'
    );
    assert.equal(env.BATCH_REGION, 'cn-north-1');
    assert.equal(env.DEBUG_DUMP, undefined, '任务记录中未批准的键应被过滤');
    assert.deepEqual(
      Object.keys(env).sort(),
      ['BATCH_PROFILE', 'BATCH_REGION'],
      '环境对象应只包含批准的变量'
    );
  } finally {
    delete process.env.LEAKED_SECRET;
    delete process.env.BATCH_PROFILE;
  }
});

test('env-inheritance', '重试不共享前一次任务的环境对象', () => {
  const ctx = freshContext();
  const task = loadTask('task-1001.json');

  invoke(task, ctx); // 首次
  invoke(task, ctx); // 重试
  const [first, second] = ctx.adapter.calls;

  assert.notEqual(
    first.env, second.env,
    '两次调用不得共享同一个环境对象（引用必须不同）'
  );

  // 模拟首次调用后环境对象被污染，重试不得受影响
  first.env.INJECTED = 'tampered';
  first.env.BATCH_PROFILE = 'tampered';
  assert.equal(second.env.INJECTED, undefined, '前一次的污染不得带入重试');
  assert.equal(second.env.BATCH_PROFILE, 'nightly', '重试的环境取值不得被前一次改写');
});

// ---------- 审计日志 ----------

test('other', '适配器抛错时审计日志只包含任务编号和字段名', () => {
  const ctx = freshContext();
  const auditFile = path.join(
    fs.mkdtempSync(path.join(os.tmpdir(), 'audit-')),
    'audit.log'
  );
  const audit = new AuditLog(auditFile);
  ctx.adapter.failNextWith(new Error('spawn failed'));

  assert.throws(
    () => invoke(loadTask('task-1001.json'), { ...ctx, audit }),
    /spawn failed/
  );

  const entries = audit.readEntries();
  assert.equal(entries.length, 1, '应恰好写入一条审计记录');
  const entry = entries[0];
  assert.equal(entry.taskId, 'TASK-1001', '审计记录应包含任务编号');
  assert.deepEqual(
    entry.fields, ['program', 'args', 'cwd', 'env'],
    '审计记录应只列出字段名'
  );

  const raw = fs.readFileSync(auditFile, 'utf8');
  for (const secret of ['nightly', 'cn-north-1', 'daily report', 'batch-worker', 'workdirs']) {
    assert.ok(
      !raw.includes(secret),
      `审计日志不得包含取值 "${secret}"，实际内容：${raw}`
    );
  }
});

// ---------- 运行器 ----------

(async () => {
  let exitCode = 0;
  let failed = 0;
  for (const t of tests) {
    try {
      await t.fn();
      console.log(`PASS [${t.category}] ${t.name}`);
    } catch (err) {
      failed += 1;
      exitCode |= EXIT_BIT[t.category] || EXIT_BIT.other;
      console.error(`FAIL [${t.category}] ${t.name}`);
      console.error(`  ${err.message}`);
    }
  }
  console.log(
    `\n${tests.length - failed}/${tests.length} 通过；退出码 ${exitCode}` +
      '（1=其他, 2=参数边界丢失, 4=环境变量继承异常，可按位组合）'
  );
  process.exit(exitCode);
})();
