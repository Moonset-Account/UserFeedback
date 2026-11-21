//! Exact layout and jump relaxation for a small instruction stream.
//!
//! A [`Program`] is a sequence of four kinds of [`Item`]:
//!
//! * [`Item::Bytes`] — a fixed-length byte segment,
//! * [`Item::Label`] — a named position marker (occupies no bytes),
//! * [`Item::Align`] — zero-fill up to the next multiple of a power-of-two
//!   boundary (no fill when already aligned),
//! * [`Item::Jump`] — a relative branch to a label that is either 2 bytes
//!   (`short`, signed 8-bit displacement `-128..=127`) or 5 bytes (`long`,
//!   signed 32-bit displacement covering every label in the fragment).
//!
//! Every displacement is measured from the end of the jump instruction to the
//! target label.  [`Program::relax`] enumerates all short/long combinations
//! (at most [`MAX_JUMPS`] = 12 jumps, so at most 4096 candidates), keeps the
//! legal layout with the smallest total byte count, and breaks ties by making
//! the earliest jump in input order short.  The returned [`Layout`] reports
//! label addresses, per-alignment padding, jump forms and displacements.
//!
//! Shortening a jump does not necessarily shorten the whole fragment: the
//! bytes saved can be swallowed by a later alignment.  Because the search is
//! exhaustive over jump forms, such layouts are compared by their *final*
//! total size rather than by a local "shorter is better" rule.
//!
//! The implementation uses only the Rust standard library and never panics on
//! malformed input; all problems are reported as [`LayoutError`].
//!
//! # Example
//!
//! ```
//! use jump_relaxer::{Item, JumpForm, Program};
//!
//! let program = Program::from_items(vec![
//!     Item::jump("target"),
//!     Item::align(8),
//!     Item::bytes(vec![0u8; 200]),
//!     Item::label("target"),
//! ]);
//! let layout = program.relax().unwrap();
//! layout.verify().unwrap();
//!
//! // The jump cannot reach 203 bytes away with a signed 8-bit displacement.
//! assert_eq!(layout.jumps[0].form, JumpForm::Long);
//! assert_eq!(layout.jumps[0].displacement, 203);
//! // The alignment site is at address 5 and is padded to 8: 3 zero bytes.
//! assert_eq!(layout.alignments[0].padding, 3);
//! assert_eq!(layout.label_address("target"), Some(208));
//! assert_eq!(layout.total_size, 208);
//! ```

#![forbid(unsafe_code)]
#![warn(missing_docs)]

use std::collections::{HashMap, HashSet};
use std::error::Error;
use std::fmt;

/// Maximum number of jumps accepted by [`Program::relax`].
pub const MAX_JUMPS: usize = 12;

/// Size in bytes of a short jump.
pub const SHORT_JUMP_SIZE: u64 = 2;

/// Size in bytes of a long jump.
pub const LONG_JUMP_SIZE: u64 = 5;

/// Smallest displacement representable by a short jump.
pub const SHORT_MIN: i64 = -128;

/// Largest displacement representable by a short jump.
pub const SHORT_MAX: i64 = 127;

/// One element of an input stream.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Item {
    /// A fixed-length byte segment; its length is the number of bytes.
    Bytes(Vec<u8>),
    /// A named position marker.  Labels occupy zero bytes.
    Label(String),
    /// Zero-fill up to the next multiple of the power-of-two boundary.
    Align(u32),
    /// A relative jump to the named label.
    Jump(String),
}

impl Item {
    /// Build a fixed byte segment.
    pub fn bytes(bytes: impl Into<Vec<u8>>) -> Self {
        Item::Bytes(bytes.into())
    }

    /// Build a label marker.
    pub fn label(name: impl Into<String>) -> Self {
        Item::Label(name.into())
    }

    /// Build an alignment directive.
    pub fn align(boundary: u32) -> Self {
        Item::Align(boundary)
    }

    /// Build a relative jump.
    pub fn jump(target: impl Into<String>) -> Self {
        Item::Jump(target.into())
    }
}

/// An input instruction stream.  Use the builder methods and then call
/// [`Program::relax`].
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Program {
    items: Vec<Item>,
}

impl Program {
    /// Create an empty program.
    pub fn new() -> Self {
        Self { items: Vec::new() }
    }

    /// Create a program from an existing item list.
    pub fn from_items(items: Vec<Item>) -> Self {
        Self { items }
    }

    /// Append an item.
    pub fn push(&mut self, item: Item) -> &mut Self {
        self.items.push(item);
        self
    }

