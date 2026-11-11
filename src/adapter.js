'use strict';

/**
 * 内存适配器：模拟子进程调用边界（替代 child_process.spawn）。
 * 记录每次调用的程序标识、参数数组和批准后的环境变量。
 *
 * 注意：env 按引用保存而非拷贝——回归套件需要据此检测
 * 调用方是否在多次调用/重试之间共享同一个环境对象。
 */
class InMemoryAdapter {
  constructor() {
    this.calls = [];
    this._failNextWith = null;
  }

  /** 让下一次 spawn 抛出指定错误（用于审计日志回归测试）。 */
  failNextWith(error) {
    this._failNextWith = error;
  }

  /**
   * 模拟 spawn(program, args, { cwd, env })。
   * 契约：args 必须是数组（参数边界由数组元素表达，不允许拼接成字符串）。
   */
  spawn(program, args, options = {}) {
    if (this._failNextWith) {
      const err = this._failNextWith;
      this._failNextWith = null;
      throw err;
    }
    this.calls.push({
      program,
      argsIsArray: Array.isArray(args),
      args: Array.isArray(args) ? [...args] : args,
      cwd: options.cwd,
      env: options.env, // 按引用捕获，便于检测跨调用共享
    });
    return { pid: 4200 + this.calls.length, status: 0 };
  }

  get lastCall() {
    return this.calls[this.calls.length - 1];
  }
}

module.exports = { InMemoryAdapter };
