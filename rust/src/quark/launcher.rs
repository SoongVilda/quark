// quark launcher -- full Proton replacement.
//
// Steam calls: ./proton <verb> <exe> [args...]
// quark sets up the prefix, deploys DXVK/VKD3D, bridges Steam client,
// then launches wine.
//
// Optimization: after first launch, the .quark_deployed cache records
// what was deployed and from where. Subsequent launches skip all file ops
// when the cache is valid — straight to wine64.

use crate::{log_info, log_warn, log_error, log_verbose};

use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Instant;

fn copy_dir_recursive(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let ty = entry.file_type()?;
        let src_path = entry.path();
        let dst_path = dst.join(entry.file_name());
        if ty.is_dir() {
            copy_dir_recursive(&src_path, &dst_path)?;
        } else if ty.is_symlink() {
            let target = std::fs::read_link(&src_path)?;
            let resolved = src_path.parent().map(|p| p.join(&target));
            if let Some(r) = resolved {
                if r.exists() {
                    std::fs::copy(&r, &dst_path)?;
                    continue;
                }
            }
        } else {
            std::fs::copy(&src_path, &dst_path)?;
        }
    }
    Ok(())
}

/// The wineserver that matches `wine_dir`'s ntdll. A Wine tree's own
/// bin/wineserver speaks its protocol version; anything else may not.
pub(crate) fn wineserver_exe(wine_dir: &Path) -> PathBuf {
    let own = wine_dir.join("bin/wineserver");
    if own.exists() { own } else { PathBuf::from("/usr/bin/wineserver") }
}

/// Ensure fds 0, 1, 2 are open. Wine's socketpair() picks the lowest free fd.
/// Under Steam's reaper, stdin may be closed → fd 0 is free → socketpair gets
/// fd 0 → child's set_stdio_fd overwrites it with /dev/null → WINESERVERSOCKET
/// points to /dev/null instead of the socket → child crashes.
fn ensure_stdio_fds() {
    for fd in 0..3 {
        unsafe {
            if libc::fcntl(fd, libc::F_GETFD) == -1 {
                libc::open(c"/dev/null".as_ptr(), libc::O_RDWR);
            }
        }
    }
}

const DXVK_DLLS: &[&str] = &["d3d11", "d3d10core", "d3d9", "dxgi"];
const VKD3D_DLLS: &[&str] = &["d3d12", "d3d12core"];
const NVAPI_DLLS: &[&str] = &["nvapi64", "nvofapi64"];
/// System-installed by the NVIDIA driver, not shipped here.
const DLSS_DLLS: &[&str] = &["nvngx", "_nvngx", "nvngx_dlssg"];

pub(crate) const BASE_OVERRIDES: &[(&str, &str)] = &[
    ("lsteamclient", "b"),  // builtin: Wine bridge to native Steam client (built from Proton source)
    ("steam.exe", "b"),  // builtin: our Wine builtin steam.exe (built from Proton source)
    ("dotnetfx35.exe", "b"),
    ("dotnetfx35setup.exe", "b"),
    ("beclient.dll", "b,n"),
    ("beclient_x64.dll", "b,n"),
    // XInput: use Wine builtins — routes through winebus.sys for HID enumeration
    ("xinput1_1.dll", "b"),
    ("xinput1_2.dll", "b"),
    ("xinput1_3.dll", "b"),
    ("xinput1_4.dll", "b"),
    ("xinput9_1_0.dll", "b"),
    ("xinputuap.dll", "b"),
    // EasyAntiCheat: builtin from Proton EAC Runtime (Steam tool 1826330)
    ("easyanticheat", "b"),
    ("easyanticheat_x64", "b"),
    ("easyanticheat_x86", "b"),
    // winebth.sys crashes winedevice.exe — disable it (matches Proton)
    ("winebth.sys", "d"),
    // vrclient: SteamVR not needed — steam.exe asserts if it can't init VR
    ("vrclient_x64", "d"),
    // OpenCL: disabled for most games (Proton compat)
    ("opencl", "n,d"),
    // gameinput: crashes in some games (CW Bug 26376)
    ("gameinput", "d"),
];

// Adaptive deployment plan — built from PE scan result
struct DeployPlan {
    needs_nvapi: bool,
    needs_dlss: bool,
    scan_hash: u64,
}

impl DeployPlan {
    fn from_scan(scan: Option<&crate::pe_scanner::PeScanResult>) -> Self {
        let Some(scan) = scan else {
            // No scan (non-game verb, missing exe) — DXVK+VKD3D deploy defensively below
            return Self { needs_nvapi: false, needs_dlss: false, scan_hash: 0 };
        };

        let is_nvidia = std::path::Path::new("/proc/driver/nvidia/version").exists();

        // DXVK and VKD3D-Proton are always deployed (unconditionally below). Many
        // games discover D3D12 at runtime via DXGI (no direct d3d12.dll import), and
        // launcher stubs often have zero D3D imports. The translation layers are
        // harmless when unused — they only activate on actual API calls.
        let needs_nvapi = is_nvidia && scan.needs_nvapi;
        let needs_dlss = is_nvidia && scan.imports.iter().any(|s| s.contains("nvngx"));

        // Hash for cache: if imports change, re-deploy
        use std::hash::{Hash, Hasher};
        use std::collections::hash_map::DefaultHasher;
        let mut h = DefaultHasher::new();
        scan.render_api.hash(&mut h);
        scan.imports.len().hash(&mut h);
        for imp in &scan.imports { imp.hash(&mut h); }
        let scan_hash = h.finish();

        Self { needs_nvapi, needs_dlss, scan_hash }
    }
}

/// The inherited LD_PRELOAD with the Steam overlay (gameoverlayrenderer) stripped
/// -- it hooks SDL/GL calls and crashes before the Wayland surface is configured.
fn ld_preload_without_overlay() -> String {
    std::env::var("LD_PRELOAD").unwrap_or_default()
        .split(':')
        .filter(|p| !p.is_empty() && !p.contains("gameoverlayrenderer"))
        .collect::<Vec<&str>>()
        .join(":")
}

// Steam client files: (source_name in legacycompat/, dest_name in prefix)
const STEAM_CLIENT_FILES: &[(&str, &str)] = &[
    ("steamclient64.dll", "steamclient64.dll"),
    ("steamclient.dll", "steamclient.dll"),
    ("GameOverlayRenderer64.dll", "GameOverlayRenderer64.dll"),
    ("SteamService.exe", "steam.exe"),
    ("Steam.dll", "Steam.dll"),
];

const CACHE_FILE: &str = ".quark_deployed";

fn launches_game(verb: &str) -> bool {
    verb == "waitforexitandrun" || verb == "run"
}

pub fn run(verb: &str, args: &[OsString]) -> i32 {
    ensure_stdio_fds();

    if matches!(verb, "getcompatpath" | "getnativepath") {
        print_compat_path(args);
        return 0;
    }

    let t_start = Instant::now();

    // Phase 1: Locate everything
    let wine_dir = find_wine();
    let steam_dir = find_steam_dir();
    let wine64 = wine_binary(&wine_dir);

    if !wine64.exists() {
        log_error!("wine not found at {}", wine64.display());
        log_error!("Need Wine binaries. Install Wine from your package manager.");
        return 1;
    }

    // Proton's tree is optional — only needed for steam.exe sourcing now.
    // DXVK/VKD3D are deployed by install.py into quark's own lib/ dir.
    let proton_dir = find_proton_files();
    let dxvk_src_dir = dxvk_source_dir(&wine_dir, proton_dir.as_deref());

    let pfx = match compat_prefix(verb) {
        Ok(Some(pfx)) => pfx,
        Ok(None) => return 0,
        Err(code) => return code,
    };

    let self_exe = match std::env::current_exe() {
        Ok(p) => p,
        Err(e) => {
            log_error!("Failed to determine own executable path: {e}");
            return 1;
        }
    };
    let t_discover = t_start.elapsed();

    log_verbose!("wine64: {}", wine64.display());
    log_verbose!("wineserver: {}", wineserver_exe(&wine_dir).display());
    log_verbose!("steam: {}", steam_dir.display());

    // Per-component cache — only redeploy what actually changed
    let cache = DeployCache::load(&pfx);
    let wine_hash = dir_hash(&wine_dir);
    let dxvk_hash = dir_hash(&dxvk_src_dir.join("lib/wine/dxvk"));
    let vkd3d_hash = dir_hash(&dxvk_src_dir.join("lib/wine/vkd3d-proton"));
    let steam_hash = dir_hash(&steam_dir.join("legacycompat"));

    let wine_valid = cache.as_ref().is_some_and(|c| c.wine_hash == wine_hash);
    let dxvk_valid = cache.as_ref().is_some_and(|c| c.dxvk_hash == dxvk_hash);
    let vkd3d_valid = cache.as_ref().is_some_and(|c| c.vkd3d_hash == vkd3d_hash);
    let steam_valid = cache.as_ref().is_some_and(|c| c.steam_hash == steam_hash);

    // Phase 2: Prefix setup (idempotent)
    let t2 = Instant::now();
    prepare_prefix_layout(&wine_dir, &pfx);
    seed_prefix_registry(&self_exe, &pfx);
    ensure_registry_stubs(&pfx);
    inject_registry_keys(&pfx);

    // Deploy steam.exe and lsteamclient.dll BEFORE wineboot runs.
    // Wineboot's entry command is steam.exe — it must exist in system32 first.
    deploy_steam_exe(&self_exe, &pfx);
    deploy_lsteamclient_to_prefix(&self_exe, &pfx);

    if !wine_valid {
        setup_prefix(&wine_dir, &pfx, &wine64, &self_exe);
    }
    // Re-deploy after setup_prefix (wineboot may overwrite system32)
    deploy_steam_exe(&self_exe, &pfx);
    deploy_lsteamclient_to_prefix(&self_exe, &pfx);

    // Always ensure user profile directories exist — cheap no-op if already present.
    // Must be unconditional: setup_prefix is gated by cache but games always need these.
    ensure_user_profile_dirs(&pfx);
    let t_prefix = t2.elapsed();

    // PE scan: determine what this game needs BEFORE deploying DLLs
    let scan_result = if launches_game(verb) {
        args.first().and_then(|exe| scan_game(Path::new(exe)))
    } else {
        None
    };
    let plan = DeployPlan::from_scan(scan_result.as_ref());
    let scan_valid = cache.as_ref().is_some_and(|c| c.scan_hash == plan.scan_hash);

    if let Some(ref scan) = scan_result {
        log_verbose!("PE scan: {:?} | nvapi={} dlss={} steam_api={} imports={}",
            scan.render_api, plan.needs_nvapi, plan.needs_dlss, scan.needs_steam_api, scan.imports.len());
    }

    // Phase 3: Deploy DLLs based on PE scan (per-component, scan-driven)
    let t3 = Instant::now();
    let dxvk_deployed: Vec<&str> =
        if dxvk_valid && scan_valid { DXVK_DLLS.to_vec() } else { deploy_dxvk(&dxvk_src_dir, &pfx) };
    let vkd3d_deployed: Vec<&str> =
        if vkd3d_valid && scan_valid { VKD3D_DLLS.to_vec() } else { deploy_vkd3d(&dxvk_src_dir, &pfx) };
    let nvapi_deployed: Vec<&str> = if plan.needs_nvapi {
        if let Some(ref proton) = proton_dir { deploy_nvapi(proton, &pfx) } else { vec![] }
    } else { vec![] };
    let dlss_deployed: Vec<&str> = if plan.needs_dlss { deploy_dlss(&pfx) } else { vec![] };
    let t_dxvk = t3.elapsed();

    // Phase 4: Deploy Steam client DLLs (per-component)
    let t4 = Instant::now();
    // Every launch: file_matches makes it a stat per file, and a prefix that
    // holds the bridge under Valve's names is repaired rather than cached.
    deploy_steam_client(&steam_dir, &pfx);
    // Ensure our steam_bridge exists in prefix system32 (overwrites Proton's).
    deploy_steam_exe(&self_exe, &pfx);
    // lsteamclient.dll must exist in system32 so build_module's LdrLoadDll can find it.
    // Without this, the steamclient trampoline (Wine patch 001) never activates.
    deploy_lsteamclient_to_prefix(&self_exe, &pfx);
    let t_steam = t4.elapsed();

    // Save cache if anything changed
    let all_valid = wine_valid && dxvk_valid && vkd3d_valid && steam_valid && scan_valid;
    if !all_valid {
        DeployCache { wine_hash, dxvk_hash, vkd3d_hash, steam_hash, scan_hash: plan.scan_hash }.save(&pfx);
        log_verbose!("deployment cache written");
    } else {
        log_verbose!("cache hit — skipped all file ops");
    }

    // Phase 5: Build environment
    let log_dir = Path::new(crate::log::LOG_DIR);
    let trace = std::env::var("QUARK_TRACE_OPCODES").is_ok()
        || log_dir.join("TRACE_OPCODES").exists();

    let shader_cache_enabled = self_exe.parent()
        .map(|dir| dir.join("shader_cache_enabled").exists())
        .unwrap_or(false);

    // Parse env_config FIRST so launcher-affecting env vars (WINEDEBUG and
    // friends) are in place before build_env_vars and resolve_winedebug read them.
    let custom_env = parse_env_config(&self_exe);
    for (k, v) in &custom_env {
        unsafe { std::env::set_var(k, v); }
    }

    let env_vars = build_env_vars(
        &wine_dir, &pfx,
        &dxvk_deployed, &vkd3d_deployed, &nvapi_deployed, &dlss_deployed,
        shader_cache_enabled,
    );

    // Resolve WINEDEBUG once. Priority:
    // 1. env_config / shell env (user's explicit override)
    // 2. QUARK_TRACE_OPCODES → "+server,+timestamp"
    // 3. --verbose → "+process,err"
    // 4. default → "-all"
    let winedebug = resolve_winedebug(trace);

    log_verbose!("timing: discover={}ms prefix={}ms dxvk={}ms steam={}ms total={}ms",
        t_discover.as_millis(), t_prefix.as_millis(),
        t_dxvk.as_millis(), t_steam.as_millis(), t_start.elapsed().as_millis());

    // Phase 6: Launch
    remove_stale_wine_servers();

    if launches_game(verb) {
        let launch = Launch {
            wine_dir: &wine_dir,
            wine64: &wine64,
            pfx: &pfx,
            env_vars: &env_vars,
            custom_env: &custom_env,
            winedebug: &winedebug,
            log_dir,
        };
        return launch_game(&launch, args, scan_result.as_ref());
    }

    log_error!("Unknown verb: {verb}");
    1
}

