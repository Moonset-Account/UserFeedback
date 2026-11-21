//! Integration tests for the public API.
//!
//! The tests are grouped by the scenario families named in the README:
//! normal, boundary, interleaved, and error handling.  A separate reference
//! model below re-derives every layout directly from the definition of the
//! problem (enumerate all short/long assignments, pick minimum total size,
//! break ties lexicographically in input order) so that the property tests
//! compare two independent implementations rather than one implementation
//! against itself.

use jump_relaxer::{
    Item, JumpForm, Layout, LayoutError, PlacementKind, Program, LONG_JUMP_SIZE, MAX_JUMPS,
    SHORT_JUMP_SIZE, SHORT_MAX, SHORT_MIN,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn layout(items: Vec<Item>) -> Layout {
    let result = Program::from_items(items).relax().expect("layout should succeed");
    result.verify().expect("layout should be self-consistent");
    result
}

fn jump_forms(layout: &Layout) -> Vec<JumpForm> {
    layout.jumps.iter().map(|j| j.form).collect()
}

fn form(c: char) -> JumpForm {
    match c {
        's' => JumpForm::Short,
        'l' => JumpForm::Long,
        other => panic!("unknown form {other:?}"),
    }
}

fn expected_forms(spec: &str) -> Vec<JumpForm> {
    spec.split_whitespace().map(|token| form(token.chars().next().unwrap())).collect()
}

// ---------------------------------------------------------------------------
// Reference model (independent of the crate implementation)
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
enum RefItem {
    Bytes(u64),
    Label(usize),
    Align(u64),
    Jump(usize),
}

#[derive(Clone, Debug)]
struct RefLayout {
    total: u64,
    label_addresses: Vec<u64>,
    forms: Vec<bool>, // true = long
    displacements: Vec<i64>,
    padding: Vec<u64>,
    jump_starts: Vec<u64>,
}

fn ref_walk(
    items: &[RefItem],
    long_mask: u32,
    label_count: usize,
) -> (Vec<u64>, Vec<u64>, Vec<u64>, u64) {
    let mut pos: u64 = 0;
    let mut starts = Vec::with_capacity(items.len());
    let mut padding = Vec::with_capacity(items.len());
    let mut labels = vec![0u64; label_count];
    let mut jump_ordinal = 0usize;
    for item in items {
        starts.push(pos);
        match *item {
            RefItem::Bytes(n) => {
                padding.push(0);
                pos += n;
            }
            RefItem::Label(k) => {
                padding.push(0);
                labels[k] = pos;
            }
            RefItem::Align(b) => {
                let pad = (b - pos % b) % b;
                padding.push(pad);
                pos += pad;
            }
            RefItem::Jump(_) => {
                let size = if (long_mask >> jump_ordinal) & 1 == 1 {
                    LONG_JUMP_SIZE
                } else {
                    SHORT_JUMP_SIZE
                };
                padding.push(0);
                pos += size;
                jump_ordinal += 1;
            }
        }
    }
    (starts, labels, padding, pos)
}

fn ref_relax(items: &[RefItem], label_count: usize) -> Option<RefLayout> {
    let jumps: Vec<(usize, usize)> = items
        .iter()
        .enumerate()
        .filter_map(|(i, item)| match item {
            RefItem::Jump(k) => Some((i, *k)),
            _ => None,
        })
        .collect();
    let mut best: Option<(u64, Vec<bool>, RefLayout)> = None;
    for mask in 0u32..(1u32 << jumps.len()) {
        let (starts, labels, padding, total) = ref_walk(items, mask, label_count);
        let mut displacements = Vec::new();
        let mut forms = Vec::new();
        let mut legal = true;
        for (ordinal, (item_index, target)) in jumps.iter().enumerate() {
            let long = (mask >> ordinal) & 1 == 1;
            let size = if long { LONG_JUMP_SIZE } else { SHORT_JUMP_SIZE };
            let displacement = labels[*target] as i64 - (starts[*item_index] as i64 + size as i64);
            if long {
                if displacement < i32::MIN as i64 || displacement > i32::MAX as i64 {
                    legal = false;
                }
            } else if displacement < SHORT_MIN || displacement > SHORT_MAX {
                legal = false;
            }
            forms.push(long);
            displacements.push(displacement);
        }
        if !legal {
            continue;
        }
        let padding: Vec<u64> = items
            .iter()
            .zip(padding)
            .filter(|(item, _)| matches!(item, RefItem::Align(_)))
            .map(|(_, pad)| pad)
            .collect();
        let candidate = RefLayout {
            total,
            label_addresses: labels,
            forms: forms.clone(),
            displacements,
            padding,
            jump_starts: jumps.iter().map(|(i, _)| starts[*i]).collect(),
        };
        let replace = match &best {
            None => true,
            Some((best_total, best_forms, _)) => {
                if total < *best_total {
                    true
                } else if total == *best_total {
                    // Lexicographic in input order: short (false) wins.
                    candidate.forms < *best_forms
                } else {
                    false
                }
            }
        };
        if replace {
            best = Some((total, forms, candidate));
        }
    }
    best.map(|(_, _, layout)| layout)
}

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }

    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

