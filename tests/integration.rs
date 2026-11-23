//! Integration tests covering normal, boundary and interleaved (mutually recursive
//! ring) scenarios, address-identity handling, layout/backfill overflow checks,
//! shortest-path diagnostics and input-order independence.

use section_folder::*;

// ---------------------------------------------------------------------------
// builders
// ---------------------------------------------------------------------------

fn section(
    name: &str,
    bytes: Vec<u8>,
    alignment: usize,
    relocations: Vec<Relocation>,
) -> Section {
    Section {
        name: name.to_string(),
        bytes,
        alignment,
        relocations,
    }
}

fn symbol(name: &str, sec: usize, offset: usize, address_taken: bool) -> Symbol {
    Symbol {
        name: name.to_string(),
        section: sec,
        offset,
        address_taken,
    }
}

fn abs4(symbol_idx: usize, offset: usize, addend: i64) -> Relocation {
    Relocation {
        rtype: RelocType::absolute(4),
        offset,
        target_symbol: symbol_idx,
        addend,
    }
}

fn rel(width: usize, symbol_idx: usize, offset: usize, addend: i64) -> Relocation {
    Relocation {
        rtype: RelocType::relative(width),
        offset,
        target_symbol: symbol_idx,
        addend,
    }
}

/// 16-byte section with one 8-byte absolute slot at offset 0. All ring sections
/// share identical content; their distinguishing structure comes only from
/// relocations, so mutually recursive equivalence really exercises refinement.
fn ring_section(name: &str) -> Section {
    let mut bytes = vec![0u8; 16];
    bytes[0..8].copy_from_slice(&0xDEAD_BEEF_CAFE_BABEu64.to_le_bytes());
    bytes[8..].copy_from_slice(&[9, 8, 7, 6, 5, 4, 3, 2]);
    section(name, bytes, 8, vec![abs4(0, 0, 0)])
}

// ---------------------------------------------------------------------------
// 1. plain folding
// ---------------------------------------------------------------------------

#[test]
fn identical_plain_sections_fold_and_different_bytes_do_not() {
    let input = LinkerInput {
        sections: vec![
            section("a", vec![1, 2, 3, 4], 1, vec![]),
            section("b", vec![1, 2, 3, 4], 1, vec![]),
            section("c", vec![1, 2, 3, 5], 1, vec![]),
        ],
        symbols: vec![],
    };
    let result = fold(&input).unwrap();
    assert_eq!(result.classes.len(), 2);
    assert!(result.are_equivalent("a", "b"));
    assert!(!result.are_equivalent("a", "c"));
    let rep = result
        .classes
        .iter()
        .find(|c| c.members.contains(&"a".to_string()))
        .unwrap();
    assert_eq!(rep.representative, "a");
    assert_eq!(rep.members, vec!["a", "b"]);
}

#[test]
fn placeholder_bytes_are_masked_when_comparing() {
    let mut left = vec![0u8; 16];
    let mut right = vec![0u8; 16];
    left[0..8].copy_from_slice(&111u64.to_le_bytes());
    right[0..8].copy_from_slice(&222u64.to_le_bytes());
    // identical outside the slot
    left[8..].copy_from_slice(&[9, 8, 7, 6, 5, 4, 3, 2]);
    right[8..].copy_from_slice(&[9, 8, 7, 6, 5, 4, 3, 2]);

    let input = LinkerInput {
        sections: vec![
            section("left", left, 8, vec![abs4(0, 0, 0)]),
            section("right", right, 8, vec![abs4(0, 0, 0)]),
        ],
        symbols: vec![symbol("g", 0, 2, false)],
    };
    let result = fold(&input).unwrap();
    assert_eq!(result.classes.len(), 1, "slot bytes must be masked");
}

