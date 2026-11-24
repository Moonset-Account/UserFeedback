#!/usr/bin/env node
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

import { reviewPackage } from './index.mjs';
import { formatTextReport } from './format.mjs';

function parseArgs(argv) {
  const args = { positional: [], json: false, strict: false, strictResidual: false, help: false };
  for (const arg of argv) {
    if (arg === '--json') args.json = true;
    else if (arg === '--strict') {
      args.strict = true;
      args.strictResidual = true;
    } else if (arg === '--strict-residual') args.strictResidual = true;
    else if (arg === '--help' || arg === '-h') args.help = true;
    else if (arg.startsWith('--')) throw new Error(`Unknown option: ${arg}`);
    else args.positional.push(arg);
  }
  return args;
}

const help = `Usage: extension-review <package.json> [--json] [--strict] [--strict-residual]

Reviews browser variants, expands loading conditions, compares requested
authorization with variant approvals, and lists residual optional requests.

Options:
  --json               Print the full structured report as JSON
  --strict             Treat warnings (including residual requests) as errors
  --strict-residual    Give residual requests error severity
  -h, --help           Show this help
`;

async function loadPackage(filePath) {
  const resolved = path.resolve(process.cwd(), filePath);
  if (resolved.endsWith('.mjs') || resolved.endsWith('.js')) {
    const mod = await import(pathToFileURL(resolved).href);
    return mod.syntheticPackage ?? mod.package ?? mod.default ?? mod;
  }
  return JSON.parse(await readFile(resolved, 'utf8'));
}

async function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error(error.message);
    process.exit(2);
  }

  if (args.help || args.positional.length === 0) {
    process.stdout.write(help);
    process.exit(args.help ? 0 : 2);
  }

  const pkg = await loadPackage(args.positional[0]);
  const report = reviewPackage(pkg, {
    strict: args.strict,
    strictResidual: args.strictResidual,
  });

  const output = args.json ? JSON.stringify(report, null, 2) : formatTextReport(report);
  process.stdout.write(`${output}\n`, () => {
    process.exit(report.releaseApproved ? 0 : 1);
  });
}

main().catch((error) => {
  console.error(`Review failed: ${error instanceof Error ? error.stack : String(error)}`);
  process.exit(2);
});
