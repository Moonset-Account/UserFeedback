#!/usr/bin/env node
// 处置模块命令行入口（实际入口）。
//
// 用法：
//   quarantine --root <处置工作区> quarantine --detection <检测结果JSON文件>
//   quarantine --root <处置工作区> restore   --object-id <对象标识>
//   quarantine --root <处置工作区> reconcile
//   quarantine --root <处置工作区> status    [--object-id <对象标识>]
//
// 检测结果 JSON 文件示例：
//   {
//     "objectId": "det-1001",
//     "originalPath": "/tmp/sample/malware.bin",
//     "digest": "<sha256 hex>",
//     "owner": "host-a/user-7",
//     "summary": "Trojan.Generic 检测命中"
//   }
//
// 退出码：0 成功；10 重新确认阻断；11 还原冲突（双份已保留）；
//         20 其他处置错误；21 输入 / 文件错误。
import { readFile } from 'node:fs/promises';
import { QuarantineManager, QuarantineError } from './quarantine.js';

function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (token.startsWith('--')) {
      const key = token.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--')) {
        args[key] = true;
      } else {
        args[key] = next;
        i += 1;
      }
    } else {
      args._.push(token);
    }
  }
  return args;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const command = args._[0];
  if (!command || args.help) {
    process.stderr.write('可用命令: quarantine | restore | reconcile | status\n');
    process.exit(21);
  }
  if (!args.root) {
    process.stderr.write('缺少 --root <处置工作区>\n');
    process.exit(21);
  }

  const manager = await new QuarantineManager(args.root).init();

  switch (command) {
    case 'quarantine': {
      if (!args.detection) {
        process.stderr.write('缺少 --detection <检测结果JSON文件>\n');
        process.exit(21);
      }
      const detection = JSON.parse(await readFile(args.detection, 'utf8'));
      const result = await manager.quarantine(detection);
      process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
      return;
    }
    case 'restore': {
      if (!args['object-id']) {
        process.stderr.write('缺少 --object-id <对象标识>\n');
        process.exit(21);
      }
      const result = await manager.restore(args['object-id']);
      process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
      return;
    }
    case 'reconcile': {
      const report = await manager.reconcile();
      process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
      return;
    }
    case 'status': {
      if (args['object-id']) {
        process.stdout.write(`${JSON.stringify(manager.getStatus(args['object-id']), null, 2)}\n`);
      } else {
        process.stdout.write(`${JSON.stringify(manager.listEntries(), null, 2)}\n`);
      }
      return;
    }
    default:
      process.stderr.write(`未知命令: ${command}\n`);
      process.exit(21);
  }
}

main().catch((error) => {
  const payload = {
    error: error.name,
    code: error.code ?? 'INTERNAL',
    message: error.message,
    details: error.details ?? {},
  };
  process.stderr.write(`${JSON.stringify(payload, null, 2)}\n`);
  if (error instanceof QuarantineError) {
    if (error.code === 'RECONFIRM_REQUIRED') process.exit(10);
    if (error.code === 'RESTORE_CONFLICT') process.exit(11);
    process.exit(20);
  }
  process.exit(21);
});
