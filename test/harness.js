import assert from 'node:assert/strict';

/**
 * 场景级测试框架：
 *  - event() 记录带序号的事件顺序，立即打印，保证“每个场景的事件顺序”可见；
 *  - check() 包装断言，失败时累计而不是中断进程，最终统一以退出码反映结果；
 *  - 每个场景结束打印预期/实际对照与结论。
 */
export class Scenario {
  #seq = 0;
  #failures = [];
  #checks = 0;

  constructor(name) {
    this.name = name;
  }

  /** 记录并打印一个事件（事件顺序即调用顺序） */
  event(message, data) {
    this.#seq += 1;
    const suffix = data === undefined ? '' : `  ${JSON.stringify(data)}`;
    console.log(`    [${this.#seq}] ${message}${suffix}`);
  }

  /** 断言实际值深度等于预期值；失败被捕获并记录，不抛出中断 */
  check(label, actual, expected) {
    this.#checks += 1;
    try {
      assert.deepStrictEqual(actual, expected);
      console.log(`    ✓ ${label}`);
    } catch (err) {
      this.#failures.push({ label, expected, actual, message: err.message });
      console.log(`    ✗ ${label}`);
      console.log(`        预期: ${JSON.stringify(expected)}`);
      console.log(`        实际: ${JSON.stringify(actual)}`);
    }
  }

  /** 布尔断言（用于草稿快照等可读性更强的检查） */
  checkTrue(label, condition, evidence) {
    this.#checks += 1;
    if (condition) {
      console.log(`    ✓ ${label}`);
    } else {
      this.#failures.push({ label, expected: true, actual: condition, evidence });
      console.log(`    ✗ ${label}（证据: ${JSON.stringify(evidence) ?? '无'}）`);
    }
  }

  get passed() {
    return this.#failures.length === 0;
  }

  get failureCount() {
    return this.#failures.length;
  }

  get checkCount() {
    return this.#checks;
  }

  printSummary() {
    if (this.passed) {
      console.log(`    结论: 通过（${this.#checks} 项断言全部成立）\n`);
    } else {
      console.log(`    结论: 失败（${this.#failures.length}/${this.#checks} 项断言不成立）\n`);
      for (const f of this.#failures) {
        console.log(`      - ${f.label}: ${f.message.split('\n')[0]}`);
      }
      console.log('');
    }
  }
}

/** 运行全部场景，返回是否全部通过（供进程退出码使用） */
export async function runScenarios(scenarios) {
  let totalChecks = 0;
  let totalFailures = 0;
  for (const scenario of scenarios) {
    console.log(`\n=== 场景: ${scenario.name} ===`);
    await scenario.run();
    scenario.printSummary();
    totalChecks += scenario.checkCount;
    totalFailures += scenario.failureCount;
  }
  console.log('────────────────────────────────────────');
  console.log(`汇总: ${scenarios.length} 个场景，${totalChecks} 项断言，失败 ${totalFailures} 项`);
  return totalFailures === 0;
}
