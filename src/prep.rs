//! Input validation and deterministic pre-processing shared by all entry points.

use crate::model::{LinkerError, LinkerInput, RelocKind, RelocType, Relocation, Symbol};

/// A relocation view with a total order so comparison never depends on input order.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub(crate) struct CanonReloc {
    pub offset: usize,
    pub kind: RelocKind,
    pub width: usize,
    pub target_symbol: usize,
    pub addend: i64,
}

impl CanonReloc {
    pub fn rtype(&self) -> RelocType {
        RelocType {
            kind: self.kind,
            width: self.width,
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct PreparedSection {
    pub name: String,
    pub alignment: usize,
    /// Section bytes with every relocation slot replaced by zero.
    pub masked_bytes: Vec<u8>,
    /// Original bytes, used for backfilling during layout.
    pub raw_bytes: Vec<u8>,
    /// Relocations in canonical (total) order.
    pub relocs: Vec<CanonReloc>,
    /// True when an address-taken symbol is defined in this section.
    pub kept: bool,
    /// Names of address-taken symbols defined in this section, sorted.
    pub kept_symbols: Vec<String>,
}

#[derive(Clone, Debug)]
pub(crate) struct Prepared {
    pub sections: Vec<PreparedSection>,
    pub symbols: Vec<Symbol>,
}

fn is_power_of_two(n: usize) -> bool {
    n != 0 && (n & (n - 1)) == 0
}

impl Prepared {
    pub(crate) fn from_input(input: &LinkerInput) -> Result<Self, LinkerError> {
        let LinkerInput { sections, symbols } = input;

        // --- validate sections ---
        let mut seen_sections = std::collections::HashSet::new();
        for (i, s) in sections.iter().enumerate() {
            if s.name.is_empty() {
                return Err(LinkerError::EmptySectionName { index: i });
            }
            if !seen_sections.insert(s.name.clone()) {
                return Err(LinkerError::DuplicateSectionName(s.name.clone()));
            }
            if !is_power_of_two(s.alignment) {
                return Err(LinkerError::InvalidAlignment {
                    section: s.name.clone(),
                    alignment: s.alignment,
                });
            }
        }

        // --- validate symbols ---
        let mut seen_symbols = std::collections::HashSet::new();
        for (i, sym) in symbols.iter().enumerate() {
            if sym.name.is_empty() {
                return Err(LinkerError::EmptySymbolName { index: i });
            }
            if !seen_symbols.insert(sym.name.clone()) {
                return Err(LinkerError::DuplicateSymbolName(sym.name.clone()));
            }
            if sym.section >= sections.len() {
                return Err(LinkerError::SectionIndexOutOfBounds {
                    index: sym.section,
                    section_count: sections.len(),
                });
            }
            let size = sections[sym.section].bytes.len();
            if sym.offset > size {
                return Err(LinkerError::SymbolOffsetOutOfBounds {
                    symbol: sym.name.clone(),
                    section: sections[sym.section].name.clone(),
                    offset: sym.offset,
                    section_size: size,
                });
            }
        }

        // --- validate relocation slots ---
        for s in sections.iter() {
            for r in s.relocations.iter() {
                if r.rtype.width == 0 || r.rtype.width > 8 {
                    return Err(LinkerError::InvalidRelocWidth {
                        section: s.name.clone(),
                        width: r.rtype.width,
                    });
                }
                if r.target_symbol >= symbols.len() {
                    return Err(LinkerError::SymbolIndexOutOfBounds {
                        index: r.target_symbol,
                        symbol_count: symbols.len(),
                    });
                }
                let end = r.offset.checked_add(r.rtype.width).ok_or_else(|| {
                    LinkerError::RelocSlotOutOfBounds {
                        section: s.name.clone(),
                        offset: r.offset,
                        width: r.rtype.width,
                        section_size: s.bytes.len(),
                    }
                })?;
                if end > s.bytes.len() {
                    return Err(LinkerError::RelocSlotOutOfBounds {
                        section: s.name.clone(),
                        offset: r.offset,
                        width: r.rtype.width,
                        section_size: s.bytes.len(),
                    });
                }
            }
        }

        // --- build prepared sections ---
        let mut prepared_sections: Vec<PreparedSection> = sections
            .iter()
            .map(|s| {
                let mut masked_bytes = s.bytes.clone();
                for Relocation { rtype, offset, .. } in &s.relocations {
                    for b in &mut masked_bytes[*offset..*offset + rtype.width] {
                        *b = 0;
                    }
                }
                let mut relocs: Vec<CanonReloc> = s
                    .relocations
                    .iter()
                    .map(|r| CanonReloc {
                        offset: r.offset,
                        kind: r.rtype.kind,
                        width: r.rtype.width,
                        target_symbol: r.target_symbol,
                        addend: r.addend,
                    })
                    .collect();
                relocs.sort_by(|x, y| {
                    (x.offset, x.kind, x.width, x.target_symbol, x.addend).cmp(&(
                        y.offset,
                        y.kind,
                        y.width,
                        y.target_symbol,
                        y.addend,
                    ))
                });
                PreparedSection {
                    name: s.name.clone(),
                    alignment: s.alignment,
                    masked_bytes,
                    raw_bytes: s.bytes.clone(),
                    relocs,
                    kept: false,
                    kept_symbols: Vec::new(),
                }
            })
            .collect();

        // Address-taken symbols pin their defining section and make references exact.
        for sym in symbols.iter().filter(|s| s.address_taken) {
            prepared_sections[sym.section].kept = true;
            prepared_sections[sym.section]
                .kept_symbols
                .push(sym.name.clone());
        }
        for ps in prepared_sections.iter_mut() {
            ps.kept_symbols.sort();
        }

        Ok(Prepared {
            sections: prepared_sections,
            symbols: symbols.clone(),
        })
    }

    pub(crate) fn section_index(&self, name: &str) -> Result<usize, LinkerError> {
        self.sections
            .iter()
            .position(|s| s.name == name)
            .ok_or_else(|| LinkerError::UnknownSection(name.to_string()))
    }
}
