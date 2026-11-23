#!/usr/bin/env node
/**
 * Replay driver: prints the structured report produced by the same scenario
 * module the test suite uses, and exits non-zero if any check fails.
 *
 * Usage: node scripts/replay.mjs
 */
import { runSharedQueryReplay } from '../src/scenarios.js';

const report = await runSharedQueryReplay();
console.log(JSON.stringify(report, null, 2));

if (report.verdict !== 'PASS') {
  process.exitCode = 1;
}
