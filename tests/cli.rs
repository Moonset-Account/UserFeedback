//! End-to-end tests of the `relax` command-line tool.
//!
//! The binary is run for real (via `CARGO_BIN_EXE_relax`) on the files in
//! `fixtures/`, and its output is checked against the values the library
//! computes for the same input.  Nothing here relies on canned output.

use std::path::PathBuf;
use std::process::Command;

use jump_relaxer::{Item, Program};

fn fixture(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("fixtures")
        .join(name)
}

fn run(name: &str) -> String {
    let output = Command::new(env!("CARGO_BIN_EXE_relax"))
        .arg(fixture(name))
        .output()
        .expect("failed to run relax binary");
    assert!(
        output.status.success(),
        "relax failed on {name}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).expect("stdout is UTF-8")
}

#[test]
fn normal_fixture_layout_is_reported() {
    // Same item stream as fixtures/normal.txt, evaluated through the library.
    let layout = Program::from_items(vec![
        Item::bytes(vec![0x90u8; 8]),
        Item::label("top"),
        Item::align(16),
        Item::jump("bottom"),
        Item::bytes(vec![0xccu8; 2]),
        Item::label("bottom"),
        Item::jump("top"),
    ])
    .relax()
    .unwrap();

    let stdout = run("normal.txt");
    assert!(
        stdout.contains(&format!("total size: {} bytes", layout.total_size)),
        "unexpected output:\n{stdout}"
    );
    for jump in &layout.jumps {
        assert!(
            stdout.contains(&jump.form.to_string()),
            "missing jump form in:\n{stdout}"
        );
        assert!(
            stdout.contains(&format!("displacement {:+}", jump.displacement)),
            "missing displacement {} in:\n{stdout}",
            jump.displacement
        );
    }
    for alignment in &layout.alignments {
        assert!(
            stdout.contains(&format!("{} zero byte(s)", alignment.padding)),
            "missing padding in:\n{stdout}"
        );
    }
    for label in &layout.labels {
        assert!(
            stdout.contains(&format!("{:<12} 0x{:04x}", label.name, label.address)),
            "missing label {} in:\n{stdout}",
            label.name
        );
    }
}

#[test]
fn tie_fixture_prefers_short() {
    let stdout = run("tie.txt");
    assert!(stdout.contains("total size: 10 bytes"), "{stdout}");
    assert!(stdout.contains("6 zero byte(s)"), "{stdout}");
    assert!(stdout.contains("displacement +8"), "{stdout}");
    assert!(stdout.contains("displacement +0"), "{stdout}");
    assert!(!stdout.contains("long"), "{stdout}");
}

#[test]
fn boundary_fixture_uses_both_forms() {
    let layout = Program::from_items(vec![
        Item::jump("fwd"),
        Item::bytes(vec![0u8; 127]),
        Item::label("fwd"),
        Item::bytes(vec![0u8; 40]),
        Item::label("back"),
        Item::bytes(vec![0u8; 126]),
        Item::jump("back"),
        Item::jump("back"),
    ])
    .relax()
    .unwrap();
    let forms: Vec<String> = layout.jumps.iter().map(|j| j.form.to_string()).collect();
    assert_eq!(forms, vec!["short", "short", "long"]);
    assert_eq!(layout.jumps[0].displacement, 127);
    assert_eq!(layout.jumps[1].displacement, -128);
    assert_eq!(layout.jumps[2].displacement, -133);

    let stdout = run("boundary.txt");
    assert!(stdout.contains("displacement +127"), "{stdout}");
    assert!(stdout.contains("displacement -128"), "{stdout}");
    assert!(stdout.contains("displacement -133"), "{stdout}");
    assert!(stdout.contains(&format!("total size: {} bytes", layout.total_size)), "{stdout}");
}

#[test]
fn missing_file_is_a_clean_error() {
    let output = Command::new(env!("CARGO_BIN_EXE_relax"))
        .arg(fixture("does-not-exist.txt"))
        .output()
        .expect("failed to run relax binary");
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("cannot read"), "stderr: {stderr}");
    assert!(output.stdout.is_empty());
}

#[test]
fn bad_alignment_is_a_clean_error() {
    let dir = std::env::temp_dir();
    let path = dir.join(format!("jump-relaxer-bad-align-{}.txt", std::process::id()));
    std::fs::write(&path, "bytes 90\nalign 3\n").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_relax"))
        .arg(&path)
        .output()
        .expect("failed to run relax binary");
    let _ = std::fs::remove_file(&path);
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("not a power of two"), "stderr: {stderr}");
}
