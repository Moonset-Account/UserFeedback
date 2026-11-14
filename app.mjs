#!/usr/bin/env node
// CLI 主入口：读取离线结构化记录（JSON），输出消息定界一致性审计报告。
//
// 用法:
//   node app.mjs <input.json> [--json]
//
// 退出码:
//   0  分析完成，未发现边界分歧 / 非法记录 / 冲突记录
//   2  分析完成，但存在边界分歧、非法记录或 record_id 冲突（发布检查应阻断）
//   1  用法错误、输入文件不可读或 JSON 解析失败
import { readFile } from 'node:fs/promises';
import { analyzeBatch, renderText } from './lib/analyze.mjs';

function usage() {
  return 'usage: node app.mjs <input.json> [--json]';
}

async function main(argv) {
  const args = [...argv];
  let asJson = false;
  const jsonIdx = args.indexOf('--json');
  if (jsonIdx !== -1) {
    asJson = true;
    args.splice(jsonIdx, 1);
  }
  if (args.length !== 1) {
    console.error(usage());
    return 1;
  }
  let input;
  try {
    const raw = await readFile(args[0], 'utf8');
    input = JSON.parse(raw);
  } catch (err) {
    console.error(`error: cannot read/parse input file: ${err.message}`);
    return 1;
  }
  let report;
  try {
    report = analyzeBatch(input);
  } catch (err) {
    console.error(`error: ${err.message}`);
    return 1;
  }
  if (asJson) {
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  } else {
    process.stdout.write(`${renderText(report)}\n`);
  }
  const blocking =
    report.summary.boundary_divergence > 0 ||
    report.summary.invalid_records > 0 ||
    report.summary.conflicts > 0;
  return blocking ? 2 : 0;
}

main(process.argv.slice(2))
  .then((code) => {
    process.exitCode = code;
  })
  .catch((err) => {
    console.error(`error: ${err.message}`);
    process.exitCode = 1;
  });
