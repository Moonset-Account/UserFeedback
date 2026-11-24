import {
  reviewPackage,
  reviewVariant,
  reviewManifest,
  auditPackage,
  auditManifest,
  review,
  checkPackage,
  checkManifest,
  expandResources,
  buildVariantResources,
} from './review.mjs';
import { evaluateCondition, createVariantContext, compareVersionNumbers } from './conditions.mjs';
import { normalizeSiteScope, siteCovers, findCoveringScopes, globToRegExp } from './scopes.mjs';
import { formatTextReport } from './format.mjs';

export { formatTextReport };

export {
  reviewPackage,
  reviewVariant,
  reviewManifest,
  auditPackage,
  auditManifest,
  review,
  checkPackage,
  checkManifest,
  expandResources,
  buildVariantResources,
  evaluateCondition,
  createVariantContext,
  normalizeSiteScope,
  siteCovers,
  findCoveringScopes,
  globToRegExp,
};

export const compareVersions = compareVersionNumbers;

function defaultApi(pkg, options) {
  return reviewPackage(pkg, options);
}

Object.assign(defaultApi, {
  reviewPackage,
  reviewVariant,
  reviewManifest,
  auditPackage,
  auditManifest,
  review,
  checkPackage,
  checkManifest,
  expandResources,
  buildVariantResources,
  evaluateCondition,
  createVariantContext,
  normalizeSiteScope,
  siteCovers,
  findCoveringScopes,
  globToRegExp,
  compareVersions: compareVersionNumbers,
  formatTextReport,
});

export default defaultApi;