#[test]
fn reloc_addend_and_target_offset_participate_in_equivalence() {
    // Two sections reference the *same* symbol, so any divergence can only come
    // from the slot tuple (type/offset/addend) or the symbol's in-section position.
    let mk_input = |addend: i64, sym_offset: usize| LinkerInput {
        sections: vec![
            section(
                "x",
                vec![0u8; 8],
                1,
                vec![Relocation {
                    rtype: RelocType::absolute(4),
                    offset: 0,
                    target_symbol: 0,
                    addend,
                }],
            ),
            section(
                "y",
                vec![0u8; 8],
                1,
                vec![Relocation {
                    rtype: RelocType::absolute(4),
                    offset: 0,
                    target_symbol: 0,
                    addend,
                }],
            ),
            section("other", vec![1u8], 1, vec![]),
        ],
        symbols: vec![symbol("g0", 2, sym_offset, false)],
    };

    // Same addend and same target in-section offset -> fold.
    let same = fold(&mk_input(7, 0)).unwrap();
    assert!(same.are_equivalent("x", "y"));
    assert_eq!(same.classes.len(), 2); // {x,y}, other

    // Different addend -> do not fold.
    let mut diff_addend = mk_input(7, 0);
    diff_addend.sections[1].relocations[0].addend = 8;
    let r = fold(&diff_addend).unwrap();
    assert!(!r.are_equivalent("x", "y"));

    // Different slot offset -> do not fold.
    let mut diff_slot = mk_input(7, 0);
    diff_slot.sections[1].relocations[0].offset = 4;
    let r = fold(&diff_slot).unwrap();
    assert!(!r.are_equivalent("x", "y"));

    // Different target symbol position inside its section: give the two
    // referencing sections distinct payload sections whose symbols sit at
    // different offsets, but make the payloads' masked bytes identical.
    let offset_input = LinkerInput {
        sections: vec![
            section(
                "x",
                vec![0u8; 8],
                1,
                vec![Relocation {
                    rtype: RelocType::absolute(4),
                    offset: 0,
                    target_symbol: 0,
                    addend: 7,
                }],
            ),
            section(
                "y",
                vec![0u8; 8],
                1,
                vec![Relocation {
                    rtype: RelocType::absolute(4),
                    offset: 0,
                    target_symbol: 1,
                    addend: 7,
                }],
            ),
            section("px", vec![0u8; 16], 1, vec![]),
            section("py", vec![0u8; 16], 1, vec![]),
        ],
        symbols: vec![
            symbol("gx", 2, 0, false),
            symbol("gy", 3, 4, false),
        ],
    };
    let r = fold(&offset_input).unwrap();
    assert!(!r.are_equivalent("x", "y"));
    // But the two payload sections themselves are still foldable.
    assert!(r.are_equivalent("px", "py"));
}

// ---------------------------------------------------------------------------
// 2. mutually recursive rings
// ---------------------------------------------------------------------------
//
// A ring on its own is a single bisimulation class: with no external anchor,
// every role sees the same one-hop neighbourhood. The realistic linker case
// always has an anchor — here the rings' terminal sections reference a shared,
// statically distinct sink section. That anchor lets partition refinement
// recover the ring roles, after which two structurally identical rings fold
// role by role despite their mutual recursion.

/// Two identical 3-section rings (a->b->c->sink) plus a shared sink.
fn two_rings_with_sink() -> LinkerInput {
    let sections = vec![
        ring_section("a1"),
        ring_section("b1"),
        ring_section("c1"),
        ring_section("a2"),
        ring_section("b2"),
        ring_section("c2"),
        section("sink", vec![42u8; 8], 1, vec![]),
    ];
    let symbols = vec![
        symbol("sa1", 0, 0, false),
        symbol("sb1", 1, 0, false),
        symbol("sc1", 2, 0, false),
        symbol("sa2", 3, 0, false),
        symbol("sb2", 4, 0, false),
        symbol("sc2", 5, 0, false),
        symbol("ssink", 6, 0, false),
    ];
    let mut sections = sections;
    sections[0].relocations = vec![abs4(1, 0, 0)];
    sections[1].relocations = vec![abs4(2, 0, 0)];
    sections[2].relocations = vec![abs4(6, 0, 0)]; // c1 -> sink
    sections[3].relocations = vec![abs4(4, 0, 0)];
    sections[4].relocations = vec![abs4(5, 0, 0)];
    sections[5].relocations = vec![abs4(6, 0, 0)]; // c2 -> sink
    LinkerInput { sections, symbols }
}

