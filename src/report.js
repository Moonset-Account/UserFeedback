const SEV_ICON = { critical: '🟥', high: '🟧', medium: '🟨', low: '🟦', info: '⬜' };

export function toMarkdown(r, { maxPerSection = 60 } = {}) {
  const L = [];
  const verdict = r.ok ? '✅ pass' : `❌ fail (${r.blocking.length} blocking, threshold ${r.threshold})`;
  L.push(`## secscan ${r.secscanVersion} · ${r.mode} · ${verdict}`);
  L.push('');
  const ran = r.toolRuns.filter((t) => t.ran);
  const skipped = r.toolRuns.filter((t) => !t.ran);
  L.push(`Scanned ${r.detect.files} files in ${(r.ms / 1000).toFixed(1)}s. Tools: ${ran.map((t) => `${t.tool} ${t.version || ''}`.trim()).join(', ') || 'none'}.` +
    (skipped.length ? ` Skipped: ${skipped.map((t) => `${t.tool} (${t.reason})`).join('; ')}.` : ''));
  const errs = ran.filter((t) => t.error);
  if (errs.length) L.push(`\n⚠️ Tool errors: ${errs.map((t) => `${t.tool}: ${t.error}`).join(' · ')}`);
  L.push('');
  L.push('| | new | baseline | suppressed |');
  L.push('|---|---:|---:|---:|');
  L.push(`| findings | ${r.counts.new || 0} | ${r.counts.baseline || 0} | ${r.counts.suppressed || 0} |`);
  const sev = Object.entries(r.counts.bySeverity).sort((a, b) => sevRank(b[0]) - sevRank(a[0])).map(([s, n]) => `${SEV_ICON[s] || ''} ${n} ${s}`).join(' · ');
  if (sev) L.push(`\nNew by severity: ${sev}`);
  if (!r.baseline.exists) L.push('\n_No baseline yet. Run `secscan baseline update` after review to accept the current state._');
  else L.push(`\nBaseline: ${r.baseline.size} accepted finding(s), updated ${r.baseline.updatedAt}${r.baseline.commit ? ` at ${r.baseline.commit.slice(0, 8)}` : ''}.`);
  L.push('');

  if (r.newFindings.length) {
    L.push(`### New findings (${r.newFindings.length})`);
    L.push('');
    for (const f of r.newFindings.slice(0, maxPerSection)) L.push(fmtFinding(f));
    if (r.newFindings.length > maxPerSection) L.push(`\n…and ${r.newFindings.length - maxPerSection} more (see JSON/SARIF).`);
    L.push('');
  } else {
    L.push('### New findings\n\nNone.\n');
  }

  const depsWithChange = r.deps.filter((d) => d.added?.length || d.changed?.length || d.removed?.length || d.flags?.length || d.error);
  if (depsWithChange.length) {
    L.push('### Dependency changes since baseline');
    L.push('');
    for (const d of depsWithChange) {
      if (d.error) { L.push(`- ${d.lockfile}: error ${d.error}`); continue; }
      L.push(`**${d.lockfile}** (${d.total} packages${d.since ? `, since ${String(d.since).slice(0, 8)}` : ', no baseline commit'}): +${d.added.length} added, ~${d.changed.length} changed, -${d.removed.length} removed`);
      for (const fl of d.flags) L.push(`- ⚠️ ${fl.kind}: \`${fl.package}@${fl.version}\` — ${fl.detail}`);
      const show = [...d.added.map((p) => `+ ${p.name}@${p.version}${p.dev ? ' (dev)' : ''}${p.meta?.ageDays != null ? `, ${p.meta.ageDays}d old` : ''}`),
        ...d.changed.map((p) => `~ ${p.name} ${p.from} → ${p.version}`)].slice(0, 30);
      if (show.length) { L.push('```'); L.push(...show); L.push('```'); }
    }
    L.push('');
  }

  if (r.resolved.length) {
    L.push(`### Resolved since baseline (${r.resolved.length})`);
    L.push('');
    for (const x of r.resolved.slice(0, 20)) L.push(`- ${x.tool} ${x.ruleId} ${x.file || ''}${x.line ? `:${x.line}` : ''}`);
    if (r.resolved.length > 20) L.push(`- …and ${r.resolved.length - 20} more`);
    L.push('\n_Run `secscan baseline update` to drop them from the baseline._\n');
  }

  const supp = r.findings.filter((f) => f.status === 'suppressed');
  if (supp.length) {
    L.push(`<details><summary>Suppressed (${supp.length})</summary>\n`);
    for (const f of supp.slice(0, 40)) L.push(`- ${f.tool} ${f.ruleId} ${loc(f)} — ${f.suppressedBy}`);
    L.push('\n</details>\n');
  }
  return L.join('\n');
}

function fmtFinding(f) {
  const head = `- ${SEV_ICON[f.severity] || ''} **${f.severity}** \`${f.ruleId}\` ${loc(f)}`;
  const body = `  ${oneLine(f.message)}${f.cwe ? ` [${f.cwe}]` : ''}${f.extra?.confidence ? ` (confidence ${f.extra.confidence})` : ''}`;
  const snip = f.snippet && f.category === 'sast' ? `\n  \`${oneLine(f.snippet).slice(0, 140)}\`` : '';
  return `${head}\n${body}${snip}`;
}

function loc(f) { return f.file ? `${f.file}${f.line ? `:${f.line}` : ''}` : ''; }
function oneLine(s) { return String(s || '').replace(/\s+/g, ' ').trim(); }
function sevRank(s) { return { critical: 4, high: 3, medium: 2, low: 1, info: 0 }[s] ?? 0; }

/** Compact text for terminals and hook output. */
export function toText(r) {
  const lines = [];
  lines.push(`secscan ${r.mode}: ${r.ok ? 'PASS' : 'FAIL'} · ${r.counts.new || 0} new, ${r.counts.baseline || 0} baseline, ${r.counts.suppressed || 0} suppressed · ${(r.ms / 1000).toFixed(1)}s`);
  for (const t of r.toolRuns) lines.push(`  ${t.ran ? '✓' : '·'} ${t.tool.padEnd(12)} ${t.ran ? `${t.count} finding(s) in ${t.ms}ms${t.error ? ` ⚠ ${t.error}` : ''}` : t.reason}`);
  if (r.newFindings.length) {
    lines.push('');
    for (const f of r.newFindings.slice(0, 40)) lines.push(`  ${f.severity.toUpperCase().padEnd(8)} ${f.ruleId}  ${loc(f)}\n           ${oneLine(f.message).slice(0, 160)}`);
    if (r.newFindings.length > 40) lines.push(`  …and ${r.newFindings.length - 40} more`);
  }
  for (const d of r.deps) for (const fl of d.flags || []) lines.push(`  DEP      ${fl.kind} ${fl.package}@${fl.version}: ${fl.detail}`);
  if (r.blocking.length) lines.push(`\n${r.blocking.length} blocking finding(s) at threshold "${r.threshold}".`);
  return lines.join('\n');
}
