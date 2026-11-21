import { validateVersionManifest } from './errors.mjs';

/**
 * Compute the weight chunks a version declares, in ascending block order.
 * A version manifest names every block id it needs; each block is one
 * weight chunk delivered independently.
 */
export function requiredBlockIds(manifest) {
  validateVersionManifest(manifest);
  return [...manifest.blocks].sort((a, b) => a - b);
}

/**
 * The number of slots a version needs in the shared slot table.
 *
 * The manifest declares this explicitly so a version whose slot demand does
 * not match its block layout is rejected at admission time rather than
 * discovered mid-load. Every block needs at least one slot, so the declared
 * count may exceed the block count (e.g. a wide block spanning two slots)
 * but never fall below it.
 */
export function requiredSlotCount(manifest) {
  const blocks = requiredBlockIds(manifest);
  if (blocks.length > manifest.slotCount) {
    throw new Error(
      `version ${manifest.version}: declares ${manifest.slotCount} slots for ${blocks.length} blocks`,
    );
  }
  return manifest.slotCount;
}