/// Same as [`two_rings_with_sink`], plus a third ring whose terminal c3 defines
/// an address-taken symbol and therefore cannot fold (safe-ICF variant).
fn two_rings_plus_address_sensitive_variant() -> LinkerInput {
    let sections = vec![
        ring_section("a1"),
        ring_section("b1"),
        ring_section("c1"),
        ring_section("a2"),
        ring_section("b2"),
        ring_section("c2"),
        ring_section("a3"),
        ring_section("b3"),
        ring_section("c3"),
        section("sink", vec![42u8; 8], 1, vec![]),
    ];
    let symbols = vec![
        symbol("sa1", 0, 0, false),
        symbol("sb1", 1, 0, false),
        symbol("sc1", 2, 0, false),
        symbol("sa2", 3, 0, false),
        symbol("sb2", 4, 0, false),
        symbol("sc2", 5, 0, false),
        symbol("sa3", 6, 0, false),
        symbol("sb3", 7, 0, false),
        // the only structural difference: sc3 has address identity
        symbol("sc3", 8, 0, true),
        symbol("ssink", 9, 0, false),
    ];
    let mut sections = sections;
    sections[0].relocations = vec![abs4(1, 0, 0)];
    sections[1].relocations = vec![abs4(2, 0, 0)];
    sections[2].relocations = vec![abs4(9, 0, 0)];
    sections[3].relocations = vec![abs4(4, 0, 0)];
    sections[4].relocations = vec![abs4(5, 0, 0)];
    sections[5].relocations = vec![abs4(9, 0, 0)];
    sections[6].relocations = vec![abs4(7, 0, 0)];
    sections[7].relocations = vec![abs4(8, 0, 0)];
    sections[8].relocations = vec![abs4(9, 0, 0)]; // c3 -> sink
    LinkerInput { sections, symbols }
}

#[test]
fn two_mutually_recursive_rings_fully_fold() {
    let input = two_rings_with_sink();
    let result = fold(&input).unwrap();
    // three role classes each holding both rings, plus the singleton sink
    assert_eq!(result.classes.len(), 4);
    assert!(result.are_equivalent("a1", "a2"));
    assert!(result.are_equivalent("b1", "b2"));
    assert!(result.are_equivalent("c1", "c2"));
    assert!(!result.are_equivalent("a1", "sink"));
}

#[test]
fn anchorless_isomorphic_ring_is_treated_as_one_class() {
    // A single self-isomorphic ring with no distinguishing anchor has no
    // information to split it: every section is bisimilar, so it folds wholesale.
    let mut sections = vec![
        ring_section("r0"),
        ring_section("r1"),
        ring_section("r2"),
    ];
    let symbols = vec![
        symbol("s0", 0, 0, false),
        symbol("s1", 1, 0, false),
        symbol("s2", 2, 0, false),
    ];
    sections[0].relocations = vec![abs4(1, 0, 0)];
    sections[1].relocations = vec![abs4(2, 0, 0)];
    sections[2].relocations = vec![abs4(0, 0, 0)];
    let result = fold(&LinkerInput { sections, symbols }).unwrap();
    assert_eq!(result.classes.len(), 1);
}

#[test]
fn address_sensitive_entry_pins_only_its_ring_end() {
    let result = fold(&two_rings_plus_address_sensitive_variant()).unwrap();
    // The two ordinary rings still fold role by role.
    assert!(result.are_equivalent("a1", "a2"));
    assert!(result.are_equivalent("b1", "b2"));
    assert!(result.are_equivalent("c1", "c2"));
    // The variant ring never joins the ordinary rings.
    assert!(!result.are_equivalent("a1", "a3"));
    assert!(!result.are_equivalent("b1", "b3"));
    // c3 is pinned as a singleton by its address-taken symbol.
    let c3 = result
        .classes
        .iter()
        .find(|c| c.representative == "c3")
        .unwrap();
    assert_eq!(c3.members, vec!["c3"]);
    // The three ordinary-ring role classes remain two-member classes.
    let folded_pairs = result
        .classes
        .iter()
        .filter(|c| c.members.len() == 2)
        .count();
    assert_eq!(folded_pairs, 3);
}

// ---------------------------------------------------------------------------
// 3. shortest distinguishing path
// ---------------------------------------------------------------------------

#[test]
fn distinguish_returns_none_for_equivalent_pair() {
    let input = two_rings_with_sink();
    assert!(distinguish(&input, "a1", "a2").unwrap().is_none());
    assert!(distinguish(&input, "c1", "c2").unwrap().is_none());
}

