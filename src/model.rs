//! Public data model for the section folding and layout module.

use std::fmt;

/// Kind of relocation encoded in a slot.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum RelocKind {
    /// Absolute value: final target address (plus addend) is written into the slot.
    Absolute,
    /// PC-relative value: `target - (slot_address + slot_width)` (plus addend).
    Relative,
}

/// A relocation slot type: relocation kind together with the slot width in bytes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct RelocType {
    pub kind: RelocKind,
    /// Width of the slot in bytes. Supported widths are 1..=8.
    pub width: usize,
}

impl RelocType {
    pub const fn absolute(width: usize) -> Self {
        Self {
            kind: RelocKind::Absolute,
            width,
        }
    }

    pub const fn relative(width: usize) -> Self {
        Self {
            kind: RelocKind::Relative,
            width,
        }
    }
}

impl fmt::Display for RelocType {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.kind {
            RelocKind::Absolute => write!(f, "Absolute/{}", self.width),
            RelocKind::Relative => write!(f, "Relative/{}", self.width),
        }
    }
}

/// A named symbol. Symbols are addressable entities located at an offset inside a section.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Symbol {
    pub name: String,
    /// Index of the section that defines this symbol.
    pub section: usize,
    /// Offset of the symbol inside its defining section.
    pub offset: usize,
    /// When true, program code compares the *address* of this symbol, so its identity
    /// must be preserved: the defining section is never folded with another section,
    /// and relocations targeting this symbol are compared by exact symbol identity.
    pub address_taken: bool,
}

/// A relocation slot embedded in a section.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Relocation {
    pub rtype: RelocType,
    /// Byte offset of the slot inside its section.
    pub offset: usize,
    /// Index (in [`LinkerInput::symbols`]) of the targeted symbol.
    pub target_symbol: usize,
    /// Constant addend applied to the relocated value.
    pub addend: i64,
}

/// An input section: raw bytes, required alignment and embedded relocation slots.
#[derive(Clone, Debug)]
pub struct Section {
    pub name: String,
    pub bytes: Vec<u8>,
    /// Required address alignment. Must be a non-zero power of two.
    pub alignment: usize,
    pub relocations: Vec<Relocation>,
}

/// Complete linker input.
#[derive(Clone, Debug, Default)]
pub struct LinkerInput {
    pub sections: Vec<Section>,
    pub symbols: Vec<Symbol>,
}

/// Errors raised by validation, folding, layout and diagnostics.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LinkerError {
    DuplicateSectionName(String),
    EmptySectionName {
        index: usize,
    },
    InvalidAlignment {
        section: String,
        alignment: usize,
    },
    SectionIndexOutOfBounds {
        index: usize,
        section_count: usize,
    },
    SymbolIndexOutOfBounds {
        index: usize,
        symbol_count: usize,
    },
    EmptySymbolName {
        index: usize,
    },
    DuplicateSymbolName(String),
    SymbolOffsetOutOfBounds {
        symbol: String,
        section: String,
        offset: usize,
        section_size: usize,
    },
    InvalidRelocWidth {
        section: String,
        width: usize,
    },
    RelocSlotOutOfBounds {
        section: String,
        offset: usize,
        width: usize,
        section_size: usize,
    },
    UnknownSection(String),
    /// A relocated value cannot be represented in the given slot width.
    Overflow {
        section: String,
        reloc_offset: usize,
        value: i128,
        width: usize,
        min: i128,
        max: i128,
    },
    /// Defensive invariant: a non-equivalent pair produced no distinguishing path.
    DistinctionExhausted {
        section_a: String,
        section_b: String,
    },
}

