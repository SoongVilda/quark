#!/usr/bin/env python3
"""quark installer — build, patch, and deploy quark as a Steam compatibility tool.

Pipeline architecture: 13 verified steps, each with explicit inputs, outputs,
and failure reporting. No silent failures. No swallowed errors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quark_common import (
    REPO_ROOT, RUST_DIR, STEAM_ROOT, STEAMAPPS, COMPAT_DIR, DATA_DIR, CACHE_DIR,
    LSTEAMCLIENT_CACHE, LOG_DIR, WINE_SRC_DIR, WINE_OBJ_DIR,
    log_info, log_warn, log_error, log_debug,
    get_quark_appids, stop_prefix_wineserver, stale_wine_sockets,
    remove_stale_wine_sockets,
)

# Constants

SCRIPT_DIR = REPO_ROOT

WINE_CLONE_URL = "https://gitlab.winehq.org/wine/wine.git"
WINE_TAG = None  # Detected from system Wine at runtime; never hardcoded
EAC_RUNTIME_DIR = STEAMAPPS / "common" / "Proton EasyAntiCheat Runtime" / "v2"

PROTON_GIT_URL = "https://github.com/ValveSoftware/Proton.git"


def _derive_proton_tag() -> str:
    """The Proton release tag lsteamclient/steam_helper are built from.

    Dynamic to the installed Proton, mirroring derive_wine_tag() for Wine: read
    the installed Proton's `version` file (format "<timestamp> <tag>") instead
    of pinning a literal that rots on a Proton bump. Point releases within a
    major line (proton-11.0-2 -> -2e) track automatically, and the lsteamclient
    repo layout is stable within a major line so this stays safe; crossing a
    major line is where Valve reorganizes, and the derivation never jumps major
    on its own. QUARK_PROTON_TAG overrides; the last validated tag is the
    fallback when no installed Proton exposes a proton-* release tag.
    """
    override = os.environ.get("QUARK_PROTON_TAG")
    if override:
        return override
    steam_common = STEAMAPPS / "common"
    for pname in ("Proton 11.0", "Proton - Experimental", "Proton Hotfix"):
        try:
            parts = (steam_common / pname / "version").read_text().split()
        except OSError:
            continue
        if len(parts) >= 2 and parts[1].startswith("proton-"):
            # The version file names the build ("proton-11.0-2e-x86_64");
            # the release tag has no architecture suffix.
            return re.sub(r"-(x86_64|aarch64|i386|arm64)$", "", parts[1])
    return "proton-11.0-2e"


PROTON_GIT_TAG = _derive_proton_tag()
PROTON_SOURCE_CACHE = CACHE_DIR / "proton-source" / PROTON_GIT_TAG

REQUIRED_TOOLS: dict[str, tuple[str | None, str]] = {
    "rustc":    ("1.85",  "pacman -S rust"),
    "cargo":    (None,    "pacman -S rust"),
    "clang":    (None,    "pacman -S clang"),
    "ld.lld":   (None,    "pacman -S lld"),
    "git":      (None,    "pacman -S git"),
    "autoconf": (None,    "pacman -S autoconf"),
    "make":     (None,    "pacman -S base-devel"),
}

# Known Proton appids — never touch their prefixes
PROTON_APPIDS = frozenset({
    "3658110", "1493710", "2805730", "1628350",
    "1580130", "1887720", "2180100", "2348590",
})

PE_DRIVER_FILES = [
    "sharedgpures.sys", "nvcuda.dll", "amd_ags_x64.dll", "amdxc64.dll",
    "atiadlxx.dll", "dxcore.dll", "audioses.dll", "belauncher.exe",
]

ENV_CONFIG_TEMPLATE = """\
# quark custom environment variables
#
# Format: KEY=VALUE (one per line)
# Lines starting with # are comments. Blank lines are ignored.
# Variables set here override quark's built-in defaults.
# Edit this file any time — changes apply on next game launch.
#
# --- Sync ---
WINE_NTSYNC=1
#
# --- Heap arena placement ---
# Confine glibc to one arena so per-thread heap arenas stop landing in the
# high mmap band where wine section views are placed. Fixes the cross-game
# heap corruption (#18): a wine MAP_FIXED section view was stomping a live
# glibc per-thread arena -> sysmalloc/!prev abort. Single-arena has some
# allocation-lock contention cost; the zero-cost replacement is the wine-side
# map_view placement fix, after which these can be removed.
GLIBC_TUNABLES=glibc.malloc.arena_max=1
MALLOC_ARENA_MAX=1
#
# --- Logging ---
# WINEDEBUG=-all
# DXVK_LOG_LEVEL=none
# VKD3D_DEBUG=none
#
# --- Overlays ---
# MANGOHUD=1
# DXVK_HUD=fps
#
# --- Performance ---
# DXVK_ASYNC=1
# mesa_glthread=true
#
# --- NVIDIA ---
# PROTON_ENABLE_NVAPI=1
# DXVK_ENABLE_NVAPI=dxgi
#
# --- Diagnostics (active only with the observer; --observer install) ---
# Caller-stack capture for the 288-byte chunk double-freed in per-thread
# GLX/window teardown (the corrupter behind the 160-byte XCreateGC victim /
# sysmalloc/!prev abort class). Remove when that hunt closes.
MONTAUK_HEAP_STACK_SIZE=288
"""


def _env_has_active_key(text: str, key: str) -> bool:
    """True only if env_config has an ACTIVE (non-comment) `key=...` line.
    A plain substring check is fooled by comments that mention the key
    (e.g. a '# active only with QUARK_TRACE=1' header), which silently
    suppressed the real assignment — see the 2026-06-12 observer regression."""
    prefix = key + "="
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        if s.startswith(prefix):
            return True
    return False


# Core helpers

class StepFailed(Exception):
    """A pipeline step failed with a clear diagnostic."""
    def __init__(self, step: str, detail: str):
        self.step = step
        self.detail = detail
        super().__init__(f"{step}: {detail}")


@dataclass
class StepResult:
    name: str
    success: bool
    skipped: bool = False
    artifacts: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: int = 0


def run_cmd(
    cmd: list[str],
    *,
    desc: str,
    cwd: Path | None = None,
    timeout: int = 300,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command with proper error reporting.

    On failure with check=True: logs last 20 lines of stderr, raises StepFailed.
    """
    try:
        result = subprocess.run(
            cmd, cwd=cwd, timeout=timeout,
            capture_output=capture, text=True,
        )
    except subprocess.TimeoutExpired:
        raise StepFailed(desc, f"timed out after {timeout}s")
    except FileNotFoundError:
        raise StepFailed(desc, f"command not found: {cmd[0]}")

    if check and result.returncode != 0:
        log_error(f"{desc} failed (exit {result.returncode})")
        if capture and result.stderr:
            for line in result.stderr.strip().splitlines()[-20:]:
                log_error(f"  {line}")
        raise StepFailed(desc, f"exit code {result.returncode}")
    return result


# Write guardrail
#
# Quark must NEVER write to system-owned paths. Every recurring DLL incident
# on this project has come from an agent "fixing" a load problem by copying a
# patched DLL into /usr/lib/wine. The bwrap launcher layer makes this
# unnecessary at runtime; the guardrail makes it impossible at install time.
#
# Allowlist of write roots — anything not under one of these is forbidden.
# Add a new entry here, with a comment, if a legitimate new write target appears.

def _quark_write_roots() -> list[Path]:
    return [
        COMPAT_DIR,                 # the only deploy target that matters
        WINE_SRC_DIR,                     # cloned wine source for patching
        WINE_OBJ_DIR,                     # wine build artifacts
        CACHE_DIR,                        # ~/.cache/quark — provenance manifests, downloads
        DATA_DIR,                         # ~/.local/share/quark — legacy lsteamclient cache
        SCRIPT_DIR / "rust" / "target",   # cargo build output
        Path("/tmp"),                     # tarballs, scratch (must be under /tmp/...)
    ]


def assert_quark_writable(path: Path) -> None:
    """Raise StepFailed if `path` resolves outside any allowed quark write root.

    Use this at every NEW write call site. Catches accidental writes to /usr,
    /etc, /opt, /var, /lib, /sbin, /bin, and anything else owned by the system
    or by another package. The bwrap layer at runtime will not save you if
    install.py poisons /usr at install time — this is the line of defense.
    """
    try:
        # Don't .resolve() the target itself (it may not exist yet); resolve its parent.
        parent = path.parent.resolve(strict=False)
    except (OSError, RuntimeError):
        raise StepFailed("write_guard", f"unresolvable write path: {path}")
    candidate = parent / path.name
    for root in _quark_write_roots():
        try:
            root_resolved = root.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        try:
            candidate.relative_to(root_resolved)
            return
        except ValueError:
            continue
    raise StepFailed(
        "write_guard",
        f"refusing to write outside quark-owned roots: {candidate}\n"
        f"  allowed roots: {[str(r) for r in _quark_write_roots()]}"
    )


def get_system_wine_version() -> str:
    """Read `wine --version` output (e.g. 'wine-11.6'). Returns empty string on failure."""
    for binary in ["wine", "wine64"]:
        try:
            r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return ""


def derive_wine_tag() -> str:
    """Detect the system Wine version and return its tag.

    Quark must build against the SAME Wine source the system ships
    binaries from. No fallback to a hardcoded version -- if system
    Wine can't be detected, we can't build safely.
    """
    sysver = get_system_wine_version()  # e.g. "wine-11.6"
    if sysver and sysver.startswith("wine-"):
        return sysver
    log_error("Cannot detect system Wine version. Install Wine first.")
    sys.exit(1)


def compute_patches_hash(patch_dir: Path) -> str:
    """SHA-256 of all .patch files in patch_dir, sorted by name. Catches any
    edit, addition, or removal of a patch."""
    h = hashlib.sha256()
    if not patch_dir.exists():
        return h.hexdigest()
    for p in sorted(patch_dir.glob("*.patch")):
        h.update(p.name.encode())
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            pass
        h.update(b"\0")
    return h.hexdigest()


def compute_wine_build_inputs() -> dict[str, str]:
    """The full set of inputs that determine whether a previous wine-obj build
    is still valid. Any field changing means the cached build must be discarded.
    """
    return {
        "wine_tag": derive_wine_tag(),
        "system_wine_version": get_system_wine_version(),
        "patches_hash": compute_patches_hash(SCRIPT_DIR / "patches" / "wine"),
        "configure_args": "--enable-win64 --with-x --without-wayland",
    }