/// getcompatpath / getnativepath: echo the path back, raw bytes intact.
fn print_compat_path(args: &[OsString]) {
    if let Some(path) = args.first() {
        use std::io::Write;
        use std::os::unix::ffi::OsStrExt;
        let mut out = std::io::stdout().lock();
        let _ = out.write_all(path.as_bytes()).and_then(|_| out.write_all(b"\n"));
    }
}

/// Where DXVK/VKD3D are sourced from. Checks for an actual DLL file (not
/// just the directory) to avoid false positives.
fn dxvk_source_dir(wine_dir: &Path, proton_dir: Option<&Path>) -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_default();
    let compat_tool_dir = PathBuf::from(&home)
        .join(".local/share/Steam/compatibilitytools.d/quark");
    if compat_tool_dir.join("lib/wine/dxvk/x86_64-windows/d3d11.dll").exists() {
        compat_tool_dir
    } else if wine_dir.join("lib/wine/dxvk/x86_64-windows/d3d11.dll").exists() {
        wine_dir.to_path_buf()
    } else if let Some(proton) = proton_dir {
        if proton.join("lib/wine/dxvk/x86_64-windows/d3d11.dll").exists() {
            log_verbose!("DXVK/VKD3D: sourcing from Proton ({})", proton.display());
            proton.to_path_buf()
        } else {
            log_warn!("DXVK: no DLLs found — D3D games will use Wine's builtin (may crash on NVIDIA)");
            wine_dir.to_path_buf()
        }
    } else {
        log_warn!("DXVK/VKD3D: no source found — D3D games will use Wine's builtin");
        wine_dir.to_path_buf()
    }
}

/// The prefix Steam assigned this launch: Ok(Some) to proceed, Ok(None) to
/// exit 0 without a Wine session, Err(exit code) on failure.
fn compat_prefix(verb: &str) -> Result<Option<PathBuf>, i32> {
    let compat_data = std::env::var("STEAM_COMPAT_DATA_PATH").unwrap_or_default();
    let steam_appid = std::env::var("SteamAppId").unwrap_or_default();
    log_verbose!("STEAM_COMPAT_DATA_PATH={compat_data} SteamAppId={steam_appid} verb={verb}");
    if compat_data.is_empty() {
        log_error!("STEAM_COMPAT_DATA_PATH not set — not launched from Steam?");
        return Err(1);
    }
    // Steam calls "proton run" with compatdata/0 and empty SteamAppId as a
    // prefix probe before the real game launch. Starting a full wineserver for
    // this pollutes the socket directory. Skip it entirely.
    if (compat_data.ends_with("/0") || compat_data.ends_with("/0/")) && verb == "run" {
        log_verbose!("Skipping Steam prefix probe (compatdata/0, verb=run)");
        return Ok(None);
    }
    let pfx = PathBuf::from(&compat_data).join("pfx");
    if let Err(e) = std::fs::create_dir_all(&pfx) {
        log_error!("Failed to create prefix directory {}: {e}", pfx.display());
        return Err(1);
    }
    Ok(Some(pfx))
}

/// Sync the Wine tree's PE files into prefix system32 BEFORE wineboot --init.
/// wineboot needs kernel32.dll et al to exist in the prefix. On a fresh
/// prefix this directory is empty, causing "could not load kernel32.dll".
/// The source is the tree quark runs (patched modules included), the same
/// layer install.py's prefix sync writes, so neither undoes the other.
pub(crate) fn prepare_prefix_layout(wine_dir: &Path, pfx: &Path) {
    let sys32 = pfx.join("drive_c/windows/system32");
    let _ = std::fs::create_dir_all(&sys32);
    // Create Windows directory structure that wineboot normally populates.
    // Games (especially LÖVE/SDL) need these to exist during init.
    let win = pfx.join("drive_c/windows");
    for d in [
        "syswow64", "Fonts", "temp", "system", "inf", "help", "logs",
        "winsxs", "resources", "security", "tasks", "performance",
        "Microsoft.NET", "Installer", "Globalization", "twain_32", "twain_64",
    ] {
        let _ = std::fs::create_dir_all(win.join(d));
    }
    let _ = std::fs::create_dir_all(pfx.join("drive_c/ProgramData"));
    let _ = std::fs::create_dir_all(pfx.join("drive_c/Program Files/Common Files"));
    let _ = std::fs::create_dir_all(pfx.join("drive_c/Program Files (x86)/Common Files"));
    // wine.inf installs drivers to %12% (system32\drivers) and registers each
    // service binary there; a .sys anywhere else fails to load (c0000135).
    let drivers = sys32.join("drivers");
    let src_dir = wine_dir.join("lib/wine/x86_64-windows");
    let src_ntdll = src_dir.join("ntdll.dll");
    let pfx_ntdll = sys32.join("ntdll.dll");
    if src_ntdll.exists() {
        let src_size = src_ntdll.metadata().map(|m| m.len()).unwrap_or(0);
        let pfx_size = pfx_ntdll.metadata().map(|m| m.len()).unwrap_or(0);
        // A prefix synced before drivers had their own directory gets its
        // drivers alone; a full sync would replace the DXVK DLLs the deploy
        // cache believes are in place.
        let full = src_size != pfx_size;
        let drivers_only = !full && !drivers.is_dir();
        if full || drivers_only {
            log_verbose!("Syncing Wine PE files to prefix from {}...", src_dir.display());
            let _ = std::fs::create_dir_all(&drivers);
            let mut copied = 0u32;
            if let Ok(entries) = std::fs::read_dir(&src_dir) {
                for entry in entries.flatten() {
                    let path = entry.path();
                    let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("");
                    if drivers_only && ext != "sys" {
                        continue;
                    }
                    if ext == "dll" || ext == "exe" || ext == "drv" || ext == "sys" || ext == "ocx" || ext == "cpl" || ext == "acm" || ext == "ax" {
                        let dir = if ext == "sys" { &drivers } else { &sys32 };
                        let target = dir.join(entry.file_name());
                        let _ = std::fs::copy(&path, &target);
                        copied += 1;
                    }
                }
            }
            log_verbose!("Synced {copied} PE files to prefix system32");
        }
    }
}

/// Ensure pfx dir and stub .reg files exist BEFORE wineboot starts the
/// wineserver. It loads .reg files at startup — if we inject keys after
/// that, it never sees them. So: create stubs → inject keys → wineboot.
fn ensure_registry_stubs(pfx: &Path) {
    let _ = std::fs::create_dir_all(pfx);
    for reg_file in ["system.reg", "user.reg", "userdef.reg"] {
        let reg_path = pfx.join(reg_file);
        if !reg_path.exists() {
            let header = if reg_file.starts_with("user") {
                "WINE REGISTRY Version 2\n;; All keys relative to REGISTRY\\User\n\n#arch=win64\n"
            } else {
                "WINE REGISTRY Version 2\n;; All keys relative to REGISTRY\\Machine\n\n#arch=win64\n"
            };
            let _ = std::fs::write(&reg_path, header);
        }
    }
}

/// Scan the game's imports. Some games (UE5) use a launcher stub that
/// imports nothing — the real binary is in Binaries/Win64/, so a stub with
/// no D3D imports defers to that child.
fn scan_game(exe: &Path) -> Option<crate::pe_scanner::PeScanResult> {
    let primary = if exe.exists() { crate::pe_scanner::scan_pe(exe) } else { None };
    let dominated = primary.as_ref().is_some_and(|s| s.imports.is_empty()
        || !s.imports.iter().any(|i| i.contains("d3d")));
    if dominated {
        if let Some(parent) = exe.parent() {
            // UE pattern: <Game>/<Project>/Binaries/Win64/<Project>-Win64-Shipping.exe
            let stem = exe.file_stem().and_then(|s| s.to_str()).unwrap_or("");
            let ue_bin = parent.join(stem).join("Binaries/Win64");
            if ue_bin.exists() {
                let pattern = format!("{stem}-Win64-Shipping.exe");
                if let Ok(rd) = std::fs::read_dir(&ue_bin) {
                    for entry in rd.flatten() {
                        if entry.file_name().to_str().is_some_and(|n| n.eq_ignore_ascii_case(&pattern)) {
                            if let Some(child_scan) = crate::pe_scanner::scan_pe(&entry.path()) {
                                log_verbose!("PE scan: using UE child {}", entry.path().display());
                                return Some(child_scan);
                            }
                        }
                    }
                }
            }
        }
    }
    primary
}

/// Clean stale wineserver sockets from previous runs.
/// Wine connects to any existing socket for the prefix's inode.
/// If a stale socket exists from a crashed/killed server, Wine hangs
/// trying to connect to a dead daemon.
fn remove_stale_wine_servers() {
    let uid = unsafe { libc::getuid() };
    let wine_tmp = format!("/tmp/.wine-{uid}");
    if let Ok(entries) = std::fs::read_dir(&wine_tmp) {
        for entry in entries.flatten() {
            let sock = entry.path().join("socket");
            if sock.exists() {
                // Try connecting with timeout — if refused or timeout, it's stale
                use std::os::unix::net::UnixStream;
                match UnixStream::connect(&sock) {
                    Ok(s) => drop(s), // live server, leave it
                    Err(_) => {
                        log_verbose!("Removing stale server dir: {}", entry.path().display());
                        let _ = std::fs::remove_dir_all(entry.path());
                    }
                }
            }
        }
    }
}

/// Everything a game launch reads, resolved by `run` before Phase 6.
struct Launch<'a> {
    wine_dir: &'a Path,
    wine64: &'a Path,
    pfx: &'a Path,
    env_vars: &'a [(&'static str, String)],
    custom_env: &'a [(String, String)],
    winedebug: &'a str,
    log_dir: &'a Path,
}

