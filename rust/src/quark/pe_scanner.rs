// PE Import Scanner — reads a Windows executable's import table to determine
// what graphics API and runtime DLLs it needs. Positional reads only; the
// file is never loaded whole.

use std::fs::File;
use std::os::unix::fs::FileExt;
use std::path::Path;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum RenderApi {
    Vulkan,      // imports vulkan-1.dll
    DirectX12,   // imports d3d12.dll
    DirectX11,   // imports d3d11.dll or d3d10*.dll
    DirectX9,    // imports d3d9.dll
    DirectX8,    // imports d3d8.dll or ddraw.dll
    OpenGL,      // imports opengl32.dll
    None,        // no graphics imports detected
}

#[derive(Debug)]
pub struct PeScanResult {
    pub render_api: RenderApi,
    pub imports: Vec<String>,  // all imported DLL names (lowercase)
    pub needs_steam_api: bool, // imports steam_api64.dll or steam_api.dll
    pub needs_nvapi: bool,     // imports nvapi64.dll or nvapi.dll
}

fn u16_at(b: &[u8], o: usize) -> u16 {
    u16::from_le_bytes([b[o], b[o + 1]])
}

fn u32_at(b: &[u8], o: usize) -> u32 {
    u32::from_le_bytes([b[o], b[o + 1], b[o + 2], b[o + 3]])
}

pub fn scan_pe(exe_path: &Path) -> Option<PeScanResult> {
    let file = File::open(exe_path).ok()?;

    // DOS header (64 bytes)
    let mut dos = [0u8; 64];
    file.read_exact_at(&mut dos, 0).ok()?;
    if &dos[..2] != b"MZ" {
        return None;
    }

    // e_lfanew at offset 0x3C
    let pe_offset = u64::from(u32_at(&dos, 0x3C));

    // PE signature + COFF header + optional header start
    let mut pe_buf = [0u8; 256];
    let n = file.read_at(&mut pe_buf, pe_offset).ok()?;
    if n < 120 || &pe_buf[..4] != b"PE\0\0" {
        return None;
    }

    // COFF header starts at pe_offset+4
    let num_sections = usize::from(u16_at(&pe_buf, 6));
    let optional_hdr_size = u64::from(u16_at(&pe_buf, 20));

    // Optional header starts at pe_offset+24
    let is_pe32plus = u16_at(&pe_buf, 24) == 0x20B; // PE32+ (64-bit)

    // Import directory RVA, bounded by the bytes actually read
    let import_dir_offset = if is_pe32plus { 24 + 120 } else { 24 + 104 };
    if import_dir_offset + 8 > n {
        return None;
    }
    let import_rva = u32_at(&pe_buf, import_dir_offset);

    if import_rva == 0 {
        return Some(PeScanResult {
            render_api: RenderApi::None,
            imports: Vec::new(),
            needs_steam_api: false,
            needs_nvapi: false,
        });
    }

    // Section headers resolve RVA → file offset
    let mut sections_buf = vec![0u8; num_sections * 40];
    file.read_exact_at(&mut sections_buf, pe_offset + 24 + optional_hdr_size).ok()?;

    let rva_to_offset = |rva: u32| -> Option<u64> {
        let rva = u64::from(rva);
        sections_buf.chunks_exact(40).find_map(|s| {
            let virt_size = u64::from(u32_at(s, 8));
            let virt_addr = u64::from(u32_at(s, 12));
            let raw_offset = u64::from(u32_at(s, 20));
            (rva >= virt_addr && rva < virt_addr + virt_size)
                .then(|| rva - virt_addr + raw_offset)
        })
    };

    // Import directory entries (20 bytes each, null-terminated array)
    let mut imports = Vec::new();
    if let Some(import_file_offset) = rva_to_offset(import_rva) {
        let mut entry_buf = [0u8; 20];
        for idx in 0..1000u64 {
            if file.read_exact_at(&mut entry_buf, import_file_offset + idx * 20).is_err() {
                break;
            }
            if entry_buf.iter().all(|&b| b == 0) {
                break;
            }

            // Name RVA is at offset 12 in the import descriptor
            if let Some(name_offset) = rva_to_offset(u32_at(&entry_buf, 12)) {
                let mut name_buf = [0u8; 128];
                if let Ok(n) = file.read_at(&mut name_buf, name_offset) {
                    let end = name_buf[..n].iter().position(|&b| b == 0).unwrap_or(n);
                    if let Ok(name) = std::str::from_utf8(&name_buf[..end]) {
                        imports.push(name.to_lowercase());
                    }
                }
            }
        }
    }

    // Classify render API by import priority (most specific first)
    let render_api = if imports.iter().any(|s| s == "vulkan-1.dll") {
        RenderApi::Vulkan
    } else if imports.iter().any(|s| s == "d3d12.dll" || s == "d3d12core.dll") {
        RenderApi::DirectX12
    } else if imports.iter().any(|s| s == "d3d11.dll" || s == "d3d10.dll" || s == "d3d10core.dll" || s == "d3d10_1.dll") {
        RenderApi::DirectX11
    } else if imports.iter().any(|s| s == "d3d9.dll") {
        RenderApi::DirectX9
    } else if imports.iter().any(|s| s == "d3d8.dll" || s == "ddraw.dll") {
        RenderApi::DirectX8
    } else if imports.iter().any(|s| s == "opengl32.dll") {
        RenderApi::OpenGL
    } else {
        RenderApi::None
    };

    let needs_steam_api = imports.iter().any(|s| s == "steam_api64.dll" || s == "steam_api.dll");
    let needs_nvapi = imports.iter().any(|s| s == "nvapi64.dll" || s == "nvapi.dll");

    Some(PeScanResult {
        render_api,
        imports,
        needs_steam_api,
        needs_nvapi,
    })
}
