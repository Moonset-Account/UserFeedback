//! Diagnostics: explain why two sections were not merged.
//!
//! For any non-equivalent pair, the verifier returns either:
//! * a direct difference (different bytes/alignment, or the exact address-taken
//!   symbols that pin one of the sections), or
//! * the shortest relocation path from each section to the first pair of targets
//!   that differ, together with the reason they differ.

use std::collections::{HashSet, VecDeque};
use std::fmt;

use crate::fold::fold_prepared;
use crate::model::{LinkerError, LinkerInput};
use crate::prep::Prepared;

/// A direct, zero-step difference between two sections.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DirectDifference {
    /// Masked bytes differ (relocation placeholder positions excluded).
    MaskedBytes,
    /// Section sizes differ.
    Size { size_a: usize, size_b: usize },
    /// Required alignment differs.
    Alignment {
        alignment_a: usize,
        alignment_b: usize,
    },
    /// One or both sections define address-taken symbols and are pinned by identity.
    KeptByAddressIdentity {
        kept_symbols_a: Vec<String>,
        kept_symbols_b: Vec<String>,
    },
}

impl fmt::Display for DirectDifference {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DirectDifference::MaskedBytes => {
                write!(f, "masked content bytes differ")
            }
            DirectDifference::Size { size_a, size_b } => {
                write!(f, "sizes differ: {size_a} vs {size_b}")
            }
            DirectDifference::Alignment {
                alignment_a,
                alignment_b,
            } => write!(
                f,
                "alignments differ: {alignment_a} vs {alignment_b}"
            ),
            DirectDifference::KeptByAddressIdentity {
                kept_symbols_a,
                kept_symbols_b,
            } => write!(
                f,
                "address-identity entry pins a section (symbols a=[{}], b=[{}])",
                kept_symbols_a.join(","),
                kept_symbols_b.join(",")
            ),
        }
    }
}

/// One hop in a distinguishing relocation path.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DistinctionStep {
    /// Section the hop starts from.
    pub section: String,
    /// Offset of the followed relocation slot.
    pub reloc_offset: usize,
    /// Slot type and width.
    pub reloc_type: String,
    /// Target symbol followed by the hop.
    pub target_symbol: String,
    /// Offset of the target symbol inside its defining section.
    pub target_in_section_offset: usize,
}

impl fmt::Display for DistinctionStep {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{} --[{} @ {} -> {}+{}]-->",
            self.section, self.reloc_type, self.reloc_offset, self.target_symbol,
            self.target_in_section_offset
        )
    }
}

/// Why a pair of sections reached at the end of a path differs.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PathTermination {
    /// The two target sections differ by a direct property.
    Direct(DirectDifference),
    /// The slot tuples differ (type/offset/addend/target position).
    RelocSlotMismatch {
        detail_a: String,
        detail_b: String,
    },
    /// The slots target different address-taken symbols.
    ExactAddressSymbol {
        symbol_a: String,
        symbol_b: String,
    },
}

impl fmt::Display for PathTermination {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            PathTermination::Direct(d) => write!(f, "{d}"),
            PathTermination::RelocSlotMismatch { detail_a, detail_b } => {
                write!(f, "relocation slot differs: {detail_a} vs {detail_b}")
            }
            PathTermination::ExactAddressSymbol { symbol_a, symbol_b } => write!(
                f,
                "references to address-sensitive symbols differ: {symbol_a} vs {symbol_b}"
            ),
        }
    }
}

/// The full distinction result for a non-equivalent pair of sections.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Distinction {
    pub section_a: String,
    pub section_b: String,
    /// Zero-length when the difference is direct.
    pub path_a: Vec<DistinctionStep>,
    pub path_b: Vec<DistinctionStep>,
    pub termination: PathTermination,
}

impl fmt::Display for Distinction {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.path_a.is_empty() && self.path_b.is_empty() {
            return writeln!(
                f,
                "`{}` and `{}` differ directly: {}",
                self.section_a, self.section_b, self.termination
            );
        }
        writeln!(
            f,
            "`{}` and `{}` are distinguished by the shortest relocation path:",
            self.section_a, self.section_b
        )?;
        for (a, b) in self.path_a.iter().zip(self.path_b.iter()) {
            writeln!(f, "  {a}")?;
            writeln!(f, "  {b}")?;
        }
        writeln!(f, "  termination: {}", self.termination)
    }
}

fn direct_difference(p: &Prepared, a: usize, b: usize) -> Option<DirectDifference> {
    let sa = &p.sections[a];
    let sb = &p.sections[b];
    if sa.kept || sb.kept {
        return Some(DirectDifference::KeptByAddressIdentity {
            kept_symbols_a: sa.kept_symbols.clone(),
            kept_symbols_b: sb.kept_symbols.clone(),
        });
    }
    if sa.masked_bytes.len() != sb.masked_bytes.len() {
        return Some(DirectDifference::Size {
            size_a: sa.masked_bytes.len(),
            size_b: sb.masked_bytes.len(),
        });
    }
    if sa.alignment != sb.alignment {
        return Some(DirectDifference::Alignment {
            alignment_a: sa.alignment,
            alignment_b: sb.alignment,
        });
    }
    if sa.masked_bytes != sb.masked_bytes {
        return Some(DirectDifference::MaskedBytes);
    }
    None
}

fn make_step(p: &Prepared, section: usize, slot: usize) -> DistinctionStep {
    let s = &p.sections[section];
    let r = &s.relocs[slot];
    let sym = &p.symbols[r.target_symbol];
    DistinctionStep {
        section: s.name.clone(),
        reloc_offset: r.offset,
        reloc_type: r.rtype().to_string(),
        target_symbol: sym.name.clone(),
        target_in_section_offset: sym.offset,
    }
}

