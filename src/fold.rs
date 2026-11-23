//! Section folding: maximal equivalence partition via partition refinement.
//!
//! Two sections are equivalent when, simultaneously:
//! * their alignment, size and slot-masked bytes match,
//! * every canonical relocation slot has identical type, offset, addend, and
//!   identical target symbol *in-section position*,
//! * a slot either references the exact same address-taken symbol, or references
//!   equivalent (folded) target sections.
//!
//! The last clause is a recursive (and mutually recursive) condition. Solving it as
//! a greatest fixed point with partition refinement handles reference cycles
//! naturally: initially only static structure groups sections, then the partition is
//! refined until it stabilises. Sections defining address-taken symbols are excluded
//! from folding and always form singleton classes.

use std::collections::BTreeMap;
use std::hash::Hash;
use std::rc::Rc;

use crate::model::{EquivClass, FoldResult, LinkerError, LinkerInput};
use crate::prep::{CanonReloc, Prepared, PreparedSection};

/// Static relocation facts used in the initial signature.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct ConstReloc {
    offset: usize,
    kind: u8,
    width: usize,
    addend: i64,
    /// Offset of the target symbol inside its defining section.
    target_in_section_offset: usize,
}

/// The complete static signature of a section.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct ConstKey {
    alignment: usize,
    masked_bytes: Vec<u8>,
    relocs: Vec<ConstReloc>,
    kept_symbols: Vec<String>,
}

fn kind_byte(k: crate::model::RelocKind) -> u8 {
    match k {
        crate::model::RelocKind::Absolute => 0,
        crate::model::RelocKind::Relative => 1,
    }
}

fn build_const_key(p: &Prepared, s: &PreparedSection) -> ConstKey {
    let relocs = s
        .relocs
        .iter()
        .map(|r| -> ConstReloc {
            let sym = &p.symbols[r.target_symbol];
            ConstReloc {
                offset: r.offset,
                kind: kind_byte(r.kind),
                width: r.width,
                addend: r.addend,
                target_in_section_offset: sym.offset,
            }
        })
        .collect();
    ConstKey {
        alignment: s.alignment,
        masked_bytes: s.masked_bytes.clone(),
        relocs,
        kept_symbols: s.kept_symbols.clone(),
    }
}

/// Per-relocation facts needed during refinement. Static slot facts (offset, kind,
/// width, addend, in-section target offset) already live in the section's
/// [`ConstKey`]; refinement only needs to know how to treat the target.
#[derive(Clone, Debug)]
struct SigReloc {
    /// Some(name) when the target symbol has address identity. Two slots then match
    /// only for the exact same symbol, even if their target sections would fold.
    exact_symbol: Option<String>,
    target_section: usize,
    target_in_section_offset: usize,
}

#[derive(Clone, Debug)]
struct Sig {
    const_key: Rc<ConstKey>,
    relocs: Vec<SigReloc>,
}

/// Exact, collision-free per-round grouping key. The constant part is shared via
/// an `Rc`, so comparisons are pointer checks plus cheap field comparisons.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct RoundKey {
    const_key: Rc<ConstKey>,
    /// One entry per relocation, ordered exactly like `Sig::relocs`.
    edges: Vec<EdgeKey>,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum EdgeKey {
    /// Reference to an address-sensitive symbol, matched by exact name.
    ExactSymbol(String),
    /// Ordinary symbol in a pinned section: the section's permanent singleton id
    /// together with the symbol's in-section offset.
    KeptTarget { kept_id: usize, in_section_offset: usize },
    /// Ordinary reference whose target section participates in folding.
    FoldTarget { class_id: usize, in_section_offset: usize },
}

fn build_round_key(sig: &Sig, class_of: &[usize], kept_ids: &[Option<usize>]) -> RoundKey {
    let edges = sig
        .relocs
        .iter()
        .map(|r| {
            if let Some(name) = &r.exact_symbol {
                EdgeKey::ExactSymbol(name.clone())
            } else if let Some(kept_id) = kept_ids[r.target_section] {
                EdgeKey::KeptTarget {
                    kept_id,
                    in_section_offset: r.target_in_section_offset,
                }
            } else {
                EdgeKey::FoldTarget {
                    class_id: class_of[r.target_section],
                    in_section_offset: r.target_in_section_offset,
                }
            }
        })
        .collect();
    RoundKey {
        const_key: Rc::clone(&sig.const_key),
        edges,
    }
}

/// Compute the maximal equivalence partition of the input sections.
pub fn fold(input: &LinkerInput) -> Result<FoldResult, LinkerError> {
    let prepared = Prepared::from_input(input)?;
    Ok(fold_prepared(&prepared))
}