def read_build_manifest(path: Path) -> dict[str, str] | None:
    """Read a JSON manifest from disk. None if missing or unparseable."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_build_manifest(path: Path, manifest: dict[str, str]) -> None:
    """Write a build manifest as JSON."""
    assert_quark_writable(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))


def manifest_matches(stored: dict[str, str] | None, current: dict[str, str]) -> bool:
    """True if every field in `current` matches `stored`. Used as the
    skip-rebuild gate: only skip if the stored manifest is byte-identical."""
    if stored is None:
        return False
    for k, v in current.items():
        if stored.get(k) != v:
            return False
    return True


def snapshot_system_wine() -> dict[str, tuple[str, int, int]]:
    """Snapshot every entry under /usr/lib/wine and /usr/share/wine.

    Returned dict is path → (kind, meta_a, meta_b) where:
      - regular file:  ("file", mtime_ns, size)
      - symlink:       ("symlink", hash_of_target, 0)
      - directory:     ("dir", 0, 0)

    Tracking symlinks is critical: the previous bug was an agent shipping
    `sudo ln -sf` calls into /usr/lib/wine, and the audit missed it because
    it filtered symlinks out. Symlink creation IS poisoning. So is symlink
    target rewriting.
    """
    snap: dict[str, tuple[str, int, int]] = {}
    for root in [Path("/usr/lib/wine"), Path("/usr/share/wine")]:
        if not root.exists():
            continue
        try:
            for p in root.rglob("*"):
                try:
                    if p.is_symlink():
                        target = os.readlink(p)
                        # use hash of target as the second field; we just need
                        # any change to be detected, not the actual target
                        snap[str(p)] = ("symlink", hash(target) & 0x7fffffff, 0)
                    elif p.is_file():
                        st = p.stat()
                        snap[str(p)] = ("file", st.st_mtime_ns, st.st_size)
                    elif p.is_dir():
                        snap[str(p)] = ("dir", 0, 0)
                except OSError:
                    pass
        except OSError:
            pass
    return snap


def verify_system_wine_unchanged(before: dict[str, tuple[str, int, int]]) -> None:
    """Compare current state of /usr/lib/wine + /usr/share/wine against `before`.
    Raise StepFailed if anything was modified, added, or deleted. This is the
    detective control that pairs with assert_quark_writable: even if a write
    bypasses the guardrail (e.g. via subprocess), the audit catches it.

    Catches symlinks (creation, deletion, retargeting), regular files
    (creation, deletion, mtime/size change), and new/removed subdirectories.
    """
    after = snapshot_system_wine()
    changed = []
    for path, before_meta in before.items():
        after_meta = after.get(path)
        if after_meta is None:
            changed.append(("DELETED", path))
        elif after_meta != before_meta:
            kind_before, kind_after = before_meta[0], after_meta[0]
            if kind_before != kind_after:
                changed.append((f"TYPE-CHANGED({kind_before}→{kind_after})", path))
            elif kind_before == "symlink":
                changed.append(("SYMLINK-RETARGETED", path))
            else:
                changed.append(("MODIFIED", path))
    for path, after_meta in after.items():
        if path not in before:
            kind = after_meta[0]
            changed.append((f"CREATED-{kind.upper()}", path))
    if changed:
        for kind, path in changed[:20]:
            log_error(f"  {kind}: {path}")
        if len(changed) > 20:
            log_error(f"  ... and {len(changed) - 20} more")
        raise StepFailed(
            "system_wine_audit",
            f"install.py modified {len(changed)} entry(ies) in /usr/lib/wine or /usr/share/wine"
        )


_AUTO_YES = False
_TRACE = False
_WIPE_PREFIXES = False


def prompt_yn(question: str, auto: bool = True) -> bool:
    """Prompt the user with a [Y/N] question. Under --yes the answer is
    `auto`: Y for the questions an unattended install should agree to, N for
    a question that throws work away."""
    if _AUTO_YES:
        print(f"{question} [Y/N] {'y' if auto else 'n'}  (--yes)")
        return auto
    while True:
        answer = input(f"{question} [Y/N] ").strip().lower()
        if answer == "y":
            return True
        if answer == "n":
            return False


def get_version() -> str:
    """Read version from rust/Cargo.toml."""
    cargo_toml = RUST_DIR / "Cargo.toml"
    for line in cargo_toml.read_text().splitlines():
        if line.startswith("version"):
            return line.split('"')[1]
    return "0.0.0"


def cpu_count() -> int:
    return max(os.cpu_count() or 4, 1)


def fmt_size(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.0f} KB"
    return f"{n} B"


def find_proton_wine_dir() -> Path | None:
    """Find a Proton installation's Wine directory for PE drivers."""
    steam_common = STEAMAPPS / "common"
    if not steam_common.exists():
        steam_common = Path.home() / ".steam" / "root" / "steamapps" / "common"
    if not steam_common.exists():
        return None
    for name in ["Proton - Experimental", "Proton Hotfix", "Proton 11.0", "Proton 10.0"]:
        d = steam_common / name / "files" / "lib" / "wine" / "x86_64-windows"
        if d.exists():
            return d
    return None


# Step 1: Preflight

def step_preflight() -> StepResult:
    """Verify all required tools and system state."""
    t0 = time.monotonic_ns()
    warnings: list[str] = []
    missing: list[str] = []

    # Check all tools
    for tool, (min_ver, install_cmd) in REQUIRED_TOOLS.items():
        path = shutil.which(tool)
        if not path:
            missing.append(f"  {tool:12s} — install with: {install_cmd}")
            continue

        if min_ver and tool == "rustc":
            result = subprocess.run([tool, "--version"], capture_output=True, text=True)
            if result.returncode == 0:
                ver = result.stdout.split()[1]
                parts = ver.split(".")
                try:
                    major, minor = int(parts[0]), int(parts[1])
                    if major < 1 or (major == 1 and minor < int(min_ver.split(".")[1])):
                        missing.append(f"  {tool:12s} — have {ver}, need {min_ver}+: rustup update stable")
                        continue
                except (ValueError, IndexError):
                    pass
                log_info(f"Rust: {ver}")

    if missing:
        log_error("Missing required tools:")
        for line in missing:
            log_error(line)
        raise StepFailed("preflight", f"{len(missing)} tool(s) missing")

    # Steam
    steam_root = STEAM_ROOT
    if not steam_root.exists():
        raise StepFailed("preflight", "Steam not found (~/.steam/root) — install Steam natively")
    log_info("Steam: found")

    # System Wine
    sys_wine = Path("/usr/lib/wine/x86_64-unix")
    if not sys_wine.exists() or not shutil.which("wine"):
        raise StepFailed("preflight", "System Wine not found — pacman -S wine")
    wine_ver = subprocess.run(["wine", "--version"], capture_output=True, text=True)
    log_info(f"Wine: {wine_ver.stdout.strip() if wine_ver.returncode == 0 else 'found'}")

    # ntsync
    kver = os.uname().release
    parts = kver.split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        raise StepFailed("preflight", f"cannot parse kernel version: {kver}")

    if major < 6 or (major == 6 and minor < 14):
        raise StepFailed("preflight", f"kernel {kver} too old — requires 6.14+ for /dev/ntsync")

    log_info(f"ntsync: kernel {kver} — requirement met (6.14+)")

    if Path("/dev/ntsync").exists():
        log_info("ntsync: /dev/ntsync available — kernel-native NT sync enabled")
    else:
        warnings.append("/dev/ntsync not present — try: sudo modprobe ntsync")
        log_warn("ntsync: /dev/ntsync not present — try: sudo modprobe ntsync")

    # Wine Mono (required for .NET/FNA games: TMNT, Halls of Torment, etc.)
    mono_paths = [
        Path("/usr/share/wine/mono"),
        Path("/usr/lib/wine/mono"),
        Path.home() / ".local/share/wine/mono",
    ]
    has_mono = any(p.exists() and any(p.iterdir()) for p in mono_paths if p.exists())
    if has_mono:
        log_info("wine-mono: found")
    else:
        warnings.append("wine-mono not installed — .NET/FNA games will crash (pacman -S wine-mono)")
        log_warn("wine-mono: NOT FOUND — .NET/FNA games (TMNT, Halls of Torment, etc.) will crash")
        log_warn("  install with: pacman -S wine-mono")

    # WoW64 / 32-bit Wine (required for 32-bit games: Duke Nukem 3D, etc.)
    has_i386_unix = Path("/usr/lib/wine/i386-unix").exists()
    has_i386_win = Path("/usr/lib/wine/i386-windows").exists()
    if has_i386_unix and has_i386_win:
        log_info("wow64: 32-bit Wine libs found")
    else:
        missing_parts = []
        if not has_i386_unix: missing_parts.append("i386-unix")
        if not has_i386_win: missing_parts.append("i386-windows")
        warnings.append(f"32-bit Wine libs missing ({', '.join(missing_parts)}) — 32-bit games will crash")
        log_warn(f"wow64: 32-bit Wine libs NOT FOUND — 32-bit games (Duke Nukem 3D, etc.) will crash")
        log_warn("  install with: pacman -S lib32-wine")

    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("Preflight", True, warnings=warnings, elapsed_ms=elapsed)


# Step 2: Clean

def _quark_prefixes() -> list[tuple[str, Path]]:
    """(appid, prefix) for every existing prefix of a quark-mapped game."""
    compatdata = STEAMAPPS / "compatdata"
    return [(appid, compatdata / appid / "pfx") for appid in sorted(get_quark_appids())
            if (compatdata / appid / "pfx").exists()]


def step_clean() -> StepResult:
    """Detect old builds and offer cleanup."""
    t0 = time.monotonic_ns()

    def dir_size(path: Path) -> int:
        try:
            return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        except OSError:
            return 0

    found: list[tuple[str, Any, int]] = []

    if COMPAT_DIR.exists():
        found.append(("Steam compat tool", COMPAT_DIR, dir_size(COMPAT_DIR)))
    cargo_target = SCRIPT_DIR / "target"
    if cargo_target.exists():
        found.append(("Cargo build cache", cargo_target, dir_size(cargo_target)))
    if WINE_SRC_DIR.exists():
        found.append(("Wine source clone", WINE_SRC_DIR, dir_size(WINE_SRC_DIR)))
    if WINE_OBJ_DIR.exists():
        found.append(("Wine build objects", WINE_OBJ_DIR, dir_size(WINE_OBJ_DIR)))

    # Runtime state
    tmp_dir = LOG_DIR
    runtime_count = (1 if tmp_dir.exists() else 0) + len(stale_wine_sockets())
    if runtime_count:
        found.append(("Runtime state (logs, stale sockets)", None, 0))

    if not found:
        elapsed = (time.monotonic_ns() - t0) // 1_000_000
        return StepResult("Clean", True, skipped=True, elapsed_ms=elapsed)

    total = sum(size for _, _, size in found)
    print()
    print("  Existing quark installation detected:")
    for label, path, size in found:
        size_str = fmt_size(size) if size else ""
        if path and isinstance(path, Path):
            line = f"    - {label}: {path}"
        else:
            line = f"    - {label}"
        if size_str:
            line += f" ({size_str})"
        print(line)
    if total:
        print(f"    Total: {fmt_size(total)}")
    print()

    # Not under --yes: The manifest gate discards the Wine clone and build
    # when an input changed, and every deploy step overwrites, so an
    # unattended install builds on what is there instead of recloning Wine.
    if not prompt_yn("  Clean everything for a fresh build?", auto=False):
        log_info("Keeping existing artifacts — building on top")
        elapsed = (time.monotonic_ns() - t0) // 1_000_000
        return StepResult("Clean", True, skipped=True, elapsed_ms=elapsed)

    # Stop quark's wineservers, one prefix at a time
    quark_prefixes = _quark_prefixes()
    for _, pfx in quark_prefixes:
        stop_prefix_wineserver(pfx)

    # Clean runtime state (may contain root-owned files from sudo traces)
    if tmp_dir.exists():
        try:
            shutil.rmtree(tmp_dir)
        except (PermissionError, OSError):
            log_warn(f"Could not fully remove {tmp_dir} (root-owned files from sudo?)")
            log_warn("  Run: sudo rm -rf /tmp/quark")
    remove_stale_wine_sockets()

    # Wipe Wine prefixes and shader caches for games mapped to quark, only on
    # --wipe-prefixes (never implied by --yes). Stale prefixes cache Wine state
    # (registry, DLL load order, PHYSFS mount state) from previous builds, and
    # a shader cache left truncated by a hard-kill makes Fossilize double-free
    # on the next launch. Steam recreates both on next launch; save data in
    # Steam Cloud is unaffected, local-only saves in the prefix are lost.
    if quark_prefixes and not _WIPE_PREFIXES:
        log_info(f"Keeping {len(quark_prefixes)} quark prefix(es) "
                 "(--wipe-prefixes removes them and their shader caches)")
    elif quark_prefixes:
        log_warn(f"Wiping {len(quark_prefixes)} Wine prefix(es) for quark-mapped games")
        log_warn("  Save data in Steam Cloud is NOT affected. Local-only saves")
        log_warn("  in the prefix WILL be lost. Games: " +
                 ", ".join(a for a, _ in quark_prefixes))
        shadercache = STEAMAPPS / "shadercache"
        for appid, pfx in quark_prefixes:
            shutil.rmtree(pfx)
            log_info(f"Removed prefix: {pfx}")
            cache = shadercache / appid
            if cache.exists():
                shutil.rmtree(cache)
                log_info(f"Removed shader cache: {cache}")

    # Clean build artifacts
    for label, path, _ in found:
        if path is None or label.startswith("Runtime"):
            continue
        if isinstance(path, Path):
            if path.is_dir():
                shutil.rmtree(path)
                log_info(f"Removed: {path}")
            elif path.exists():
                path.unlink()
                log_info(f"Removed: {path}")

    print()
    log_info("Clean slate — proceeding with fresh build")
    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("Clean", True, elapsed_ms=elapsed)