#[test]
fn distinguish_finds_shortest_path_to_address_sensitive_variant() {
    let input = two_rings_plus_address_sensitive_variant();
    let d = distinguish(&input, "a1", "a3")
        .unwrap()
        .expect("a1 and a3 must be distinguishable");
    // a1 and a3 have identical static signatures; following their slots one hop
    // reaches b1 vs b3, and the divergence (the pinned sc3 symbol) is exposed
    // two hops in along the shortest path.
    assert_eq!(d.path_a.len(), d.path_b.len());
    assert!(!d.path_a.is_empty(), "expected a non-trivial path: {d}");
    assert_eq!(d.path_a[0].section, "a1");
    assert_eq!(d.path_b[0].section, "a3");
    match d.termination {
        PathTermination::ExactAddressSymbol {
            symbol_a,
            symbol_b,
        } => {
            assert_eq!(symbol_a, "sc1");
            assert_eq!(symbol_b, "sc3");
        }
        PathTermination::Direct(DirectDifference::KeptByAddressIdentity { .. }) => {}
        other => panic!("unexpected termination: {other:?}"),
    }
}

#[test]
fn distinguish_reports_kept_section_directly() {
    let input = two_rings_plus_address_sensitive_variant();
    // c1 and c3 point at the same sink with identical slots, but c3 defines an
    // address-taken symbol: the verifier reports that identity pin directly.
    let d = distinguish(&input, "c1", "c3").unwrap().unwrap();
    assert!(d.path_a.is_empty());
    assert!(d.path_b.is_empty());
    assert!(matches!(
        d.termination,
        PathTermination::Direct(DirectDifference::KeptByAddressIdentity { .. })
    ));
}

#[test]
fn distinguish_reports_direct_byte_difference() {
    let input = LinkerInput {
        sections: vec![
            section("p", vec![1, 1, 1], 1, vec![]),
            section("q", vec![1, 1, 2], 1, vec![]),
        ],
        symbols: vec![],
    };
    let d = distinguish(&input, "p", "q").unwrap().unwrap();
    assert!(d.path_a.is_empty());
    assert!(matches!(
        d.termination,
        PathTermination::Direct(DirectDifference::MaskedBytes)
    ));
}

#[test]
fn distinguish_reports_direct_alignment_difference() {
    let input = LinkerInput {
        sections: vec![
            section("p", vec![0, 0], 1, vec![]),
            section("q", vec![0, 0], 2, vec![]),
        ],
        symbols: vec![],
    };
    let d = distinguish(&input, "p", "q").unwrap().unwrap();
    assert!(matches!(
        d.termination,
        PathTermination::Direct(DirectDifference::Alignment { .. })
    ));
}

#[test]
fn distinguish_walks_deep_ring_chain_to_terminal_mismatch() {
    // Two 3-hop chains whose sections have identical masked bytes at every level.
    // The terminal sections are identical too and point at payload sections; one
    // payload carries an extra relocation slot. BFS must walk all three hops and
    // then one final edge before reporting the slot-tuple mismatch.
    let build = |prefix: &str, extra_slot: bool| -> (Vec<Section>, Vec<Symbol>) {
        let mut secs: Vec<Section> = (0..3)
            .map(|i| ring_section(&format!("{prefix}{i}")))
            .collect();
        // terminal section at index 3 (identical in both chains)
        secs.push(ring_section(&format!("{prefix}term")));
        // payload at index 4: same bytes in both chains; y gets an extra slot
        secs.push(section(
            &format!("{prefix}payload"),
            vec![0u8; 16],
            1,
            vec![],
        ));

        let syms: Vec<Symbol> = (0..5)
            .map(|i| symbol(&format!("{prefix}s{i}"), i, 0, false))
            .collect();
        // chain 0 -> 1 -> 2 -> 3(term); term references payload (index 4)
        secs[0].relocations = vec![abs4(1, 0, 0)];
        secs[1].relocations = vec![abs4(2, 0, 0)];
        secs[2].relocations = vec![abs4(3, 0, 0)];
        secs[3].relocations = vec![abs4(4, 0, 0)];
        if extra_slot {
            secs[4].relocations.push(Relocation {
                rtype: RelocType::absolute(1),
                offset: 8,
                target_symbol: 4,
                addend: 0,
            });
        }
        (secs, syms)
    };

    let (mut secs_x, syms_x) = build("x", false);
    let (mut secs_y, syms_y_unshifted) = build("y", true);
    let syms_y: Vec<Symbol> = syms_y_unshifted
        .into_iter()
        .map(|s| Symbol {
            section: s.section + 5,
            ..s
        })
        .collect();
    for s in secs_y.iter_mut() {
        for r in s.relocations.iter_mut() {
            r.target_symbol += 5;
        }
    }
    secs_x.append(&mut secs_y);
    let mut symbols = syms_x;
    symbols.extend(syms_y);
    let input = LinkerInput {
        sections: secs_x,
        symbols,
    };

    let result = fold(&input).unwrap();
    assert!(!result.are_equivalent("x0", "y0"));
    let d = distinguish(&input, "x0", "y0").unwrap().unwrap();
    // hops: x0->x1->x2->xterm->xpayload (and the y counterpart)
    assert_eq!(d.path_a.len(), 4, "full path: {d}");
    assert_eq!(d.path_a[2].section, "x2");
    assert_eq!(d.path_a[3].section, "xterm");
    assert_eq!(d.path_b[3].section, "yterm");
    assert!(
        matches!(d.termination, PathTermination::RelocSlotMismatch { .. }),
        "unexpected termination: {:?}",
        d.termination
    );
}

