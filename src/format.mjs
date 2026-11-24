/**
 * Deterministic human-readable report formatting.
 */

function line(char = '-', width = 72) {
  return char.repeat(width);
}

function requestLabel(request) {
  const target = request.kind === 'permission' ? request.permission : request.pattern;
  const feature = request.feature ? ` [feature=${request.feature}]` : '';
  return `${request.timing.padEnd(8)} ${target}${feature}`;
}

export function formatTextReport(report) {
  const out = [];
  out.push(`Extension package review: ${report.packageName ?? '(unnamed)'}${report.version ? `@${report.version}` : ''}`);
  out.push(line('='));
  out.push(`Variants: ${report.summary.variants}; errors: ${report.summary.errors}; warnings: ${report.summary.warnings}`);
  out.push(
    `Extra install permissions: ${report.summary.extraInstallPermissions}; ` +
    `unapproved sites: ${report.summary.unapprovedSites}; ` +
    `unattributed optional requests: ${report.summary.unattributedOptionalRequests}; ` +
    `residual optional requests: ${report.summary.residualOptionalRequests}`,
  );

  for (const variant of report.variants) {
    out.push('');
    out.push(line());
    out.push(`Variant ${variant.variantId} (browser=${variant.browser ?? 'unknown'}) — ${variant.ok ? 'PASS' : 'FAIL'}`);
    out.push(line());
    out.push(`Loaded resources (${variant.loadedResources.length}):`);
    for (const resource of variant.loadedResources) out.push(`  + ${resource.name}`);
    out.push(`Skipped resources (${variant.skippedResources.length}):`);
    for (const resource of variant.skippedResources) out.push(`  - ${resource.name} (${resource.reason})`);

    if (variant.extraInstallPermissions.length) {
      out.push('Extra install-time permissions:');
      for (const request of variant.extraInstallPermissions) out.push(`  ! ${requestLabel(request)}`);
    }

    if (variant.unapprovedSites.length) {
      out.push('Unapproved site scopes:');
      for (const request of variant.unapprovedSites) out.push(`  ! ${requestLabel(request)}`);
    }

    const pendingOptional = variant.optionalRequestReviews.filter((r) => !r.approved);
    if (pendingOptional.length) {
      out.push('Optional/enable requests needing attention:');
      for (const request of pendingOptional) {
        out.push(`  ? ${requestLabel(request)} — scope=${request.scopeApproved ? 'approved' : 'denied'}, attribution=${request.attributionStatus}`);
      }
    }

    if (variant.residualOptionalRequests.length) {
      out.push('Residual requests from optional features without a loaded implementation:');
      for (const request of variant.residualOptionalRequests) {
        out.push(`  ~ ${requestLabel(request)}${request.active ? ' (still actively declared)' : ''}`);
      }
    }

    if (variant.issues.length) {
      out.push('Issues:');
      for (const issue of variant.issues) {
        out.push(`  [${issue.severity}] ${issue.code}: ${issue.message}`);
      }
    }
  }

  out.push('');
  out.push(report.releaseApproved ? 'RELEASE REVIEW: APPROVED' : report.ok ? 'RELEASE REVIEW: WARNINGS' : 'RELEASE REVIEW: CHANGES REQUIRED');
  return out.join('\n');
}