// ---------------------------------------------------------------------------
// Normal scenarios
// ---------------------------------------------------------------------------

#[test]
fn bytes_labels_and_align_are_placed() {
    let program = layout(vec![
        Item::bytes(vec![0xaa; 3]),
        Item::label("start"),
        Item::align(8),
        Item::bytes(vec![0xbb; 2]),
        Item::label("end"),
    ]);

    assert_eq!(program.total_size, 10);
    assert_eq!(program.label_address("start"), Some(3));
    assert_eq!(program.label_address("end"), Some(10));
    assert_eq!(program.alignments.len(), 1);
    assert_eq!(program.alignments[0].address, 3);
    assert_eq!(program.alignments[0].padding, 5);
    assert!(program.jumps.is_empty());
}

#[test]
fn already_aligned_site_adds_no_padding() {
    let program = layout(vec![
        Item::bytes(vec![0; 16]),
        Item::align(16),
        Item::label("here"),
        Item::align(1),
        Item::bytes(vec![0; 1]),
    ]);
    assert_eq!(program.alignments[0].padding, 0);
    assert_eq!(program.alignments[1].padding, 0);
    assert_eq!(program.label_address("here"), Some(16));
    assert_eq!(program.total_size, 17);
}

#[test]
fn short_forward_and_backward_jumps() {
    let program = layout(vec![
        Item::label("top"),
        Item::bytes(vec![0; 10]),
        Item::jump("bottom"),
        Item::bytes(vec![0; 4]),
        Item::label("bottom"),
        Item::jump("top"),
    ]);
    assert_eq!(jump_forms(&program), expected_forms("s s"));
    // Forward jump at 10, end 12, target 16 -> +4.
    assert_eq!(program.jumps[0].displacement, 4);
    // Backward jump at 16? No: after label bottom at 16, jump at 16, end 18,
    // target 0 -> -18.
    assert_eq!(program.jumps[1].start, 16);
    assert_eq!(program.jumps[1].displacement, -18);
    assert_eq!(program.total_size, 18);
}

#[test]
fn long_jump_used_when_short_cannot_reach() {
    let program = layout(vec![
        Item::jump("far"),
        Item::bytes(vec![0; 200]),
        Item::label("far"),
    ]);
    assert_eq!(jump_forms(&program), expected_forms("l"));
    assert_eq!(program.jumps[0].start, 0);
    assert_eq!(program.jumps[0].displacement, 200);
    assert_eq!(program.total_size, 205);
}

#[test]
fn mixed_short_and_long_jumps() {
    // Two forward branches: the near one fits, the far one does not.
    let program = layout(vec![
        Item::jump("near"),
        Item::jump("far"),
        Item::bytes(vec![0; 10]),
        Item::label("near"),
        Item::bytes(vec![0; 200]),
        Item::label("far"),
    ]);
    assert_eq!(jump_forms(&program), expected_forms("s l"));
    // jump near @0 short (0..2), jump far @2 long (2..7), 10 bytes -> near
    // label at 17, 200 bytes -> far label at 217.
    assert_eq!(program.jumps[0].displacement, 15);
    assert_eq!(program.jumps[1].start, 2);
    assert_eq!(program.jumps[1].displacement, 210);
    assert_eq!(program.total_size, 217);
}