#[test]
fn distinguish_flags_different_address_taken_symbols_at_a_slot() {
    let input = LinkerInput {
        sections: vec![
            section("p", vec![0; 8], 1, vec![abs4(0, 0, 0)]),
            section("q", vec![0; 8], 1, vec![abs4(1, 0, 0)]),
            section("kp", vec![7], 1, vec![]),
            section("kq", vec![7], 1, vec![]),
        ],
        symbols: vec![
            symbol("ga", 2, 0, true),
            symbol("gb", 3, 0, true),
        ],
    };
    let d = distinguish(&input, "p", "q").unwrap().unwrap();
    match d.termination {
        PathTermination::ExactAddressSymbol {
            symbol_a,
            symbol_b,
        } => {
            assert_eq!(symbol_a, "ga");
            assert_eq!(symbol_b, "gb");
        }
        other => panic!("unexpected termination: {other:?}"),
    }
}

// ---------------------------------------------------------------------------
// 4. input order independence
// ---------------------------------------------------------------------------

#[test]
fn shuffled_input_produces_identical_layout() {
    let original = two_rings_plus_address_sensitive_variant();
    let layout_a = layout(&original).unwrap();

    // permutation: reverse section order, then keep symbols consistent by
    // rebuilding through index remapping.
    let n = original.sections.len();
    let perm: Vec<usize> = (0..n).rev().collect();
    let old_to_new: Vec<usize> = {
        let mut v = vec![0usize; n];
        for (new_idx, &old_idx) in perm.iter().enumerate() {
            v[old_idx] = new_idx;
        }
        v
    };
    let mut sections = Vec::new();
    for &old in &perm {
        let s = original.sections[old].clone();
        sections.push(s);
    }
    let mut symbols: Vec<Symbol> = original
        .symbols
        .iter()
        .map(|s| Symbol {
            section: old_to_new[s.section],
            ..s.clone()
        })
        .collect();
    // reverse symbol order too, and remap reloc target indices
    symbols.reverse();
    let old_sym_to_new: Vec<usize> = {
        let mut v = vec![0usize; original.symbols.len()];
        for (new_idx, old_idx) in (0..original.symbols.len()).rev().enumerate() {
            v[old_idx] = new_idx;
        }
        v
    };
    for s in sections.iter_mut() {
        for r in s.relocations.iter_mut() {
            r.target_symbol = old_sym_to_new[r.target_symbol];
        }
    }
    let shuffled = LinkerInput { sections, symbols };
    let layout_b = layout(&shuffled).unwrap();

    assert_eq!(layout_a.placed.len(), layout_b.placed.len());
    for (pa, pb) in layout_a.placed.iter().zip(layout_b.placed.iter()) {
        assert_eq!(pa.representative, pb.representative);
        assert_eq!(pa.address, pb.address);
        assert_eq!(pa.alignment, pb.alignment);
        assert_eq!(pa.bytes, pb.bytes);
        assert_eq!(pa.members, pb.members);
    }
    assert_eq!(layout_a.symbol_addresses, layout_b.symbol_addresses);

    // representative order is lexicographic in both runs
    let names: Vec<&str> = layout_b.placed.iter().map(|p| p.representative.as_str()).collect();
    let mut sorted = names.clone();
    sorted.sort();
    assert_eq!(names, sorted);
}

// ---------------------------------------------------------------------------
// 5. layout: alignment, backfill, overflow
// ---------------------------------------------------------------------------