    /// Append a fixed byte segment.
    pub fn bytes(&mut self, bytes: impl Into<Vec<u8>>) -> &mut Self {
        self.push(Item::Bytes(bytes.into()))
    }

    /// Append a label marker.
    pub fn label(&mut self, name: impl Into<String>) -> &mut Self {
        self.push(Item::Label(name.into()))
    }

    /// Append an alignment directive.
    pub fn align(&mut self, boundary: u32) -> &mut Self {
        self.push(Item::Align(boundary))
    }

    /// Append a relative jump.
    pub fn jump(&mut self, target: impl Into<String>) -> &mut Self {
        self.push(Item::Jump(target.into()))
    }

    /// Read the items in input order.
    pub fn items(&self) -> &[Item] {
        &self.items
    }

    /// Compute the minimum-size legal layout.
    ///
    /// The search is exhaustive over the short/long choice of every jump.
    /// Among layouts with equal total size, the one whose earliest differing
    /// jump is short wins (a lexicographic comparison over jump forms in input
    /// order).
    pub fn relax(&self) -> Result<Layout, LayoutError> {
        let prepared = Prepared::new(&self.items)?;
        prepared.relax()
    }
}

/// The final form of a jump.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum JumpForm {
    /// 2-byte instruction with a signed 8-bit displacement.
    Short,
    /// 5-byte instruction with a signed 32-bit displacement.
    Long,
}

impl JumpForm {
    /// Encoded size in bytes.
    pub fn size(self) -> u64 {
        match self {
            JumpForm::Short => SHORT_JUMP_SIZE,
            JumpForm::Long => LONG_JUMP_SIZE,
        }
    }

    /// True for [`JumpForm::Short`].
    pub fn is_short(self) -> bool {
        matches!(self, JumpForm::Short)
    }
}

impl fmt::Display for JumpForm {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            JumpForm::Short => f.write_str("short"),
            JumpForm::Long => f.write_str("long"),
        }
    }
}

/// The kind of one placed item in the final layout.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PlacementKind {
    /// Fixed byte segment.
    Bytes,
    /// Zero-byte label marker.
    Label,
    /// Alignment fill.
    Align {
        /// Power-of-two boundary.
        boundary: u32,
    },
    /// Relative jump.
    Jump {
        /// Final short/long choice.
        form: JumpForm,
    },
}

/// Address and size of one input item in the final layout.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Placement {
    /// Index of the item in the input stream.
    pub item_index: usize,
    /// Address of the item in the final layout.
    pub address: u64,
    /// Number of bytes the item contributes (zero for labels).
    pub size: u64,
    /// What kind of item this is.
    pub kind: PlacementKind,
}

/// Final address of a label.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LabelAddress {
    /// Label name as given in the input.
    pub name: String,
    /// Index of the input [`Item::Label`] that declared this label.
    pub item_index: usize,
    /// Address of the label in the final layout.
    pub address: u64,
}

/// Zero-fill produced by one alignment directive.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AlignmentPadding {
    /// Index of the input [`Item::Align`].
    pub item_index: usize,
    /// Requested power-of-two boundary.
    pub boundary: u32,
    /// Address immediately before the padding (the alignment site).
    pub address: u64,
    /// Number of zero bytes inserted; `(boundary - address % boundary) % boundary`.
    pub padding: u64,
}

/// One jump in the final layout.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PlacedJump {
    /// Index of the input [`Item::Jump`].
    pub item_index: usize,
    /// Target label name.
    pub target: String,
    /// Address of the jump instruction itself.
    pub start: u64,
    /// Final short/long choice.
    pub form: JumpForm,
    /// Signed displacement measured from the end of the instruction.
    pub displacement: i64,
    /// Address of the target label in the final layout.
    pub target_address: u64,
}

impl PlacedJump {
    /// Re-decode the target address from the final instruction position and
    /// displacement: `start + size + displacement`.
    pub fn decoded_target(&self) -> i128 {
        self.start as i128 + self.form.size() as i128 + self.displacement as i128
    }
}

/// The complete, self-consistent result of relaxation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Layout {
    /// Total size of the laid-out fragment in bytes.
    pub total_size: u64,
    /// Label addresses in declaration (input) order.
    pub labels: Vec<LabelAddress>,
    /// Padding produced by each alignment directive, in input order.
    pub alignments: Vec<AlignmentPadding>,
    /// Placed jumps, in input order.
    pub jumps: Vec<PlacedJump>,
    /// Every input item's address, size and kind, in input order.
    pub placements: Vec<Placement>,
}