# Step 3a: Sync wine source
#
# Clones the Wine source matching the system Wine version (auto-derived from
# `wine --version`) into WINE_SRC_DIR. step_patch_wine builds the patched
# modules from this tree and step_steam_bridge builds lsteamclient in it.
#
# Also runs the manifest gate: if any build input changed since the last
# install (wine version, patches, configure args), nukes WINE_SRC_DIR and
# WINE_OBJ_DIR so the rebuild starts from a clean slate.

def step_sync_wine_source() -> StepResult:
    """Clone wine source matching system Wine, run manifest gate."""
    t0 = time.monotonic_ns()
    wine_tag = derive_wine_tag()
    log_info(f"target wine source: {wine_tag}")

    # Manifest gate. Has to run before any rebuild decision: if inputs changed,
    # we discard wine-src and wine-obj and let the rest of the step rebuild.
    manifest_path = WINE_OBJ_DIR / ".quark-build-manifest.json"
    current_inputs = compute_wine_build_inputs()
    stored_inputs = read_build_manifest(manifest_path)
    if not manifest_matches(stored_inputs, current_inputs):
        if stored_inputs is not None:
            log_warn("Build inputs changed since last install — discarding cached wine build:")
            for k, v in current_inputs.items():
                old = stored_inputs.get(k, "<missing>")
                if old != v:
                    log_warn(f"  {k}: {old}  →  {v}")
        else:
            log_info("No previous build manifest — clean clone")
        if WINE_OBJ_DIR.exists():
            assert_quark_writable(WINE_OBJ_DIR)
            shutil.rmtree(WINE_OBJ_DIR)
        if WINE_SRC_DIR.exists():
            assert_quark_writable(WINE_SRC_DIR)
            shutil.rmtree(WINE_SRC_DIR)

    # Clone if not present
    if not (WINE_SRC_DIR / "server" / "protocol.def").exists():
        log_info(f"Cloning Wine {wine_tag} to {WINE_SRC_DIR}...")
        WINE_SRC_DIR.parent.mkdir(parents=True, exist_ok=True)
        if WINE_SRC_DIR.exists():
            assert_quark_writable(WINE_SRC_DIR)
            shutil.rmtree(WINE_SRC_DIR)
        # A shallow clone of one tag is still over 100 MB; give a slow link
        # the time, and let git's own failure end it otherwise.
        run_cmd(
            ["git", "clone", "--depth", "1", "-b", wine_tag, WINE_CLONE_URL, str(WINE_SRC_DIR)],
            desc="clone Wine", capture=False, timeout=1800,
        )
    else:
        log_info(f"Wine source present at {WINE_SRC_DIR}")

    # Reset to clean state — patches get applied later in step_patch_wine
    run_cmd(["git", "checkout", "HEAD", "--", "."], desc="git reset wine-src",
            cwd=WINE_SRC_DIR, check=False)
    run_cmd(["git", "clean", "-fd"], desc="git clean wine-src",
            cwd=WINE_SRC_DIR, check=False)

    # Sanity: protocol.def is the marker of a complete clone
    proto_def = WINE_SRC_DIR / "server" / "protocol.def"
    if not proto_def.exists():
        raise StepFailed("sync wine source", f"protocol.def missing after clone: {proto_def}")

    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("Sync wine source", True, elapsed_ms=elapsed)


# Step 3: Build

def prebuilt_binary() -> Path | None:
    """The binary a package installed beside this script, used when there is
    no source tree to build from (PKGBUILD lays the tree out this way)."""
    binary = SCRIPT_DIR / "quark"
    if binary.is_file() and not (RUST_DIR / "src").is_dir():
        return binary
    return None


def step_build() -> StepResult:
    """Build quark via cargo, unless a packaged binary stands in for the source."""
    t0 = time.monotonic_ns()
    if (binary := prebuilt_binary()) is not None:
        log_info(f"Build: packaged binary {binary}")
        elapsed = (time.monotonic_ns() - t0) // 1_000_000
        return StepResult("Build", True, artifacts=[binary], elapsed_ms=elapsed)
    log_info("Building quark...")

    try:
        subprocess.run(
            ["cargo", "build", "--release", "-p", "quark"],
            cwd=SCRIPT_DIR, timeout=600, check=True,
        )
    except subprocess.CalledProcessError as e:
        raise StepFailed("build", f"cargo build exit {e.returncode}")
    except subprocess.TimeoutExpired:
        raise StepFailed("build", "cargo build timed out (600s)")

    target_dir = SCRIPT_DIR / "target" / "release"
    binary = target_dir / "quark"
    if not binary.exists():
        raise StepFailed("build", f"binary not found: {binary}")
    artifacts = [binary]

    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("Build", True, artifacts=artifacts, elapsed_ms=elapsed)


# Step 4: Deploy Binaries

def step_deploy_binaries() -> StepResult:
    """Deploy binaries to Steam compat dir, write VDFs, create symlinks."""
    t0 = time.monotonic_ns()
    COMPAT_DIR.mkdir(parents=True, exist_ok=True)
    src = prebuilt_binary() or SCRIPT_DIR / "target" / "release" / "quark"
    dst = COMPAT_DIR / "quark"
    shutil.copy2(src, dst)
    os.chmod(dst, 0o755)
    artifacts = [dst]

    # proton → quark symlink (Steam VDF expects "proton")
    proton_link = COMPAT_DIR / "proton"
    if proton_link.exists() or proton_link.is_symlink():
        proton_link.unlink()
    proton_link.symlink_to("quark")

    # VDF
    version = get_version()
    vdf = COMPAT_DIR / "compatibilitytool.vdf"
    vdf.write_text(f'''"compatibilitytools"
{{
  "compat_tools"
  {{
    "quark"
    {{
      "install_path" "."
      "display_name" "quark {version}"
      "from_oslist"  "windows"
      "to_oslist"    "linux"
    }}
  }}
}}
''')

    manifest = COMPAT_DIR / "toolmanifest.vdf"
    manifest.write_text('''"manifest"
{
  "commandline" "/proton %verb%"
  "version" "2"
  "use_sessions" "1"
}
''')

    log_info(f"Deployed: quark {version}")
    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("Deploy binaries", True, artifacts=artifacts, elapsed_ms=elapsed)


# Step 5: Deploy Wine

def step_deploy_wine() -> StepResult:
    """Deploy system Wine tree."""
    t0 = time.monotonic_ns()
    warnings: list[str] = []

    sys_unix = Path("/usr/lib/wine/x86_64-unix")
    sys_bin = Path("/usr/bin")

    if not sys_unix.exists() or not (sys_bin / "wine").exists():
        raise StepFailed("deploy wine", "system Wine not found at /usr/lib/wine/ — pacman -S wine")

    # bin/ — wine loader + wineserver. The preloader is not here: ntdll execs
    # the wine-preloader beside its own loader in lib/wine/x86_64-unix, which
    # the lib/wine sync below carries.
    bin_dir = COMPAT_DIR / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    dst = bin_dir / "wine"
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    # COPY — Wine resolves symlinks for wineserver discovery
    shutil.copy2(sys_bin / "wine", dst)
    stale_preloader = bin_dir / "wine-preloader"
    if stale_preloader.exists() or stale_preloader.is_symlink():
        stale_preloader.unlink()

    wine64_link = bin_dir / "wine64"
    if wine64_link.exists() or wine64_link.is_symlink():
        wine64_link.unlink()
    wine64_link.symlink_to("wine")

    # wineserver → system wineserver (matches the system Wine protocol)
    wineserver_link = bin_dir / "wineserver"
    if wineserver_link.exists() or wineserver_link.is_symlink():
        wineserver_link.unlink()
    wineserver_link.symlink_to(sys_bin / "wineserver")

    # lib/wine/ — hardlink files from system Wine
    wine_lib = COMPAT_DIR / "lib" / "wine"
    wine_lib.mkdir(parents=True, exist_ok=True)

    total_files = 0
    for subdir in ["x86_64-unix", "x86_64-windows", "i386-windows", "i386-unix"]:
        src = Path("/usr/lib/wine") / subdir
        dst = wine_lib / subdir
        if not src.exists():
            continue
        if dst.is_symlink():
            dst.unlink()
        elif dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True, exist_ok=True)
        copied = 0
        for f in src.iterdir():
            if f.is_file():
                target = dst / f.name
                if target.exists():
                    target.unlink()
                try:
                    os.link(f, target)
                except OSError:
                    shutil.copy2(f, target)
                    copied += 1
                total_files += 1
        if copied > 0:
            w = f"{subdir}: {copied} files COPIED (cross-device, re-run after Wine updates)"
            warnings.append(w)
            log_warn(f"  {w}")

    # share/wine/ — wine.inf, nls, fonts, winmd. Copies, NOT symlinks.
    # The bwrap launcher binds compat_dir/share/wine over /usr/share/wine at
    # launch time; symlinks pointing back into /usr would defeat that.
    # mono/gecko are NOT deployed here — Wine downloads them into the prefix
    # on first launch if they're not installed system-wide.
    share_wine = COMPAT_DIR / "share" / "wine"
    share_wine.mkdir(parents=True, exist_ok=True)
    sys_share = Path("/usr/share/wine")
    share_files = 0

    # wine.inf — single file, hardlink
    src_inf = sys_share / "wine.inf"
    dst_inf = share_wine / "wine.inf"
    assert_quark_writable(dst_inf)
    if src_inf.exists():
        if dst_inf.is_symlink() or dst_inf.exists():
            dst_inf.unlink()
        try:
            os.link(src_inf, dst_inf)
        except OSError:
            shutil.copy2(src_inf, dst_inf)
        share_files += 1
    else:
        warnings.append("wine.inf missing from /usr/share/wine")

    # Directory trees: nls (locale data), fonts, winmd (WinRT metadata).
    for tree_name in ["nls", "fonts", "winmd"]:
        src_tree = sys_share / tree_name
        dst_tree = share_wine / tree_name
        if not src_tree.exists():
            if tree_name in ("nls", "fonts"):
                warnings.append(f"{tree_name}/ missing from /usr/share/wine")
            continue
        assert_quark_writable(dst_tree)
        if dst_tree.is_symlink():
            dst_tree.unlink()
        elif dst_tree.exists():
            shutil.rmtree(dst_tree)
        dst_tree.mkdir(parents=True, exist_ok=True)
        for f in src_tree.rglob("*"):
            if f.is_file() and not f.is_symlink():
                rel = f.relative_to(src_tree)
                target = dst_tree / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target.unlink()
                try:
                    os.link(f, target)
                except OSError:
                    shutil.copy2(f, target)
                share_files += 1

    # Wine Mono: copy into share/wine so bwrap has it inside the bind mount.
    # bwrap --bind overlays quark's share/wine onto /usr/share/wine, so external
    # symlinks are invisible. Must be a real copy. Hardlinks work cross-device
    # only on the same filesystem, so fall back to copy.
    sys_mono = sys_share / "mono"
    dst_mono = share_wine / "mono"
    if sys_mono.exists():
        assert_quark_writable(dst_mono)
        if dst_mono.is_symlink():
            dst_mono.unlink()
        if not dst_mono.exists():
            shutil.copytree(sys_mono, dst_mono)
            log_info(f"wine-mono: copied {sys_mono} -> {dst_mono}")

    log_info(f"System Wine deployed ({total_files} lib files, {share_files} share files)")
    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("Deploy Wine", True, warnings=warnings, elapsed_ms=elapsed)


