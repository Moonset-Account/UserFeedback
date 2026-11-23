//! # section-folder
//!
//! A dependency-free linker-style module that:
//!
//! 1. folds identical read-only sections into maximal equivalence classes while
//!    honouring relocations (including mutually recursive reference cycles) and
//!    address-identity symbols,
//! 2. lays the folded classes out with alignment, backfilling absolute and
//!    PC-relative relocation values and checking slot-width representability,
//! 3. explains why two sections were not merged, giving the shortest relocation
//!    path to the distinguishing pair or a direct identity/byte difference.
//!
//! See `fold`, `layout` and `distinguish` for the public entry points.

pub mod explain;
pub mod fold;
pub mod layout;
pub mod model;
pub mod prep;

pub use explain::{distinguish, DirectDifference, Distinction, DistinctionStep, PathTermination};
pub use fold::fold;
pub use layout::layout;
pub use model::{
    EquivClass, FoldResult, Layout, LinkerError, LinkerInput, PlacedSection, RelocKind,
    RelocType, Relocation, Section, Symbol,
};

#[cfg(test)]
mod unit_tests {
    use super::*;

    fn sym(name: &str, section: usize, offset: usize, address_taken: bool) -> Symbol {
        Symbol {
            name: name.to_string(),
            section,
            offset,
            address_taken,
        }
    }

    #[test]
    fn alignment_rounds_up_to_next_boundary() {
        assert_eq!(layout::align_up(0, 4), 0);
        assert_eq!(layout::align_up(1, 4), 4);
        assert_eq!(layout::align_up(7, 8), 8);
        assert_eq!(layout::align_up(8, 8), 8);
        assert_eq!(layout::align_up(9, 16), 16);
    }

    #[test]
    fn reltype_display() {
        assert_eq!(RelocType::absolute(4).to_string(), "Absolute/4");
        assert_eq!(RelocType::relative(2).to_string(), "Relative/2");
    }

    #[test]
    fn validation_rejects_bad_inputs() {
        // bad alignment
        let err = fold(&LinkerInput {
            sections: vec![Section {
                name: "s".into(),
                bytes: vec![0],
                alignment: 3,
                relocations: vec![],
            }],
            symbols: vec![],
        })
        .unwrap_err();
        assert!(matches!(err, LinkerError::InvalidAlignment { .. }));

        // slot out of bounds
        let err = fold(&LinkerInput {
            sections: vec![Section {
                name: "s".into(),
                bytes: vec![0, 0],
                alignment: 1,
                relocations: vec![Relocation {
                    rtype: RelocType::absolute(4),
                    offset: 1,
                    target_symbol: 0,
                    addend: 0,
                }],
            }],
            symbols: vec![sym("g", 0, 0, false)],
        })
        .unwrap_err();
        assert!(matches!(err, LinkerError::RelocSlotOutOfBounds { .. }));

        // bad width
        let err = fold(&LinkerInput {
            sections: vec![Section {
                name: "s".into(),
                bytes: vec![0, 0, 0, 0],
                alignment: 1,
                relocations: vec![Relocation {
                    rtype: RelocType::absolute(9),
                    offset: 0,
                    target_symbol: 0,
                    addend: 0,
                }],
            }],
            symbols: vec![sym("g", 0, 0, false)],
        })
        .unwrap_err();
        assert!(matches!(err, LinkerError::InvalidRelocWidth { .. }));

        // duplicate section names
        let err = fold(&LinkerInput {
            sections: vec![
                Section {
                    name: "s".into(),
                    bytes: vec![0],
                    alignment: 1,
                    relocations: vec![],
                },
                Section {
                    name: "s".into(),
                    bytes: vec![0],
                    alignment: 1,
                    relocations: vec![],
                },
            ],
            symbols: vec![],
        })
        .unwrap_err();
        assert!(matches!(err, LinkerError::DuplicateSectionName(_)));
    }

    #[test]
    fn empty_input_is_valid() {
        let result = fold(&LinkerInput::default()).unwrap();
        assert!(result.classes.is_empty());
        let out = layout(&LinkerInput::default()).unwrap();
        assert!(out.placed.is_empty());
    }
}