impl Layout {
    /// Look up a label address by name.
    pub fn label_address(&self, name: &str) -> Option<u64> {
        self.labels
            .iter()
            .find(|label| label.name == name)
            .map(|label| label.address)
    }

    /// Look up the jump belonging to an input item index.
    pub fn jump(&self, item_index: usize) -> Option<&PlacedJump> {
        self.jumps
            .iter()
            .find(|jump| jump.item_index == item_index)
    }

    /// Check every invariant of the final layout, in particular that every
    /// displacement re-decodes to its target and fits its instruction form.
    ///
    /// This is a real consistency check over the reported fields; it does not
    /// consult the original [`Program`].
    pub fn verify(&self) -> Result<(), LayoutError> {
        // Placements must tile [0, total_size) exactly, in input order.
        let mut expected = 0u64;
        for (index, placement) in self.placements.iter().enumerate() {
            if placement.item_index != index {
                return Err(inconsistent("placements are not in input order"));
            }
            if placement.address != expected {
                return Err(inconsistent("placement addresses are not contiguous"));
            }
            expected = expected
                .checked_add(placement.size)
                .ok_or_else(|| inconsistent("placement sizes overflow"))?;
        }
        if expected != self.total_size {
            return Err(inconsistent("total size does not match placements"));
        }

        let label_count = self
            .placements
            .iter()
            .filter(|p| p.kind == PlacementKind::Label)
            .count();
        let align_count = self
            .placements
            .iter()
            .filter(|p| matches!(p.kind, PlacementKind::Align { .. }))
            .count();
        let jump_count = self
            .placements
            .iter()
            .filter(|p| matches!(p.kind, PlacementKind::Jump { .. }))
            .count();
        if self.labels.len() != label_count {
            return Err(inconsistent("label record count does not match placements"));
        }
        if self.alignments.len() != align_count {
            return Err(inconsistent("alignment record count does not match placements"));
        }
        if self.jumps.len() != jump_count {
            return Err(inconsistent("jump record count does not match placements"));
        }

        let mut seen_labels = HashSet::new();
        for label in &self.labels {
            if !seen_labels.insert(label.name.as_str()) {
                return Err(inconsistent("duplicate label record"));
            }
            let placement = self
                .placements
                .get(label.item_index)
                .ok_or_else(|| inconsistent("label item index is out of range"))?;
            if placement.kind != PlacementKind::Label {
                return Err(inconsistent("label record points at a non-label item"));
            }
            if placement.address != label.address {
                return Err(inconsistent("label address does not match its placement"));
            }
        }

        let mut seen_aligns = HashSet::new();
        for alignment in &self.alignments {
            if !seen_aligns.insert(alignment.item_index) {
                return Err(inconsistent("duplicate alignment record"));
            }
            if alignment.boundary == 0 || !alignment.boundary.is_power_of_two() {
                return Err(inconsistent("alignment boundary is not a power of two"));
            }
            let placement = self
                .placements
                .get(alignment.item_index)
                .ok_or_else(|| inconsistent("alignment item index is out of range"))?;
            match placement.kind {
                PlacementKind::Align { boundary } if boundary == alignment.boundary => {}
                _ => return Err(inconsistent("alignment record does not match its placement")),
            }
            if placement.address != alignment.address || placement.size != alignment.padding {
                return Err(inconsistent("alignment address or padding does not match placement"));
            }
            let boundary = alignment.boundary as u64;
            let expected_padding = (boundary - placement.address % boundary) % boundary;
            if placement.size != expected_padding || alignment.padding != expected_padding {
                return Err(inconsistent("alignment padding formula is violated"));
            }
        }

        let mut seen_jumps = HashSet::new();
        for jump in &self.jumps {
            if !seen_jumps.insert(jump.item_index) {
                return Err(inconsistent("duplicate jump record"));
            }
            let placement = self
                .placements
                .get(jump.item_index)
                .ok_or_else(|| inconsistent("jump item index is out of range"))?;
            match placement.kind {
                PlacementKind::Jump { form } if form == jump.form => {}
                _ => return Err(inconsistent("jump record does not match its placement")),
            }
            if placement.address != jump.start || placement.size != jump.form.size() {
                return Err(inconsistent("jump address or size does not match placement"));
            }
            let target = self
                .labels
                .iter()
                .find(|label| label.name == jump.target)
                .ok_or_else(|| inconsistent("jump target label is missing"))?;
            if target.address != jump.target_address {
                return Err(inconsistent("jump target address is wrong"));
            }
            let decoded = jump.decoded_target();
            if decoded != jump.target_address as i128 {
                return Err(inconsistent("displacement does not re-decode to the target"));
            }
            match jump.form {
                JumpForm::Short => {
                    if !(SHORT_MIN..=SHORT_MAX).contains(&jump.displacement) {
                        return Err(inconsistent("short jump displacement is out of range"));
                    }
                }
                JumpForm::Long => {
                    if jump.displacement < i32::MIN as i64 || jump.displacement > i32::MAX as i64 {
                        return Err(inconsistent("long jump displacement is out of range"));
                    }
                }
            }
        }

        Ok(())
    }
}

