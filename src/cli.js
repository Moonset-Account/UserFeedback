#!/usr/bin/env node
import { readFile } from 'node:fs/promises';
import process from 'node:process';
import { inspectPair, ReportStore } from './index.js';

function usage() {
  console.error(`用法: attribution-check --gateway <网关事件JSON> --business <业务端事件JSON> [--batch-id <批次号>]

退出码:
  0 检查通过（无阻止性问题）
  1 检查发现阻止性问题
  2 输入或参数错误`);
}

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (token === '--gateway' || token === '--business' || token === '--batch-id') {
      const value = argv[i + 1];
      if (value === undefined) throw new Error(`${token} 缺少参数值`);
      args[token.slice(2)] = value;
      i += 1;
    } else if (token === '-h' || token === '--help') {
      args.help = true;
    } else {
      throw new Error(`未知参数: ${token}`);
    }
  }
  return args;
}

async function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (err) {
    console.error(err.message);
    usage();
    process.exit(2);
  }
  if (args.help || !args.gateway || !args.business) {
    usage();
    process.exit(args.help ? 0 : 2);
  }

  let gateway;
  let business;
  try {
    gateway = JSON.parse(await readFile(args.gateway, 'utf8'));
    business = JSON.parse(await readFile(args.business, 'utf8'));
  } catch (err) {
    console.error(`读取输入失败: ${err.message}`);
    process.exit(2);
  }

  let result;
  try {
    if (args['batch-id']) {
      const store = new ReportStore();
      const imported = store.importPair(gateway, business, args['batch-id']);
      // 同一批次重复导入：再导一次验证幂等
      const again = store.importPair(gateway, business, args['batch-id']);
      result = { ...imported, repeatedImport: { deduped: again.deduped, newIssueCount: again.newIssueCount }, report: imported.report };
    } else {
      result = { report: inspectPair(gateway, business) };
    }
    result.report.generatedAt = new Date().toISOString();
  } catch (err) {
    console.error(`检查失败: ${err.message}`);
    process.exit(2);
  }

  console.log(JSON.stringify(result, null, 2));
  process.exit(result.report.blocking ? 1 : 0);
}

main().catch((err) => {
  console.error(`未预期错误: ${err.stack ?? err}`);
  process.exit(2);
});