fn launch_game(l: &Launch, args: &[OsString], scan_result: Option<&crate::pe_scanner::PeScanResult>) -> i32 {
    let game_exe = match args.first() {
        Some(exe) => exe,
        None => {
            log_error!("No executable specified");
            return 1;
        }
    };
    let game_exe_str = game_exe.to_string_lossy();

    // Steam runs utility exes that don't need a full Wine session:
    // - iscriptevaluator.exe: DirectX/vcredist install scripts (Wine provides these)
    // - d3ddriverquery64.exe: GPU capability probe (fails without full display driver)
    // Skip them — returning 0 tells Steam the compat tool is healthy.
    let dominated_exes = ["iscriptevaluator", "d3ddriverquery"];
    if dominated_exes.iter().any(|pat| game_exe_str.to_ascii_lowercase().contains(pat)) {
        log_verbose!("skipping Steam utility: {game_exe_str}");
        return 0;
    }

    // Sanity check: does the game exe exist?
    let game_path = Path::new(game_exe);
    if !game_path.exists() {
        log_warn!("game exe not found at {game_exe_str} (may be a Windows path — continuing)");
    }

    log_info!("launching: {game_exe_str}");

    // Pre-launch validation: verify deployed DLLs exist in prefix
    if let Some(scan) = scan_result {
        let sys32 = l.pfx.join("drive_c/windows/system32");
        let mut fatal: Vec<String> = Vec::new();
        if !sys32.join("d3d11.dll").exists() {
            fatal.push("d3d11.dll (DXVK)".into());
        }
        if !sys32.join("d3d12.dll").exists() {
            fatal.push("d3d12.dll (VKD3D-Proton)".into());
        }
        if !fatal.is_empty() {
            for dll in &fatal {
                log_error!("FATAL: {dll} not deployed — game needs {:?}", scan.render_api);
            }
            log_error!("Check DXVK/VKD3D source dirs. Game will crash without these.");
        }
    }

    // Clean stale crash dumps — engines like Godot read their crash dir
    // on startup and may enter recovery paths if old dumps exist.
    clean_crash_dumps(l.pfx);

    // Save data protection: snapshot before launch
    let save_backup = snapshot_save_data(l.pfx);

    let mut cmd = game_command(l, game_exe, &args[1..]);
    // Ignored here, before the game spawns: it inherits it.
    unsafe {
        libc::signal(libc::SIGINT, libc::SIG_IGN);
        libc::signal(libc::SIGTERM, libc::SIG_IGN);
    }
    cmd.env("WINEDEBUG", l.winedebug);

    // Strip Steam overlay from LD_PRELOAD — it hooks SDL/GL calls and
    // crashes before the Wayland surface is configured.
    let preload = ld_preload_without_overlay();
    if preload.is_empty() {
        cmd.env_remove("LD_PRELOAD");
    } else {
        cmd.env("LD_PRELOAD", &preload);
    }

    let stderr_log_path = redirect_game_output(&mut cmd, l.log_dir);

    // User overrides win. Applied LAST so any cmd.env hardcoded above
    // (e.g. WINE_NTSYNC=1) yields to env_config when the user explicitly
    // sets a different value. Comment-out in env_config is now meaningful
    // for any var quark hardcodes — uncomment + change to override.
    for (k, v) in l.custom_env {
        cmd.env(k, v);
    }

    let mut child = cmd.spawn().unwrap_or_else(|e| {
        log_error!("Failed to exec wine64: {e}");
        std::process::exit(1);
    });

    let incident_game_stderr = spawn_observer(l, game_path);

    // Now wait for the game to exit
    let child_pid = child.id() as i32;
    let status = child.wait().unwrap_or_else(|e| {
        log_error!("Failed to wait for wine64: {e}");
        std::process::exit(1);
    });
    kill_session(child_pid);

    if let Some(ref log_path) = stderr_log_path {
        record_exit(log_path, &status, incident_game_stderr.as_deref());
    }

    // Save data protection: restore any files that went missing
    if let Some(ref backup_dir) = save_backup {
        restore_save_data(l.pfx, backup_dir);
    }

    match status.code() {
        Some(code) => code,
        None => {
            use std::os::unix::process::ExitStatusExt;
            if let Some(sig) = status.signal() {
                log_warn!("wine64 killed by signal {sig}");
            }
            1
        }
    }
}

/// Launch through steam.exe (Proton's steam_helper). steam.exe:
/// 1. Creates Win32 events games check (Steam3Master_SharedMemLock, etc.)
/// 2. Calls steamclient_init_registry() — dlopen's native steamclient.so,
///    CreateSteamPipe()/ConnectToGlobalUser() to connect to running Steam
/// 3. Creates the "Steam" window (FindWindow detection)
/// 4. Spawns the game as a child via CreateProcess()
///
/// Without this, SteamAPI_IsSteamRunning() fails.
fn game_command(l: &Launch, game_exe: &OsString, game_args: &[OsString]) -> Command {
    let game_path = Path::new(game_exe);
    let mut cmd = build_wine_command(l.wine64, l.wine_dir);
    cmd.arg("C:\\windows\\system32\\steam.exe");
    cmd.arg(game_exe);
    cmd.args(game_args);
    if let Some(parent) = game_path.parent() {
        if parent.exists() { cmd.current_dir(parent); }
    }
    // Order: quark defaults → quark hardcoded essentials → user's
    // env_config (last write wins, so user overrides reach the game).
    // Previously custom_env was applied BEFORE the cmd.env("WINE_NTSYNC",
    // "1") below, which meant any attempt to disable ntsync via
    // env_config was silently overridden. Now custom_env wins.
    for (k, v) in l.env_vars {
        cmd.env(k, v);
    }
    cmd.env("WINEPREFIX", l.pfx);
    cmd.env("WINESERVER", wineserver_exe(l.wine_dir));
    cmd.env("WINE_NTSYNC", "1");
    // Windows TEMP/TMP: stock prefixes get these via shell32
    // create_extra_folders writing HKCU\Environment; that chain never
    // runs under quark, so NO Windows process had TEMP — wine-mono fed
    // the NULL into g_path_is_absolute and aborted (TMNT,
    // gmisc-win32.c:138). ntdll FILTERS plain TEMP=/TMP= out of the unix
    // env import (env.c is_special_env_var) but imports WINETEMP=/WINETMP=
    // AS TEMP=/TMP= (env.c ~:888) — that is the sanctioned channel. The
    // user-overrides-win custom_env loop below can still override.
    let win_temp = format!("C:\\users\\{}\\Temp", profile_user(l.pfx));
    cmd.env("WINETEMP", &win_temp);
    cmd.env("WINETMP", &win_temp);
    let steam_app_id = std::env::var("SteamGameId")
        .or_else(|_| std::env::var("SteamAppId"))
        .or_else(|_| std::env::var("STEAM_COMPAT_APP_ID"))
        .unwrap_or_default();
    cmd.env("SteamAppId", &steam_app_id);
    cmd.env("SteamGameId", &steam_app_id);
    cmd.env("STEAM_COMPAT_CLIENT_INSTALL_PATH",
        std::env::var("STEAM_COMPAT_CLIENT_INSTALL_PATH")
            .unwrap_or_else(|_| format!("{}/.local/share/Steam", std::env::var("HOME").unwrap_or_default())));

    // The launcher ignores SIGINT and SIGTERM for itself; an ignored
    // disposition survives exec, so the child resets both or a SIGTERM
    // to the game is a no-op and it never gets its clean exit (which is
    // where Wine puts a changed display mode back).
    use std::os::unix::process::CommandExt;
    unsafe {
        cmd.pre_exec(|| {
            libc::setsid();
            libc::signal(libc::SIGINT, libc::SIG_DFL);
            libc::signal(libc::SIGTERM, libc::SIG_DFL);
            Ok(())
        });
    }
    cmd
}

/// Send the game's stderr and stdout to LOG_DIR; returns the stderr log.
fn redirect_game_output(cmd: &mut Command, log_dir: &Path) -> Option<PathBuf> {
    let _ = std::fs::create_dir_all(log_dir);
    let path = log_dir.join("wine_stderr.log");
    match std::fs::File::create(&path) {
        Ok(f) => {
            cmd.stderr(std::process::Stdio::from(f));
            if let Ok(f2) = std::fs::File::create(log_dir.join("wine_stdout.log")) {
                cmd.stdout(std::process::Stdio::from(f2));
            }
            Some(path)
        }
        Err(_) => None,
    }
}

/// Observer (opt-in via env_config QUARK_TRACE=1, set by
/// install.py --observer). Spawns montauk as a sibling watching the
/// game's process tree by name pattern (montauk's --trace expects a
/// substring/regex matched against process names — NOT a PID).
///
/// Pattern strategy: use the game's exe basename without extension.
/// Wine spawns the game .exe as a child of wine64; montauk's tracepoint
/// on sched_process_fork auto-adds children once any process matches.
/// Fire-and-forget — observer death does not affect the game. Returns
/// where to preserve the game's stderr for this incident.
fn spawn_observer(l: &Launch, game_path: &Path) -> Option<PathBuf> {
    if std::env::var("QUARK_TRACE").as_deref() != Ok("1") {
        return None;
    }
    let incidents = l.log_dir.join("incidents");
    let _ = std::fs::create_dir_all(&incidents);
    // Kernel TASK_COMM_LEN is 16 (15 chars + NUL); the kernel
    // truncates process comm to 15 chars. Montauk's --trace
    // substring-matches against /proc/PID/comm — a pattern longer
    // than 15 chars CANNOT substring-match the truncated comm.
    // Clamp the pattern to 15 bytes so e.g. "VampireSurvivors"
    // becomes "VampireSurvivor" which actually matches.
    let raw = game_path
        .file_stem()
        .and_then(|s| s.to_str())
        .map(|s| s.to_string())
        .unwrap_or_else(|| "wine".to_string());
    let pattern: String = raw.bytes().take(15)
        .map(|b| b as char).collect();
    // Observer: incident-only mode by default. We DON'T pass
    // --trace-out (would dump 500MB+ of per-event data to disk per
    // launch) and we DON'T pass --log (Prometheus snapshots —
    // useful for live monitoring, not for crash diagnosis). Just
    // attach BPF and let SIGNAL events surface to stderr.
    //
    // Diagnostic mode: set QUARK_TRACE_OUT=1 to also dump
    // the full binary event stream (I/O syscalls including pread64,
    // sched events, ntsync) to LOG_DIR/incidents/<game>-io.bin.
    // Read it with `montauk --decode` / `montauk --analyze`.
    let stderr_log = incidents.join(format!("{pattern}-stderr.log"));
    // <pattern>-stderr.log above is MONTAUK's stderr. The game's
    // real stderr (glibc abort text, Wine err channels) goes to
    // LOG_DIR/wine_stderr.log, which the next launch truncates.
    // Preserve it per-incident after the game exits.
    let incident_game_stderr = incidents.join(format!("{pattern}-game-stderr.log"));
    // Trace the game, steam.exe (the game's parent on every launch, so a
    // hang in the Steam bridge is in it) and the wineserver in one capture:
    // the montauk `handles` join needs the wineserver's ntsync ops AND the
    // guest's waits in the same trace. montauk --trace takes a
    // comma-separated comm list. Game first = the BPF .rodata fast-path
    // primary (instant exec-time capture).
    let trace_pattern = format!("{},steam.exe,wineserver", pattern);
    let mut ocmd = std::process::Command::new("montauk");
    ocmd.arg("--trace").arg(&trace_pattern)
        .arg("--headless");
    // Point montauk's ntdll uprobes (keyed-event critical sections + the
    // wait-site stacks on NtWaitForSingleObject/NtWaitForMultipleObjects) at
    // the deployed unix-side ntdll.so. Without MONTAUK_NTDLL_PATH the uprobes
    // silently skip, so keyed-event and wait-site capture never attach.
    let ntdll_so = l.wine_dir.join("lib/wine/x86_64-unix/ntdll.so");
    if ntdll_so.exists() {
        ocmd.env("MONTAUK_NTDLL_PATH", &ntdll_so);
    }
    if std::env::var("QUARK_TRACE_OUT").ok().as_deref() == Some("1") {
        let trace_out = incidents.join(format!("{pattern}-io.bin"));
        ocmd.arg("--trace-out").arg(&trace_out);
        log_info!("montauk observer: --trace-out → {}", trace_out.display());
    }
    ocmd.stdout(std::process::Stdio::null())
        .stderr(std::fs::File::create(&stderr_log)
            .map(std::process::Stdio::from)
            .unwrap_or_else(|_| std::process::Stdio::null()));
    match ocmd.spawn() {
        Ok(c) => log_info!("montauk observer spawned (pid {}) tracing \"{}\" \
                            → {}", c.id(), pattern, stderr_log.display()),
        Err(e) => log_warn!("montauk observer spawn failed: {e} \
                            (install montauk or unset QUARK_TRACE)"),
    }
    Some(incident_game_stderr)
}