/// Errors produced while validating input or laying it out.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LayoutError {
    /// More than [`MAX_JUMPS`] relative jumps were supplied.
    TooManyJumps {
        /// Number of jumps found.
        count: usize,
    },
    /// A label with an empty name was supplied.
    EmptyLabelName {
        /// Index of the offending item.
        item_index: usize,
    },
    /// A label name was declared more than once.
    DuplicateLabel {
        /// The repeated name.
        name: String,
    },
    /// A jump references a label that is never declared.
    UndefinedLabel {
        /// The missing name.
        name: String,
    },
    /// An alignment boundary is zero or not a power of two.
    InvalidAlignment {
        /// Index of the offending item.
        item_index: usize,
        /// The rejected boundary.
        boundary: u32,
    },
    /// Address or size arithmetic overflowed `u64`.
    AddressOverflow,
    /// Even a long jump cannot reach the target.
    LongJumpOutOfRange {
        /// Index of the offending jump item.
        item_index: usize,
        /// Displacement that did not fit the long form.
        displacement: i128,
    },
    /// A relaxed layout failed its own consistency check.
    InconsistentLayout {
        /// Human-readable reason.
        detail: String,
    },
}

impl fmt::Display for LayoutError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LayoutError::TooManyJumps { count } => {
                write!(f, "too many jumps: {count} (maximum is {MAX_JUMPS})")
            }
            LayoutError::EmptyLabelName { item_index } => {
                write!(f, "item {item_index}: label name is empty")
            }
            LayoutError::DuplicateLabel { name } => {
                write!(f, "duplicate label {name:?}")
            }
            LayoutError::UndefinedLabel { name } => {
                write!(f, "undefined label {name:?}")
            }
            LayoutError::InvalidAlignment {
                item_index,
                boundary,
            } => write!(
                f,
                "item {item_index}: alignment boundary {boundary} is not a power of two"
            ),
            LayoutError::AddressOverflow => f.write_str("address arithmetic overflowed"),
            LayoutError::LongJumpOutOfRange {
                item_index,
                displacement,
            } => write!(
                f,
                "item {item_index}: long jump displacement {displacement} does not fit in i32"
            ),
            LayoutError::InconsistentLayout { detail } => {
                write!(f, "inconsistent layout: {detail}")
            }
        }
    }
}

impl Error for LayoutError {}

fn inconsistent(detail: &str) -> LayoutError {
    LayoutError::InconsistentLayout {
        detail: detail.to_string(),
    }
}

// ---------------------------------------------------------------------------
// Internal preparation and exhaustive search
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
enum Meta {
    Bytes(u64),
    Label(usize),
    Align(u32),
    Jump(usize),
}

#[derive(Clone, Debug)]
struct LabelMeta {
    name: String,
    item_index: usize,
}

#[derive(Clone, Debug)]
struct JumpMeta {
    item_index: usize,
    target: usize,
}

struct Prepared {
    metas: Vec<Meta>,
    labels: Vec<LabelMeta>,
    jumps: Vec<JumpMeta>,
}

struct Walk {
    positions: Vec<u64>,
    label_addresses: Vec<u64>,
    total: u64,
}

#[derive(Clone, Copy, Debug)]
enum JumpIssue {
    ShortOutOfRange,
    LongOutOfRange { item_index: usize, displacement: i128 },
}