#[test]
fn layout_honours_alignment_and_backfills_absolute_values() {
    // Classes are ordered by representative name and addresses are assigned in
    // that order, advancing the cursor to satisfy each alignment.
    let input = LinkerInput {
        sections: vec![
            section("a_big", vec![0u8; 4], 16, vec![]),
            section("z_small", vec![0u8; 4], 1, vec![abs4(0, 0, 0)]),
        ],
        // g0 belongs to z_small, which is section index 1.
        symbols: vec![symbol("g0", 1, 0, false)],
    };
    let out = layout(&input).unwrap();
    let find = |name: &str| out.placed.iter().find(|p| p.representative == name).unwrap();
    assert_eq!(find("a_big").address, 0);
    let small = find("z_small");
    // a_big occupies [0,4); z_small needs no extra padding with alignment 1.
    assert_eq!(small.address, 4);
    assert_eq!(small.bytes, 4u32.to_le_bytes().to_vec()); // g0 at address 4
    assert_eq!(out.symbol_addresses["g0"], 4);

    // A high-alignment section encountered after some content gets padded.
    let padded = LinkerInput {
        sections: vec![
            section("a_first", vec![0u8; 4], 1, vec![]),
            section("z_aligned", vec![0u8; 4], 16, vec![]),
        ],
        symbols: vec![],
    };
    let out2 = layout(&padded).unwrap();
    assert_eq!(out2.placed[0].address, 0);
    assert_eq!(out2.placed[1].address, 16); // 4 -> next 16-byte boundary
}

#[test]
fn absolute_backfill_uses_target_class_address_plus_addend() {
    let input = LinkerInput {
        sections: vec![
            section("src", vec![0u8; 16], 8, vec![abs4(0, 0, 5)]),
            section("dst1", vec![1u8; 8], 8, vec![]),
            section("dst2", vec![1u8; 8], 8, vec![]),
        ],
        symbols: vec![symbol("gd", 1, 3, false)],
    };
    let out = layout(&input).unwrap();
    // Representative order: dst1(dst class, addr 0), src(addr 8).
    // dst1/dst2 fold; gd = 0 + 3 = 3, addend 5 -> slot value 8.
    assert_eq!(out.symbol_addresses["gd"], 3);
    let expected = 8u32.to_le_bytes().to_vec();
    let mut expected_full = vec![0u8; 16];
    expected_full[0..4].copy_from_slice(&expected);
    let src = out
        .placed
        .iter()
        .find(|p| p.representative == "src")
        .unwrap();
    assert_eq!(src.address, 8);
    assert_eq!(src.bytes, expected_full);
}

#[test]
fn relative_backfill_uses_pc_relative_displacement() {
    let input = LinkerInput {
        sections: vec![
            section("src", vec![0u8; 8], 1, vec![rel(2, 0, 2, 0)]),
            section("dst", vec![1u8; 4], 1, vec![]),
        ],
        symbols: vec![symbol("gd", 1, 0, false)],
    };
    let out = layout(&input).unwrap();
    // Representative order: dst at 0, src at 4. Slot at src+2, width 2 -> PC = 8;
    // gd at 0; displacement = 0 - 8 = -8.
    let src = out
        .placed
        .iter()
        .find(|p| p.representative == "src")
        .unwrap();
    assert_eq!(src.bytes[2..4], (-8i16).to_le_bytes());
}

#[test]
fn relative_addends_at_signed_boundary_pass_and_fail() {
    // dst at 0, src at 4; slot PC = 4+2+1 = 7; base displacement = -7.
    let build = |addend: i64| LinkerInput {
        sections: vec![
            section("src", vec![0u8; 8], 1, vec![rel(1, 0, 2, addend)]),
            section("dst", vec![1u8; 4], 1, vec![]),
        ],
        symbols: vec![symbol("gd", 1, 0, false)],
    };
    // -7 + (-121) = -128 -> fits i8
    let ok = layout(&build(-121)).unwrap();
    let src = ok
        .placed
        .iter()
        .find(|p| p.representative == "src")
        .unwrap();
    assert_eq!(src.bytes[2], (-128i8) as u8);
    // -7 + (-122) = -129 -> overflow
    let err = layout(&build(-122)).unwrap_err();
    match err {
        LinkerError::Overflow {
            value, width, min, max, ..
        } => {
            assert_eq!(width, 1);
            assert_eq!(value, -129);
            assert_eq!(min, -128);
            assert_eq!(max, 127);
        }
        other => panic!("unexpected error: {other:?}"),
    }
}