/// Kill the entire session spawned by wine64 (the setsid pre_exec in
/// game_command). Without this, orphaned Wine children (services.exe, game
/// .exe) survive after wine64 exits and spin at 100%+ CPU.
///
/// SIGTERM first so services.exe / wineboot / etc. get a chance to
/// flush registry hives, persist save data, and unlink temp files.
/// Most well-behaved Wine processes exit within a few hundred ms.
/// SIGKILL as the backstop for the ones that don't.
fn kill_session(child_pid: i32) {
    unsafe { libc::kill(-child_pid, libc::SIGTERM); }
    std::thread::sleep(std::time::Duration::from_millis(500));
    unsafe { libc::kill(-child_pid, libc::SIGKILL); }
}

/// Append the exit status to the game's stderr log, and preserve that log
/// next to the montauk trace — wine_stderr.log is truncated on every launch.
fn record_exit(log_path: &Path, status: &std::process::ExitStatus, incident_game_stderr: Option<&Path>) {
    let exit_info = match status.code() {
        Some(0) => "exit: 0 (clean)".to_string(),
        Some(code) => format!("exit: {code} (error)"),
        None => {
            use std::os::unix::process::ExitStatusExt;
            match status.signal() {
                Some(sig) => format!("killed by signal {sig}"),
                None => "unknown exit".to_string(),
            }
        }
    };

    if let Ok(mut f) = std::fs::OpenOptions::new().append(true).open(log_path) {
        use std::io::Write;
        let _ = writeln!(f, "\n[quark] {exit_info}");
    }
    log_verbose!("stderr log: {}", log_path.display());

    if let Some(dst) = incident_game_stderr {
        match std::fs::copy(log_path, dst) {
            Ok(_) => log_verbose!("incident game stderr: {}", dst.display()),
            Err(e) => log_warn!("incident game stderr copy failed: {e}"),
        }
    }
}

// ---------------------------------------------------------------------------
// Deployment cache
// ---------------------------------------------------------------------------

pub(crate) struct DeployCache {
    pub(crate) wine_hash: u64,
    pub(crate) dxvk_hash: u64,
    pub(crate) vkd3d_hash: u64,
    pub(crate) steam_hash: u64,
    pub(crate) scan_hash: u64,
}

impl DeployCache {
    pub(crate) fn load(pfx: &Path) -> Option<Self> {
        let data = std::fs::read_to_string(pfx.join(CACHE_FILE)).ok()?;
        let parts: Vec<&str> = data.strip_prefix("v4:")?.trim().split(',').collect();
        if parts.len() != 5 { return None; }
        Some(DeployCache {
            wine_hash: parts[0].parse().ok()?,
            dxvk_hash: parts[1].parse().ok()?,
            vkd3d_hash: parts[2].parse().ok()?,
            steam_hash: parts[3].parse().ok()?,
            scan_hash: parts[4].parse().ok()?,
        })
    }

    pub(crate) fn save(&self, pfx: &Path) {
        let data = format!("v4:{},{},{},{},{}", self.wine_hash, self.dxvk_hash,
            self.vkd3d_hash, self.steam_hash, self.scan_hash);
        if let Err(e) = std::fs::write(pfx.join(CACHE_FILE), data) {
            log_warn!("Cannot write deployment cache: {e} — next launch will re-deploy");
        }
    }
}

/// Quick hash of a directory's metadata: combine dev+ino+mtime of the dir entry.
/// Detects directory replacement (new inode) or direct modification (mtime change).
pub(crate) fn dir_hash(path: &Path) -> u64 {
    use std::os::unix::fs::MetadataExt;
    let meta = match std::fs::metadata(path) {
        Ok(m) => m,
        Err(_) => return 0,
    };
    let dev = meta.dev();
    let ino = meta.ino();
    let mtime = meta.mtime() as u64;
    // Simple hash combine
    dev.wrapping_mul(6364136223846793005)
        ^ ino.wrapping_mul(1442695040888963407)
        ^ mtime
}

// ---------------------------------------------------------------------------
// Prefix setup
// ---------------------------------------------------------------------------

fn setup_prefix(wine_dir: &Path, pfx: &Path, wine64: &Path, self_exe: &Path) {
    let default_pfx = wine_dir.join("share").join("default_pfx");
    if default_pfx.exists() {
        setup_prefix_from_template(&default_pfx, pfx);
    } else {
        setup_prefix_with_wineboot(wine_dir, pfx, wine64, self_exe);
    }

    // Prevent Wine's internal wineboot from re-running wine.inf processing.
    // Our setup_prefix handles prefix init; the Wine-level update spawns
    // multiple rundll32 passes (PreInstall + DefaultInstall) that can take
    // minutes and block the game from loading.
    let ts_file = pfx.join(".update-timestamp");
    let _ = std::fs::write(&ts_file, "disable\n");
}

/// Cache invalidation drives re-setup. copy_dir_fast skips existing files,
/// so re-running is safe and adds anything missing (e.g. symlinks from a buggy deploy).
fn setup_prefix_from_template(default_pfx: &Path, pfx: &Path) {
    let fresh = !pfx.join("system.reg").exists();
    if fresh {
        log_verbose!("Setting up prefix from template...");
    } else {
        log_verbose!("Repairing prefix from template...");
    }
    let count = copy_dir_fast(default_pfx, pfx);
    if count > 0 {
        log_verbose!("Prefix: {count} files deployed");
    }
    ensure_dosdevices(pfx);
}

/// No template available (system Wine) — fall back to wineboot.
fn setup_prefix_with_wineboot(wine_dir: &Path, pfx: &Path, wine64: &Path, self_exe: &Path) {
    // Create user profile dirs FIRST — wineboot's SHGetFolderPath needs them.
    ensure_user_profile_dirs(pfx);
    ensure_dosdevices(pfx);
    // Windows system dirs
    let _ = std::fs::create_dir_all(pfx.join("drive_c/windows/system32"));
    let _ = std::fs::create_dir_all(pfx.join("drive_c/windows/syswow64"));
    let _ = std::fs::create_dir_all(pfx.join("drive_c/Program Files"));
    let _ = std::fs::create_dir_all(pfx.join("drive_c/Program Files (x86)"));

    // A prefix seeded from the baked template has its registry already
    // (seed_prefix_registry); without one, wineboot --init applies wine.inf.
    // The launcher's own stub .reg files and directory layout say nothing
    // about whether a prefix was initialized.
    if quark_template(self_exe).is_some() {
        seed_from_quark_template(self_exe, pfx);
    } else {
        run_wineboot_init(wine_dir, pfx, wine64, self_exe);
    }
}

/// The c: and z: drive mappings.
fn ensure_dosdevices(pfx: &Path) {
    let dosdevices = pfx.join("dosdevices");
    if let Err(e) = std::fs::create_dir_all(&dosdevices) {
        log_error!("Cannot create dosdevices dir: {e} — drive mappings will fail");
    }
    let c_link = dosdevices.join("c:");
    let z_link = dosdevices.join("z:");
    if !c_link.exists() {
        if let Err(e) = std::os::unix::fs::symlink("../drive_c", &c_link) {
            log_error!("Cannot create c: drive symlink: {e}");
        }
    }
    if !z_link.exists() {
        if let Err(e) = std::os::unix::fs::symlink("/", &z_link) {
            log_error!("Cannot create z: drive symlink: {e}");
        }
    }
}

/// Copy quark's baked prefix template .reg files (COM registrations).
/// These contain 500+ CLSID entries from Wine's FakeDlls pass, baked
/// at install time by install.py's step_bake_prefix_template.
/// install.py's baked prefix template beside the launcher, if it has a
/// registry.
fn quark_template(self_exe: &Path) -> Option<PathBuf> {
    let tpl = self_exe.parent()?.join("default_pfx");
    tpl.join("system.reg").exists().then_some(tpl)
}

/// A new prefix takes the baked template's registry (its full COM class
/// registrations) before ensure_registry_stubs writes header-only .reg
/// files, which would otherwise keep the template out for good. Existing
/// .reg files are never replaced.
pub(crate) fn seed_prefix_registry(self_exe: &Path, pfx: &Path) {
    let Some(tpl) = quark_template(self_exe) else { return };
    let _ = std::fs::create_dir_all(pfx);
    for reg in ["system.reg", "user.reg", "userdef.reg"] {
        let src = tpl.join(reg);
        let dst = pfx.join(reg);
        if src.exists() && !dst.exists() {
            match std::fs::copy(&src, &dst) {
                Ok(_) => log_verbose!("Prefix: {reg} seeded from {}", tpl.display()),
                Err(e) => log_warn!("Prefix: seeding {reg} from {} failed: {e}", tpl.display()),
            }
            // The user registry's shell folders (TEMP's AppData\Local\Temp
            // among them) name directories only the template's tree holds.
            if reg == "user.reg" {
                mirror_dirs(&tpl.join("drive_c/users"), &pfx.join("drive_c/users"));
            }
        }
    }
}

/// Recreate src's directories under dst, without its files or symlinks, so
/// nothing in the prefix shares an inode with the template.
fn mirror_dirs(src: &Path, dst: &Path) {
    let Ok(entries) = std::fs::read_dir(src) else { return };
    for entry in entries.flatten() {
        if entry.file_type().is_ok_and(|t| t.is_dir()) {
            let sub = dst.join(entry.file_name());
            if let Err(e) = std::fs::create_dir_all(&sub) {
                log_warn!("Prefix: creating {} failed: {e}", sub.display());
                continue;
            }
            mirror_dirs(&entry.path(), &sub);
        }
    }
}

fn seed_from_quark_template(self_exe: &Path, pfx: &Path) {
    let Some(tpl) = quark_template(self_exe) else { return };
    // The baked template includes filesystem layout that
    // wineboot --init does NOT recreate — most importantly
    // system32\wbem and syswow64\wbem, registered in
    // system.reg as the load paths for WBEM Locator
    // (CLSID 4590f811). Unity audio init goes through
    // CoCreateInstance(WBEM Locator) for device enumeration;
    // a missing wbemprox.dll on that path makes Wine's
    // apartment_add_dll fail and Unity's audio_client_timer
    // hits its internal debugbreak.
    for subdir in [
        "drive_c/windows/system32/wbem",
        "drive_c/windows/syswow64/wbem",
    ] {
        let src = tpl.join(subdir);
        let dst = pfx.join(subdir);
        if src.is_dir() && !dst.exists() {
            if let Err(e) = copy_dir_recursive(&src, &dst) {
                log_verbose!("Prefix: failed to seed {}: {}", subdir, e);
            }
        }
    }
    log_verbose!("Prefix: seeded wbem from baked template {}", tpl.display());
}

fn run_wineboot_init(wine_dir: &Path, pfx: &Path, wine64: &Path, self_exe: &Path) {
    log_verbose!("No default_pfx template, running wineboot --init...");
    let mut cmd = build_wine_command(wine64, wine_dir);
    cmd.args(["wineboot", "--init"]);
    cmd.env("WINEPREFIX", pfx.as_os_str());
    cmd.env("USER", profile_user(pfx));
    cmd.env("WINESERVER", wineserver_exe(wine_dir));
    cmd.env("LD_LIBRARY_PATH", wine_ld_path(wine_dir));
    // WINELOADER: Wine child processes re-exec via this path.
    cmd.env("WINELOADER", wine64.as_os_str());
    cmd.env("WINEDLLPATH", wine_dll_path(wine_dir));
    let dll_dir = format!("\\??\\Z:{}", wine_dir.join("lib/wine").display());
    cmd.env("WINEDLLDIR0", &dll_dir);
    cmd.env("WINEDEBUG", resolve_winedebug(false));
    // No fsync/esync — system Wine uses ntsync inproc sync.
    cmd.env("WINEESYNC", "0");
    // Preload assertion suppressor: converts assert() failures in Wine's
    // ntdll unix layer to warnings. Targets add_fd_to_cache collision
    // and user_check_not_lock race during display driver init.
    let noassert = {
        // Check installed path first, then dev path
        let installed = PathBuf::from(std::env::var("HOME").unwrap_or_default())
            .join(".local/share/Steam/compatibilitytools.d/quark/lib/noassert.so");
        if installed.exists() { installed }
        else { self_exe.parent().unwrap_or(Path::new(".")).join("lib/noassert.so") }
    };
    // Strip Steam overlay from LD_PRELOAD for wineboot too
    let mut preload = ld_preload_without_overlay();
    if noassert.exists() {
        if !preload.is_empty() { preload.push(':'); }
        preload.push_str(&noassert.display().to_string());
    }
    if preload.is_empty() {
        cmd.env_remove("LD_PRELOAD");
    } else {
        cmd.env("LD_PRELOAD", &preload);
    }
    match cmd.status() {
        Ok(s) if !s.success() => log_error!("wineboot --init failed with exit code {}", s.code().unwrap_or(-1)),
        Err(e) => log_error!("Failed to run wineboot: {e}"),
        _ => {}
    }
}