pub(crate) fn fold_prepared(prepared: &Prepared) -> FoldResult {
    let n = prepared.sections.len();

    // Kept sections receive permanent singleton ids.
    let mut kept_ids: Vec<Option<usize>> = vec![None; n];
    let mut next_kept_id = 0usize;
    for (i, s) in prepared.sections.iter().enumerate() {
        if s.kept {
            kept_ids[i] = Some(next_kept_id);
            next_kept_id += 1;
        }
    }

    // Build signatures and the initial grouping by static structure.
    let sigs: Vec<Sig> = prepared
        .sections
        .iter()
        .map(|s| {
            let const_key = Rc::new(build_const_key(prepared, s));
            let relocs: Vec<SigReloc> = s
                .relocs
                .iter()
                .map(|r: &CanonReloc| {
                    let sym = &prepared.symbols[r.target_symbol];
                    SigReloc {
                        exact_symbol: if sym.address_taken {
                            Some(sym.name.clone())
                        } else {
                            None
                        },
                        target_section: sym.section,
                        target_in_section_offset: sym.offset,
                    }
                })
                .collect();
            Sig { const_key, relocs }
        })
        .collect();

    // Initial grouping by exact static-signature value comparison.
    let mut const_groups: Vec<(Rc<ConstKey>, Vec<usize>)> = Vec::new();
    for (i, sig) in sigs.iter().enumerate() {
        let pos = const_groups
            .iter()
            .position(|(k, _)| k == &sig.const_key);
        match pos {
            Some(p) => const_groups[p].1.push(i),
            None => const_groups.push((Rc::clone(&sig.const_key), vec![i])),
        }
    }

    // Assign initial classes: kept sections are always singleton even when their
    // static structure coincides with another section.
    let mut class_of = vec![usize::MAX; n];
    let mut class_members: Vec<Vec<usize>> = Vec::new();
    for (_key, members) in &const_groups {
        let foldable: Vec<usize> = members
            .iter()
            .copied()
            .filter(|&i| kept_ids[i].is_none())
            .collect();
        if !foldable.is_empty() {
            let id = class_members.len();
            for &i in &foldable {
                class_of[i] = id;
            }
            class_members.push(foldable);
        }
        for &i in members {
            if kept_ids[i].is_some() {
                let id = class_members.len();
                class_of[i] = id;
                class_members.push(vec![i]);
            }
        }
    }

    // Refine until the partition stabilises (greatest fixed point).
    loop {
        let mut round_keys: Vec<RoundKey> = Vec::with_capacity(n);
        for i in 0..n {
            round_keys.push(build_round_key(&sigs[i], &class_of, &kept_ids));
        }

        let mut new_class_of = vec![usize::MAX; n];
        let mut new_members: Vec<Vec<usize>> = Vec::new();
        // Keep traversal deterministic by walking old classes in order.
        for members in &class_members {
            let mut sub_groups: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
            // Exact key comparison; the table maps each distinct key to a stable
            // position used as the sub-group key.
            let mut key_table: Vec<RoundKey> = Vec::new();
            for &i in members {
                let key = &round_keys[i];
                let pos = key_table
                    .iter()
                    .position(|k| k == key)
                    .unwrap_or_else(|| {
                        key_table.push(key.clone());
                        key_table.len() - 1
                    });
                sub_groups.entry(pos).or_default().push(i);
            }
            for (_, mut group) in sub_groups {
                group.sort_unstable();
                let id = new_members.len();
                for &i in &group {
                    new_class_of[i] = id;
                }
                new_members.push(group);
            }
        }

        let stable = new_members.len() == class_members.len()
            && new_members
                .iter()
                .zip(class_members.iter())
                .all(|(a, b)| a == b);
        class_of = new_class_of;
        class_members = new_members;
        if stable {
            break;
        }
    }

    // Build public classes ordered by representative name.
    let mut classes: Vec<EquivClass> = class_members
        .into_iter()
        .map(|mut members| {
            members.sort_unstable_by_key(|&i| &prepared.sections[i].name);
            let representative_index = members[0];
            EquivClass {
                id: 0,
                representative: prepared.sections[representative_index].name.clone(),
                representative_index,
                members: members
                    .iter()
                    .map(|&i| prepared.sections[i].name.clone())
                    .collect(),
                member_indices: members,
            }
        })
        .collect();
    classes.sort_by(|a, b| a.representative.cmp(&b.representative));
    for (id, c) in classes.iter_mut().enumerate() {
        c.id = id;
    }
    let class_of_section = {
        let mut v = vec![0usize; n];
        for c in &classes {
            for &i in &c.member_indices {
                v[i] = c.id;
            }
        }
        v
    };

    FoldResult::new(classes, class_of_section)
}