impl Prepared {
    fn new(items: &[Item]) -> Result<Self, LayoutError> {
        // Pass 1: collect and validate labels.
        let mut label_map: HashMap<String, usize> = HashMap::new();
        let mut labels = Vec::new();
        for (item_index, item) in items.iter().enumerate() {
            if let Item::Label(name) = item {
                if name.is_empty() {
                    return Err(LayoutError::EmptyLabelName { item_index });
                }
                if label_map.contains_key(name) {
                    return Err(LayoutError::DuplicateLabel { name: name.clone() });
                }
                label_map.insert(name.clone(), labels.len());
                labels.push(LabelMeta {
                    name: name.clone(),
                    item_index,
                });
            }
        }

        // Pass 2: validate the rest and resolve jump targets.
        let mut metas = Vec::with_capacity(items.len());
        let mut jumps: Vec<JumpMeta> = Vec::new();
        for (item_index, item) in items.iter().enumerate() {
            match item {
                Item::Bytes(bytes) => {
                    let len = u64::try_from(bytes.len()).map_err(|_| LayoutError::AddressOverflow)?;
                    metas.push(Meta::Bytes(len));
                }
                Item::Label(name) => {
                    let ordinal = label_map[name];
                    metas.push(Meta::Label(ordinal));
                }
                Item::Align(boundary) => {
                    if !boundary.is_power_of_two() {
                        return Err(LayoutError::InvalidAlignment {
                            item_index,
                            boundary: *boundary,
                        });
                    }
                    metas.push(Meta::Align(*boundary));
                }
                Item::Jump(target) => {
                    let target = *label_map
                        .get(target)
                        .ok_or_else(|| LayoutError::UndefinedLabel {
                            name: target.clone(),
                        })?;
                    let ordinal = jumps.len();
                    jumps.push(JumpMeta {
                        item_index,
                        target,
                    });
                    metas.push(Meta::Jump(ordinal));
                }
            }
        }
        if jumps.len() > MAX_JUMPS {
            return Err(LayoutError::TooManyJumps { count: jumps.len() });
        }

        Ok(Self {
            metas,
            labels,
            jumps,
        })
    }

    fn jump_size(&self, jump: usize, mask: u32) -> u64 {
        if mask & (1u32 << jump) != 0 {
            LONG_JUMP_SIZE
        } else {
            SHORT_JUMP_SIZE
        }
    }

    fn jump_form(&self, jump: usize, mask: u32) -> JumpForm {
        if mask & (1u32 << jump) != 0 {
            JumpForm::Long
        } else {
            JumpForm::Short
        }
    }

    /// Assign addresses for one short/long assignment (`mask` bit = 1 means
    /// the jump at that input-order index is long).
    fn walk(&self, mask: u32) -> Result<Walk, LayoutError> {
        let mut positions = vec![0u64; self.metas.len()];
        let mut label_addresses = vec![0u64; self.labels.len()];
        let mut position = 0u64;
        for (index, meta) in self.metas.iter().enumerate() {
            positions[index] = position;
            match *meta {
                Meta::Bytes(len) => {
                    position = position
                        .checked_add(len)
                        .ok_or(LayoutError::AddressOverflow)?;
                }
                Meta::Label(ordinal) => label_addresses[ordinal] = position,
                Meta::Align(boundary) => {
                    let boundary = boundary as u64;
                    let remainder = position % boundary;
                    if remainder != 0 {
                        position = position
                            .checked_add(boundary - remainder)
                            .ok_or(LayoutError::AddressOverflow)?;
                    }
                }
                Meta::Jump(jump) => {
                    position = position
                        .checked_add(self.jump_size(jump, mask))
                        .ok_or(LayoutError::AddressOverflow)?;
                }
            }
        }
        Ok(Walk {
            positions,
            label_addresses,
            total: position,
        })
    }

    /// Check that every jump form fits its selected instruction size.
    ///
    /// Long-range failures are reported in preference to short-range ones and
    /// are detected independently of the scan order, so a mask that happens to
    /// have an unreachable short jump first cannot mask an unreachable long
    /// jump.
    fn check_jumps(&self, mask: u32, walk: &Walk) -> Result<(), JumpIssue> {
        let mut short_out_of_range = false;
        let mut long_issue = None;
        for (jump_ordinal, meta) in self.jumps.iter().enumerate() {
            let size = self.jump_size(jump_ordinal, mask);
            let start = walk.positions[meta.item_index] as i128;
            let target = walk.label_addresses[meta.target] as i128;
            let displacement = target - (start + size as i128);
            if size == SHORT_JUMP_SIZE {
                if displacement < SHORT_MIN as i128 || displacement > SHORT_MAX as i128 {
                    short_out_of_range = true;
                }
            } else if displacement < i32::MIN as i128 || displacement > i32::MAX as i128 {
                long_issue = Some(JumpIssue::LongOutOfRange {
                    item_index: meta.item_index,
                    displacement,
                });
            }
        }
        match long_issue {
            Some(issue) => Err(issue),
            None if short_out_of_range => Err(JumpIssue::ShortOutOfRange),
            None => Ok(()),
        }
    }