/// Inject registry keys that Proton normally installs via redistributable packages.
/// The DLLs are already in the prefix (from template symlinks), but some games
/// check registry keys to verify installation before even trying to load them.
fn inject_registry_keys(pfx: &Path) {
    use std::io::Write;
    let sys_reg = pfx.join("system.reg");
    let user_reg = pfx.join("user.reg");

    // ── Template-based registry: copy from stock Wine prefix ──
    // Instead of injecting dozens of individual keys (services, shell folders,
    // VC++ runtime, etc.), copy the complete registry from ~/.wine which was
    // created by wineboot --init under stock wineserver. This gives us a
    // fully correct registry matching the installed Wine version.
    let home = std::env::var("HOME").unwrap_or_else(|_| "/root".into());
    let stock_prefix = PathBuf::from(&home).join(".wine");
    let stock_sys = stock_prefix.join("system.reg");
    let stock_user = stock_prefix.join("user.reg");
    let stock_userdef = stock_prefix.join("userdef.reg");

    // Only copy if the target is a stub (our minimal header) or missing.
    // If the target already has real content (>1KB), don't overwrite — the
    // wineserver may have saved updated registry state from a previous session.
    let is_stub = |path: &Path| -> bool {
        path.metadata().map(|m| m.len() < 1024).unwrap_or(true)
    };

    if stock_sys.exists() && is_stub(&sys_reg) {
        match std::fs::copy(&stock_sys, &sys_reg) {
            Ok(bytes) => log_verbose!("Registry: copied stock system.reg ({bytes} bytes)"),
            Err(e) => log_error!("Failed to copy stock system.reg: {e}"),
        }
    }
    if stock_user.exists() && is_stub(&user_reg) {
        match std::fs::copy(&stock_user, &user_reg) {
            Ok(bytes) => log_verbose!("Registry: copied stock user.reg ({bytes} bytes)"),
            Err(e) => log_error!("Failed to copy stock user.reg: {e}"),
        }
    }
    if stock_userdef.exists() {
        let userdef_reg = pfx.join("userdef.reg");
        if is_stub(&userdef_reg) {
            let _ = std::fs::copy(&stock_userdef, &userdef_reg);
        }
    }

    // Steam paths (HKLM + HKCU) — only if not already present
    {
        let sys_contents = std::fs::read_to_string(&sys_reg).unwrap_or_default();
        if !sys_contents.contains("[Software\\\\Valve\\\\Steam]") {
            let keys = "\n[Software\\\\Valve\\\\Steam] 1772204972\n#time=1dca7fb13d11a48\n\"InstallPath\"=\"C:\\\\Program Files (x86)\\\\Steam\"\n\"SteamPath\"=\"C:\\\\Program Files (x86)\\\\Steam\"\n\n";
            if let Ok(mut f) = std::fs::OpenOptions::new().append(true).open(&sys_reg) {
                let _ = f.write_all(keys.as_bytes());
                log_verbose!("Registry: injected Steam InstallPath");
            }
        }

        let user_contents = std::fs::read_to_string(&user_reg).unwrap_or_default();
        if !user_contents.contains("Valve\\\\Steam\\\\ActiveProcess") {
            let keys = "\n[Software\\\\Valve\\\\Steam] 1772204972\n#time=1dca7fb13d11a48\n\"language\"=\"english\"\n\"SteamExe\"=\"C:\\\\Program Files (x86)\\\\Steam\\\\Steam.exe\"\n\"SteamPath\"=\"C:\\\\Program Files (x86)\\\\Steam\"\n\n[Software\\\\Valve\\\\Steam\\\\ActiveProcess] 1772204972\n#time=1dca7fb13d11a48\n\"PID\"=dword:0000fffe\n\"SteamClientDll\"=\"C:\\\\Program Files (x86)\\\\Steam\\\\steamclient.dll\"\n\"SteamClientDll64\"=\"C:\\\\Program Files (x86)\\\\Steam\\\\steamclient64.dll\"\n\"SteamPath\"=\"C:\\\\Program Files (x86)\\\\Steam\"\n\n";
            if let Ok(mut f) = std::fs::OpenOptions::new().append(true).open(&user_reg) {
                let _ = f.write_all(keys.as_bytes());
                log_verbose!("Registry: injected Steam client paths (HKCU)");
            }
        }
    }
}

/// Fast recursive directory copy using getdents64 + hardlinks.
/// getdents64: one syscall fills a 32KB buffer with hundreds of entries.
/// Hardlinks: near-instant, share disk blocks.
/// Falls back to copy on cross-device.
/// Returns number of files deployed.
pub(crate) fn copy_dir_fast(src: &Path, dst: &Path) -> u32 {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;

    let src_c = match CString::new(src.as_os_str().as_bytes()) {
        Ok(c) => c,
        Err(_) => return 0,
    };
    let fd = unsafe { libc::open(src_c.as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY) };
    if fd < 0 {
        log_error!("copy_dir_fast: failed to open {}", src.display());
        return 0;
    }

    if let Err(e) = std::fs::create_dir_all(dst) {
        log_error!("copy_dir_fast: cannot create {}: {e}", dst.display());
        unsafe { libc::close(fd) };
        return 0;
    }

    // 32KB buffer — one syscall gets hundreds of entries
    let mut buf = [0u8; 32 * 1024];
    let mut count = 0u32;

    loop {
        let nread = unsafe {
            libc::syscall(libc::SYS_getdents64, fd, buf.as_mut_ptr(), buf.len()) as isize
        };
        if nread < 0 {
            log_error!("copy_dir_fast: getdents64 failed on {} (errno={})",
                src.display(), std::io::Error::last_os_error());
            break;
        }
        if nread == 0 {
            break; // end of directory
        }

        let mut pos = 0usize;
        while pos < nread as usize {
            // linux_dirent64: d_ino(8) + d_off(8) + d_reclen(2) + d_type(1) + d_name(...)
            let d_reclen = u16::from_ne_bytes([buf[pos + 16], buf[pos + 17]]) as usize;
            let d_type = buf[pos + 18];

            // Extract null-terminated name
            let name_start = pos + 19;
            let name_end = buf[name_start..pos + d_reclen]
                .iter()
                .position(|&b| b == 0)
                .map(|i| name_start + i)
                .unwrap_or(pos + d_reclen);
            let name = &buf[name_start..name_end];

            // Skip . and ..
            if name == b"." || name == b".." {
                pos += d_reclen;
                continue;
            }

            let name_os = std::ffi::OsStr::from_bytes(name);
            let src_path = src.join(name_os);
            let dst_path = dst.join(name_os);

            // DT_UNKNOWN=0, DT_DIR=4, DT_REG=8, DT_LNK=10
            // Some filesystems (NFS, FUSE) report DT_UNKNOWN — fall back to stat
            let d_type = if d_type == 0 {
                match src_path.symlink_metadata() {
                    Ok(m) if m.is_dir() => 4,
                    Ok(m) if m.file_type().is_symlink() => 10,
                    Ok(_) => 8, // regular or other → treat as regular
                    Err(e) => {
                        log_warn!("copy_dir_fast: cannot stat {}: {e}", src_path.display());
                        pos += d_reclen; continue;
                    }
                }
            } else {
                d_type
            };

            if d_type == 4 {
                count += copy_dir_fast(&src_path, &dst_path);
            } else if d_type == 10 {
                // Symlink: template uses relative paths (../../../../../lib/wine/...)
                // that only resolve inside Proton's tree. We must resolve against the
                // source and create absolute symlinks to the real files.
                let needs_fix = if let Ok(meta) = dst_path.symlink_metadata() {
                    if meta.file_type().is_symlink() {
                        // Already a symlink — fix only if broken (target missing)
                        // and fixable: the source now resolves, or the link
                        // differs from the template's. A link dead in the
                        // template too is already the faithful copy.
                        !dst_path.exists()
                            && (std::fs::canonicalize(&src_path).is_ok()
                                || std::fs::read_link(&dst_path).ok() != std::fs::read_link(&src_path).ok())
                    } else {
                        true // regular file where symlink should be
                    }
                } else {
                    true // doesn't exist at all
                };
                if needs_fix {
                    if let Err(e) = std::fs::remove_file(&dst_path) {
                        if e.kind() != std::io::ErrorKind::NotFound {
                            log_warn!("Cannot remove {}: {e}", dst_path.display());
                        }
                    }
                    // Canonicalize resolves the relative symlink in the source tree
                    // to an absolute path pointing at the real file in Proton
                    if let Ok(real_target) = std::fs::canonicalize(&src_path) {
                        if let Err(e) = std::os::unix::fs::symlink(&real_target, &dst_path) {
                            log_warn!("Symlink failed {}: {e}", dst_path.display());
                        }
                    } else if let Ok(target) = std::fs::read_link(&src_path) {
                        // Dead symlink in template — copy as-is
                        if let Err(e) = std::os::unix::fs::symlink(&target, &dst_path) {
                            log_warn!("Symlink failed {}: {e}", dst_path.display());
                        }
                    }
                    count += 1;
                }
            } else if !dst_path.exists() {
                // Regular file: hardlink first (instant), copy fallback
                if std::fs::hard_link(&src_path, &dst_path).is_ok()
                    || std::fs::copy(&src_path, &dst_path).is_ok()
                {
                    count += 1;
                } else {
                    log_warn!("copy_dir_fast: failed to deploy {}", src_path.display());
                }
            }

            pos += d_reclen;
        }
    }

    unsafe { libc::close(fd) };
    count
}

// ---------------------------------------------------------------------------
// DXVK / VKD3D-Proton deployment (conditional)
// ---------------------------------------------------------------------------

fn deploy_dxvk(wine_dir: &Path, pfx: &Path) -> Vec<&'static str> {
    // 64-bit → system32
    let src64 = wine_dir.join("lib/wine/dxvk/x86_64-windows");
    let sys32 = pfx.join("drive_c/windows/system32");
    let deployed = deploy_dlls(&src64, &sys32, DXVK_DLLS, "DXVK");

    // 32-bit → syswow64 (for 32-bit games running under WoW64)
    let src32 = wine_dir.join("lib/wine/dxvk/i386-windows");
    let syswow64 = pfx.join("drive_c/windows/syswow64");
    deploy_dlls(&src32, &syswow64, DXVK_DLLS, "DXVK-32");

    deployed
}

fn deploy_vkd3d(wine_dir: &Path, pfx: &Path) -> Vec<&'static str> {
    // 64-bit → system32
    let src64 = wine_dir.join("lib/wine/vkd3d-proton/x86_64-windows");
    let sys32 = pfx.join("drive_c/windows/system32");
    let deployed = deploy_dlls(&src64, &sys32, VKD3D_DLLS, "VKD3D");

    // 32-bit → syswow64
    let src32 = wine_dir.join("lib/wine/vkd3d-proton/i386-windows");
    let syswow64 = pfx.join("drive_c/windows/syswow64");
    deploy_dlls(&src32, &syswow64, VKD3D_DLLS, "VKD3D-32");

    deployed
}

fn deploy_nvapi(proton_dir: &Path, pfx: &Path) -> Vec<&'static str> {
    let src64 = proton_dir.join("lib/wine/nvapi/x86_64-windows");
    let sys32 = pfx.join("drive_c/windows/system32");
    deploy_dlls(&src64, &sys32, NVAPI_DLLS, "NVAPI")
}

fn deploy_dlss(pfx: &Path) -> Vec<&'static str> {
    let src = PathBuf::from("/usr/lib/nvidia/wine");
    let sys32 = pfx.join("drive_c/windows/system32");
    deploy_dlls(&src, &sys32, DLSS_DLLS, "DLSS")
}