#[test]
fn self_reference_and_zero_displacement() {
    let program = layout(vec![Item::jump("here"), Item::label("here")]);
    assert_eq!(jump_forms(&program), expected_forms("s"));
    assert_eq!(program.jumps[0].displacement, 0);
    assert_eq!(program.total_size, 2);
}

// ---------------------------------------------------------------------------
// Boundary scenarios
// ---------------------------------------------------------------------------

#[test]
fn short_displacement_upper_bound_is_inclusive() {
    for (distance, want) in [(126u64, "s"), (127, "s"), (128, "l")] {
        let program = layout(vec![
            Item::jump("t"),
            Item::bytes(vec![0; distance as usize]),
            Item::label("t"),
        ]);
        assert_eq!(
            program.jumps[0].displacement,
            distance as i64,
            "distance {distance}"
        );
        assert_eq!(jump_forms(&program), expected_forms(want), "distance {distance}");
        assert_eq!(program.total_size, distance + program.jumps[0].form.size());
    }
}

#[test]
fn short_displacement_lower_bound_is_inclusive() {
    // Label first, then bytes, then the jump back to it.
    // Short displacement is -(bytes + 2).
    for (bytes, want, disp) in [(126u64, "s", -128i64), (127, "l", -132)] {
        let program = layout(vec![
            Item::label("t"),
            Item::bytes(vec![0; bytes as usize]),
            Item::jump("t"),
        ]);
        assert_eq!(program.jumps[0].displacement, disp, "bytes {bytes}");
        assert_eq!(jump_forms(&program), expected_forms(want), "bytes {bytes}");
        assert_eq!(program.total_size, bytes + program.jumps[0].form.size());
    }
}

#[test]
fn zero_length_bytes_and_empty_program() {
    let empty = layout(vec![]);
    assert_eq!(empty.total_size, 0);
    assert!(empty.placements.is_empty());

    let zeros = layout(vec![Item::bytes(Vec::new()), Item::label("x"), Item::bytes(vec![1])]);
    assert_eq!(zeros.total_size, 1);
    assert_eq!(zeros.label_address("x"), Some(0));
}

#[test]
fn alignment_boundary_one_is_a_no_op() {
    let program = layout(vec![Item::bytes(vec![0; 5]), Item::align(1), Item::label("x")]);
    assert_eq!(program.alignments[0].padding, 0);
    assert_eq!(program.label_address("x"), Some(5));
}

#[test]
fn maximum_jump_count_is_accepted() {
    let mut items = vec![Item::label("t")];
    for _ in 0..MAX_JUMPS {
        items.push(Item::jump("t"));
    }
    let program = layout(items);
    assert_eq!(program.jumps.len(), MAX_JUMPS);
    assert!(jump_forms(&program).iter().all(|f| *f == JumpForm::Short));
    // Each short jump is 2 bytes, all backward to address 0.
    for (ordinal, jump) in program.jumps.iter().enumerate() {
        let start = 2 * ordinal as u64;
        assert_eq!(jump.start, start);
        assert_eq!(jump.displacement, -(start as i64 + 2));
    }
    assert_eq!(program.total_size, 2 * MAX_JUMPS as u64);
}

#[test]
fn alignment_that_absorbs_the_saving_produces_a_tie() {
    // With both jumps short: jump0 at 0..2, align 8 pads 6 (to 8), jump1
    // 8..10, label at 10.  Total 10.
    // With jump0 long: jump0 0..5, align 8 pads 3 (to 8), jump1 8..10,
    // label at 10.  Total 10 as well: the three saved bytes are entirely
    // swallowed by the alignment.  The tie-break must still pick short.
    let program = layout(vec![
        Item::jump("t"),
        Item::align(8),
        Item::jump("t"),
        Item::label("t"),
    ]);
    assert_eq!(program.total_size, 10);
    assert_eq!(program.alignments[0].padding, 6);
    assert_eq!(jump_forms(&program), expected_forms("s s"));
    assert_eq!(program.jumps[0].displacement, 8);
    assert_eq!(program.jumps[1].displacement, 0);

    // The long variant is genuinely legal and the same size, so the choice
    // really is made by the tie-break.
    let reference = ref_relax(
        &[
            RefItem::Jump(0),
            RefItem::Align(8),
            RefItem::Jump(0),
            RefItem::Label(0),
        ],
        1,
    )
    .unwrap();
    assert_eq!(reference.total, program.total_size);
    assert_eq!(reference.forms, vec![false, false]);
}

