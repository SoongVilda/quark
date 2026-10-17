// pe_scanner against synthetic PE32+ images: import extraction, render-API
// classification and the failure paths (truncated, non-PE, missing).

#[path = "../../rust/src/quark/pe_scanner.rs"]
mod pe_scanner;

use pe_scanner::*;
use std::path::{Path, PathBuf};

const PE_OFFSET: usize = 0x40;
const OPT_HDR_SIZE: usize = 0xF0; // PE32+
const SECTION_VA: u32 = 0x1000;
const SECTION_RAW: usize = 0x400;

fn put_u16(b: &mut [u8], o: usize, v: u16) {
    b[o..o + 2].copy_from_slice(&v.to_le_bytes());
}

fn put_u32(b: &mut [u8], o: usize, v: u32) {
    b[o..o + 4].copy_from_slice(&v.to_le_bytes());
}

/// A minimal PE32+ with one section holding an import directory that names
/// `dlls`. With `dlls` empty the import directory RVA is zero.
fn pe_with_imports(dlls: &[&str]) -> Vec<u8> {
    let mut b = vec![0u8; SECTION_RAW + 0x400];
    b[..2].copy_from_slice(b"MZ");
    put_u32(&mut b, 0x3C, PE_OFFSET as u32);

    let pe = PE_OFFSET;
    b[pe..pe + 4].copy_from_slice(b"PE\0\0");
    put_u16(&mut b, pe + 6, 1); // one section
    put_u16(&mut b, pe + 20, OPT_HDR_SIZE as u16);
    put_u16(&mut b, pe + 24, 0x20B); // PE32+ magic
    if !dlls.is_empty() {
        put_u32(&mut b, pe + 24 + 120, SECTION_VA); // import directory RVA
    }

    let sec = pe + 24 + OPT_HDR_SIZE;
    put_u32(&mut b, sec + 8, 0x400); // virtual size
    put_u32(&mut b, sec + 12, SECTION_VA);
    put_u32(&mut b, sec + 20, SECTION_RAW as u32);

    // Descriptors first (20 bytes each, zero terminator), names after.
    let names_rva = SECTION_VA + 0x100;
    let mut name_off = 0usize;
    for (i, dll) in dlls.iter().enumerate() {
        put_u32(&mut b, SECTION_RAW + i * 20 + 12, names_rva + name_off as u32);
        let at = SECTION_RAW + 0x100 + name_off;
        b[at..at + dll.len()].copy_from_slice(dll.as_bytes());
        name_off += dll.len() + 1;
    }
    b
}

fn write_temp(name: &str, bytes: &[u8]) -> PathBuf {
    let path = std::env::temp_dir().join(format!("quark-pe-test-{}-{name}", std::process::id()));
    std::fs::write(&path, bytes).unwrap();
    path
}

fn scan(name: &str, bytes: &[u8]) -> Option<PeScanResult> {
    let path = write_temp(name, bytes);
    let result = scan_pe(&path);
    std::fs::remove_file(&path).unwrap();
    result
}

#[test]
fn reads_imports_and_classifies() {
    let r = scan("d3d11", &pe_with_imports(&["D3D11.dll", "steam_api64.dll", "KERNEL32.dll"])).unwrap();
    assert_eq!(r.imports, ["d3d11.dll", "steam_api64.dll", "kernel32.dll"]);
    assert_eq!(r.render_api, RenderApi::DirectX11);
    assert!(r.needs_steam_api);
    assert!(!r.needs_nvapi);
}

#[test]
fn most_specific_render_api_wins() {
    let r = scan("mixed", &pe_with_imports(&["opengl32.dll", "d3d9.dll", "vulkan-1.dll"])).unwrap();
    assert_eq!(r.render_api, RenderApi::Vulkan);
}

#[test]
fn nvapi_detected() {
    let r = scan("nvapi", &pe_with_imports(&["nvapi64.dll"])).unwrap();
    assert!(r.needs_nvapi);
    assert_eq!(r.render_api, RenderApi::None);
}

#[test]
fn no_import_directory_is_empty_scan() {
    let r = scan("noimports", &pe_with_imports(&[])).unwrap();
    assert!(r.imports.is_empty());
    assert_eq!(r.render_api, RenderApi::None);
}

#[test]
fn rejects_non_pe() {
    assert!(scan("text", b"#!/bin/sh\necho not a PE\n").is_none());
    let mut bad_sig = pe_with_imports(&["d3d11.dll"]);
    bad_sig[PE_OFFSET] = b'X';
    assert!(scan("badsig", &bad_sig).is_none());
}

#[test]
fn truncated_headers_fail_instead_of_reading_zeroes() {
    let full = pe_with_imports(&["d3d11.dll"]);
    // Cut inside the section table: the header read must fail the scan.
    let cut = PE_OFFSET + 24 + OPT_HDR_SIZE + 10;
    assert!(scan("truncated", &full[..cut]).is_none());
    // Cut before the import directory field: bounded by bytes read.
    assert!(scan("short", &full[..PE_OFFSET + 130]).is_none());
}

#[test]
fn missing_file_is_none() {
    assert!(scan_pe(Path::new("/nonexistent/quark/game.exe")).is_none());
}