pub(crate) fn deploy_dlls(
    src_dir: &Path, dst_dir: &Path, dlls: &[&'static str], label: &str,
) -> Vec<&'static str> {
    if !src_dir.exists() {
        log_warn!("{label}: source dir not found ({})", src_dir.display());
        return vec![];
    }
    if let Err(e) = std::fs::create_dir_all(dst_dir) {
        log_error!("{label}: cannot create {}: {e}", dst_dir.display());
        return vec![];
    }
    let mut deployed = Vec::new();
    let mut skipped = 0u32;
    for name in dlls {
        let src = src_dir.join(format!("{name}.dll"));
        let dst = dst_dir.join(format!("{name}.dll"));
        if !src.exists() {
            continue;
        }
        // Skip if already deployed and same size
        if file_matches(&src, &dst) {
            skipped += 1;
            deployed.push(*name);
            continue;
        }
        // Remove destination first — hardlinked files from Proton are read-only,
        // and copy() can't overwrite them. Unlinking is always allowed.
        if let Err(e) = std::fs::remove_file(&dst) {
            if dst.exists() {
                log_warn!("{label}: cannot remove old {name}.dll: {e}");
            }
        }
        match std::fs::copy(&src, &dst) {
            Ok(_) => deployed.push(*name),
            Err(e) => log_warn!("{label}: failed to copy {name}.dll: {e}"),
        }
    }
    let copied = deployed.len() as u32 - skipped;
    if copied > 0 {
        log_verbose!("{label}: deployed {copied} DLLs ({skipped} already current)");
    } else if skipped > 0 {
        log_verbose!("{label}: {skipped} DLLs already current");
    }
    deployed
}

/// Check if dst exists and is up-to-date relative to src (same size, dst mtime >= src mtime).
pub(crate) fn file_matches(src: &Path, dst: &Path) -> bool {
    let Ok(src_meta) = std::fs::metadata(src) else { return false };
    let Ok(dst_meta) = std::fs::metadata(dst) else { return false };
    src_meta.len() == dst_meta.len()
        && src_meta.modified().ok() <= dst_meta.modified().ok()
}

// ---------------------------------------------------------------------------
// Steam client integration (conditional)
// ---------------------------------------------------------------------------

/// Valve's own client DLLs into the prefix's Steam dir, as Proton has them.
/// The trampoline (Wine patch 001) treats whatever loads as steamclient64.dll
/// as Valve's native DLL: It fixes up its imports, skips its DllMain and
/// redirects its exports to the builtin lsteamclient it loads beside it. The
/// bridge deployed under that name is the builtin itself, so the skipped
/// DllMain was its own init and no bridge call ever reached Steam.
pub(crate) fn deploy_steam_client(steam_dir: &Path, pfx: &Path) {
    let legacy = steam_dir.join("legacycompat");
    if !legacy.exists() {
        log_warn!("Steam legacycompat not found at {}", legacy.display());
        return;
    }

    let steam_pfx = pfx.join("drive_c/Program Files (x86)/Steam");
    if let Err(e) = std::fs::create_dir_all(&steam_pfx) {
        log_error!("Cannot create Steam client dir: {e}");
        return;
    }
    // Left by the deploy that copied the bridge there as well.
    let stale = pfx.join("drive_c/windows/system32/steamclient64.dll");
    if stale.exists() {
        match std::fs::remove_file(&stale) {
            Ok(_) => log_verbose!("Steam client: removed the bridge copy in system32"),
            Err(e) => log_warn!("Steam client: cannot remove {}: {e}", stale.display()),
        }
    }

    let mut copied = 0u32;
    let mut skipped = 0u32;
    for (src_name, dst_name) in STEAM_CLIENT_FILES {
        let src = legacy.join(src_name);
        let dst = steam_pfx.join(dst_name);
        if !src.exists() {
            continue;
        }
        if file_matches(&src, &dst) {
            skipped += 1;
            continue;
        }
        if let Err(e) = std::fs::remove_file(&dst) {
            if dst.exists() {
                log_warn!("Steam client: cannot remove old {dst_name}: {e}");
            }
        }
        if let Err(e) = std::fs::copy(&src, &dst) {
            log_warn!("Steam client: failed to copy {src_name}: {e}");
        } else {
            copied += 1;
        }
    }
    if copied > 0 {
        log_verbose!("Steam client: deployed {copied} files ({skipped} already current)");
    } else if skipped > 0 {
        log_verbose!("Steam client: {skipped} files already current");
    }
}

/// Ensure steam.exe exists in the prefix's system32 and syswow64.
/// Sources: cached copy (~/.local/share/quark/steam.exe) → Proton (fallback).
fn deploy_steam_exe(self_exe: &Path, pfx: &Path) {
    // Source our steam_bridge from the quark lib directory (deployed by install.py).
    // Always overwrite — Proton's builtin steam.exe may already exist in the prefix,
    // and our native bridge must replace it (WINEDLLOVERRIDES=steam.exe=n).
    let bridge_src = self_exe.parent()
        .map(|d| d.join("lib/wine/x86_64-windows/steam.exe"));

    let src = match bridge_src {
        Some(ref p) if p.exists() => p,
        _ => {
            log_warn!("steam_bridge not found — games requiring Steam API will fail");
            return;
        }
    };

    let sys32 = pfx.join("drive_c/windows/system32");
    let syswow64 = pfx.join("drive_c/windows/syswow64");

    for dst_dir in [&sys32, &syswow64] {
        let dst = dst_dir.join("steam.exe");
        // Clean up broken symlinks
        if dst.symlink_metadata().is_ok() && !dst.exists() {
            let _ = std::fs::remove_file(&dst);
        }
        let _ = std::fs::create_dir_all(dst_dir);
        if file_matches(src, &dst) { continue; }  // already current; cheap re-deploy no-op
        if let Err(e) = std::fs::copy(src, &dst) {
            log_warn!("steam.exe: failed to deploy to {}: {e}", dst_dir.display());
        }
    }
}

// ---------------------------------------------------------------------------
// Environment construction
// ---------------------------------------------------------------------------

fn deploy_lsteamclient_to_prefix(self_exe: &Path, pfx: &Path) {
    let src = self_exe.parent()
        .map(|d| d.join("lib/wine/x86_64-windows/lsteamclient.dll"));
    let src = match src {
        Some(ref p) if p.exists() => p,
        _ => return,
    };
    let sys32 = pfx.join("drive_c/windows/system32");
    let _ = std::fs::create_dir_all(&sys32);
    let dst = sys32.join("lsteamclient.dll");
    if file_matches(src, &dst) { return; }  // already current; cheap re-deploy no-op
    if let Err(e) = std::fs::copy(src, &dst) {
        log_warn!("lsteamclient.dll: failed to deploy to system32: {e}");
    }
}

/// WINEDLLPATH: the Wine tree's PE and unix module dirs, then system Wine's.
/// quark-built modules (lsteamclient, steam.exe) live in the Wine tree;
/// without them on the path LoadLibraryW("lsteamclient") gets an empty stub
/// and Steam auth fails.
pub(crate) fn wine_dll_path(wine_dir: &Path) -> String {
    [
        wine_dir.join("lib/wine/x86_64-unix"),
        wine_dir.join("lib/wine/x86_64-windows"),
        PathBuf::from("/usr/lib/wine/x86_64-unix"),
        PathBuf::from("/usr/lib/wine/x86_64-windows"),
    ]
    .iter()
    .filter(|p| p.exists())
    .map(|p| p.display().to_string())
    .collect::<Vec<_>>()
    .join(":")
}

/// LD_LIBRARY_PATH: Wine's unix modules list ntdll.so as NEEDED, so the
/// x86_64-unix dir must resolve by name, ahead of the inherited path.
pub(crate) fn wine_ld_path(wine_dir: &Path) -> String {
    let mut parts: Vec<String> = [wine_dir.join("lib/wine/x86_64-unix"), wine_dir.join("lib")]
        .iter()
        .filter(|p| p.exists())
        .map(|p| p.display().to_string())
        .collect();
    if let Ok(existing) = std::env::var("LD_LIBRARY_PATH") {
        parts.push(existing);
    }
    parts.join(":")
}

