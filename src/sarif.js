/** Render unified findings as SARIF 2.1.0, one run per tool. */

const LEVEL = { critical: 'error', high: 'error', medium: 'warning', low: 'note', info: 'note' };

export function toSarif(findings, { toolVersions = {}, secscanVersion = '0.0.0' } = {}) {
  const byTool = new Map();
  for (const f of findings) {
    if (!byTool.has(f.tool)) byTool.set(f.tool, []);
    byTool.get(f.tool).push(f);
  }
  const runs = [];
  for (const [tool, list] of byTool) {
    const rules = new Map();
    const results = [];
    for (const f of list) {
      if (!rules.has(f.ruleId)) {
        rules.set(f.ruleId, {
          id: f.ruleId,
          shortDescription: { text: (f.extra?.title || f.message || f.ruleId).slice(0, 120) },
          properties: { category: f.category, ...(f.cwe ? { cwe: f.cwe } : {}) },
        });
      }
      const result = {
        ruleId: f.ruleId,
        level: LEVEL[f.severity] || 'warning',
        message: { text: f.message },
        partialFingerprints: { 'secscan/v1': f.fingerprint },
        properties: {
          'secscan/severity': f.severity,
          'secscan/category': f.category,
          'secscan/status': f.status,
          ...(f.suppressedBy ? { 'secscan/suppressedBy': f.suppressedBy } : {}),
          ...(f.cwe ? { cwe: f.cwe } : {}),
          ...f.extra,
        },
      };
      if (f.file) {
        const loc = { physicalLocation: { artifactLocation: { uri: f.file, uriBaseId: '%SRCROOT%' } } };
        if (f.line) {
          loc.physicalLocation.region = { startLine: f.line, ...(f.endLine ? { endLine: f.endLine } : {}) };
          if (f.snippet && f.category !== 'secrets') loc.physicalLocation.region.snippet = { text: f.snippet.slice(0, 400) };
        }
        result.locations = [loc];
      }
      if (f.status === 'suppressed') {
        result.suppressions = [{ kind: 'inSource', justification: f.suppressedBy || 'suppressed' }];
      }
      results.push(result);
    }
    runs.push({
      tool: {
        driver: {
          name: tool,
          ...(toolVersions[tool] ? { version: String(toolVersions[tool]) } : {}),
          informationUri: 'https://github.com/zapai-inc/secscan',
          rules: [...rules.values()],
        },
      },
      automationDetails: { id: `secscan/${tool}` },
      properties: { 'secscan/version': secscanVersion },
      results,
    });
  }
  return {
    $schema: 'https://json.schemastore.org/sarif-2.1.0.json',
    version: '2.1.0',
    runs,
  };
}