    /// Tie-break key: fold the long flags in input order with the earliest
    /// jump as the most significant bit, so the numerically smallest key
    /// prefers short jumps at the earliest possible position.
    fn tie_key(&self, mask: u32) -> u32 {
        let mut key = 0u32;
        for jump in 0..self.jumps.len() {
            let long = (mask >> jump) & 1;
            key = (key << 1) | long;
        }
        key
    }

    fn relax(&self) -> Result<Layout, LayoutError> {
        let mask_count = 1u32 << self.jumps.len();
        let mut best: Option<(u64, u32, u32)> = None; // (total, tie key, mask)
        let mut best_overflow: Option<(usize, i128)> = None;

        for mask in 0..mask_count {
            let walk = self.walk(mask)?;
            match self.check_jumps(mask, &walk) {
                Ok(()) => {
                    let candidate = (walk.total, self.tie_key(mask), mask);
                    if best.map_or(true, |current| candidate < current) {
                        best = Some(candidate);
                    }
                }
                Err(JumpIssue::ShortOutOfRange) => {}
                Err(JumpIssue::LongOutOfRange {
                    item_index,
                    displacement,
                }) => {
                    // Keep the earliest offending jump so the reported error is
                    // deterministic regardless of enumeration order.
                    let earlier = match best_overflow {
                        Some((seen_index, _)) => item_index < seen_index,
                        None => true,
                    };
                    if earlier {
                        best_overflow = Some((item_index, displacement));
                    }
                }
            }
        }

        let Some((_, _, mask)) = best else {
            if let Some((item_index, displacement)) = best_overflow {
                return Err(LayoutError::LongJumpOutOfRange {
                    item_index,
                    displacement,
                });
            }
            // A layout with every jump long always exists unless a long jump
            // itself is out of range, so this arm is defensive.
            return Err(inconsistent("no legal layout found"));
        };

        self.build(mask)
    }

    fn build(&self, mask: u32) -> Result<Layout, LayoutError> {
        let walk = self.walk(mask)?;

        let mut placements = Vec::with_capacity(self.metas.len());
        for (item_index, meta) in self.metas.iter().enumerate() {
            let address = walk.positions[item_index];
            let (size, kind) = match *meta {
                Meta::Bytes(len) => (len, PlacementKind::Bytes),
                Meta::Label(_) => (0, PlacementKind::Label),
                Meta::Align(boundary) => {
                    let boundary64 = boundary as u64;
                    let padding = (boundary64 - address % boundary64) % boundary64;
                    (padding, PlacementKind::Align { boundary })
                }
                Meta::Jump(jump) => (
                    self.jump_size(jump, mask),
                    PlacementKind::Jump {
                        form: self.jump_form(jump, mask),
                    },
                ),
            };
            placements.push(Placement {
                item_index,
                address,
                size,
                kind,
            });
        }

        let labels: Vec<LabelAddress> = self
            .labels
            .iter()
            .enumerate()
            .map(|(ordinal, meta)| LabelAddress {
                name: meta.name.clone(),
                item_index: meta.item_index,
                address: walk.label_addresses[ordinal],
            })
            .collect();

        let mut alignments = Vec::new();
        for (item_index, meta) in self.metas.iter().enumerate() {
            if let Meta::Align(boundary) = *meta {
                let address = walk.positions[item_index];
                let boundary64 = boundary as u64;
                let padding = (boundary64 - address % boundary64) % boundary64;
                alignments.push(AlignmentPadding {
                    item_index,
                    boundary,
                    address,
                    padding,
                });
            }
        }

        let mut jumps = Vec::with_capacity(self.jumps.len());
        for (ordinal, meta) in self.jumps.iter().enumerate() {
            let form = self.jump_form(ordinal, mask);
            let start = walk.positions[meta.item_index];
            let target_address = walk.label_addresses[meta.target];
            let displacement = target_address as i128 - (start as i128 + form.size() as i128);
            let displacement = i64::try_from(displacement)
                .map_err(|_| LayoutError::AddressOverflow)?;
            jumps.push(PlacedJump {
                item_index: meta.item_index,
                target: self.labels[meta.target].name.clone(),
                start,
                form,
                displacement,
                target_address,
            });
        }

        Ok(Layout {
            total_size: walk.total,
            labels,
            alignments,
            jumps,
            placements,
        })
    }
}