#[test]
fn absolute_overflow_reports_unsigned_range() {
    // 8-byte section aligned to 256 lands after 256 bytes; u8 cannot hold it.
    let input = LinkerInput {
        sections: vec![
            section("pad", vec![0u8; 250], 1, vec![]),
            section(
                "src",
                vec![0u8; 8],
                256,
                vec![Relocation {
                    rtype: RelocType::absolute(1),
                    offset: 0,
                    target_symbol: 0,
                    addend: 0,
                }],
            ),
        ],
        symbols: vec![symbol("gd", 1, 0, false)],
    };
    let err = layout(&input).unwrap_err();
    match err {
        LinkerError::Overflow {
            value, width, min, max, ..
        } => {
            assert_eq!(width, 1);
            assert_eq!(min, 0);
            assert_eq!(max, 255);
            assert!(value > 255);
        }
        other => panic!("unexpected error: {other:?}"),
    }
}

#[test]
fn absolute_values_at_width_boundaries_are_representable() {
    // target exactly at 255 -> u8 ok; 256 would overflow, covered above.
    let input = LinkerInput {
        sections: vec![
            section("pad", vec![0u8; 255], 1, vec![]),
            section(
                "src",
                vec![0u8; 4],
                1,
                vec![Relocation {
                    rtype: RelocType::absolute(1),
                    offset: 0,
                    target_symbol: 0,
                    addend: 0,
                }],
            ),
        ],
        symbols: vec![symbol("gd", 1, 0, false)],
    };
    let out = layout(&input).unwrap();
    assert_eq!(out.placed[1].bytes[0], 255);
}

// ---------------------------------------------------------------------------
// 6. address-identity semantics
// ---------------------------------------------------------------------------

#[test]
fn kept_sections_are_singletons_even_with_identical_bytes() {
    let input = LinkerInput {
        sections: vec![
            section("k1", vec![9, 9], 1, vec![]),
            section("k2", vec![9, 9], 1, vec![]),
        ],
        symbols: vec![
            symbol("s1", 0, 0, true),
            symbol("s2", 1, 0, true),
        ],
    };
    let result = fold(&input).unwrap();
    assert_eq!(result.classes.len(), 2);
    let d = distinguish(&input, "k1", "k2").unwrap().unwrap();
    assert!(matches!(
        d.termination,
        PathTermination::Direct(DirectDifference::KeptByAddressIdentity { .. })
    ));
}

#[test]
fn references_to_the_same_kept_symbol_still_allow_folding() {
    let input = LinkerInput {
        sections: vec![
            section("p", vec![0u8; 8], 1, vec![abs4(0, 0, 0)]),
            section("q", vec![0u8; 8], 1, vec![abs4(0, 0, 0)]),
            section("keeper", vec![1], 1, vec![]),
        ],
        symbols: vec![symbol("gk", 2, 0, true)],
    };
    let result = fold(&input).unwrap();
    assert_eq!(result.classes.len(), 2);
    assert!(result.are_equivalent("p", "q"));
}

// ---------------------------------------------------------------------------
// 7. more validation
// ---------------------------------------------------------------------------

#[test]
fn unknown_section_name_in_distinguish_is_an_error() {
    let input = LinkerInput {
        sections: vec![section("p", vec![0], 1, vec![])],
        symbols: vec![],
    };
    let err = distinguish(&input, "p", "ghost").unwrap_err();
    assert!(matches!(err, LinkerError::UnknownSection(_)));
}

#[test]
fn symbol_out_of_section_and_bad_symbol_index_are_rejected() {
    let err = fold(&LinkerInput {
        sections: vec![section("s", vec![0; 2], 1, vec![])],
        symbols: vec![symbol("g", 0, 10, false)],
    })
    .unwrap_err();
    assert!(matches!(
        err,
        LinkerError::SymbolOffsetOutOfBounds { .. }
    ));

    let err = fold(&LinkerInput {
        sections: vec![section(
            "s",
            vec![0; 4],
            1,
            vec![Relocation {
                rtype: RelocType::absolute(1),
                offset: 0,
                target_symbol: 3,
                addend: 0,
            }],
        )],
        symbols: vec![],
    })
    .unwrap_err();
    assert!(matches!(err, LinkerError::SymbolIndexOutOfBounds { .. }));
}