fn slot_tuple_str(p: &Prepared, section: usize, slot: usize) -> String {
    let s = &p.sections[section];
    let r = &s.relocs[slot];
    format!(
        "{}@off={}+addend{}->sym{}+{}",
        r.rtype(),
        r.offset,
        r.addend,
        p.symbols[r.target_symbol].name,
        p.symbols[r.target_symbol].offset
    )
}

/// Explain the difference between two sections identified by name.
///
/// Returns `Ok(None)` when the sections are equivalent (the fold merged them),
/// otherwise a shortest-path distinction or a direct difference.
pub fn distinguish(
    input: &LinkerInput,
    section_a: &str,
    section_b: &str,
) -> Result<Option<Distinction>, LinkerError> {
    let prepared = Prepared::from_input(input)?;
    let a = prepared.section_index(section_a)?;
    let b = prepared.section_index(section_b)?;
    if a == b {
        return Ok(None);
    }
    let fold_result = fold_prepared(&prepared);
    if fold_result.class_of(a) == fold_result.class_of(b) {
        return Ok(None);
    }

    // Direct difference first: a zero-length path is always shortest.
    if let Some(diff) = direct_difference(&prepared, a, b) {
        return Ok(Some(Distinction {
            section_a: prepared.sections[a].name.clone(),
            section_b: prepared.sections[b].name.clone(),
            path_a: Vec::new(),
            path_b: Vec::new(),
            termination: PathTermination::Direct(diff),
        }));
    }

    // BFS over pairs of sections, following relocation slots in lock step.
    let mut queue: VecDeque<(usize, usize, Vec<DistinctionStep>, Vec<DistinctionStep>)> =
        VecDeque::new();
    let mut visited: HashSet<(usize, usize)> = HashSet::new();
    visited.insert((a, b));
    queue.push_back((a, b, Vec::new(), Vec::new()));

    while let Some((x, y, path_x, path_y)) = queue.pop_front() {
        let sx = &prepared.sections[x];
        let sy = &prepared.sections[y];

        if sx.relocs.len() != sy.relocs.len() {
            // A differing slot count manifests at the first unmatched slot.
            let slot = sx.relocs.len().min(sy.relocs.len());
            return Ok(Some(Distinction {
                section_a: prepared.sections[a].name.clone(),
                section_b: prepared.sections[b].name.clone(),
                path_a: path_x,
                path_b: path_y,
                termination: PathTermination::RelocSlotMismatch {
                    detail_a: if slot < sx.relocs.len() {
                        slot_tuple_str(&prepared, x, slot)
                    } else {
                        "<no slot>".to_string()
                    },
                    detail_b: if slot < sy.relocs.len() {
                        slot_tuple_str(&prepared, y, slot)
                    } else {
                        "<no slot>".to_string()
                    },
                },
            }));
        }

        for slot in 0..sx.relocs.len() {
            let rx = &sx.relocs[slot];
            let ry = &sy.relocs[slot];
            let sym_x = &prepared.symbols[rx.target_symbol];
            let sym_y = &prepared.symbols[ry.target_symbol];

            let static_match = rx.offset == ry.offset
                && rx.kind == ry.kind
                && rx.width == ry.width
                && rx.addend == ry.addend
                && sym_x.offset == sym_y.offset;

            if !static_match {
                return Ok(Some(Distinction {
                    section_a: prepared.sections[a].name.clone(),
                    section_b: prepared.sections[b].name.clone(),
                    path_a: path_x,
                    path_b: path_y,
                    termination: PathTermination::RelocSlotMismatch {
                        detail_a: slot_tuple_str(&prepared, x, slot),
                        detail_b: slot_tuple_str(&prepared, y, slot),
                    },
                }));
            }

            if sym_x.address_taken || sym_y.address_taken {
                if sym_x.name != sym_y.name {
                    return Ok(Some(Distinction {
                        section_a: prepared.sections[a].name.clone(),
                        section_b: prepared.sections[b].name.clone(),
                        path_a: path_x,
                        path_b: path_y,
                        termination: PathTermination::ExactAddressSymbol {
                            symbol_a: sym_x.name.clone(),
                            symbol_b: sym_y.name.clone(),
                        },
                    }));
                }
                // Same exact address-sensitive symbol: the hop is identity-equivalent,
                // continue with the next slot.
                continue;
            }

            let nx = sym_x.section;
            let ny = sym_y.section;
            let key = (nx, ny);
            if !visited.insert(key) {
                continue;
            }

            if let Some(diff) = direct_difference(&prepared, nx, ny) {
                let mut px = path_x.clone();
                let mut py = path_y.clone();
                px.push(make_step(&prepared, x, slot));
                py.push(make_step(&prepared, y, slot));
                return Ok(Some(Distinction {
                    section_a: prepared.sections[a].name.clone(),
                    section_b: prepared.sections[b].name.clone(),
                    path_a: px,
                    path_b: py,
                    termination: PathTermination::Direct(diff),
                }));
            }

            let mut px = path_x.clone();
            let mut py = path_y.clone();
            px.push(make_step(&prepared, x, slot));
            py.push(make_step(&prepared, y, slot));
            queue.push_back((nx, ny, px, py));
        }
    }

    // Reaching here means BFS exhausted all reachable pairs without a difference:
    // the sections should have been folded, which is an invariant violation.
    Err(LinkerError::DistinctionExhausted {
        section_a: prepared.sections[a].name.clone(),
        section_b: prepared.sections[b].name.clone(),
    })
}