#[test]
fn tie_is_broken_towards_the_earliest_jump_being_short() {
    // Both jumps short: 0..2, align 8 pads 6 (to 8), 8..10, label at 10.
    // Jump0 long, jump1 short: 0..5, align pads 3 (to 8), 8..10, label at
    // 10.  Same total of 10, so the tie-break decides and the earlier jump
    // must be the short one.
    // Jump0 short, jump1 long: 0..2, align pads 6 (to 8), 8..13, label at
    // 13.  Jump0 long, jump1 long: 0..5, align pads 3 (to 8), 8..13.  Both
    // are 13 and strictly worse.
    let items = vec![
        Item::jump("t"),
        Item::align(8),
        Item::jump("t"),
        Item::label("t"),
    ];
    let program = layout(items);

    let ref_items = [
        RefItem::Jump(0),
        RefItem::Align(8),
        RefItem::Jump(0),
        RefItem::Label(0),
    ];
    let mut totals = Vec::new();
    for mask in 0u32..4 {
        let (starts, labels, _, total) = ref_walk(&ref_items, mask, 1);
        let d0 = labels[0] as i64 - (starts[0] as i64 + if mask & 1 == 1 { 5 } else { 2 });
        let d1 = labels[0] as i64 - (starts[2] as i64 + if mask & 2 == 2 { 5 } else { 2 });
        assert!((-128..=127).contains(&d0) || mask & 1 == 1);
        assert!((-128..=127).contains(&d1) || mask & 2 == 2);
        totals.push((mask, total));
    }
    assert_eq!(totals, vec![(0, 10), (1, 10), (2, 13), (3, 13)]);

    assert_eq!(program.total_size, 10);
    assert_eq!(program.alignments[0].padding, 6);
    assert_eq!(jump_forms(&program), expected_forms("s s"));
    assert_eq!(program.jumps[0].displacement, 8);
    assert_eq!(program.jumps[1].displacement, 0);
}

// ---------------------------------------------------------------------------
// Interleaved scenarios
// ---------------------------------------------------------------------------

#[test]
fn alignments_between_jumps_interleave_with_relaxation() {
    // Three jumps, two alignments; the middle jump's shortening shifts the
    // later alignment and therefore the reach of the last jump.
    let program = layout(vec![
        Item::jump("a"),
        Item::align(4),
        Item::jump("b"),
        Item::bytes(vec![0; 60]),
        Item::align(64),
        Item::jump("c"),
        Item::label("a"),
        Item::bytes(vec![0; 3]),
        Item::label("b"),
        Item::bytes(vec![0; 3]),
        Item::label("c"),
    ]);
    program.verify().unwrap();
    assert_eq!(program.jumps.len(), 3);
    assert_eq!(program.alignments.len(), 2);
    // Every reported displacement re-decodes to its label from the final
    // layout.
    for jump in &program.jumps {
        assert_eq!(
            jump.start as i128 + jump.form.size() as i128 + jump.displacement as i128,
            jump.target_address as i128
        );
        assert_eq!(program.label_address(&jump.target), Some(jump.target_address));
    }
}

#[test]
fn labels_at_alignment_boundaries_report_final_addresses() {
    let program = layout(vec![
        Item::label("before"),
        Item::align(8),
        Item::label("after"),
        Item::jump("before"),
        Item::label("tail"),
    ]);
    assert_eq!(program.label_address("before"), Some(0));
    assert_eq!(program.label_address("after"), Some(0));
    // Jump at address 0, short size 2, target 0 -> -2.
    assert_eq!(program.jumps[0].start, 0);
    assert_eq!(program.jumps[0].displacement, -2);
    assert_eq!(program.total_size, 2);
}

