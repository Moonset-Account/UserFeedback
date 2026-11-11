'use strict';

const fs = require('node:fs');
const path = require('node:path');

/** 策略违规（目录限制、不可变参数被覆盖等）。 */
class PolicyViolation extends Error {
  constructor(message) {
    super(message);
    this.name = 'PolicyViolation';
  }
}

function loadJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

/**
 * 校验并解析工作目录：必须落在任一允许的根目录之内，且真实存在。
 */
function resolveWorkdir(task, policy, baseDir) {
  const resolved = path.resolve(baseDir, task.workdir);
  const inside = policy.allowedWorkdirRoots.some((root) => {
    const r = path.resolve(baseDir, root);
    return resolved === r || resolved.startsWith(r + path.sep);
  });
  if (!inside) {
    throw new PolicyViolation(
      `workdir "${task.workdir}" 不在允许的根目录内`
    );
  }
  if (!fs.existsSync(resolved) || !fs.statSync(resolved).isDirectory()) {
    throw new PolicyViolation(`workdir "${task.workdir}" 不存在或不是目录`);
  }
  return resolved;
}

/**
 * 构造参数数组。
 * - 不可变参数固定在最前，任务记录不得重复设置不可变 flag；
 * - value 为 null/undefined 视为“缺失”：整个参数（含 flag）不下发；
 * - value 为空字符串是合法取值：作为独立的数组元素下发。
 */
function buildArgv(task, policy) {
  const argv = [...policy.immutableArgs];
  const immutableFlags = new Set(policy.immutableFlags);
  for (const arg of task.args || []) {
    if (immutableFlags.has(arg.name)) {
      throw new PolicyViolation(
        `任务试图覆盖不可变参数 "${arg.name}"`
      );
    }
    if (arg.value === null || arg.value === undefined) continue; // 缺失 → 省略
    argv.push(arg.name, String(arg.value)); // 空字符串保留为独立元素
  }
  return argv;
}

/**
 * 构造环境对象。
 * - 每次调用返回全新对象，绝不复用（重试不得共享前一次的环境对象）；
 * - 只包含策略批准的变量，取值仅来自任务记录，绝不继承 process.env。
 */
function buildEnv(task, policy) {
  const env = {};
  const taskEnv = task.env || {};
  for (const key of policy.approvedEnvVars) {
    if (Object.prototype.hasOwnProperty.call(taskEnv, key)) {
      env[key] = String(taskEnv[key]);
    }
  }
  return env;
}

/**
 * 执行一次任务调用。
 * 适配器抛错时：审计日志只记录任务编号与字段名（不记录任何取值），随后向上抛出。
 */
function invoke(task, { adapter, policy, baseDir, audit }) {
  const cwd = resolveWorkdir(task, policy, baseDir);
  const args = buildArgv(task, policy);
  const env = buildEnv(task, policy);
  try {
    const result = adapter.spawn(task.program, args, { cwd, env });
    if (audit) audit.write({ taskId: task.taskId, outcome: 'ok' });
    return result;
  } catch (err) {
    if (audit) {
      audit.write({
        taskId: task.taskId,
        outcome: 'adapter-error',
        fields: ['program', 'args', 'cwd', 'env'], // 仅字段名，不含取值
      });
    }
    throw err;
  }
}

module.exports = {
  PolicyViolation,
  loadJson,
  resolveWorkdir,
  buildArgv,
  buildEnv,
  invoke,
};