pub(crate) fn build_env_vars(
    wine_dir: &Path, pfx: &Path,
    dxvk: &[&str], vkd3d: &[&str], nvapi: &[&str], dlss: &[&str],
    shader_cache_enabled: bool,
) -> Vec<(&'static str, String)> {
    let cur_path = std::env::var("PATH").unwrap_or_default();
    let wine_bin = wine_dir.join("bin");

    // WINEDLLOVERRIDES: base + deployed DLLs
    let mut overrides: Vec<String> = Vec::new();
    for (name, mode) in BASE_OVERRIDES {
        overrides.push(format!("{name}={mode}"));
    }
    for name in dxvk { overrides.push(format!("{name}=n")); }
    for name in vkd3d { overrides.push(format!("{name}=n")); }
    for name in nvapi { overrides.push(format!("{name}=n")); }
    for name in dlss { overrides.push(format!("{name}=n")); }
    if !nvapi.is_empty() { overrides.push("nvcuda=b".into()); }
    let dll_overrides = overrides.join(";");

    // WINELOADER: Wine child processes re-exec via this path. Without it,
    // fork+exec fails with "could not exec the wine loader".
    let wine64 = wine_binary(wine_dir);

    // Wine NT-path environment variables. Wine's ntdll uses these to find
    // DLLs, shared data, and the prefix. Format: \\??\\Z:\\unix\\path
    let home = std::env::var("HOME").unwrap_or_else(|_| "/home/user".into());
    // NT path prefix: \??\Z: (one backslash, ??, one backslash, Z:)
    let nt_prefix = "\\??\\Z:";
    let wine_data_dir = format!("{}{}", nt_prefix, wine_dir.join("share/wine").display().to_string().replace('/', "\\"));
    let wine_dll_dir0 = format!("{}{}", nt_prefix, wine_dir.join("lib/wine").display().to_string().replace('/', "\\"));
    let wine_config_dir = format!("{}{}", nt_prefix, pfx.display().to_string().replace('/', "\\"));
    let wine_home_dir = format!("{}{}", nt_prefix, home.replace('/', "\\"));
    let user = profile_user(pfx);

    let mut vars = vec![
        ("WINEPREFIX", pfx.display().to_string()),
        ("WINESERVER", wineserver_exe(wine_dir).display().to_string()),
        ("WINELOADER", wine64.display().to_string()),
        ("WINEDLLPATH", wine_dll_path(wine_dir)),
        ("WINEDATADIR", wine_data_dir),
        ("WINEDLLDIR0", wine_dll_dir0.clone()),
        ("WINEDLLDIR1", wine_dll_dir0), // vkd3d also searched here
        ("WINECONFIGDIR", wine_config_dir),
        ("WINEHOMEDIR", wine_home_dir),
        // Wine names its user after $USER: ntdll's user_name, the
        // WINEUSERNAME it appends (which GetUserNameW returns, and which
        // shell32 writes into every %USERPROFILE% shell folder) and the
        // profile wineboot creates. The prefix's own profile is the one
        // source; the host user would split the prefix between two homes.
        ("USER", user.clone()),
        ("PATH", format!("{}:{}", wine_bin.display(), cur_path)),
        ("LD_LIBRARY_PATH", wine_ld_path(wine_dir)),
        ("WINEDLLOVERRIDES", dll_overrides),
        ("DXVK_LOG_LEVEL", std::env::var("DXVK_LOG_LEVEL").unwrap_or_else(|_| "none".into())),
        ("VKD3D_DEBUG", "none".into()),
        ("WINE_LARGE_ADDRESS_AWARE", "1".into()),
        ("PROTON_VERSION", format!("quark {}", env!("CARGO_PKG_VERSION"))),

    ];

    // EAC runtime: its v2 lib dirs hold the builtin bridge DLLs, searched
    // after the Wine tree's, where Proton's ntdll adds them.
    let eac_runtime = PathBuf::from(&home)
        .join(".local/share/Steam/steamapps/common/Proton EasyAntiCheat Runtime");
    if eac_runtime.exists() {
        if let Some((_, dll_path)) = vars.iter_mut().find(|(k, _)| *k == "WINEDLLPATH") {
            for sub in ["v2/lib32", "v2/lib64"] {
                dll_path.push(':');
                dll_path.push_str(&eac_runtime.join(sub).display().to_string());
            }
        }
        vars.push(("PROTON_EAC_RUNTIME", eac_runtime.display().to_string()));
    }

    // Windows profile environment variables — Wine's SHGetFolderPathW needs these
    // to resolve %APPDATA% etc. Without registry-backed ProfileList, the fallback
    // chain fails and games (e.g. LOVE2D) throw exceptions during filesystem init.
    let profile = format!("C:\\users\\{user}");
    vars.push(("USERPROFILE", profile.clone()));
    vars.push(("APPDATA", format!("{profile}\\AppData\\Roaming")));
    vars.push(("LOCALAPPDATA", format!("{profile}\\AppData\\Local")));
    vars.push(("HOMEDRIVE", "C:".into()));
    vars.push(("HOMEPATH", format!("\\users\\{user}")));
    vars.push(("ALLUSERSPROFILE", "C:\\ProgramData".into()));
    vars.push(("ProgramData", "C:\\ProgramData".into()));

    // Async shader compilation
    vars.push(("DXVK_ASYNC", "1".into()));
    vars.push(("VKD3D_CONFIG", "shader_cache".into()));

    // Sync: ntsync inproc — send device fd via init_first_thread.
    // Client does all sync in-process via /dev/ntsync ioctl.
    // Server-side ntsync used for Select waits that need cross-process coordination.
    
    if std::path::Path::new("/dev/ntsync").exists() {
        vars.push(("WINE_NTSYNC", "1".into()));
    }
    vars.push(("WINEESYNC", "0".into()));
    vars.push(("WINEFSYNC", "0".into()));

    // NVIDIA: DXVK NVAPI support for DLSS/NGX
    if std::path::Path::new("/proc/driver/nvidia/version").exists() {
        vars.push(("DXVK_ENABLE_NVAPI", "1".into()));
        // NVIDIA EGL/GLX — force NVIDIA's implementations, not mesa's
        let nvidia_egl = "/usr/share/glvnd/egl_vendor.d/10_nvidia.json";
        if std::path::Path::new(nvidia_egl).exists() {
            vars.push(("__EGL_VENDOR_LIBRARY_FILENAMES", nvidia_egl.into()));
        }
        vars.push(("__GLX_VENDOR_LIBRARY_NAME", "nvidia".into()));
        // PRIME render offload for NVIDIA Optimus hybrid laptops. Without
        // these, Vulkan only enumerates the integrated iGPU (we saw
        // "vulkan-only gpu Intel(R) HD Graphics 630" in the log on
        // a system with a GTX 1050 Mobile dGPU). DXVK then runs on the
        // weak iGPU and games can spin in display-config polling loops
        // waiting for capabilities the iGPU lacks.
        vars.push(("__NV_PRIME_RENDER_OFFLOAD", "1".into()));
        vars.push(("__VK_LAYER_NV_optimus", "NVIDIA_only".into()));
    }

    // Proton compatibility fixes
    // Don't set WINEARCH — Wine rejects empty string. System Wine defaults to win64.
    // Restore locale (Steam sets LC_ALL=C which breaks Wine path conversion)
    if let Ok(host_lc) = std::env::var("HOST_LC_ALL") {
        if !host_lc.is_empty() {
            vars.push(("LC_ALL", host_lc));
        }
    } else {
        vars.push(("LC_ALL", "".into())); // clear Steam's LC_ALL=C
    }
    // Save original LD_LIBRARY_PATH for Wine's external app calls
    if std::env::var("ORIG_LD_LIBRARY_PATH").is_err() {
        vars.push(("ORIG_LD_LIBRARY_PATH",
            std::env::var("LD_LIBRARY_PATH").unwrap_or_default()));
    }


    // Steam Input: SDL 2.30+ reads SteamVirtualGamepadInfo to configure
    // virtual gamepads. Steam sets SteamVirtualGamepadInfo_Proton before
    // launching compat tools — Proton copies it to SteamVirtualGamepadInfo.
    // Without this, controllers are invisible to SDL-based games.
    if let Ok(gamepad_info) = std::env::var("SteamVirtualGamepadInfo_Proton") {
        vars.push(("SteamVirtualGamepadInfo", gamepad_info));
    }

    // Display driver: always winex11.drv + GLX over XWayland.

    // Shader cache optimization — opt-in via install.py prompt.
    if shader_cache_enabled {
        let shader_cache = pfx.join("shader_cache");
        if let Err(e) = std::fs::create_dir_all(&shader_cache) {
            log_warn!("Cannot create shader cache dir: {e}");
        }
        let shader_cache_str = shader_cache.display().to_string();

        // DXVK: compiled SPIR-V cache (DXBC/DXSO → SPIR-V translation results)
        vars.push(("DXVK_SHADER_CACHE_PATH", shader_cache_str.clone()));
        // VKD3D-Proton: compiled SPIR-V cache (DXBC/DXIL → SPIR-V translation results)
        vars.push(("VKD3D_SHADER_CACHE_PATH", shader_cache_str.clone()));

        // Detect GPU vendor for vendor-specific cache tuning.
        let is_nvidia = Path::new("/proc/driver/nvidia/version").exists();

        if is_nvidia {
            // NVIDIA: driver compiles SPIR-V → GPU ISA, caches in GLCache.
            // Default was 128 MB, recently raised to 1 GB — still too small for
            // AAA games with 5,000+ pipelines. 10 GB prevents eviction.
            vars.push(("__GL_SHADER_DISK_CACHE", "1".into()));
            vars.push(("__GL_SHADER_DISK_CACHE_PATH", shader_cache_str.clone()));
            vars.push(("__GL_SHADER_DISK_CACHE_SIZE", "10737418240".into())); // 10 GB
            vars.push(("__GL_SHADER_DISK_CACHE_SKIP_CLEANUP", "1".into()));
        } else {
            // Mesa/RADV (AMD, Intel): driver compiles SPIR-V → GPU ISA, caches
            // in mesa_shader_cache. Default size is small. 10 GB prevents eviction
            // across sessions. Single-file mode is Fossilize-compatible and has
            // less disk overhead than multi-file mode.
            vars.push(("MESA_SHADER_CACHE_DIR", shader_cache_str.clone()));
            vars.push(("MESA_SHADER_CACHE_MAX_SIZE", "10G".into()));
            vars.push(("MESA_DISK_CACHE_SINGLE_FILE", "1".into()));
            // RADV: Graphics Pipeline Library — allows partial pipeline
            // pre-compilation so full pipeline assembly at draw time is
            // near-instant instead of compiling from scratch.
            vars.push(("RADV_PERFTEST", "gpl".into()));
        }
    }

    vars
}

/// Parse user-supplied environment variables from env_config file.
/// Format: KEY=VALUE lines. # comments and blank lines are ignored.
/// Returns empty Vec if the file is missing or unreadable.
pub(crate) fn parse_env_config(self_exe: &Path) -> Vec<(String, String)> {
    let config_path = match self_exe.parent() {
        Some(dir) => dir.join("env_config"),
        None => return Vec::new(),
    };

    let contents = match std::fs::read_to_string(&config_path) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };

    let mut vars = Vec::new();
    for raw_line in contents.lines() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some(eq_pos) = line.find('=') else {
            log_warn!("env_config: ignoring malformed line: {line}");
            continue;
        };
        let key = line[..eq_pos].trim();
        let value = line[eq_pos + 1..].trim();
        if key.is_empty() {
            continue;
        }
        vars.push((key.to_string(), value.to_string()));
    }

    if !vars.is_empty() {
        log_verbose!("env_config: loaded {} custom variable(s)", vars.len());
    }

    vars
}

/// Resolve WINEDEBUG value once. Called after env_config is applied.
/// Priority: env_config/shell > QUARK_TRACE_OPCODES > --verbose > default.
fn resolve_winedebug(trace: bool) -> String {
    // If the user (or env_config) explicitly set WINEDEBUG, respect it.
    if let Ok(val) = std::env::var("WINEDEBUG") {
        return val;
    }
    if trace {
        "+server,+timestamp".into()
    } else if crate::log::is_verbose() {
        "+process,err".into()
    } else {
        "-all".into()
    }
}

// Discovery

/// Check if a Wine directory has a usable Wine binary.
/// Wine 10.0+ unified to just "wine"; older builds use "wine64".
pub(crate) fn has_wine_bin(dir: &Path) -> bool {
    dir.join("lib/wine/x86_64-unix/wine").exists()
        || dir.join("bin/wine64").exists()
        || dir.join("bin/wine").exists()
}

/// Get the Wine binary path from a Wine directory.
/// Prefers the wine binary alongside ntdll.so — Wine resolves PE builtins
/// relative to ntdll.so's realpath, so the binary MUST be in the same
/// directory as ntdll.so for our patched ntdll.dll to be loaded.
pub(crate) fn wine_binary(dir: &Path) -> PathBuf {
    let unix_wine = dir.join("lib/wine/x86_64-unix/wine");
    if unix_wine.exists() { return unix_wine; }
    let w64 = dir.join("bin/wine64");
    if w64.exists() { w64 } else { dir.join("bin/wine") }
}

/// Build a Command that runs `wine_bin` inside a bwrap mount namespace where
/// `/usr/lib/wine` and `/usr/share/wine` are bind-mounted from quark's tree.
///
/// This is the architectural fix for "patched DLLs got poisoned in /usr."
/// Wine's loader has /usr/lib/wine baked in at compile time and ignores most
/// override knobs for the early bootstrap (ntdll.so, win32u.so). bwrap solves
/// it at the kernel level: inside the namespace, /usr/lib/wine *is* quark's
/// tree, so the unmodified system Wine binary loads quark's patched DLLs
/// without ever touching the real /usr.
///
/// Falls back to direct exec (no bwrap) if:
///   - bwrap is not installed
///   - quark's lib/wine or share/wine tree is missing (install.py never ran)
///
/// In the fallback case the user runs against unpatched system Wine, which
/// is ugly but not poisonous.
fn build_wine_command(wine_bin: &Path, wine_dir: &Path) -> Command {
    let bwrap = Path::new("/usr/bin/bwrap");
    let quark_lib_wine = wine_dir.join("lib/wine");
    let quark_share_wine = wine_dir.join("share/wine");

    if !bwrap.exists() {
        log_warn!("bwrap not found at /usr/bin/bwrap — exec'ing wine directly.");
        log_warn!("Patched DLLs will NOT be loaded. Install bubblewrap (pacman -S bubblewrap).");
        return Command::new(wine_bin);
    }
    if !quark_lib_wine.exists() || !quark_share_wine.exists() {
        log_warn!("quark Wine tree incomplete (missing {} or {}) — exec'ing wine directly.",
            quark_lib_wine.display(), quark_share_wine.display());
        log_warn!("Re-run install.py to populate the compat tree.");
        return Command::new(wine_bin);
    }

    let mut cmd = Command::new(bwrap);
    // Pass through the entire host filesystem first.
    cmd.arg("--dev-bind").arg("/").arg("/");
    // Overlay quark's Wine tree on top. Inside the namespace, when Wine's
    // loader opens /usr/lib/wine/x86_64-unix/ntdll.so it gets quark's patched
    // copy. The actual /usr on disk is never touched.
    cmd.arg("--bind").arg(&quark_lib_wine).arg("/usr/lib/wine");
    cmd.arg("--bind").arg(&quark_share_wine).arg("/usr/share/wine");
    // bwrap exits when its parent (this launcher) exits. Belt-and-braces
    // alongside the existing death-pipe.
    cmd.arg("--die-with-parent");
    cmd.arg("--");
    cmd.arg(wine_bin);
    cmd
}

/// Find Wine binaries. Priority:
/// 1. QUARK_WINE_DIR env var (explicit override)
/// 2. quark's own staged binaries — system Wine copied here by
///    install.py so argv[0]/../lib/ resolves to our custom ntdll.so
///    (built from matching upstream Wine version)
/// 3. System Wine
/// 4. Proton Experimental
/// 5. Any Proton version
fn find_wine() -> PathBuf {
    if let Ok(dir) = std::env::var("QUARK_WINE_DIR") {
        let p = PathBuf::from(dir);
        if has_wine_bin(&p) {
            return p;
        }
    }

    // quark's staged wine binaries — uses our patched ntdll.so
    if let Ok(self_exe) = std::env::current_exe() {
        if let Some(self_dir) = self_exe.parent() {
            if has_wine_bin(self_dir)
                && self_dir.join("lib/wine/x86_64-unix/ntdll.so").exists()
            {
                return self_dir.to_path_buf();
            }
        }
    }

    let home = std::env::var("HOME").unwrap_or_default();

    if has_wine_bin(&PathBuf::from("/usr")) {
        return PathBuf::from("/usr");
    }

    let proton_exp = PathBuf::from(&home)
        .join(".steam/root/steamapps/common/Proton - Experimental/files");
    if has_wine_bin(&proton_exp) {
        log_warn!("system Wine not found — using Proton Experimental's wine");
        return proton_exp;
    }

    let common = PathBuf::from(&home).join(".steam/root/steamapps/common");
    if let Ok(entries) = std::fs::read_dir(&common) {
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            if name.starts_with("Proton") {
                let files = entry.path().join("files");
                if has_wine_bin(&files) {
                    log_warn!("system Wine not found — using {name}'s wine");
                    return files;
                }
            }
        }
    }

    PathBuf::from("/nonexistent/wine")
}

