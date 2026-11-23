//! Layout: assign aligned addresses to folded classes and backfill relocations.
//!
//! Classes are visited in representative-name order and placed at the first address
//! that satisfies the representative section's alignment. Every relocation slot of
//! the representative is then rewritten with its final absolute or PC-relative value;
//! if the value does not fit the slot width, an [`LinkerError::Overflow`] is returned
//! containing the violated range.

use std::collections::BTreeMap;

use crate::fold::fold_prepared;
use crate::model::{
    FoldResult, Layout, LinkerError, LinkerInput, PlacedSection, RelocKind,
};
use crate::prep::Prepared;

pub(crate) fn align_up(addr: u64, alignment: usize) -> u64 {
    let a = alignment as u64;
    (addr + a - 1) & !(a - 1)
}

fn signed_range(width: usize) -> (i128, i128) {
    let bits = (width * 8) as u32;
    let min = -(1i128 << (bits - 1));
    let max = (1i128 << (bits - 1)) - 1;
    (min, max)
}

fn unsigned_max(width: usize) -> i128 {
    let bits = (width * 8) as u32;
    if bits == 128 {
        i128::MAX
    } else {
        (1i128 << bits) - 1
    }
}

fn write_le(bytes: &mut [u8], offset: usize, width: usize, value: u64) {
    let mut v = value;
    for k in 0..width {
        bytes[offset + k] = (v & 0xff) as u8;
        v >>= 8;
    }
}

/// Fold the input and lay out the resulting classes.
pub fn layout(input: &LinkerInput) -> Result<Layout, LinkerError> {
    let prepared = Prepared::from_input(input)?;
    let fold_result = fold_prepared(&prepared);
    layout_prepared(&prepared, &fold_result)
}

pub(crate) fn layout_prepared(
    prepared: &Prepared,
    fold_result: &FoldResult,
) -> Result<Layout, LinkerError> {
    // First pass: assign addresses.
    let mut cursor: u64 = 0;
    let mut class_address: Vec<u64> = Vec::with_capacity(fold_result.classes.len());
    for class in &fold_result.classes {
        let rep = &prepared.sections[class.representative_index];
        let addr = align_up(cursor, rep.alignment);
        class_address.push(addr);
        cursor = addr + rep.raw_bytes.len() as u64;
    }

    // Absolute address of every symbol: address of its section's class + in-section offset.
    let mut symbol_addresses: BTreeMap<String, u64> = BTreeMap::new();
    for sym in &prepared.symbols {
        let class_id = fold_result
            .class_of(sym.section)
            .expect("every section belongs to a class");
        let addr = class_address[class_id] + sym.offset as u64;
        symbol_addresses.insert(sym.name.clone(), addr);
    }

    // Second pass: backfill representative bytes and check representability.
    let mut placed = Vec::with_capacity(fold_result.classes.len());
    for class in &fold_result.classes {
        let rep_index = class.representative_index;
        let rep = &prepared.sections[rep_index];
        let base = class_address[class.id];
        let mut bytes = rep.raw_bytes.clone();

        for r in &rep.relocs {
            let sym = &prepared.symbols[r.target_symbol];
            let target_class = fold_result
                .class_of(sym.section)
                .expect("target section belongs to a class");
            let target_addr = class_address[target_class] + sym.offset as u64;

            let slot_addr = base + r.offset as u64;
            let value: i128 = match r.kind {
                RelocKind::Absolute => target_addr as i128 + r.addend as i128,
                RelocKind::Relative => {
                    target_addr as i128 - (slot_addr + r.width as u64) as i128
                        + r.addend as i128
                }
            };

            let (min, max) = match r.kind {
                RelocKind::Absolute => (0, unsigned_max(r.width)),
                RelocKind::Relative => signed_range(r.width),
            };
            if !(min..=max).contains(&value) {
                return Err(LinkerError::Overflow {
                    section: rep.name.clone(),
                    reloc_offset: r.offset,
                    value,
                    width: r.width,
                    min,
                    max,
                });
            }
            // Two's-complement truncation to the slot width (the range check
            // guarantees the value is representable).
            let mask = if r.width == 8 {
                u64::MAX
            } else {
                (1u64 << (r.width * 8)) - 1
            };
            let encoded = (value as u64) & mask;
            write_le(&mut bytes, r.offset, r.width, encoded);
        }

        placed.push(PlacedSection {
            class_id: class.id,
            representative: class.representative.clone(),
            members: class.members.clone(),
            address: base,
            size: bytes.len(),
            alignment: rep.alignment,
            bytes,
        });
    }

    Ok(Layout {
        placed,
        symbol_addresses,
    })
}
