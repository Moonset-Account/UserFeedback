//! Concurrency/interleaving checks.
//!
//! The public entry points are pure functions over borrowed input with no shared
//! mutable state: many threads can fold, lay out and diagnose the same and
//! permuted inputs concurrently and must always observe identical results.

use section_folder::*;
use std::collections::HashSet;
use std::sync::Arc;
use std::thread;

fn abs4(symbol_idx: usize, offset: usize, addend: i64) -> Relocation {
    Relocation {
        rtype: RelocType::absolute(4),
        offset,
        target_symbol: symbol_idx,
        addend,
    }
}

fn section(
    name: &str,
    bytes: Vec<u8>,
    alignment: usize,
    relocations: Vec<Relocation>,
) -> Section {
    Section {
        name: name.into(),
        bytes,
        alignment,
        relocations,
    }
}

fn ring_section(name: &str) -> Section {
    let mut bytes = vec![0u8; 16];
    bytes[0..8].copy_from_slice(&0xDEAD_BEEF_CAFE_BABEu64.to_le_bytes());
    bytes[8..].copy_from_slice(&[9, 8, 7, 6, 5, 4, 3, 2]);
    Section {
        name: name.into(),
        bytes,
        alignment: 8,
        relocations: vec![abs4(0, 0, 0)],
    }
}

fn sym(name: &str, sec: usize, offset: usize, at: bool) -> Symbol {
    Symbol {
        name: name.into(),
        section: sec,
        offset,
        address_taken: at,
    }
}

fn shared_input() -> LinkerInput {
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
        sym("sa1", 0, 0, false),
        sym("sb1", 1, 0, false),
        sym("sc1", 2, 0, false),
        sym("sa2", 3, 0, false),
        sym("sb2", 4, 0, false),
        sym("sc2", 5, 0, false),
        sym("ssink", 6, 0, false),
    ];
    let mut sections = sections;
    sections[0].relocations = vec![abs4(1, 0, 0)];
    sections[1].relocations = vec![abs4(2, 0, 0)];
    sections[2].relocations = vec![abs4(6, 0, 0)];
    sections[3].relocations = vec![abs4(4, 0, 0)];
    sections[4].relocations = vec![abs4(5, 0, 0)];
    sections[5].relocations = vec![abs4(6, 0, 0)];
    LinkerInput { sections, symbols }
}

#[test]
fn parallel_fold_and_layout_are_deterministic() {
    let input = Arc::new(shared_input());
    let mut handles = Vec::new();
    for _ in 0..8 {
        let input = Arc::clone(&input);
        handles.push(thread::spawn(move || {
            let fold_result = fold(&input).unwrap();
            let layout_result = layout(&input).unwrap();
            let distinction = distinguish(&input, "a1", "sink").unwrap().unwrap();
            (fold_result, layout_result, distinction)
        }));
    }

    let first = handles.remove(0).join().unwrap();
    let baseline_fold = first.0;
    let baseline_layout = first.1;
    let baseline_distinction = first.2;
    assert_eq!(baseline_fold.classes.len(), 4);

    for h in handles {
        let (f, l, d) = h.join().unwrap();
        assert_eq!(f.classes, baseline_fold.classes);
        assert_eq!(l.placed, baseline_layout.placed);
        assert_eq!(l.symbol_addresses, baseline_layout.symbol_addresses);
        assert_eq!(d, baseline_distinction);
    }
}

#[test]
fn concurrent_permutations_all_converge_on_one_layout() {
    let base = Arc::new(shared_input());
    let mut handles = Vec::new();

    for shift in 0..6usize {
        let base = Arc::clone(&base);
        handles.push(thread::spawn(move || {
            // rotate sections by `shift`, remapping symbol section indices and
            // relocation targets so the program stays semantically identical.
            let n = base.sections.len();
            let mut sections: Vec<Section> = (0..n)
                .map(|i| base.sections[(i + shift) % n].clone())
                .collect();
            let old_to_new: Vec<usize> = (0..n)
                .map(|old| (old + n - shift) % n)
                .collect();
            let symbols: Vec<Symbol> = base
                .symbols
                .iter()
                .map(|s| Symbol {
                    section: old_to_new[s.section],
                    ..s.clone()
                })
                .collect();
            let old_sym_to_new: Vec<usize> = (0..base.symbols.len()).collect();
            for s in sections.iter_mut() {
                for r in s.relocations.iter_mut() {
                    r.target_symbol = old_sym_to_new[r.target_symbol];
                }
            }
            let input = LinkerInput { sections, symbols };
            let out = layout(&input).unwrap();
            out.placed
                .into_iter()
                .map(|p| (p.representative, p.address, p.bytes))
                .collect::<Vec<_>>()
        }));
    }

    let sets: Vec<HashSet<_>> = handles
        .into_iter()
        .map(|h| h.join().unwrap().into_iter().collect())
        .collect();
    for s in &sets {
        assert_eq!(s, &sets[0], "every permutation must yield the same layout");
    }
}