// ---------------------------------------------------------------------------
// 7. cross-entry consistency and end-to-end ring backfill
// ---------------------------------------------------------------------------

#[test]
fn fold_layout_and_distinguish_agree_on_every_pair() {
    let input = two_rings_plus_address_sensitive_variant();
    let fold_result = fold(&input).unwrap();
    let layout_result = layout(&input).unwrap();

    // Every folded class maps to exactly one placed section and vice versa.
    assert_eq!(fold_result.classes.len(), layout_result.placed.len());
    for class in &fold_result.classes {
        let placed = layout_result
            .placed
            .iter()
            .find(|p| p.representative == class.representative)
            .unwrap();
        assert_eq!(placed.members, class.members);
    }

    // For every section pair, distinguish must agree with the fold partition.
    let names: Vec<&str> = input.sections.iter().map(|s| s.name.as_str()).collect();
    for (i, &a) in names.iter().enumerate() {
        for &b in names.iter().skip(i + 1) {
            let equivalent = fold_result.are_equivalent(a, b);
            match distinguish(&input, a, b).unwrap() {
                None => assert!(equivalent, "{a} vs {b}: fold split them but distinguish found no difference"),
                Some(d) => {
                    assert!(!equivalent, "{a} vs {b}: fold merged them but distinguish reported {d:?}");
                    assert_eq!(d.section_a, a);
                    assert_eq!(d.section_b, b);
                    assert_eq!(d.path_a.len(), d.path_b.len());
                }
            }
        }
    }
}

#[test]
fn layout_backfills_every_relocation_of_folded_recursive_rings() {
    let input = two_rings_with_sink();
    let out = layout(&input).unwrap();

    // Four classes: three ring roles (each two members) + sink.
    assert_eq!(out.placed.len(), 4);
    for placed in &out.placed {
        // Every ring section carries one 4-byte absolute slot at offset 0; its
        // backfilled value must equal the target symbol's final address.
        if placed.representative != "sink" {
            let encoded = u32::from_le_bytes(
                placed.bytes[0..4].try_into().unwrap(),
            ) as u64;
            let target_name = match placed.representative.as_str() {
                "a1" | "a2" => "sb1",
                "b1" | "b2" => "sc1",
                "c1" | "c2" => "ssink",
                other => panic!("unexpected representative {other}"),
            };
            // b1/b2 and c1/c2 fold, so either ring's symbol resolves to the
            // same class address.
            let target = if target_name == "sb1" {
                *out.symbol_addresses
                    .iter()
                    .find(|(k, _)| k.as_str() == "sb1" || k.as_str() == "sb2")
                    .map(|(_, v)| v)
                    .unwrap()
            } else if target_name == "sc1" {
                *out.symbol_addresses
                    .iter()
                    .find(|(k, _)| k.as_str() == "sc1" || k.as_str() == "sc2")
                    .map(|(_, v)| v)
                    .unwrap()
            } else {
                out.symbol_addresses["ssink"]
            };
            assert_eq!(encoded, target, "slot in {} mis-backfilled", placed.representative);
        }
    }

    // Bytes outside any relocation slot must be preserved verbatim.
    let a_class = out
        .placed
        .iter()
        .find(|p| p.representative == "a1")
        .unwrap();
    assert_eq!(&a_class.bytes[8..], &[9, 8, 7, 6, 5, 4, 3, 2]);
}

#[test]
fn maximum_partition_is_greatest_fixed_point_under_extra_inequality() {
    // Adding a second, genuinely different ring must strictly refine the
    // partition rather than merge anything that was previously distinct.
    let base = two_rings_with_sink();
    let base_result = fold(&base).unwrap();

    let mut extended = base.clone();
    extended.sections.push(section("extra", vec![1, 2, 3], 1, vec![]));
    let ext_result = fold(&extended).unwrap();

    // Old sections keep their equivalence relationships; only new classes appear.
    for c1 in &base_result.classes {
        for c2 in &base_result.classes {
            if c1.id != c2.id {
                assert!(!ext_result.are_equivalent(&c1.representative, &c2.representative));
            } else {
                assert!(ext_result.are_equivalent(
                    &c1.members[0],
                    c1.members.last().unwrap()
                ));
            }
        }
    }
    assert_eq!(ext_result.classes.len(), base_result.classes.len() + 1);
}