/// Find Proton's files directory for steam.exe sourcing (fallback).
/// DXVK/VKD3D are now deployed directly by install.py.
fn find_proton_files() -> Option<PathBuf> {
    let home = std::env::var("HOME").unwrap_or_default();

    // Proton Experimental
    let proton_exp = PathBuf::from(&home)
        .join(".steam/root/steamapps/common/Proton - Experimental/files");
    if proton_exp.join("lib/wine").exists() {
        return Some(proton_exp);
    }

    // Any Proton version
    let common = PathBuf::from(&home).join(".steam/root/steamapps/common");
    if let Ok(entries) = std::fs::read_dir(&common) {
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            if name.starts_with("Proton") {
                let files = entry.path().join("files");
                if files.join("lib/wine").exists() {
                    return Some(files);
                }
            }
        }
    }

    None
}

/// Find Steam installation directory.
fn find_steam_dir() -> PathBuf {
    // Steam sets this when launching compat tools
    if let Ok(dir) = std::env::var("STEAM_COMPAT_CLIENT_INSTALL_PATH") {
        let p = PathBuf::from(&dir);
        if p.join("linux64/steamclient.so").exists() {
            return p;
        }
    }

    let home = std::env::var("HOME").unwrap_or_default();

    // Standard Steam symlink
    let steam = PathBuf::from(&home).join(".steam/root");
    if steam.join("linux64/steamclient.so").exists() {
        return steam;
    }

    // Flatpak / alternative
    let alt = PathBuf::from(&home).join(".local/share/Steam");
    if alt.join("linux64/steamclient.so").exists() {
        return alt;
    }

    log_warn!("Steam directory not found — game may not connect to Steam");
    PathBuf::from(&home).join(".steam/root")
}

/// The user the prefix belongs to: whose profile its registry's ProfileList
/// names, which is the template's (Proton bakes steamuser) or wineboot's.
/// A prefix with no registry yet is about to be created for this name.
pub(crate) fn profile_user(pfx: &Path) -> String {
    use std::io::BufRead;
    if let Ok(f) = std::fs::File::open(pfx.join("system.reg")) {
        for line in std::io::BufReader::new(f).lines().map_while(Result::ok) {
            if let Some(path) = line.strip_prefix("\"ProfileImagePath\"=\"") {
                if let Some(name) = path.trim_end_matches('"').rsplit("\\\\").next() {
                    if !name.is_empty() {
                        return name.to_string();
                    }
                }
            }
        }
    }
    "steamuser".to_string()
}

/// The prefix's user profile, `drive_c/users/<profile_user>`.
fn user_dir(pfx: &Path) -> PathBuf {
    pfx.join("drive_c/users").join(profile_user(pfx))
}

/// Create the standard Windows user-profile directory tree in the prefix.
/// Idempotent (create_dir_all no-ops when present); games and wineboot's
/// SHGetFolderPath both need these to exist.
fn ensure_user_profile_dirs(pfx: &Path) {
    let user_dir = user_dir(pfx);
    for subdir in [
        "", "AppData", "AppData/Roaming", "AppData/Local",
        "AppData/Local/Microsoft", "AppData/Local/Microsoft/Windows",
        "AppData/Local/Microsoft/Windows/INetCache",
        "AppData/Local/Microsoft/Windows/History",
        "AppData/Local/Microsoft/Windows/INetCookies",
        "AppData/LocalLow", "Desktop", "Documents", "Downloads",
        "Music", "Pictures", "Videos", "Temp",
    ] {
        let _ = std::fs::create_dir_all(user_dir.join(subdir));
    }
    let public_dir = pfx.join("drive_c/users/Public");
    let _ = std::fs::create_dir_all(public_dir.join("Desktop"));
    let _ = std::fs::create_dir_all(public_dir.join("Documents"));
}

// ---------------------------------------------------------------------------
// Save data protection
// ---------------------------------------------------------------------------
// Snapshot save data before launch, restore any files that go missing.
// Prevents Steam Cloud sync from wiping saves on first launch with a new
// compatibility tool (empty registry → game thinks first run → sync conflict).

const SAVE_SCAN_DIRS: &[&str] = &[
    "AppData/Roaming",
    "AppData/Local",
    "AppData/LocalLow",
    "Documents",
];

const SAVE_SKIP_DIRS: &[&str] = &["Microsoft", "Temp"];

/// Snapshot all save data under the prefix before game launch.
/// Returns the backup directory path if anything was backed up.
pub(crate) fn snapshot_save_data(pfx: &Path) -> Option<PathBuf> {
    let user_dir = user_dir(pfx);
    if !user_dir.exists() {
        return None;
    }

    // Backup dir lives in $STEAM_COMPAT_DATA_PATH (parent of pfx)
    let backup_dir = pfx.parent()?.join("save_backup");

    // Remove old backup — we only keep the latest snapshot
    if backup_dir.exists() {
        if let Err(e) = std::fs::remove_dir_all(&backup_dir) {
            log_warn!("save backup: cannot remove old backup: {e}");
        }
    }

    let mut total_files = 0u32;
    let mut total_bytes = 0u64;

    for scan_dir in SAVE_SCAN_DIRS {
        let src = user_dir.join(scan_dir);
        if !src.is_dir() {
            continue;
        }

        // Scan subdirectories (game-specific folders like "FromSoftware/EldenRing")
        let entries = match std::fs::read_dir(&src) {
            Ok(e) => e,
            Err(_) => continue,
        };

        for entry in entries.flatten() {
            let name = entry.file_name();
            let name_str = name.to_string_lossy();

            // Skip non-save system directories
            if SAVE_SKIP_DIRS.iter().any(|s| name_str.eq_ignore_ascii_case(s)) {
                continue;
            }

            let entry_path = entry.path();
            if !entry_path.is_dir() {
                continue;
            }

            // Only backup dirs that contain actual files
            let (files, bytes) = count_files_recursive(&entry_path);
            if files == 0 {
                continue;
            }

            let dst = backup_dir.join(scan_dir).join(&name);
            copy_save_recursive(&entry_path, &dst);
            total_files += files;
            total_bytes += bytes;
        }
    }

    if total_files == 0 {
        return None;
    }

    // Write manifest for diagnostics
    let manifest = format!("{total_files} files, {total_bytes} bytes\n");
    if let Err(e) = std::fs::write(backup_dir.join("manifest.txt"), manifest) {
        log_warn!("save backup: cannot write manifest: {e}");
    }

    log_verbose!("save data snapshot: {total_files} files, {total_bytes} bytes");

    Some(backup_dir)
}

/// After game exits, check if any save files that existed pre-launch are now
/// missing. Restore only those — never overwrite saves the game just wrote.
pub(crate) fn restore_save_data(pfx: &Path, backup_dir: &Path) {
    if !backup_dir.exists() {
        return;
    }

    let user_dir = user_dir(pfx);
    let mut restored = 0u32;
    let mut unchanged = 0u32;

    for scan_dir in SAVE_SCAN_DIRS {
        let backup_scan = backup_dir.join(scan_dir);
        if !backup_scan.is_dir() {
            continue;
        }

        let entries = match std::fs::read_dir(&backup_scan) {
            Ok(e) => e,
            Err(_) => continue,
        };

        for entry in entries.flatten() {
            let backup_path = entry.path();
            if !backup_path.is_dir() {
                continue;
            }

            let original_path = user_dir.join(scan_dir).join(entry.file_name());
            let (r, u) = restore_missing_files(&backup_path, &original_path);
            restored += r;
            unchanged += u;
        }
    }

    log_verbose!("save data check: {restored} files restored, {unchanged} unchanged");

    if restored > 0 {
        // Keep backup as extra safety layer
        log_warn!("{restored} save files were missing after game exit — restored from backup");
    } else {
        // All good — clean up backup
        if let Err(e) = std::fs::remove_dir_all(backup_dir) {
            log_warn!("save backup: cleanup failed: {e}");
        }
    }
}

/// Recursively restore files that exist in backup but are missing from original.
fn restore_missing_files(backup: &Path, original: &Path) -> (u32, u32) {
    let mut restored = 0u32;
    let mut unchanged = 0u32;

    let entries = match std::fs::read_dir(backup) {
        Ok(e) => e,
        Err(_) => return (0, 0),
    };

    for entry in entries.flatten() {
        let backup_file = entry.path();
        let original_file = original.join(entry.file_name());

        if backup_file.is_dir() {
            let (r, u) = restore_missing_files(&backup_file, &original_file);
            restored += r;
            unchanged += u;
        } else {
            if original_file.exists() {
                unchanged += 1;
            } else {
                // File existed pre-launch but is now gone — restore it
                if let Some(parent) = original_file.parent() {
                    if let Err(e) = std::fs::create_dir_all(parent) {
                        log_warn!("save restore: cannot create dir {}: {e}", parent.display());
                    }
                }
                match std::fs::copy(&backup_file, &original_file) {
                    Ok(_) => restored += 1,
                    Err(e) => log_warn!("save restore: failed to restore {}: {e}", original_file.display()),
                }
            }
        }
    }

    (restored, unchanged)
}

/// Count files and total bytes in a directory tree.
fn count_files_recursive(dir: &Path) -> (u32, u64) {
    let mut files = 0u32;
    let mut bytes = 0u64;

    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => return (0, 0),
    };

    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            let (f, b) = count_files_recursive(&path);
            files += f;
            bytes += b;
        } else {
            files += 1;
            bytes += entry.metadata().map(|m| m.len()).unwrap_or(0);
        }
    }

    (files, bytes)
}

/// Recursive copy for save data (actual copies, not hardlinks).
/// Save data is small (KB to low MB) so std::fs::copy is fine.
fn copy_save_recursive(src: &Path, dst: &Path) {
    if let Err(e) = std::fs::create_dir_all(dst) {
        log_warn!("save backup: cannot create dir {}: {e}", dst.display());
        return;
    }

    let entries = match std::fs::read_dir(src) {
        Ok(e) => e,
        Err(_) => return,
    };

    for entry in entries.flatten() {
        let src_path = entry.path();
        let dst_path = dst.join(entry.file_name());

        if src_path.is_dir() {
            copy_save_recursive(&src_path, &dst_path);
        } else if let Err(e) = std::fs::copy(&src_path, &dst_path) {
            log_warn!("save backup: failed to copy {}: {e}", src_path.display());
        }
    }
}

/// Remove .dmp and .mdmp crash dumps from the prefix before launch.
/// Engines (Godot, Unity, Unreal) read their crash directories on startup
/// and may enter recovery/report paths that interfere with normal launch.
fn clean_crash_dumps(pfx: &Path) {
    let user_dir = user_dir(pfx);
    if !user_dir.exists() {
        return;
    }

    let mut removed = 0u32;
    for scan_dir in SAVE_SCAN_DIRS {
        let dir = user_dir.join(scan_dir);
        if dir.is_dir() {
            removed += remove_dumps_recursive(&dir);
        }
    }
    if removed > 0 {
        log_verbose!("cleaned {removed} stale crash dump(s)");
    }
}

fn remove_dumps_recursive(dir: &Path) -> u32 {
    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => return 0,
    };
    let mut removed = 0u32;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            removed += remove_dumps_recursive(&path);
        } else if let Some(ext) = path.extension() {
            let ext = ext.to_string_lossy();
            if (ext.eq_ignore_ascii_case("dmp") || ext.eq_ignore_ascii_case("mdmp"))
                && std::fs::remove_file(&path).is_ok()
            {
                removed += 1;
            }
        }
    }
    removed
}