impl fmt::Display for LinkerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LinkerError::DuplicateSectionName(n) => {
                write!(f, "duplicate section name `{n}`")
            }
            LinkerError::EmptySectionName { index } => {
                write!(f, "section at index {index} has an empty name")
            }
            LinkerError::InvalidAlignment { section, alignment } => write!(
                f,
                "section `{section}` has invalid alignment {alignment} (must be a non-zero power of two)"
            ),
            LinkerError::SectionIndexOutOfBounds {
                index,
                section_count,
            } => write!(
                f,
                "section index {index} out of bounds (there are {section_count} sections)"
            ),
            LinkerError::SymbolIndexOutOfBounds {
                index,
                symbol_count,
            } => write!(
                f,
                "symbol index {index} out of bounds (there are {symbol_count} symbols)"
            ),
            LinkerError::EmptySymbolName { index } => {
                write!(f, "symbol at index {index} has an empty name")
            }
            LinkerError::DuplicateSymbolName(n) => write!(f, "duplicate symbol name `{n}`"),
            LinkerError::SymbolOffsetOutOfBounds {
                symbol,
                section,
                offset,
                section_size,
            } => write!(
                f,
                "symbol `{symbol}` offset {offset} is outside section `{section}` of size {section_size}"
            ),
            LinkerError::InvalidRelocWidth { section, width } => write!(
                f,
                "relocation in section `{section}` has unsupported width {width} (allowed 1..=8)"
            ),
            LinkerError::RelocSlotOutOfBounds {
                section,
                offset,
                width,
                section_size,
            } => write!(
                f,
                "relocation slot in section `{section}` spans [{offset}, {}) but section size is {section_size}",
                offset + width
            ),
            LinkerError::UnknownSection(n) => write!(f, "unknown section `{n}`"),
            LinkerError::Overflow {
                section,
                reloc_offset,
                value,
                width,
                min,
                max,
            } => write!(
                f,
                "relocation at offset {reloc_offset} of section `{section}` produced value {value} which does not fit a {width}-byte slot (range [{min}, {max}])"
            ),
            LinkerError::DistinctionExhausted {
                section_a,
                section_b,
            } => write!(
                f,
                "internal invariant: sections `{section_a}` and `{section_b}` are not equivalent but no distinguishing path exists"
            ),
        }
    }
}

impl std::error::Error for LinkerError {}

/// One equivalence class produced by folding.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EquivClass {
    /// Class id, equal to the class position in [`FoldResult::classes`].
    pub id: usize,
    /// Lexicographically smallest member name; the class is laid out under this name.
    pub representative: String,
    pub representative_index: usize,
    /// All member section names, sorted lexicographically.
    pub members: Vec<String>,
    pub member_indices: Vec<usize>,
}

/// The maximal equivalence partition of the input sections.
#[derive(Clone, Debug)]
pub struct FoldResult {
    /// Classes ordered by representative name.
    pub classes: Vec<EquivClass>,
    class_of_section: Vec<usize>,
}

impl FoldResult {
    pub(crate) fn new(classes: Vec<EquivClass>, class_of_section: Vec<usize>) -> Self {
        Self {
            classes,
            class_of_section,
        }
    }

    /// Class id of a section by its index, or `None` if the index is out of bounds.
    pub fn class_of(&self, section_index: usize) -> Option<usize> {
        self.class_of_section.get(section_index).copied()
    }

    /// Whether two sections (by name) ended up in the same equivalence class.
    pub fn are_equivalent(&self, a: &str, b: &str) -> bool {
        let pos = |name: &str| -> Option<usize> {
            self.classes
                .iter()
                .find(|c| c.members.iter().any(|m| m == name))
                .map(|c| c.id)
        };
        match (pos(a), pos(b)) {
            (Some(x), Some(y)) => x == y,
            _ => false,
        }
    }
}

/// A folded class placed in the output image, with all slots already backfilled.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PlacedSection {
    pub class_id: usize,
    pub representative: String,
    pub members: Vec<String>,
    pub address: u64,
    pub size: usize,
    pub alignment: usize,
    /// Output bytes: raw section bytes with every relocation slot overwritten.
    pub bytes: Vec<u8>,
}

/// The complete laid-out output image.
#[derive(Clone, Debug)]
pub struct Layout {
    /// Placed classes, ordered by representative name and increasing address.
    pub placed: Vec<PlacedSection>,
    /// Final absolute address of every named symbol.
    pub symbol_addresses: std::collections::BTreeMap<String, u64>,
}
