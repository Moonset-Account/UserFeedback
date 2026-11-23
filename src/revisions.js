/**
 * Revision-vector consistency.
 *
 * A "read set" is an object mapping service name -> revision number, meaning
 * "this entity occurrence was assembled from reads of service s at revision
 * r". A schema may declare `coReadable`: an array of complete revision
 * vectors, each describing one consistent snapshot across services.
 *
 * A read set is co-readable iff at least one declared vector agrees with
 * every read it contains (exact match per read service; services not read
 * are unconstrained). Exact match — not domination — is what makes stale
 * reads detectable: two different revisions of the same service can never
 * be merged into one consistent snapshot.
 */

export function isCoReadable(readset, coReadable) {
  if (!coReadable) return true;
  const entries = Object.entries(readset ?? {});
  return coReadable.some((vector) =>
    entries.every(([service, revision]) => vector[service] === revision),
  );
}

/**
 * Merge revision vectors. Returns null when two vectors disagree on the
 * revision of the same service (a hard conflict: one service cannot be read
 * at two revisions inside one consistent snapshot).
 */
export function mergeVectors(...vectors) {
  const out = {};
  for (const vector of vectors) {
    if (!vector) continue;
    for (const [service, revision] of Object.entries(vector)) {
      if (out[service] !== undefined && out[service] !== revision) return null;
      out[service] = revision;
    }
  }
  return out;
}

/**
 * Assess whether a late-arriving receipt for an earlier (e.g. timed-out and
 * retried) batch may still be used for each of several response branches
 * (aliases).
 *
 * receipt:  { service, revision, items? } — the late batch receipt.
 * branches: { [aliasPath]: readset } — the revision read set each branch
 *           has already committed to (as reported in result.vectors).
 * coReadable: the currently declared co-readable vectors.
 *
 * Returns per-alias evidence: usable flag, human-readable reason, and the
 * exact vectors compared, so callers can show *why* the same receipt is
 * acceptable for one alias and not for another.
 */
export function assessLateReceipt({ receipt, branches, coReadable }) {
  const receiptVector = { [receipt.service]: receipt.revision };
  const aliases = {};
  for (const [aliasPath, branchVector] of Object.entries(branches)) {
    const merged = mergeVectors(branchVector, receiptVector);
    const evidence = {
      branchVector: branchVector ?? {},
      receiptVector,
      mergedVector: merged,
    };
    if (merged === null) {
      aliases[aliasPath] = {
        usable: false,
        reason:
          `conflict: branch already committed ${receipt.service}@` +
          `${branchVector[receipt.service]}, but the late receipt carries ` +
          `${receipt.service}@${receipt.revision}; one service cannot ` +
          `contribute two revisions to a single consistent snapshot`,
        evidence,
      };
    } else if (!isCoReadable(merged, coReadable)) {
      aliases[aliasPath] = {
        usable: false,
        reason:
          `stale: merged read set ${JSON.stringify(merged)} is not covered ` +
          `by any currently co-readable revision vector, so the receipt ` +
          `cannot be stitched into this branch`,
        evidence,
      };
    } else {
      aliases[aliasPath] = {
        usable: true,
        reason:
          `receipt revision ${receipt.service}@${receipt.revision} is ` +
          `consistent with the branch's committed reads under a declared ` +
          `co-readable vector`,
        evidence,
      };
    }
  }
  return { service: receipt.service, revision: receipt.revision, aliases };
}