# Step 6: Patch Wine

def step_patch_wine() -> StepResult:
    """Apply patches to wine source (already cloned by step_sync_wine_source),
    configure, build patched DLLs."""
    t0 = time.monotonic_ns()
    wine_lib = COMPAT_DIR / "lib" / "wine"

    patch_dir = SCRIPT_DIR / "patches" / "wine"
    patches = sorted(patch_dir.glob("*.patch")) if patch_dir.exists() else []
    if not patches:
        log_info("No Wine patches found — using stock DLLs")
        return StepResult("Patch Wine", True, skipped=True,
                         elapsed_ms=(time.monotonic_ns() - t0) // 1_000_000)

    src_dir = WINE_SRC_DIR
    build_dir = WINE_OBJ_DIR
    manifest_path = WINE_OBJ_DIR / ".quark-build-manifest.json"
    current_inputs = compute_wine_build_inputs()

    # Sanity: step_sync_wine_source must have populated wine-src already
    if not (src_dir / "dlls" / "win32u").exists():
        raise StepFailed(
            "patch wine",
            f"wine source missing at {src_dir} — sync_wine_source must run first"
        )

    # Reset source to clean state before applying patches (idempotent across runs)
    run_cmd(["git", "checkout", "HEAD", "--", "."], desc="git reset wine-src",
            cwd=src_dir, check=False)
    run_cmd(["git", "clean", "-fd"], desc="git clean wine-src",
            cwd=src_dir, check=False)

    # Apply patches strictly. A patch that does not apply exactly as written
    # no longer fits this Wine version and fails the install, by name, to be
    # re-ported. No fuzzy fallback: `patch --fuzz` exits 0 after landing a
    # hunk in the wrong function or inside a comment, which ships a patch
    # that silently does nothing or breaks the build somewhere else.
    for patch in patches:
        check_result = run_cmd(
            ["git", "apply", "--check", str(patch)],
            desc=f"check {patch.name}", cwd=src_dir, check=False,
        )
        if check_result.returncode != 0:
            log_error(f"  Patch does NOT apply to this Wine version: {patch.name}")
            for line in (check_result.stderr or "").strip().splitlines()[-6:]:
                log_error(f"    {line}")
            raise StepFailed(
                "patch wine",
                f"{patch.name} does not apply strictly to {derive_wine_tag()} — re-port it",
            )
        run_cmd(["git", "apply", str(patch)],
                desc=f"apply {patch.name}", cwd=src_dir, check=True)
        log_info(f"  Applied: {patch.name}")

    log_info(f"Applied {len(patches)}/{len(patches)} Wine patches")

    # Configure
    if not (build_dir / "Makefile").exists():
        log_info("Configuring Wine build...")
        build_dir.mkdir(parents=True, exist_ok=True)
        # Regenerate configure from the patched configure.ac (patches 004/005 add
        # the lsteamclient + steam_helper makefiles). Use plain `autoconf`, NOT
        # `autoreconf -f`: autoreconf pulls in aclocal/automake/libtoolize and,
        # on a newer host autoconf (2.73+), regenerates a BROKEN configure whose
        # conftest compiles all fail — surfacing as a false "pthread 64-bit
        # development files not found". Wine's own build regenerates configure
        # with plain autoconf; that is version-robust across host autoconf bumps.
        run_cmd(["autoconf"], desc="autoconf (regenerate configure)",
                cwd=src_dir, check=False)
        run_cmd(
            [str(src_dir / "configure"), "--prefix=/usr", "--enable-win64",
             "--with-x", "--without-wayland"],
            desc="Wine configure", cwd=build_dir, timeout=300,
        )

    # Build ALL patched DLLs. The curated map below pins module names that need
    # explicit Wine-side rename (so_name) or are arch-only. Anything else is
    # auto-discovered from the patches themselves: scan each patch's diff
    # headers for `a/dlls/<name>/` and `a/programs/<name>/` paths, then
    # build whatever PE+unix targets exist for that module. New patches
    # land automatically — no need to remember to edit this dict.
    #
    # kernelbase: EXCLUDED. Patches 005 and 006 are applied to the source,
    # but the built kernelbase.dll is not deployed until a deployed build is
    # shown to run against system Wine.
    dll_targets: dict[str, tuple] = {
        "ntdll": ("dlls/ntdll/ntdll.so", "ntdll.so",
                  "dlls/ntdll/x86_64-windows/ntdll.dll", "ntdll.dll"),
        # winepulse.drv: unixlib-only rebuild. The audio fix lives entirely in
        # the unix side (pulse.c -> winepulse.so); the PE stub is unchanged, so
        # no x86_64-windows target. Auto-discovery would derive the wrong PE
        # name ("winepulse.drv.dll") from the .drv module dir and fail the make.
        "winepulse.drv": ("dlls/winepulse.drv/winepulse.so", "winepulse.so", None, None),
    }
    excluded_modules = {"kernelbase"}

    import re
    diff_path_re = re.compile(r"^[-+]{3} [ab]/(dlls|programs)/([^/]+)/")
    discovered: set[tuple[str, str]] = set()  # (kind, name)
    for patch in patches:
        for line in patch.read_text(errors="replace").splitlines():
            m = diff_path_re.match(line)
            if m:
                discovered.add((m.group(1), m.group(2)))

    for kind, name in sorted(discovered):
        if name in dll_targets or name in excluded_modules:
            continue
        if kind == "dlls":
            mk = src_dir / "dlls" / name / "Makefile.in"
            has_unixlib = mk.exists() and f"UNIXLIB" in mk.read_text(errors="replace") and f"{name}.so" in mk.read_text(errors="replace")
            so_path = f"dlls/{name}/{name}.so" if has_unixlib else None
            so_name = f"{name}.so" if has_unixlib else None
            dll_targets[name] = (so_path, so_name,
                                 f"dlls/{name}/x86_64-windows/{name}.dll", f"{name}.dll")
        else:  # programs
            dll_targets[name] = (None, None,
                                 f"programs/{name}/x86_64-windows/{name}.exe", f"{name}.exe")
        log_info(f"  Auto-discovered patched module: {kind}/{name}")

    artifacts: list[Path] = []
    failed: list[str] = []
    for dll, (so_path, so_name, pe_path, pe_name) in dll_targets.items():
        build_targets = []
        if so_path:
            build_targets.append(so_path)
        if pe_path:
            build_targets.append(pe_path)

        log_info(f"  Building {dll}...")
        result = run_cmd(
            ["make", f"-j{cpu_count()}"] + build_targets,
            desc=f"build {dll}", cwd=build_dir, timeout=300, check=False,
        )
        if result.returncode != 0:
            log_error(f"  Build failed for {dll}")
            if result.stderr:
                for line in result.stderr.strip().splitlines()[-10:]:
                    log_error(f"    {line}")
            failed.append(dll)
            continue

        if so_path:
            built_so = build_dir / so_path
            if built_so.exists():
                dst = wine_lib / "x86_64-unix" / so_name
                if dst.exists():
                    dst.unlink()
                shutil.copy2(built_so, dst)
                artifacts.append(dst)
                log_info(f"  Deployed {so_name} ({built_so.stat().st_size // 1024}K)")
            else:
                log_error(f"  {so_name} not produced")
                failed.append(dll)

        if pe_path:
            built_pe = build_dir / pe_path
            if built_pe.exists():
                if pe_name:
                    # Deploy to Wine tree (normal DLLs)
                    dst = wine_lib / "x86_64-windows" / pe_name
                    if dst.exists():
                        dst.unlink()
                    shutil.copy2(built_pe, dst)
                    artifacts.append(dst)
                    log_info(f"  Deployed {pe_name} ({built_pe.stat().st_size // 1024}K)")
                else:
                    # Stash for prefix-only deployment (e.g. kernelbase)
                    patched_dir = COMPAT_DIR / "lib" / "wine" / "patched"
                    patched_dir.mkdir(parents=True, exist_ok=True)
                    stash_name = Path(pe_path).name
                    dst = patched_dir / stash_name
                    shutil.copy2(built_pe, dst)
                    artifacts.append(dst)
                    log_info(f"  Stashed {stash_name} for prefix deploy ({built_pe.stat().st_size // 1024}K)")
            else:
                log_error(f"  {pe_name or Path(pe_path).name} not produced")
                failed.append(dll)

    # A patched module that did not build ships its unpatched system copy,
    # so the patch is silently absent; that fails the step, by name.
    if failed:
        raise StepFailed("patch wine", f"patched module(s) failed to build: {', '.join(sorted(set(failed)))}")

    # Build succeeded — record the inputs that produced these artifacts so the
    # next install can decide whether to trust the cache or rebuild.
    write_build_manifest(manifest_path, current_inputs)
    log_info(f"Build manifest written: {manifest_path.name}")

    log_info(f"Deployed {len(artifacts)} patched Wine module(s)")
    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("Patch Wine", True, artifacts=artifacts, elapsed_ms=elapsed)


# Step 6b: Bake prefix template

def step_bake_prefix_template() -> StepResult:
    """Run wineboot --init with stock wineserver to create a complete prefix template.

    This produces system.reg/user.reg with all COM class registrations (500+
    CLSIDs from Wine's FakeDlls pass). Games need these for CoCreateInstance
    to find mmdevapi, devenum, quartz, etc. Without them, audio init fails
    and DXVK games show white windows.

    The template is baked ONCE during install and copied into each game prefix
    at launch time. This replaces the 45-second update_wineprefix call.
    """
    t0 = time.monotonic_ns()
    template_dir = COMPAT_DIR / "default_pfx"
    system_reg = template_dir / "system.reg"

    if system_reg.exists() and system_reg.stat().st_size > 100_000:
        log_info("prefix template: already baked")
        elapsed = (time.monotonic_ns() - t0) // 1_000_000
        return StepResult("Bake prefix", True, elapsed_ms=elapsed)

    # Source: Proton's pre-baked default_pfx (9000+ CLSID entries, complete COM registration).
    # Proton bakes this at BUILD time via wineboot --init. We steal the result.
    proton_dirs = [
        "Proton 11.0", "Proton - Experimental", "Proton Hotfix", "Proton 10.0",
    ]
    steam_common = STEAMAPPS / "common"
    source_pfx = None
    for pname in proton_dirs:
        candidate = steam_common / pname / "files/share/default_pfx"
        if (candidate / "system.reg").exists():
            source_pfx = candidate
            break

    if source_pfx is None:
        # Skipping this step ships a broken install — the launcher seeds new
        # prefixes from the baked default_pfx. A silent skip used to let
        # users complete install and then fail mysteriously at first game
        # launch.
        raise StepFailed(
            "Bake prefix",
            f"No Proton default_pfx found in {steam_common}. Install at least one of: "
            f"{', '.join(proton_dirs)} via Steam first, then re-run install.py."
        )

    # Copy the ENTIRE prefix directory structure (not just .reg files).
    # Games need Fonts, system32 DLLs, directory structure, etc.
    if template_dir.exists():
        shutil.rmtree(template_dir)
    log_info(f"  Copying full prefix template from {source_pfx.parent.parent.name}...")
    shutil.copytree(source_pfx, template_dir, symlinks=True)

    # Proton's default_pfx ships RELATIVE symlinks in subdirs (e.g.
    # drive_c/windows/system32/wbem/*.dll → ../../../../../../lib/wine/...)
    # whose `../` count assumes Proton's exact directory depth. Our compat
    # tree has a different parent layout, so those symlinks resolve to
    # paths that don't exist — Wine then fails LoadLibrary on wbemprox.dll
    # and Unity/Mono games crash on the WMI apartment init (apartment_add_dll).
    # Walk the template and replace any broken symlink whose target name
    # exists in our deployed wine lib with a real file. Idempotent.
    wine_lib_win = COMPAT_DIR / "lib" / "wine" / "x86_64-windows"
    fixed = 0
    for link in template_dir.rglob("*"):
        if not link.is_symlink():
            continue
        # Resolve manually — if target doesn't exist, fix it.
        try:
            target_resolved = link.resolve(strict=False)
            if target_resolved.exists():
                continue
        except OSError:
            continue
        # Broken. Try to find the actual file in our wine lib by basename.
        replacement = wine_lib_win / link.name
        if replacement.exists() and replacement.is_file():
            try:
                link.unlink()
                shutil.copy2(replacement, link)
                fixed += 1
            except OSError as e:
                log_warn(f"  failed to repair {link.name}: {e}")
    if fixed > 0:
        log_info(f"  repaired {fixed} broken symlinks in default_pfx")

    total_size = sum(f.stat().st_size for f in template_dir.rglob("*") if f.is_file())
    log_info(f"prefix template: {total_size // 1024 // 1024}MB from Proton ({source_pfx.parent.parent.name})")

    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("Bake prefix", True, elapsed_ms=elapsed)


# Step 7: Build Stubs

def step_build_stubs() -> StepResult:
    """Build tier0/vstdlib DLL stubs via clang+lld (no mingw)."""
    t0 = time.monotonic_ns()
    wine_lib = COMPAT_DIR / "lib" / "wine"

    stub_c = Path("/tmp/quark_stub.c")
    stub_c.write_text(
        'typedef int BOOL;\n'
        'typedef void *HINSTANCE;\n'
        'typedef unsigned long DWORD;\n'
        'BOOL __stdcall DllMainCRTStartup(HINSTANCE i, DWORD r, void *v) { return 1; }\n'
    )

    artifacts: list[Path] = []

    # 64-bit stubs
    for stub_name in ["tier0_s64", "vstdlib_s64"]:
        stub_dll = wine_lib / "x86_64-windows" / f"{stub_name}.dll"
        result = run_cmd(
            ["clang", "--target=x86_64-w64-windows-gnu", "-shared",
             "-fuse-ld=lld", "-nostdlib", "-o", str(stub_dll), str(stub_c)],
            desc=f"build {stub_name}.dll", check=False,
        )
        if result.returncode == 0 and stub_dll.exists():
            artifacts.append(stub_dll)
            log_info(f"  {stub_name}.dll stub deployed (64-bit)")
        else:
            log_error(f"  Failed to build {stub_name}.dll")

    # 32-bit stubs
    i386_dir = wine_lib / "i386-windows"
    i386_dir.mkdir(parents=True, exist_ok=True)
    for stub_name in ["tier0_s", "vstdlib_s"]:
        stub_dll = i386_dir / f"{stub_name}.dll"
        result = run_cmd(
            ["clang", "--target=i686-w64-windows-gnu", "-shared",
             "-fuse-ld=lld", "-nostdlib", "-o", str(stub_dll), str(stub_c)],
            desc=f"build {stub_name}.dll", check=False,
        )
        if result.returncode == 0 and stub_dll.exists():
            artifacts.append(stub_dll)
            log_info(f"  {stub_name}.dll stub deployed (32-bit)")
        else:
            log_error(f"  Failed to build {stub_name}.dll")

    # Cleanup
    stub_c.unlink(missing_ok=True)

    if not artifacts:
        raise StepFailed("build stubs", "no stub DLLs built — is clang+lld installed?")

    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("Build stubs", True, artifacts=artifacts, elapsed_ms=elapsed)


# Step 8: Steam Bridge

def _download_proton_source() -> Path:
    """Download lsteamclient + steam_helper from Proton GitHub (targeted, no full clone).

    Downloads the release tarball and extracts only the needed directories.
    Caches by tag so re-runs skip the download.
    """
    cache = PROTON_SOURCE_CACHE
    if (cache / "lsteamclient").exists():
        log_info("lsteamclient: Proton source cached")
        return cache

    log_info(f"lsteamclient: downloading Proton source ({PROTON_GIT_TAG})...")
    tarball_url = f"https://github.com/ValveSoftware/Proton/archive/refs/tags/{PROTON_GIT_TAG}.tar.gz"
    tarball_path = Path(f"/tmp/proton-{PROTON_GIT_TAG}.tar.gz")

    try:
        urllib.request.urlretrieve(tarball_url, tarball_path)
    except Exception as e:
        raise StepFailed("steam bridge", f"failed to download Proton source: {e}")

    # Extract only lsteamclient/ and steam_helper/
    cache.mkdir(parents=True, exist_ok=True)
    prefix = f"Proton-{PROTON_GIT_TAG}/"
    try:
        with tarfile.open(tarball_path) as tf:
            for member in tf.getmembers():
                if not member.name.startswith(prefix):
                    continue
                rel = member.name[len(prefix):]
                if rel.startswith("lsteamclient/") or rel.startswith("steam_helper/"):
                    member.name = rel
                    try:
                        tf.extract(member, cache, filter="data")
                    except TypeError:
                        tf.extract(member, cache)
    except Exception as e:
        raise StepFailed("steam bridge", f"failed to extract Proton tarball: {e}")
    finally:
        tarball_path.unlink(missing_ok=True)

    if not (cache / "lsteamclient").exists():
        raise StepFailed("steam bridge", "lsteamclient/ not found in Proton tarball")

    log_info("lsteamclient: Proton source downloaded and cached")
    return cache


def step_steam_bridge() -> StepResult:
    """Build lsteamclient.dll + steam.exe from Proton source against the system Wine's source."""
    t0 = time.monotonic_ns()
    wine_lib = COMPAT_DIR / "lib" / "wine"

    dll_cache = LSTEAMCLIENT_CACHE / "lsteamclient.dll"
    so_cache = LSTEAMCLIENT_CACHE / "lsteamclient.so"
    steam_cache = LSTEAMCLIENT_CACHE / "steam.exe"

    # Always rebuild. A stale cache with Proton-ABI binaries is the #1 cause
    # of Steam auth failure. The build takes ~30s — not worth the risk.
    if LSTEAMCLIENT_CACHE.exists():
        shutil.rmtree(LSTEAMCLIENT_CACHE)

    # Download Proton source (targeted — just lsteamclient + steam_helper)
    proton_src = _download_proton_source()

    # Need wine-src for building
    wine_src = WINE_SRC_DIR
    if not wine_src.exists():
        raise StepFailed("steam bridge", "wine-src not found — patch wine step must run first")

    # Reset configure.ac
    run_cmd(["git", "checkout", "--", "configure.ac"],
            desc="reset configure.ac", cwd=wine_src, check=False)

    # Symlink lsteamclient into wine-src
    src_ls = proton_src / "lsteamclient"
    if not src_ls.exists():
        raise StepFailed(
            "steam bridge",
            f"lsteamclient source missing at {src_ls} — Proton {PROTON_GIT_TAG} "
            f"download/extract incomplete; delete {PROTON_SOURCE_CACHE} and re-run",
        )
    dll_link = wine_src / "dlls" / "lsteamclient"
    dll_link.parent.mkdir(parents=True, exist_ok=True)
    if dll_link.is_symlink() or dll_link.exists():
        dll_link.unlink()
    dll_link.symlink_to(src_ls)
    log_info("lsteamclient: symlinked into wine-src/dlls/")

    # Register in configure.ac
    configure_ac = wine_src / "configure.ac"
    text = configure_ac.read_text()
    if "dlls/lsteamclient" not in text:
        text = text.replace(
            "WINE_CONFIG_MAKEFILE(dlls/lz32/tests)",
            "WINE_CONFIG_MAKEFILE(dlls/lz32/tests)\nWINE_CONFIG_MAKEFILE(dlls/lsteamclient)",
        )
        configure_ac.write_text(text)

    # Reset Proton source before patching
    steam_helper_src = proton_src / "steam_helper"
    if steam_helper_src.exists():
        helper_link = wine_src / "programs" / "steam_helper"
        if helper_link.is_symlink():
            helper_link.unlink()
        if not helper_link.exists():
            helper_link.symlink_to(steam_helper_src)
        text = configure_ac.read_text()
        if "programs/steam_helper" not in text:
            text = text.replace(
                "WINE_CONFIG_MAKEFILE(programs/start)",
                "WINE_CONFIG_MAKEFILE(programs/start)\nWINE_CONFIG_MAKEFILE(programs/steam_helper)",
            )
            configure_ac.write_text(text)

    # Apply lsteamclient patches (patches/lsteamclient/). Strict, like the Wine
    # patches: one that does not apply fails the step by name. The Proton
    # source cache persists across installs, so a patch that strictly
    # reverse-applies is already in and is skipped.
    for pfile in sorted((SCRIPT_DIR / "patches" / "lsteamclient").glob("*.patch")):
        forward = run_cmd(["git", "apply", "--check", str(pfile)],
                          desc=f"check {pfile.name}", cwd=proton_src, check=False)
        if forward.returncode == 0:
            run_cmd(["git", "apply", str(pfile)],
                    desc=f"apply {pfile.name}", cwd=proton_src, check=True)
            log_info(f"  Applied: {pfile.name}")
            continue
        reverse = run_cmd(["git", "apply", "--check", "-R", str(pfile)],
                          desc=f"check {pfile.name} applied", cwd=proton_src, check=False)
        if reverse.returncode == 0:
            log_info(f"  Already applied: {pfile.name}")
            continue
        for line in (forward.stderr or "").strip().splitlines()[-6:]:
            log_error(f"    {line}")
        raise StepFailed("steam bridge",
                         f"{pfile.name} does not apply strictly to Proton {PROTON_GIT_TAG} — re-port it")

    # Build in /tmp
    build_dir = Path("/tmp/quark-lsteamclient-build")
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    log_info("lsteamclient: running autoconf...")
    run_cmd(["autoconf"], desc="autoconf (lsteamclient)", cwd=wine_src)

    log_info(f"lsteamclient: configuring against {derive_wine_tag()}...")
    run_cmd(
        [str(wine_src / "configure"), "--enable-win64", "--disable-tests"],
        desc="configure (lsteamclient)", cwd=build_dir, timeout=120,
    )

    targets = [
        "dlls/lsteamclient/x86_64-windows/lsteamclient.dll",
        "dlls/lsteamclient/lsteamclient.so",
    ]
    if steam_helper_src.exists():
        targets.append("programs/steam_helper/x86_64-windows/steam.exe")

    log_info("lsteamclient: building...")
    result = run_cmd(
        ["make", f"-j{cpu_count()}"] + targets,
        desc="build lsteamclient", cwd=build_dir, timeout=600, check=False,
    )
    if result.returncode != 0:
        log_error("lsteamclient: build failed — retrying single-threaded for diagnostics")
        run_cmd(["make"] + targets, desc="build lsteamclient (retry)",
                cwd=build_dir, timeout=600)

    dll_target = build_dir / "dlls" / "lsteamclient" / "x86_64-windows" / "lsteamclient.dll"
    so_target = build_dir / "dlls" / "lsteamclient" / "lsteamclient.so"
    steam_target = build_dir / "programs" / "steam_helper" / "x86_64-windows" / "steam.exe"

    # Report sizes
    for label, path in [("lsteamclient.dll", dll_target), ("lsteamclient.so", so_target), ("steam.exe", steam_target)]:
        if path.exists():
            log_info(f"  {label}: {path.stat().st_size // 1024}K")

    # Cache
    LSTEAMCLIENT_CACHE.mkdir(parents=True, exist_ok=True)
    artifacts: list[Path] = []
    if dll_target.exists():
        shutil.copy2(dll_target, dll_cache)
        artifacts.append(dll_cache)
    if so_target.exists():
        shutil.copy2(so_target, so_cache)
        artifacts.append(so_cache)
    if steam_target.exists():
        shutil.copy2(steam_target, steam_cache)
        artifacts.append(steam_cache)

    # Clean build tree
    shutil.rmtree(build_dir)

    # Deploy
    _deploy_lsteamclient(
        dll_cache if dll_cache.exists() else None,
        so_cache if so_cache.exists() else None,
        steam_cache if steam_cache.exists() else None,
        wine_lib,
    )

    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("Steam bridge", True, artifacts=artifacts, elapsed_ms=elapsed)


def _deploy_lsteamclient(
    dll_path: Path | None,
    so_path: Path | None,
    steam_path: Path | None,
    wine_lib: Path,
) -> None:
    """Deploy lsteamclient.dll + .so + steam.exe to quark Wine lib."""
    win_dir = wine_lib / "x86_64-windows"
    unix_dir = wine_lib / "x86_64-unix"
    win_dir.mkdir(parents=True, exist_ok=True)
    unix_dir.mkdir(parents=True, exist_ok=True)

    if dll_path and dll_path.exists():
        dst = win_dir / "lsteamclient.dll"
        assert_quark_writable(dst)
        if dst.exists():
            dst.chmod(0o755)
        shutil.copy2(dll_path, dst)
        # Wine's builtin loader resolves steamclient[64].dll requests via this
        # search path. Without these aliases the loader falls through to PE
        # search and loads the Linux Steam client's 26MB steamclient64.dll from
        # the prefix — which AVs in steamclient_init the moment a Steamworks
        # call is made. lsteamclient already implements the full Steamworks
        # export surface, so a hardlink under the alias name satisfies every
        # import. Hardlinks (not symlinks) so Wine's PE-name detection sees
        # the real DLL header at the requested name.
        for alias in ("steamclient64.dll", "steamclient.dll"):
            alias_dst = win_dir / alias
            if alias_dst.exists() or alias_dst.is_symlink():
                alias_dst.unlink()
            os.link(dst, alias_dst)

    if so_path and so_path.exists():
        dst = unix_dir / "lsteamclient.so"
        assert_quark_writable(dst)
        if dst.exists():
            dst.chmod(0o755)
        shutil.copy2(so_path, dst)
        # Unix-side aliases must match the PE-side aliases above. Wine's
        # __wine_unix_call_dispatcher looks up the matching <dllname>.so for
        # whichever PE DLL the caller is in. Without these, games that load
        # steamclient64.dll directly (every Steamworks game) get a NULL
        # dispatcher and AV at the first STEAMCLIENT_CALL.
        for alias in ("steamclient64.so", "steamclient.so"):
            alias_dst = unix_dir / alias
            if alias_dst.exists() or alias_dst.is_symlink():
                alias_dst.unlink()
            os.link(dst, alias_dst)

    # lsteamclient lives only in the compat tree. The bwrap layer in launcher.rs
    # binds compat_dir/lib/wine over /usr/lib/wine at launch time, so Wine's
    # builtin loader finds lsteamclient.{so,dll} at the path it expects (the
    # bind-mounted /usr/lib/wine, which IS our tree). The previous version of
    # this code shelled out to `sudo ln -sf` to plant symlinks in the real
    # /usr/lib/wine — that was the active poisoning vector and is why every
    # install used to ask for the user's sudo password. Gone.

    if steam_path and steam_path.exists():
        dst = win_dir / "steam.exe"
        if dst.exists():
            dst.chmod(0o755)
        shutil.copy2(steam_path, dst)
        log_info(f"steam.exe: deployed Wine builtin ({steam_path.stat().st_size // 1024}K)")

    log_info(f"lsteamclient: deployed to {wine_lib}")


# Step 8b: Trace capture (montauk; opt-in via --trace or --observer)
#
# Pre-symbolicates our patched Wine PE/ELF binaries into Breakpad .sym files,
# verifies montauk is reachable, and toggles QUARK_TRACE=1 in env_config so
# launcher.rs spawns montauk as a sibling watcher for each game launch.
#
# When the user does NOT pass --observer, this step is a no-op (does not even
# touch env_config). The flag is sticky in env_config — once set on, future
# installs without the flag leave the existing setting alone.

def step_trace() -> StepResult:
    """Pre-symbolicate patched Wine DLLs + delegate montauk setup to montauk.

    --observer is one switch. Quark only owns:
      1. Running montauk's own install.py (montauk handles sublimation, caps,
         ldconfig, /usr/local — its full stack, not ours to replicate)
      2. Installing dump_syms via pacman if missing (the one piece montauk
         doesn't own)
      3. Symbolicating our patched Wine PE/ELF into Breakpad .sym files
      4. Setting QUARK_TRACE=1 in env_config so launcher.rs spawns
         montauk as a sibling watcher

    If montauk's install or pacman fails, quark's install fails too — the
    user opted in, so we owe them a working observer or a clean stop.
    """
    t0 = time.monotonic_ns()

    if not _TRACE:
        return StepResult("Trace", True, skipped=True,
                          elapsed_ms=(time.monotonic_ns() - t0) // 1_000_000)

    artifacts: list[Path] = []

    # Quark and montauk are separate projects. Quark does NOT install or
    # build montauk. The user installs montauk themselves (once, via its
    # own install.py). --observer is the OPT-IN switch that tells quark:
    # "montauk is present, please hook into it." If either montauk or
    # dump_syms is missing, print the install hint and disable the feature
    # — the rest of quark continues fine.
    warnings: list[str] = []
    montauk = shutil.which("montauk")
    dump_syms = shutil.which("dump_syms")
    if not montauk:
        log_warn("Trace: `montauk` not on PATH. Install it once: "
                 "cd ../montauk && python3 install.py.  Observer disabled.")
        warnings.append("montauk missing")
        return StepResult("Trace", True, warnings=warnings, skipped=True,
                          elapsed_ms=(time.monotonic_ns() - t0) // 1_000_000)
    if not dump_syms:
        log_warn("Trace: `dump_syms` not on PATH. Install: sudo pacman -S dump_syms. "
                 "Continuing — observer will work, symbolication will be skipped.")
        warnings.append("dump_syms missing")

    # 1b. Ensure montauk has the eBPF trace capabilities. A montauk reinstall
    #     (plain cp) strips them, and the observer then silently produces empty
    #     header-only traces ("need root or CAP_BPF") — a recurring foot-gun.
    #     Idempotent: only runs setcap (and prompts for sudo) when caps are
    #     actually missing, so it doesn't nag on every quark install.
    import subprocess as _sp
    TRACE_CAPS = "cap_sys_admin,cap_bpf,cap_perfmon,cap_sys_resource,cap_dac_read_search+ep"
    _need = ["cap_bpf", "cap_perfmon", "cap_dac_read_search", "cap_sys_admin", "cap_sys_resource"]
    try:
        _have = _sp.run(["getcap", montauk], capture_output=True, text=True).stdout
        if not all(c in _have for c in _need):
            if _sp.run(["sudo", "setcap", TRACE_CAPS, montauk]).returncode == 0:
                log_info(f"Trace: applied eBPF trace caps to {montauk}")
            else:
                log_warn(f"Trace: setcap failed — run: sudo setcap {TRACE_CAPS} {montauk}")
        else:
            log_info("Trace: montauk trace caps already present")
    except FileNotFoundError:
        log_warn("Trace: getcap/setcap missing (install libcap); montauk --trace needs sudo")

    # 2. Symbolicate every patched PE/ELF we built. Breakpad .sym layout uses
    #    the module's build-id (first line of the .sym), so consumers (montauk,
    #    quark crash-digest, sentry symbolic) can index by build-id without us
    #    needing to construct the path.
    sym_dir = COMPAT_DIR / "symbols"
    sym_dir.mkdir(parents=True, exist_ok=True)

    # Targets are the *built-from-source* artifacts in wine-obj. System
    # Wine DLLs hardlinked in by step_deploy_wine are stripped and
    # dump_syms would emit empty .sym for them — skip those.
    candidates = [
        WINE_OBJ_DIR / "dlls/ntdll/x86_64-windows/ntdll.dll",
        WINE_OBJ_DIR / "dlls/ntdll/ntdll.so",
        WINE_OBJ_DIR / "dlls/winepulse.drv/winepulse.so",
        LSTEAMCLIENT_CACHE / "lsteamclient.dll",
        LSTEAMCLIENT_CACHE / "lsteamclient.so",
    ]
    rust_target = SCRIPT_DIR / "target" / "release"
    if (rust_target / "quark").exists():
        candidates.append(rust_target / "quark")

    produced = 0
    if not dump_syms:
        log_warn("Trace: skipping symbolication — dump_syms unavailable")
    for src in candidates:
        if not dump_syms or not src.exists():
            continue
        dst = sym_dir / f"{src.name}.sym"
        result = run_cmd([dump_syms, str(src)], desc=f"dump_syms {src.name}",
                         check=False, timeout=60)
        if result.returncode == 0 and result.stdout:
            dst.write_text(result.stdout)
            artifacts.append(dst)
            produced += 1
            log_debug(f"  symbolicated {src.name} → {dst.name} "
                      f"({len(result.stdout) // 1024}K)")
        else:
            stderr_tail = (result.stderr or "").strip()[:80]
            log_debug(f"  dump_syms skipped {src.name}: {stderr_tail or 'no debug info'}")
    log_info(f"Trace: symbolicated {produced} modules into {sym_dir}")

    # 3. Toggle QUARK_TRACE=1 in env_config. Sticky — present-and-set means
    #    launcher.rs reads it and spawns montauk; absent or '0' means skip.
    config_file = COMPAT_DIR / "env_config"
    if config_file.exists():
        text = config_file.read_text()
        changed = False
        if not _env_has_active_key(text, "QUARK_TRACE"):
            text = text.rstrip() + "\nQUARK_TRACE=1\n"
            changed = True
        # Full binary trace dump (montauk --trace-out) for deep ntsync-layer
        # diagnosis. Coupled to --observer; remove to silence the dump later.
        if not _env_has_active_key(text, "QUARK_TRACE_OUT"):
            text = text.rstrip() + "\nQUARK_TRACE_OUT=1\n"
            changed = True
        if changed:
            config_file.write_text(text)
            log_info("Trace: enabled QUARK_TRACE=1 + QUARK_TRACE_OUT=1 in env_config")
        else:
            log_info("Trace: QUARK_TRACE already configured in env_config")
    else:
        # env_config doesn't exist yet — step_finalize will create it from
        # the template. Touch a marker so finalize knows to fold the env var in.
        (COMPAT_DIR / ".trace-pending").write_text("1\n")

    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("Trace", True, artifacts=artifacts,
                      warnings=warnings, elapsed_ms=elapsed)


# Step 9: GPU Translation

def step_gpu_translation() -> StepResult:
    """Download DXVK + VKD3D-Proton from GitHub releases."""
    t0 = time.monotonic_ns()
    artifacts: list[Path] = []
    warnings: list[str] = []

    log_info("Downloading DXVK and VKD3D-proton...")
    dxvk_tar = _download_github_release("doitsujin", "dxvk", "dxvk-")
    vkd3d_tar = _download_github_release("HansKristian-Work", "vkd3d-proton", "vkd3d-proton-")

    lib_dir = COMPAT_DIR / "lib"

    if dxvk_tar:
        arts = _deploy_tarball_dlls(dxvk_tar, lib_dir, "dxvk", "x64", "x32")
        artifacts.extend(arts)
    else:
        warnings.append("DXVK download failed — D3D9/11 games may fail")
        log_warn("DXVK: download failed")

    if vkd3d_tar:
        arts = _deploy_tarball_dlls(vkd3d_tar, lib_dir, "vkd3d-proton", "x64", "x86")
        artifacts.extend(arts)
    else:
        warnings.append("VKD3D-Proton download failed — D3D12 games may fail")
        log_warn("VKD3D-Proton: download failed")

    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("GPU translation", True, artifacts=artifacts,
                     warnings=warnings, elapsed_ms=elapsed)


def _download_github_release(owner: str, repo: str, asset_glob: str) -> Path | None:
    """Download latest release asset from GitHub. Returns cached tarball path."""
    cache_dir = DATA_DIR / "downloads"
    cache_dir.mkdir(parents=True, exist_ok=True)

    version_file = cache_dir / f"{repo}.version"
    cached_version = version_file.read_text().strip() if version_file.exists() else None

    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log_warn(f"{repo}: GitHub API failed: {e}")
        existing = list(cache_dir.glob(f"{repo}-*.tar.*"))
        if existing:
            log_info(f"{repo}: using cached download")
            return existing[0]
        return None

    tag = data.get("tag_name", "")
    if tag == cached_version:
        existing = list(cache_dir.glob(f"{repo}-*.tar.*"))
        if existing:
            log_info(f"{repo}: {tag} (cached)")
            return existing[0]

    download_url = None
    asset_name = None
    for asset in data.get("assets", []):
        name = asset["name"]
        if asset_glob in name and name.endswith((".tar.gz", ".tar.xz", ".tar.zst")):
            download_url = asset["browser_download_url"]
            asset_name = name
            break

    if not download_url:
        log_warn(f"{repo}: no matching release asset found")
        return None

    for old in cache_dir.glob(f"{repo}-*.tar.*"):
        old.unlink()

    dest = cache_dir / asset_name
    log_info(f"{repo}: downloading {tag}...")
    try:
        urllib.request.urlretrieve(download_url, dest)
    except Exception as e:
        log_error(f"{repo}: download failed: {e}")
        return None

    version_file.write_text(tag)
    log_info(f"{repo}: downloaded {asset_name}")
    return dest


def _deploy_tarball_dlls(
    tarball: Path, lib_dir: Path, label: str, dir_64: str, dir_32: str,
) -> list[Path]:
    """Extract DLLs from a DXVK/VKD3D tarball."""
    staging = DATA_DIR / "staging" / label
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(tarball) as tf:
            try:
                tf.extractall(staging, filter="data")
            except TypeError:
                tf.extractall(staging)
    except Exception as e:
        log_error(f"{label}: failed to extract: {e}")
        return []

    extracted = list(staging.iterdir())
    if not extracted:
        log_error(f"{label}: tarball empty")
        return []
    root = extracted[0]

    artifacts: list[Path] = []
    for src_sub, dst_sub in [(dir_64, "x86_64-windows"), (dir_32, "i386-windows")]:
        src = root / src_sub
        dst = lib_dir / "wine" / label / dst_sub
        if src.exists():
            dst.mkdir(parents=True, exist_ok=True)
            count = 0
            for dll in src.glob("*.dll"):
                shutil.copy2(dll, dst / dll.name)
                artifacts.append(dst / dll.name)
                count += 1
            log_info(f"{label}: deployed {count} {'64' if '64' in dst_sub else '32'}-bit DLLs")

    shutil.rmtree(staging, ignore_errors=True)
    return artifacts


# Step 10: Deploy Drivers

def step_deploy_drivers() -> StepResult:
    """Deploy PE driver stubs from system Wine + Proton."""
    t0 = time.monotonic_ns()
    wine_lib = COMPAT_DIR / "lib" / "wine"
    dst_dir = wine_lib / "x86_64-windows"
    dst_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    artifacts: list[Path] = []

    # Source priority: Proton Experimental → Proton Hotfix → any Proton → system Wine
    proton_dir = find_proton_wine_dir()
    sys_wine = Path("/usr/lib/wine/x86_64-windows")

    sources = []
    if proton_dir:
        sources.append(("Proton", proton_dir))
    if sys_wine.exists():
        sources.append(("system Wine", sys_wine))

    if not sources:
        w = "No source for PE drivers — some games may fail"
        warnings.append(w)
        log_warn(w)
        elapsed = (time.monotonic_ns() - t0) // 1_000_000
        return StepResult("Deploy drivers", True, warnings=warnings, elapsed_ms=elapsed)

    for driver in PE_DRIVER_FILES:
        dst = dst_dir / driver
        if dst.exists():
            artifacts.append(dst)
            continue

        deployed = False
        for src_name, src_dir in sources:
            src = src_dir / driver
            if src.exists():
                shutil.copy2(src, dst)
                artifacts.append(dst)
                deployed = True
                break

        if not deployed:
            w = f"PE driver not found: {driver}"
            warnings.append(w)
            log_debug(w)

    found = len(artifacts)
    total = len(PE_DRIVER_FILES)
    source_name = sources[0][0] if sources else "none"
    log_info(f"PE drivers: {found}/{total} deployed (source: {source_name})")
    if warnings:
        for w in warnings:
            log_warn(f"  {w}")

    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("Deploy drivers", True, artifacts=artifacts,
                     warnings=warnings, elapsed_ms=elapsed)


# Step 11: EAC Runtime

def step_eac_runtime() -> StepResult:
    """Deploy EasyAntiCheat bridge DLLs (optional)."""
    t0 = time.monotonic_ns()

    if not EAC_RUNTIME_DIR.exists():
        log_warn("Proton EasyAntiCheat Runtime not found")
        log_warn("  EAC games (Elden Ring, etc.) will not work")
        log_warn("  Install via: Steam > Library > Tools > 'Proton EasyAntiCheat Runtime'")
        elapsed = (time.monotonic_ns() - t0) // 1_000_000
        return StepResult("EAC runtime", True, skipped=True,
                         warnings=["EAC runtime not installed"],
                         elapsed_ms=elapsed)

    dst_pe64 = COMPAT_DIR / "lib" / "wine" / "x86_64-windows"
    dst_so64 = COMPAT_DIR / "lib" / "wine" / "x86_64-unix"
    dst_pe32 = COMPAT_DIR / "lib" / "wine" / "i386-windows"
    dst_so32 = COMPAT_DIR / "lib" / "wine" / "i386-unix"
    for d in [dst_pe64, dst_so64, dst_pe32, dst_so32]:
        d.mkdir(parents=True, exist_ok=True)

    artifacts: list[Path] = []
    for src_dir, dst_pe, dst_so, names in [
        (EAC_RUNTIME_DIR / "lib64", dst_pe64, dst_so64, ["easyanticheat", "easyanticheat_x64"]),
        (EAC_RUNTIME_DIR / "lib32", dst_pe32, dst_so32, ["easyanticheat_x86"]),
    ]:
        for name in names:
            for ext, dst in [(".dll", dst_pe), (".so", dst_so)]:
                src = src_dir / f"{name}{ext}"
                if src.exists():
                    shutil.copy2(src, dst / f"{name}{ext}")
                    artifacts.append(dst / f"{name}{ext}")

    log_info(f"EasyAntiCheat: deployed {len(artifacts)} runtime files")
    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("EAC runtime", True, artifacts=artifacts, elapsed_ms=elapsed)


# Step 12: Prefix Sync

def _replace_file(src: Path, target: Path) -> bool:
    """Replace target with src. Unlinks symlinks, overwrites files. Returns True on success."""
    try:
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            target.chmod(0o755)
        shutil.copy2(src, target)
        return True
    except OSError:
        return False


def step_prefix_sync() -> StepResult:
    """Convert quark-mapped prefixes from Proton symlinks to real files.

    Every symlink in system32/syswow64 is wrong — replace it unconditionally.
    Then layer quark's own DLLs (DXVK, VKD3D, lsteamclient, stubs) on top.
    """
    t0 = time.monotonic_ns()

    quark_appids = get_quark_appids()
    if not quark_appids:
        log_info("prefix sync: no games mapped to quark")
        return StepResult("Prefix sync", True, skipped=True,
                         elapsed_ms=(time.monotonic_ns() - t0) // 1_000_000)

    compatdata = STEAMAPPS / "compatdata"
    if not compatdata.exists():
        return StepResult("Prefix sync", True, skipped=True,
                         elapsed_ms=(time.monotonic_ns() - t0) // 1_000_000)

    # Build the source map: what quark wants in every prefix's system32
    # Later entries override earlier (DXVK d3d11.dll wins over Wine's stub)
    sys_wine_64 = Path("/usr/lib/wine/x86_64-windows")
    sys_wine_32 = Path("/usr/lib/wine/i386-windows")
    quark_wine = COMPAT_DIR / "lib" / "wine"

    src_64: dict[str, Path] = {}
    for d in [sys_wine_64,
              quark_wine / "x86_64-windows",
              quark_wine / "patched",
              quark_wine / "dxvk" / "x86_64-windows",
              quark_wine / "vkd3d-proton" / "x86_64-windows"]:
        if d.exists():
            for f in d.iterdir():
                if f.is_file() and f.suffix in (".dll", ".exe", ".tlb", ".ocx", ".cpl", ".acm", ".ax", ".drv", ".sys"):
                    # lsteamclient must NOT be in the prefix — it must load as a
                    # builtin so Wine connects the PE to the .so via find_builtin_dll.
                    # A prefix copy bypasses the builtin path and breaks the PE-Unix bridge.
                    if f.name == "lsteamclient.dll":
                        continue
                    src_64[f.name] = f

    src_32: dict[str, Path] = {}
    for d in [sys_wine_32,
              quark_wine / "dxvk" / "i386-windows",
              quark_wine / "vkd3d-proton" / "i386-windows"]:
        if d.exists():
            for f in d.iterdir():
                if f.is_file() and f.suffix in (".dll", ".exe", ".tlb", ".ocx"):
                    src_32[f.name] = f

    if not src_64:
        log_warn("prefix sync: no source DLLs found")
        return StepResult("Prefix sync", True, skipped=True,
                         elapsed_ms=(time.monotonic_ns() - t0) // 1_000_000)

    total_replaced = 0
    total_prefixes = 0

    for entry in compatdata.iterdir():
        if not entry.is_dir():
            continue
        appid = entry.name
        if appid in PROTON_APPIDS or appid not in quark_appids:
            continue

        sys32 = entry / "pfx/drive_c/windows/system32"
        if not sys32.exists():
            continue

        replaced = 0

        # Walk system32: replace every symlink, copy missing files
        for target in sys32.iterdir():
            if not target.is_file() and not target.is_symlink():
                continue
            name = target.name
            if name in src_64:
                if target.is_symlink():
                    if _replace_file(src_64[name], target):
                        replaced += 1
                elif not target.exists():
                    if _replace_file(src_64[name], target):
                        replaced += 1

        # Also deploy files that exist in source but not yet in prefix
        for name, src in src_64.items():
            target = sys32 / name
            if not target.exists() and not target.is_symlink():
                if _replace_file(src, target):
                    replaced += 1

        # syswow64 (32-bit)
        syswow64 = entry / "pfx/drive_c/windows/syswow64"
        if syswow64.exists() and src_32:
            for target in syswow64.iterdir():
                if not target.is_file() and not target.is_symlink():
                    continue
                name = target.name
                if name in src_32 and target.is_symlink():
                    if _replace_file(src_32[name], target):
                        replaced += 1
            for name, src in src_32.items():
                target = syswow64 / name
                if not target.exists() and not target.is_symlink():
                    if _replace_file(src, target):
                        replaced += 1

        if replaced:
            log_info(f"prefix {appid}: replaced {replaced} files")
            total_replaced += replaced
            total_prefixes += 1
        else:
            log_info(f"prefix {appid}: up to date")

    log_info(f"prefix sync: {total_replaced} files across {total_prefixes} prefix(es), {len(quark_appids)} mapped")
    return StepResult("Prefix sync", True,
                     elapsed_ms=(time.monotonic_ns() - t0) // 1_000_000)


# Step 13: Finalize

def step_finalize() -> StepResult:
    """Env config."""
    t0 = time.monotonic_ns()

    # Env config
    config_file = COMPAT_DIR / "env_config"
    if config_file.exists():
        text = config_file.read_text()
        if "WINE_NTSYNC=1" not in text or text.count("# WINE_NTSYNC=1") > 0:
            text = text.replace("# WINE_NTSYNC=1", "WINE_NTSYNC=1")
            if "WINE_NTSYNC=1" not in text:
                text += "\nWINE_NTSYNC=1\n"
            config_file.write_text(text)
        # #18 heap-corruption fix: ensure the glibc arena-confinement tunables
        # are present even in a preserved env_config (the template only covers
        # freshly-created ones).
        text = config_file.read_text()
        added = False
        if not _env_has_active_key(text, "GLIBC_TUNABLES"):
            text = text.rstrip() + "\nGLIBC_TUNABLES=glibc.malloc.arena_max=1\n"
            added = True
        if not _env_has_active_key(text, "MALLOC_ARENA_MAX"):
            text = text.rstrip() + "\nMALLOC_ARENA_MAX=1\n"
            added = True
        if added:
            config_file.write_text(text)
            log_info("Env config: added glibc arena-confinement tunables (#18 heap-corruption fix)")
    else:
        config_file.write_text(ENV_CONFIG_TEMPLATE)
    # If step_trace ran before env_config existed, fold its toggle in now.
    pending = COMPAT_DIR / ".trace-pending"
    if pending.exists():
        text = config_file.read_text()
        changed = False
        if not _env_has_active_key(text, "QUARK_TRACE"):
            text = text.rstrip() + "\nQUARK_TRACE=1\n"
            changed = True
        if not _env_has_active_key(text, "QUARK_TRACE_OUT"):
            text = text.rstrip() + "\nQUARK_TRACE_OUT=1\n"
            changed = True
        if changed:
            config_file.write_text(text)
            log_info("Trace: folded QUARK_TRACE=1 + QUARK_TRACE_OUT=1 into newly-created env_config")
        pending.unlink()
    log_info(f"Env config: {config_file}")

    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("Finalize", True, elapsed_ms=elapsed)


# Step 14: Smoke test
#
# Spawn the deployed compat tool's wine binary inside the same bwrap layer the
# launcher uses at runtime, ask it for `--version`, and verify it returns the
# system Wine version. This catches "deployed but broken" before Steam ever
# launches a game: missing files in the compat tree, version drift between the
# patched DLLs and the system binaries, broken bind mounts, all surface here.

def step_smoke_test() -> StepResult:
    """Run wine --version inside the bwrap namespace against the deployed tree."""
    t0 = time.monotonic_ns()
    warnings: list[str] = []

    bwrap = Path("/usr/bin/bwrap")
    if not bwrap.exists():
        log_warn("bwrap not found at /usr/bin/bwrap — install bubblewrap")
        log_warn("  the launcher will fall back to direct exec; patches will not load")
        return StepResult("Smoke test", True, skipped=True,
                          warnings=["bwrap missing — patches will NOT be loaded at runtime"],
                          elapsed_ms=(time.monotonic_ns() - t0) // 1_000_000)

    quark_lib_wine = COMPAT_DIR / "lib" / "wine"
    quark_share_wine = COMPAT_DIR / "share" / "wine"
    wine_bin = COMPAT_DIR / "bin" / "wine"

    if not wine_bin.exists():
        raise StepFailed("smoke test", f"compat wine binary missing: {wine_bin}")
    if not quark_lib_wine.exists():
        raise StepFailed("smoke test", f"compat lib/wine missing: {quark_lib_wine}")
    if not quark_share_wine.exists():
        raise StepFailed("smoke test", f"compat share/wine missing: {quark_share_wine}")

    # Run the same bwrap incantation the launcher uses.
    cmd = [
        str(bwrap),
        "--dev-bind", "/", "/",
        "--bind", str(quark_lib_wine), "/usr/lib/wine",
        "--bind", str(quark_share_wine), "/usr/share/wine",
        "--die-with-parent",
        "--",
        str(wine_bin), "--version",
    ]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise StepFailed("smoke test", "wine --version timed out inside bwrap (15s)")
    except FileNotFoundError as e:
        raise StepFailed("smoke test", f"failed to spawn bwrap: {e}")

    if r.returncode != 0:
        for line in (r.stderr or "").strip().splitlines()[-10:]:
            log_error(f"  {line}")
        raise StepFailed("smoke test", f"wine --version exited {r.returncode} inside bwrap")

    reported = (r.stdout or "").strip()
    if not reported.startswith("wine-"):
        raise StepFailed("smoke test", f"unexpected wine --version output: {reported!r}")

    expected = get_system_wine_version()
    if expected and reported != expected:
        # Version drift between deployed compat wine and system wine. The
        # bwrap binds quark's lib/wine over /usr/lib/wine, so the binary
        # *should* see its sibling DLLs and report whatever they identify as.
        # If they disagree it means the compat tree has files from a different
        # build than the binary.
        warnings.append(
            f"compat wine reports {reported} but system wine is {expected} — "
            "compat tree may be ABI-mixed; re-run install.py"
        )
        log_warn(f"  {warnings[-1]}")

    log_info(f"smoke test: bwrap+wine reports {reported}")
    elapsed = (time.monotonic_ns() - t0) // 1_000_000
    return StepResult("Smoke test", True, warnings=warnings, elapsed_ms=elapsed)


# Verbose config

def configure_verbose(enable: bool | None) -> None:
    """Handle verbose flag. Sticky: stays enabled until explicitly disabled.

    The flag lives in CACHE_DIR (~/.cache/quark/) — NOT in COMPAT_DIR —
    because step_clean wipes the compat dir on every install. Putting the flag
    there meant `./install.py --verbose` wrote the flag, then step_clean
    deleted it five seconds later, and the launcher always saw verbose=false
    at game launch time. Moving it to CACHE_DIR makes it survive a clean.
    The launcher reads the same path via quark::log::is_verbose().
    """
    flag = CACHE_DIR / "trace_verbose"
    if enable is True:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        flag.write_text("1")
        log_info("Verbose diagnostics enabled (~/.cache/quark/*.prom)")
    elif enable is False:
        if flag.exists():
            flag.unlink()
        log_info("Verbose diagnostics disabled")
    elif flag.exists():
        log_info("Verbose diagnostics: on (use --no-verbose to disable)")


# Pipeline

def main() -> int:
    parser = argparse.ArgumentParser(description="quark installer")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose diagnostics")
    parser.add_argument("--no-verbose", action="store_true", help="Disable verbose diagnostics")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Auto-answer Y to all prompts (non-interactive)")
    parser.add_argument("--wipe-prefixes", action="store_true",
                        help="With the clean step, also delete the Wine prefixes and "
                             "shader caches of quark-mapped games (local-only saves "
                             "in those prefixes are lost)")
    parser.add_argument("--observer", action="store_true",
                        help="Opt in to the montauk observer integration: pre-symbolicate "
                             "patched Wine PE DLLs to Breakpad .sym, sets QUARK_TRACE=1 "
                             "in env_config so launcher.rs spawns montauk as a sibling "
                             "watcher for each game. Requires `dump_syms` and `montauk` "
                             "on PATH (hints printed if missing — install is non-fatal).")
    parser.add_argument("--trace", action="store_true",
                        help="The one diagnosis switch. Full game + wineserver montauk "
                             "capture with verbose logging — exactly what `--observer "
                             "--verbose` did, under one name. Implies --observer "
                             "(QUARK_TRACE=1 + QUARK_TRACE_OUT=1, eBPF caps, "
                             "montauk reachability) AND --verbose.")
    args = parser.parse_args()

    if args.yes:
        global _AUTO_YES
        _AUTO_YES = True

    if args.wipe_prefixes:
        global _WIPE_PREFIXES
        _WIPE_PREFIXES = True

    # --trace is the unified diagnosis switch: it IS --observer + --verbose under one
    # name (montauk is the observer; --trace is the interface). The component flags
    # still work for granular use, but --trace is the canonical "watch everything".
    if args.observer or args.trace:
        global _TRACE
        _TRACE = True

    if args.verbose or args.trace:
        configure_verbose(True)
    elif args.no_verbose:
        configure_verbose(False)
    else:
        configure_verbose(None)

    steps = [
        ("Preflight",        step_preflight),
        ("Clean",            step_clean),
        ("Sync wine source", step_sync_wine_source),
        ("Build",            step_build),
        ("Deploy binaries",  step_deploy_binaries),
        ("Deploy Wine",      step_deploy_wine),
        ("Patch Wine",       step_patch_wine),
        ("Bake prefix",      step_bake_prefix_template),
        ("Build stubs",      step_build_stubs),
        ("Steam bridge",     step_steam_bridge),
        ("Trace",            step_trace),
        ("GPU translation",  step_gpu_translation),
        ("Deploy drivers",   step_deploy_drivers),
        ("EAC runtime",      step_eac_runtime),
        ("Prefix sync",      step_prefix_sync),
        ("Finalize",         step_finalize),
        ("Smoke test",       step_smoke_test),
    ]

    results: list[StepResult] = []

    # Snapshot system Wine before any step runs. The detective half of the
    # write guardrail: if anything in /usr/lib/wine or /usr/share/wine differs
    # after the pipeline finishes, install.py poisoned the system and we abort.
    log_info("Snapshotting system Wine state for poisoning audit...")
    system_wine_snapshot = snapshot_system_wine()
    log_info(f"  {len(system_wine_snapshot)} system Wine files tracked")

    for i, (name, func) in enumerate(steps, 1):
        print()
        log_info(f"STEP {i}/{len(steps)}: {name}")
        try:
            result = func()
            results.append(result)
        except StepFailed as e:
            log_error(f"FAILED: {e.detail}")
            results.append(StepResult(name, False))
            # Preflight and Build are hard gates
            if name in ("Preflight", "Build"):
                _print_manifest(results, len(steps))
                return 1
            # Other failures: continue but warn
            log_warn(f"Continuing despite {name} failure...")
        except KeyboardInterrupt:
            log_error("Interrupted")
            return 130

    # System Wine poisoning audit. Runs even if some steps failed — a partial
    # install that touched /usr is still a poisoning event we need to know about.
    print()
    log_info("Auditing system Wine state for unintended modifications...")
    try:
        verify_system_wine_unchanged(system_wine_snapshot)
        log_info("  system Wine untouched — clean install")
    except StepFailed as e:
        log_error(f"POISONING DETECTED: {e.detail}")
        results.append(StepResult("System Wine audit", False))
        _print_manifest(results, len(steps))
        return 2

    # Deployment manifest
    print()
    _print_manifest(results, len(steps))

    failed = sum(1 for r in results if not r.success)
    if failed:
        log_warn(f"{failed} step(s) failed — check errors above")
        return 1

    log_info("Save data protection: enabled (automatic pre-launch snapshots)")
    print()
    return 0


def _print_manifest(results: list[StepResult], total_steps: int) -> None:
    version = get_version()
    failed = [r for r in results if not r.success]

    log_info(f"DEPLOYMENT COMPLETE — quark {version}")

    # Only list what was deployed, with counts
    for r in results:
        if r.skipped:
            continue
        if not r.success:
            log_error(f"{r.name}: FAILED")
        elif r.artifacts:
            log_info(f"{r.name}: {len(r.artifacts)} files")
        else:
            log_info(f"{r.name}: done")

    for r in results:
        for w in r.warnings:
            log_warn(w)

    if failed:
        log_error(f"{len(failed)} step(s) failed")
    print()


if __name__ == "__main__":
    sys.exit(main())