#[test]
fn every_placement_tiles_the_fragment() {
    let program = layout(vec![
        Item::bytes(vec![0; 7]),
        Item::label("a"),
        Item::align(16),
        Item::jump("b"),
        Item::bytes(vec![0; 300]),
        Item::label("b"),
        Item::align(4),
    ]);
    let mut address = 0u64;
    for (index, placement) in program.placements.iter().enumerate() {
        assert_eq!(placement.item_index, index);
        assert_eq!(placement.address, address);
        address += placement.size;
        match placement.kind {
            PlacementKind::Label => assert_eq!(placement.size, 0),
            PlacementKind::Align { boundary } => {
                assert!(boundary.is_power_of_two());
            }
            PlacementKind::Jump { form } => assert_eq!(placement.size, form.size()),
            PlacementKind::Bytes => {}
        }
    }
    assert_eq!(address, program.total_size);
}

// ---------------------------------------------------------------------------
// Error handling
// ---------------------------------------------------------------------------

fn expect_error(items: Vec<Item>) -> LayoutError {
    Program::from_items(items).relax().unwrap_err()
}

#[test]
fn duplicate_labels_are_rejected() {
    assert_eq!(
        expect_error(vec![Item::label("x"), Item::label("x")]),
        LayoutError::DuplicateLabel { name: "x".into() }
    );
}

#[test]
fn undefined_targets_are_rejected() {
    assert_eq!(
        expect_error(vec![Item::jump("missing")]),
        LayoutError::UndefinedLabel { name: "missing".into() }
    );
}

#[test]
fn empty_label_names_are_rejected() {
    assert_eq!(
        expect_error(vec![Item::label("")]),
        LayoutError::EmptyLabelName { item_index: 0 }
    );
}

#[test]
fn non_power_of_two_alignment_is_rejected() {
    for boundary in [0u32, 3, 5, 6, 12] {
        assert_eq!(
            expect_error(vec![Item::align(boundary)]),
            LayoutError::InvalidAlignment {
                item_index: 0,
                boundary
            },
            "boundary {boundary}"
        );
    }
}

#[test]
fn too_many_jumps_are_rejected() {
    let mut items = vec![Item::label("t")];
    for _ in 0..=MAX_JUMPS {
        items.push(Item::jump("t"));
    }
    assert_eq!(
        expect_error(items),
        LayoutError::TooManyJumps {
            count: MAX_JUMPS + 1
        }
    );
}

#[test]
fn long_jump_out_of_range_is_reported() {
    // A 2 GiB alignment pushes the fragment past the range of a signed 32-bit
    // displacement, so even the long form cannot reach the label at address
    // 0.  No large allocation is needed: the gap comes from the alignment.
    let error = expect_error(vec![
        Item::label("t"),
        Item::bytes(vec![0]),
        Item::align(1 << 31),
        Item::jump("t"),
    ]);
    assert_eq!(
        error,
        LayoutError::LongJumpOutOfRange {
            item_index: 3,
            displacement: -2_147_483_648 - 5,
        }
    );
    assert!(error.to_string().contains("does not fit in i32"));
}

#[test]
fn builder_api_matches_from_items() {
    let mut built = Program::new();
    built
        .bytes(vec![0u8; 4])
        .label("t")
        .align(8)
        .jump("t");
    let direct = Program::from_items(vec![
        Item::bytes(vec![0u8; 4]),
        Item::label("t"),
        Item::align(8),
        Item::jump("t"),
    ]);
    assert_eq!(built, direct);
    assert_eq!(built.relax().unwrap(), direct.relax().unwrap());
}

#[test]
fn verify_detects_tampered_displacement() {
    let mut program = layout(vec![
        Item::jump("t"),
        Item::bytes(vec![0; 4]),
        Item::label("t"),
    ]);
    program.jumps[0].displacement += 1;
    assert!(matches!(
        program.verify(),
        Err(LayoutError::InconsistentLayout { .. })
    ));
}

#[test]
fn verify_detects_tampered_padding() {
    let mut program = layout(vec![Item::bytes(vec![0; 3]), Item::align(8)]);
    program.alignments[0].padding = 0;
    assert!(matches!(
        program.verify(),
        Err(LayoutError::InconsistentLayout { .. })
    ));
}

