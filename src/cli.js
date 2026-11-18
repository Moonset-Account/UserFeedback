#!/usr/bin/env node
/**
 * 完整性判定 CLI：
 *   node src/cli.js --manifest ./fixtures/manifest.json --build-root ./build
 *
 * 退出码：
 *   0  正式入口全部通过（预览入口的问题仅在输出中标注，不影响退出码）
 *   1  正式入口存在阻断性违规，或清单/参数本身不合法
 */

import path from 'node:path';
import { analyzeIntegrity, loadManifest, ManifestError } from './integrity.js';

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith('--')) {
      throw new Error(`无法识别的参数: ${token}`);
    }
    const key = token.slice(2);
    const value = argv[i + 1];
    if (value === undefined || value.startsWith('--')) {
      throw new Error(`参数 --${key} 缺少取值`);
    }
    args[key] = value;
    i += 1;
  }
  return args;
}

function printReport(result) {
  const lines = [];
  lines.push(`构建目录: ${result.buildRoot}`);
  lines.push(`判定结论: ${result.ok ? '通过（正式入口无阻断违规）' : '阻断（正式入口存在完整性违规）'}`);

  for (const entry of result.entries) {
    lines.push('');
    lines.push(
      `入口 ${entry.id} [${entry.status}]${entry.blocking ? '' : '（预览，不阻塞）'} ` +
        `启用分支 ${entry.enabledBranchCount} 个，违规 ${entry.violations.length} 条`,
    );
    for (const branch of entry.branches) {
      lines.push(`  分支 ${branch.id} (region=${branch.region}) 引用链:`);
      for (const node of branch.chain) {
        const state = !node.safe
          ? '路径越界'
          : !node.exists
            ? '缺件'
            : node.approved
              ? `已批准 ${node.sha256.slice(0, 12)}…`
              : '未批准/摘要不符';
        lines.push(`    - [${node.kind}] ${node.script} -> ${state}`);
      }
    }
  }

  if (result.violations.length > 0) {
    lines.push('');
    lines.push(`阻断性违规（${result.violations.length} 条）:`);
    for (const v of result.violations) {
      lines.push(`  [${v.code}] 入口=${v.entryId} 分支=${v.branchId} 脚本=${v.script} — ${v.message}`);
    }
  }
  if (result.previewViolations.length > 0) {
    lines.push('');
    lines.push(`预览入口违规（${result.previewViolations.length} 条，不阻塞）:`);
    for (const v of result.previewViolations) {
      lines.push(`  [${v.code}] 入口=${v.entryId} 分支=${v.branchId} 脚本=${v.script} — ${v.message}`);
    }
  }
  return lines.join('\n');
}

async function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (err) {
    process.stderr.write(`参数错误: ${err.message}\n`);
    process.stderr.write('用法: node src/cli.js --manifest <清单路径> --build-root <构建目录>\n');
    process.exit(1);
  }

  if (!args.manifest || !args['build-root']) {
    process.stderr.write('用法: node src/cli.js --manifest <清单路径> --build-root <构建目录>\n');
    process.exit(1);
  }

  try {
    const manifest = await loadManifest(path.resolve(args.manifest));
    const result = await analyzeIntegrity({
      manifest,
      buildRoot: path.resolve(args['build-root']),
    });
    process.stdout.write(`${printReport(result)}\n`);
    process.exit(result.ok ? 0 : 1);
  } catch (err) {
    if (err instanceof ManifestError) {
      process.stderr.write(`清单错误: ${err.message}\n`);
    } else {
      process.stderr.write(`执行失败: ${err && err.stack ? err.stack : String(err)}\n`);
    }
    process.exit(1);
  }
}

main();
