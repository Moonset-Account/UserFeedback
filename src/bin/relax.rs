//! A tiny command-line front end for the layout engine.
//!
//! Usage:
//!
//! ```text
//! cargo run --offline --bin relax -- <program.txt>
//! ```
//!
//! The program file contains one directive per line; blank lines and lines
//! beginning with `#` are ignored:
//!
//! ```text
//! bytes 90 90 cc        # any number of hex byte pairs
//! zeros 126             # that many zero bytes
//! label entry
//! align 16
//! jump entry
//! ```
//!
//! The layout is computed for real by [`jump_relaxer::Program::relax`] and
//! printed in full: total size, label addresses, alignment padding, jump form
//! and displacement, plus a final self-consistency check.

use std::process::ExitCode;

use jump_relaxer::{Item, Layout, Program};

fn main() -> ExitCode {
    let mut args = std::env::args_os();
    let program_name = args
        .next()
        .map(|arg| arg.to_string_lossy().into_owned())
        .unwrap_or_else(|| "relax".to_string());
    let Some(path) = args.next() else {
        eprintln!("usage: {program_name} <program.txt>");
        return ExitCode::from(2);
    };
    if args.next().is_some() {
        eprintln!("usage: {program_name} <program.txt>");
        return ExitCode::from(2);
    }

    let source = match std::fs::read_to_string(&path) {
        Ok(source) => source,
        Err(error) => {
            eprintln!("cannot read {}: {error}", path.to_string_lossy());
            return ExitCode::from(1);
        }
    };

    let items = match parse(&source) {
        Ok(items) => items,
        Err(error) => {
            eprintln!("parse error: {error}");
            return ExitCode::from(1);
        }
    };

    let layout = match Program::from_items(items).relax() {
        Ok(layout) => layout,
        Err(error) => {
            eprintln!("layout error: {error}");
            return ExitCode::from(1);
        }
    };

    if let Err(error) = layout.verify() {
        eprintln!("internal consistency check failed: {error}");
        return ExitCode::from(1);
    }

    print_layout(&layout);
    ExitCode::SUCCESS
}

fn parse(source: &str) -> Result<Vec<Item>, String> {
    let mut items = Vec::new();
    for (line_number, raw) in source.lines().enumerate() {
        let line = raw.split('#').next().unwrap_or("").trim();
        if line.is_empty() {
            continue;
        }
        let mut parts = line.split_whitespace();
        let directive = parts.next().unwrap();
        match directive {
            "bytes" => {
                let mut bytes = Vec::new();
                for token in parts {
                    if token.len() != 2 {
                        return Err(format!("line {}: {token:?} is not a hex byte", line_number + 1));
                    }
                    bytes.push(
                        u8::from_str_radix(token, 16)
                            .map_err(|_| format!("line {}: bad hex byte {token:?}", line_number + 1))?,
                    );
                }
                items.push(Item::bytes(bytes));
            }
            "zeros" => {
                let value = parts
                    .next()
                    .ok_or_else(|| format!("line {}: zeros needs a count", line_number + 1))?;
                let count: usize = value
                    .parse()
                    .map_err(|_| format!("line {}: bad count {value:?}", line_number + 1))?;
                if parts.next().is_some() {
                    return Err(format!("line {}: trailing tokens", line_number + 1));
                }
                items.push(Item::bytes(vec![0u8; count]));
            }
            "label" => {
                let name = parts.next().ok_or_else(|| {
                    format!("line {}: label needs a name", line_number + 1)
                })?;
                if parts.next().is_some() {
                    return Err(format!("line {}: trailing tokens", line_number + 1));
                }
                items.push(Item::label(name));
            }
            "align" => {
                let value = parts
                    .next()
                    .ok_or_else(|| format!("line {}: align needs a boundary", line_number + 1))?;
                let boundary: u32 = value
                    .parse()
                    .map_err(|_| format!("line {}: bad boundary {value:?}", line_number + 1))?;
                if parts.next().is_some() {
                    return Err(format!("line {}: trailing tokens", line_number + 1));
                }
                items.push(Item::align(boundary));
            }
            "jump" => {
                let target = parts.next().ok_or_else(|| {
                    format!("line {}: jump needs a target", line_number + 1)
                })?;
                if parts.next().is_some() {
                    return Err(format!("line {}: trailing tokens", line_number + 1));
                }
                items.push(Item::jump(target));
            }
            other => {
                return Err(format!("line {}: unknown directive {other:?}", line_number + 1));
            }
        }
    }
    Ok(items)
}

fn print_layout(layout: &Layout) {
    println!("total size: {} bytes", layout.total_size);

    println!("\nlabels:");
    if layout.labels.is_empty() {
        println!("  (none)");
    }
    for label in &layout.labels {
        println!("  {:<12} 0x{:04x} ({})", label.name, label.address, label.address);
    }

    println!("\nalignment padding:");
    if layout.alignments.is_empty() {
        println!("  (none)");
    }
    for alignment in &layout.alignments {
        println!(
            "  item {:<3} align {:<3} at 0x{:04x}: {} zero byte(s)",
            alignment.item_index, alignment.boundary, alignment.address, alignment.padding
        );
    }

    println!("\njumps:");
    if layout.jumps.is_empty() {
        println!("  (none)");
    }
    for jump in &layout.jumps {
        println!(
            "  item {:<3} {:<5} at 0x{:04x} -> {:<10} displacement {:+}",
            jump.item_index, jump.form, jump.start, jump.target, jump.displacement
        );
    }
}