// ---------------------------------------------------------------------------
// Property tests against the independent reference model
// ---------------------------------------------------------------------------

fn random_program(rng: &mut Rng) -> (Vec<Item>, Vec<RefItem>, usize) {
    let label_count = 1 + rng.below(4) as usize;
    let jump_count = rng.below(5) as usize;
    let mut kinds: Vec<u8> = Vec::new();
    // Always define every label somewhere, then scatter jumps.
    for _ in 0..label_count {
        kinds.push(1); // label
    }
    for _ in 0..jump_count {
        kinds.push(2); // jump
    }
    for _ in 0..(1 + rng.below(8)) {
        kinds.push(0); // bytes
    }
    for _ in 0..rng.below(4) {
        kinds.push(3); // align
    }
    // Fisher-Yates shuffle.
    for i in (1..kinds.len()).rev() {
        let j = rng.below(i as u64 + 1) as usize;
        kinds.swap(i, j);
    }

    let mut items = Vec::new();
    let mut ref_items = Vec::new();
    for kind in kinds {
        match kind {
            0 => {
                let len = rng.below(90) as usize;
                items.push(Item::bytes(vec![0x5a; len]));
                ref_items.push(RefItem::Bytes(len as u64));
            }
            1 => {
                // Unique label index per item keeps names distinct.
                let index = ref_items
                    .iter()
                    .filter(|item| matches!(item, RefItem::Label(_)))
                    .count();
                items.push(Item::label(format!("L{index}")));
                ref_items.push(RefItem::Label(index));
            }
            2 => {
                let target = rng.below(label_count as u64) as usize;
                items.push(Item::jump(format!("L{target}")));
                ref_items.push(RefItem::Jump(target));
            }
            3 => {
                let boundary = [2u64, 4, 8, 16, 32, 64][rng.below(6) as usize];
                items.push(Item::align(boundary as u32));
                ref_items.push(RefItem::Align(boundary));
            }
            _ => unreachable!(),
        }
    }
    (items, ref_items, label_count)
}

#[test]
fn matches_reference_model_on_random_programs() {
    let mut rng = Rng(0x9e37_79b9_7f4a_7c15);
    let mut checked = 0u32;
    for _ in 0..40_000 {
        let (items, ref_items, label_count) = random_program(&mut rng);
        let program = Program::from_items(items);
        let actual = match program.relax() {
            Ok(layout) => layout,
            Err(err) => panic!("unexpected error {err} for {:?}", program.items()),
        };
        actual.verify().unwrap();
        let expected = ref_relax(&ref_items, label_count).expect("reference always finds a layout");

        assert_eq!(actual.total_size, expected.total, "items {:?}", program.items());
        assert_eq!(
            jump_forms(&actual),
            expected
                .forms
                .iter()
                .map(|&long| if long { JumpForm::Long } else { JumpForm::Short })
                .collect::<Vec<_>>(),
            "forms for {:?}",
            program.items()
        );
        let actual_displacements: Vec<i64> = actual.jumps.iter().map(|j| j.displacement).collect();
        assert_eq!(actual_displacements, expected.displacements, "displacements");
        let actual_starts: Vec<u64> = actual.jumps.iter().map(|j| j.start).collect();
        assert_eq!(actual_starts, expected.jump_starts, "jump starts");
        let actual_padding: Vec<u64> = actual.alignments.iter().map(|a| a.padding).collect();
        assert_eq!(actual_padding, expected.padding, "padding");
        let actual_labels: Vec<u64> = actual.labels.iter().map(|l| l.address).collect();
        assert_eq!(actual_labels, expected.label_addresses, "labels");
        checked += 1;
    }
    assert_eq!(checked, 40_000);
}

#[test]
fn relaxation_is_deterministic() {
    let items = vec![
        Item::jump("a"),
        Item::align(8),
        Item::jump("b"),
        Item::bytes(vec![0; 200]),
        Item::label("a"),
        Item::label("b"),
    ];
    let first = Program::from_items(items.clone()).relax().unwrap();
    let second = Program::from_items(items).relax().unwrap();
    assert_eq!(first, second);
}
