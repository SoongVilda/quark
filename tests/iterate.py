#!/usr/bin/env python3
"""Build → install → launch → monitor → analyze cycle for quark.

Usage:
    python3 tests/iterate.py                  # Full cycle with Balatro
    python3 tests/iterate.py --skip-build     # Skip cargo build
    python3 tests/iterate.py --timeout 60     # Longer timeout
    python3 tests/iterate.py --appid 2218750  # Different game
"""

import argparse
import glob
import json
import os
import pathlib
import re
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quark_common import (
    REPO_ROOT, QUARK_BIN, STEAM_ROOT, STEAMAPPS, COMPAT_DIR, LOG_DIR,
    log_info, log_warn, log_error, remove_stale_wine_sockets,
)

# Compat-tool launcher table. Default is quark; stock Proton entries enable
# side-by-side A/B comparison ("does the game work outside quark?")
# without leaving the iterate harness.
COMPAT_TOOLS = {
    "quark":     COMPAT_DIR / "proton",
    "proton10":  STEAMAPPS / "common/Proton 10.0/proton",
    "proton9":   STEAMAPPS / "common/Proton 9.0/proton",
    "proton-exp": STEAMAPPS / "common/Proton - Experimental/proton",
}

# Resolved per run; replaced by main() when --compat-tool is parsed.
PROTON_BIN = COMPAT_TOOLS["quark"]

def clock_raw_ns() -> int:
    """CLOCK_MONOTONIC_RAW in nanoseconds — hardware TSC, no NTP slew."""
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)

def ns_to_ms(ns: int) -> float:
    return ns / 1_000_000

WINE_INIT_GLOB = str(LOG_DIR / "wine_init_*.log")

DEFAULT_APPID = "2379780"  # Balatro
DEFAULT_TIMEOUT = 30

BASELINES_PATH = REPO_ROOT / "tests" / "baselines.json"

# x86_64 syscall numbers → names
SYSCALL_NAMES = {
    0: "read", 1: "write", 3: "close", 7: "poll", 8: "lseek",
    16: "ioctl", 23: "select", 35: "nanosleep", 44: "sendto",
    45: "recvfrom", 46: "sendmsg", 47: "recvmsg", 56: "clone",
    59: "execve", 61: "wait4", 202: "futex", 228: "clock_gettime",
    230: "clock_nanosleep", 232: "epoll_wait", 270: "ppoll",
    281: "epoll_pwait", 435: "epoll_pwait2",
}

EXE_BLACKLIST = {
    "unitycrashhandler64.exe", "unitycrashhandler32.exe",
    "crashreport.exe", "crashhandler.exe", "crashpad_handler.exe",
    "ue4prereqsetup_x64.exe", "installermessage.exe",
    "dxsetup.exe", "vcredist_x64.exe", "vcredist_x86.exe",
    "dukeworkshopuploader.exe",
    "dotnetfx35.exe", "dotnetfx35setup.exe",
    "beservice.exe", "beservice_x64.exe",
    "easyanticheat_setup.exe", "easyanticheat.exe",
    "replication-server.exe",
}

# ── Data types ────────────────────────────────────────────────────────

@dataclass
class ProcSnapshot:
    pid: int
    comm: str = ""
    state: str = ""
    wchan: str = ""
    syscall_nr: int = -1
    syscall_name: str = "?"
    syscall_args: str = ""
    fd_count: int = 0
    key_fds: list = field(default_factory=list)
    cmdline: str = ""

# ── Game discovery (from test_launch.py) ──────────────────────────────

def parse_appmanifest(path: Path) -> dict:
    text = path.read_text(errors="replace")
    result = {}
    for key in ("appid", "name", "installdir"):
        m = re.search(rf'"{key}"\s+"([^"]+)"', text)
        if m:
            result[key] = m.group(1)
    return result


def find_game_exe(install_dir: Path) -> str:
    if not install_dir.exists():
        return ""
    candidates = []
    for exe in install_dir.rglob("*.exe"):
        try:
            rel = exe.relative_to(install_dir)
            if len(rel.parts) > 3:
                continue
        except ValueError:
            continue
        if exe.name.lower() in EXE_BLACKLIST:
            continue
        parts_lower = [p.lower() for p in rel.parts]
        if any(skip in parts_lower for skip in
               ["easyanticheat", "battleye", "_commonredist", "directx", "redist"]):
            continue
        candidates.append((exe.stat().st_size, exe))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: -x[0])
    return str(candidates[0][1])


def find_game(app_id: str) -> tuple:
    """Returns (name, exe_path) for an app ID."""
    for manifest in STEAMAPPS.glob("appmanifest_*.acf"):
        info = parse_appmanifest(manifest)
        if info.get("appid") == app_id:
            install_dir = STEAMAPPS / "common" / info.get("installdir", "")
            exe = find_game_exe(install_dir)
            return (info.get("name", "?"), exe)
    return ("?", "")

# ── Process management ────────────────────────────────────────────────
#
# Steam-safety rule (the reason this file used to sign the user out):
# NEVER scan all processes and match by substring like ".exe", "wineserver",
# or bare "wine". Steam's own helpers (steamwebhelper, gameoverlayui) and
# any OTHER Proton game's wineserver/.exe processes match those patterns
# and get SIGKILLed, which corrupts Steam's session state and forces a
# re-login. Every pkill below MUST be scoped to either:
#   1. quark's compat tool directory (COMPAT_DIR string in cmdline), OR
#   2. a process whose /proc/PID/exe resolves inside COMPAT_DIR, OR
#   3. a descendant (via /proc PPID chain) of one of (1) or (2).
# Bare-name patterns are forbidden. There is a regression test in
# tests/test_iterate_steam_safety.py that enforces this.

def _quark_native_pids() -> set:
    """PIDs of processes whose binary lives inside quark's compat tool dir.

    These are quark's own native executables (proton launcher,
    lib/wine/x86_64-unix/wine, etc.) — never Steam helpers, never stock
    Proton."""
    compat_dir = str(COMPAT_DIR) + "/"
    pids = set()
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            exe = os.readlink(entry / "exe")
            if exe.startswith(compat_dir):
                pids.add(pid)
        except (FileNotFoundError, PermissionError, OSError):
            pass
    return pids


def _read_ppid(pid: int) -> int:
    """Parse PPID from /proc/PID/stat. Returns 0 if unavailable."""
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
        # stat format: pid (comm with spaces and parens) state ppid ...
        # comm is parenthesized; the LAST ')' delimits it.
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        return 0


def _descendants_of(roots: set, max_depth: int = 30) -> set:
    """All PIDs whose ancestor chain leads to a PID in roots.

    Walks the /proc snapshot once, building a child map keyed by parent,
    then BFS from roots. max_depth bounds pathological loops."""
    children: dict = {}
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        ppid = _read_ppid(pid)
        if ppid:
            children.setdefault(ppid, []).append(pid)
    out = set()
    frontier = list(roots)
    depth = 0
    while frontier and depth < max_depth:
        next_frontier = []
        for p in frontier:
            for c in children.get(p, ()):
                if c not in out and c not in roots:
                    out.add(c)
                    next_frontier.append(c)
        frontier = next_frontier
        depth += 1
    return out


def _our_quark_pids() -> set:
    """Every quark-related PID currently alive: native quark binaries +
    every descendant (wine64-preloader workers, .exe processes, etc.).

    Use this for the kill set. Never falls through to bare-name matching."""
    natives = _quark_native_pids()
    return natives | _descendants_of(natives)


TERM_GRACE_S = 8


def terminate_game(proc) -> None:
    """SIGTERM the game tree and wait for it to exit before the SIGKILL sweep.

    A Wine process that exits on its own puts back the display mode it
    changed; a SIGKILLed one leaves the panel where the game set it. The
    launcher ignores SIGTERM itself, so its pid is excluded from the wait."""
    targets = _our_quark_pids() - {proc.pid}
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    t0 = time.monotonic()
    while time.monotonic() - t0 < TERM_GRACE_S:
        if not (_our_quark_pids() & targets):
            break
        time.sleep(0.2)
    waited = time.monotonic() - t0
    alive = _our_quark_pids() & targets
    if alive:
        log_warn(f"stop: {len(alive)} game processes alive {waited:.1f}s after SIGTERM, SIGKILL")
    else:
        log_info(f"stop: game tree exited {waited:.1f}s after SIGTERM")
    proc.kill()
    proc.wait()


def cleanup_processes():
    """Tear down quark processes from any prior or current run.

    Steam-safe: every kill is scoped to /proc inspection of binaries living
    inside COMPAT_DIR plus their descendants. No bare-name pkill, no
    substring matching on cmdline. See header comment for the safety rule."""
    # Phase 1: SIGKILL everything quark-related, including .exe descendants.
    kill_set = _our_quark_pids()
    for pid in kill_set:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(0.5)

    # Phase 2: catch reparented orphans (descendants whose parent died
    # before us, so PPID is now 1 and they're no longer in our tree) and
    # the wineserver, which detaches into its own session and whose exe
    # resolves outside COMPAT_DIR. Both carry a WINESERVER env var pointing
    # into quark's compat dir — a strict quark fingerprint, no other Wine
    # process has it. Read /proc/PID/environ; skip anything we can't read.
    compatdata_root = str(STEAMAPPS / "compatdata") + "/"
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            environ = (entry / "environ").read_bytes().decode("utf-8", "replace")
        except (FileNotFoundError, PermissionError, OSError):
            continue
        env_kv = dict(
            kv.split("=", 1) for kv in environ.split("\0")
            if "=" in kv
        )
        wineprefix = env_kv.get("WINEPREFIX", "")
        wineserver = env_kv.get("WINESERVER", "")
        # Strict match: WINEPREFIX inside our compatdata AND WINESERVER
        # pointing to quark's compat dir. Both conditions required so we
        # never touch a stock-Proton orphan that happens to share the
        # compatdata path.
        if (wineprefix.startswith(compatdata_root)
                and str(COMPAT_DIR) in wineserver):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    time.sleep(0.5)

    # Phase 3: Clean stale sockets. Must come AFTER all kills + sleep so
    # the wineserver has fully exited and released the socket bind.
    remove_stale_wine_sockets()


def find_wine_pids() -> list:
    """All PIDs in quark's current process tree (for snapshotting / hang
    analysis). Scoped to quark — never returns stock Proton or Steam
    helper PIDs."""
    return sorted(_our_quark_pids())

# ── /proc inspection ──────────────────────────────────────────────────

def read_proc(pid: int, entry: str) -> str:
    try:
        return Path(f"/proc/{pid}/{entry}").read_text(errors="replace").strip()
    except (OSError, PermissionError):
        return ""


def snapshot_process(pid: int) -> ProcSnapshot:
    snap = ProcSnapshot(pid=pid)
    snap.comm = read_proc(pid, "comm")
    if not snap.comm:
        return snap

    status = read_proc(pid, "status")
    for line in status.splitlines():
        if line.startswith("State:"):
            snap.state = line.split()[1]
            break

    snap.wchan = read_proc(pid, "wchan")

    sc = read_proc(pid, "syscall")
    if sc and sc != "running":
        parts = sc.split()
        try:
            snap.syscall_nr = int(parts[0])
            snap.syscall_name = SYSCALL_NAMES.get(snap.syscall_nr, f"sys_{snap.syscall_nr}")
            snap.syscall_args = " ".join(parts[1:7])
        except (ValueError, IndexError):
            pass

    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        fds = list(fd_dir.iterdir())
        snap.fd_count = len(fds)
        for fd_link in sorted(fds, key=lambda f: int(f.name))[:20]:
            try:
                target = os.readlink(str(fd_link))
                snap.key_fds.append(f"{fd_link.name}->{target}")
            except OSError:
                pass
    except OSError:
        pass

    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        snap.cmdline = raw.replace(b"\x00", b" ").decode(errors="replace").strip()
    except OSError:
        pass

    return snap


def analyze_hang(snapshots: list) -> str:
    if not snapshots:
        return "No processes to analyze"
    lines = []
    for snap in snapshots:
        if not snap.comm:
            continue
        if snap.state in ("S", "D"):
            fd_info = ""
            if snap.syscall_name in ("read", "recvmsg", "write", "sendmsg", "poll", "ppoll", "ioctl"):
                parts = snap.syscall_args.split()
                if parts:
                    try:
                        fd_num = int(parts[0], 16)
                        for fd_entry in snap.key_fds:
                            if fd_entry.startswith(f"{fd_num}->"):
                                fd_info = fd_entry
                                break
                    except ValueError:
                        pass
            # Always try to extract fd from first syscall arg
            if not fd_info and snap.syscall_args:
                parts = snap.syscall_args.split()
                if parts:
                    try:
                        fd_num = int(parts[0], 16)
                        for fd_entry in snap.key_fds:
                            if fd_entry.startswith(f"{fd_num}->"):
                                fd_info = fd_entry
                                break
                        if not fd_info:
                            fd_info = f"fd={fd_num}"
                    except ValueError:
                        pass
            analysis = f"PID {snap.pid} ({snap.comm}) blocked in {snap.syscall_name}()"
            if snap.wchan and snap.wchan != "0":
                analysis += f" @ {snap.wchan}"
            if fd_info:
                analysis += f" on {fd_info}"
            if snap.syscall_args:
                analysis += f" args=[{snap.syscall_args}]"
            lines.append(analysis)
    return "\n  ".join(lines) if lines else "All processes in running state"

# ── Build & Install ──────────────────────────────────────────────────

def build(skip: bool = False) -> bool:
    if skip:
        log_info("build: skipped (--skip-build)")
        return True

    log_info("build: cargo build --release")
    t0 = time.monotonic()
    result = subprocess.run(
        ["cargo", "build", "--release"],
        cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=300,
    )
    elapsed = time.monotonic() - t0

    if result.returncode != 0:
        log_error(f"build: failed ({elapsed:.1f}s)")
        for line in result.stderr.splitlines()[-20:]:
            print(f"  {line}")
        return False

    warnings = sum(1 for line in result.stderr.splitlines() if "warning" in line.lower())
    log_info(f"build: ok ({elapsed:.1f}s, {warnings} warnings)")
    return True


def install(compat_tool: str = "quark") -> bool:
    # Stock Proton compat tools are pre-installed by Steam — nothing to deploy.
    if compat_tool != "quark":
        if not PROTON_BIN.exists():
            log_error(f"install: {PROTON_BIN} not found (Steam install incomplete?)")
            return False
        log_info(f"install: using stock {compat_tool} at {PROTON_BIN}")
        # Still clean logs so they reflect this run only.
        for p in glob.glob(WINE_INIT_GLOB):
            try:
                os.unlink(p)
            except OSError:
                pass
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        return True

    # Kill stale processes holding the binary
    killed = 0
    try:
        result = subprocess.run(["fuser", str(PROTON_BIN)],
                                capture_output=True, text=True, timeout=5)
        if result.stdout.strip():
            pids = result.stdout.strip().split()
            for pid in pids:
                try:
                    os.kill(int(pid.strip()), signal.SIGKILL)
                    killed += 1
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    if killed:
        log_warn(f"install: killed {killed} stale process(es)")
        time.sleep(1)

    if not QUARK_BIN.exists():
        log_error(f"install: {QUARK_BIN} not found")
        return False

    try:
        # Deploy using atomic rename to avoid ETXTBSY. If the old binary is
        # still mapped by a running process, copy+rename succeeds because
        # rename replaces the directory entry while the old inode stays
        # alive until the last process closes it.
        dst = COMPAT_DIR / "quark"
        tmp = COMPAT_DIR / ".quark.tmp"
        shutil.copy2(str(QUARK_BIN), str(tmp))
        os.rename(str(tmp), str(dst))
        # proton symlink -> quark (the launcher)
        proton_link = COMPAT_DIR / "proton"
        if proton_link.exists() or proton_link.is_symlink():
            proton_link.unlink()
        proton_link.symlink_to("quark")
    except (OSError, shutil.SameFileError) as e:
        log_error(f"install: {e}")
        return False

    # Clean logs so they reflect this run only
    for p in glob.glob(WINE_INIT_GLOB):
        try:
            os.unlink(p)
        except OSError:
            pass
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_info(f"install: deployed to {PROTON_BIN}")
    return True

# ── Stderr analysis ──────────────────────────────────────────────────

WINE_STDERR_LOG = LOG_DIR / "wine_stderr.log"
WINE_STDOUT_LOG = LOG_DIR / "wine_stdout.log"

# ── Montauk eBPF tracing ─────────────────────────────────────────────
#
# When --trace is set, spawn montauk in headless mode with --log alongside
# the game launch. Captures per-thread syscall/fd/file-io counters as
# timestamped Prometheus snapshots. The captured directory is archived
# next to the run's logs so two runs can be compared with
# `montauk --analyze A B --by capture`.
#
# Requires cap_bpf + cap_perfmon on /usr/local/bin/montauk:
#   sudo setcap cap_bpf,cap_perfmon=eip /usr/local/bin/montauk

MONTAUK_BIN = shutil.which("montauk") or "/usr/local/bin/montauk"
TRACE_BASE = LOG_DIR / "trace"


def start_trace(appid: str, tool: str, ts_prefix: str, pattern: str) -> tuple:
    """Spawn montauk in headless trace mode. Returns (process, log_dir) or
    (None, None) on failure. Never raises — tracing is best-effort."""
    if not Path(MONTAUK_BIN).exists():
        log_warn(f"trace: {MONTAUK_BIN} not found; skipping")
        return (None, None)
    log_dir = TRACE_BASE / f"{appid}_{ts_prefix}_{tool}"
    log_dir.mkdir(parents=True, exist_ok=True)
    # --trace-out: raw binary event stream (every syscall, IO, ntsync, sched).
    # Required for the Family-2 heap-corruption bisect: counters in --log
    # tell us frequency, but only the raw stream lets us walk back from the
    # libc abort write to find the corrupting pattern. Decode after the run
    # with: /usr/local/bin/montauk_trace_decode trace.bin > trace.txt
    trace_out = log_dir / "trace.bin"
    try:
        proc = subprocess.Popen(
            [MONTAUK_BIN, "--trace", pattern,
             "--trace-out", str(trace_out),
             "--log", str(log_dir), "--log-interval-ms", "500",
             "--headless"],
            stdout=subprocess.DEVNULL,
            stderr=open(log_dir / "montauk.stderr", "w"),
        )
    except OSError as e:
        log_warn(f"trace: spawn failed: {e}")
        return (None, None)
    # montauk needs ~500ms to attach BPF programs before the workload starts.
    time.sleep(0.7)
    if proc.poll() is not None:
        log_warn(f"trace: montauk exited early (code {proc.returncode}); "
                 f"check {log_dir}/montauk.stderr")
        return (None, log_dir)
    log_info(f"trace: montauk pid={proc.pid} pattern={pattern!r} → {log_dir}")
    return (proc, log_dir)


def stop_trace(proc) -> None:
    """Best-effort montauk shutdown. SIGTERM, then SIGKILL after 2s."""
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    except Exception as e:
        log_warn(f"trace: stop failed: {e}")


def decode_trace(log_dir: Path) -> None:
    """Auto-decode trace.bin if both the file and decoder exist.
    Writes trace.txt next to trace.bin so post-mortem doesn't need
    manual decoder invocation."""
    if log_dir is None:
        return
    trace_bin = log_dir / "trace.bin"
    if not trace_bin.exists() or trace_bin.stat().st_size < 100:
        # < 100 bytes means just header, no events — likely setcap missing.
        return
    montauk = shutil.which("montauk")
    if montauk is None:
        log_warn(f"trace: montauk not found; trace.bin preserved at {trace_bin}")
        return
    text_out = log_dir / "trace.txt"
    csv_out = log_dir / "trace.csv"
    try:
        with open(text_out, "w") as f:
            subprocess.run([montauk, "--decode", str(trace_bin)], stdout=f, timeout=60)
        with open(csv_out, "w") as f:
            subprocess.run([montauk, "--decode", str(trace_bin), "--csv"], stdout=f, timeout=60)
        log_info(f"trace: decoded → {text_out.name} ({text_out.stat().st_size} bytes), "
                 f"{csv_out.name}")
    except subprocess.TimeoutExpired:
        log_warn("trace: decode timed out (trace.bin preserved)")
    except Exception as e:
        log_warn(f"trace: decode failed: {e}")

# ── Screenshots ──────────────────────────────────────────────────────
#
# Capturing the screen at fixed checkpoints lets us see what the user sees
# without having to type "white screen" / "title showing" / etc. The
# screenshot tool is detected once at module load and the result is
# cached. Failures are silent — a missing tool must never abort a run.

SCREENSHOTS_DIR = LOG_DIR / "screenshots"
SCREENSHOT_CHECKPOINTS_S = (5, 15, 30, 45)  # seconds after launch

def _detect_screenshot_cmd():
    """Return a callable that takes (path) and writes a PNG, or None.

    Probe order is biased toward Wayland-native tools because the typical
    setup runs games through XWayland, and ImageMagick's `import` cannot
    grab the XWayland root window on most compositors (returns
    "missing an image filename"). Wayland-native captures of the full
    output work everywhere.

      1. KDE `spectacle -b -n -f -o`  — Plasma/Wayland, no notification
      2. `grim`                       — wlroots compositors (Sway, Hyprland)
      3. `gnome-screenshot -f`        — GNOME Mutter
      4. ImageMagick `import`         — pure-X11 fallback (last resort)
    """
    candidates = [
        (["spectacle", "-b", "-n", "-f", "-o"], lambda c, p: c + [str(p)]),
        (["grim"],                              lambda c, p: c + [str(p)]),
        (["gnome-screenshot", "-f"],            lambda c, p: c + [str(p)]),
        (["import", "-window", "root"],         lambda c, p: c + [str(p)]),
    ]
    for cmd, build in candidates:
        if shutil.which(cmd[0]):
            return (cmd, build)
    return None

_SCREENSHOT_CMD = _detect_screenshot_cmd()


def take_screenshot(appid: str, ts_prefix: str, elapsed_s: int) -> Path | None:
    """Grab a full-screen PNG into SCREENSHOTS_DIR.

    Returns the written path on success, None on any failure (missing tool,
    no display, command error). Never raises — screenshot capture is
    best-effort and must not break a run.
    """
    if _SCREENSHOT_CMD is None:
        return None
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SCREENSHOTS_DIR / f"{appid}_{ts_prefix}_t{elapsed_s:02d}s.png"
    cmd, build = _SCREENSHOT_CMD
    try:
        full = build(cmd, out_path)
        result = subprocess.run(full, capture_output=True, timeout=5)
        if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return out_path
    except (subprocess.TimeoutExpired, OSError):
        pass
    # Clean up zero-byte file if tool failed mid-write
    if out_path.exists() and out_path.stat().st_size == 0:
        try: out_path.unlink()
        except OSError: pass
    return None

def _collect_stderr_analysis() -> str:
    """Parse wine stderr for errors, aborts and unhandled exceptions."""
    lines = []

    if WINE_STDERR_LOG.exists():
        try:
            text = WINE_STDERR_LOG.read_text(errors="replace")
            # Look for error messages, missing DLLs, assertion failures
            for line in text.splitlines():
                if any(s in line.lower() for s in ["error", "abort", "assertion", "unhandled exception"]):
                    lines.append(f"  wine: {line.strip()[:200]}")
        except OSError:
            pass

    return "\n".join(lines) if lines else ""


# ── Regression baselines ─────────────────────────────────────────────
#
# Per-appid known-good snapshot stored at tests/baselines.json. Each run
# of --all compares current results against the baseline and flags drift.
# --update-baseline rewrites the file from the current results (manual
# blessing — you decide when to bake in a new good state).
#
# A regression is any of:
#   - a good milestone present in the baseline is missing now (lost RENDERING)
#   - a bad milestone NOT in the baseline appeared now (new CRASH)
#   - a new crash appeared where baseline had none
#
# Losing a bad milestone or gaining a good one is an improvement, not drift.

# Milestones that indicate healthy progress — losing one = regression.
GOOD_MILESTONES = {"x11drv", "XRandR", "LÖVE", "STEAM_API"}
# Milestones that indicate failure — gaining one we didn't have = regression.
BAD_MILESTONES = {"CRASH", "LOVE_ERROR", "SVC_ERR"}


def load_baselines() -> dict:
    if not BASELINES_PATH.exists():
        return {}
    try:
        return json.loads(BASELINES_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_baselines(baselines: dict) -> None:
    BASELINES_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINES_PATH.write_text(json.dumps(baselines, indent=2, sort_keys=True) + "\n")


def compute_drift(current: dict, baseline: dict) -> list:
    """Return list of drift messages. Empty = no regression."""
    if not baseline:
        return []
    drifts = []
    base_ms = set(baseline.get("milestones", []))
    cur_ms = set(current.get("milestones", []))
    # Good milestones present before and gone now = regression
    for m in sorted((base_ms - cur_ms) & GOOD_MILESTONES):
        drifts.append(f"lost {m}")
    # Bad milestones absent before and present now = regression
    for m in sorted((cur_ms - base_ms) & BAD_MILESTONES):
        drifts.append(f"new {m}")
    if current.get("crash") and not baseline.get("crash", False):
        drifts.append("new crash")
    return drifts


def result_to_baseline_entry(result: dict) -> dict:
    return {
        "name": result.get("name", "?"),
        "milestones": sorted(result.get("milestones", [])),
        "crash": result.get("crash", False),
        "updated": datetime.now().strftime("%Y-%m-%d"),
    }


# ── Launch & Monitor ─────────────────────────────────────────────────

_LAST_LAUNCH_STAMP: str = ""


def launch_and_monitor(app_id: str, game_name: str, exe_path: str,
                       timeout: int, compat_tool: str = "quark",
                       trace: bool = False) -> tuple:
    """Launch game, monitor, return (verdict, elapsed, snapshots, err, milestones)."""
    global _LAST_LAUNCH_STAMP
    _LAST_LAUNCH_STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_info(f"launch: {game_name} ({app_id}) via {compat_tool}")
    log_info(f"  exe: {exe_path}")

    cleanup_processes()

    env = os.environ.copy()

    # ── Core Steam-compat vars (what Steam ALWAYS sets) ────────────────
    # quark's proton-replacement and downstream Wine/Mono code read these.
    # Missing any of these makes Mono's startup hit the
    # `g_path_is_absolute(NULL)` assertion because path-construction code
    # tries to concatenate with an empty/missing base.
    env["STEAM_COMPAT_DATA_PATH"] = str(STEAMAPPS / "compatdata" / app_id)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(STEAM_ROOT)
    env["STEAM_COMPAT_APP_ID"] = app_id
    env["SteamAppId"] = app_id
    env["SteamGameId"] = app_id

    # Game install path (Mono uses this for assembly probing). Derive from
    # the exe path so it works for any appid.
    install_path = str(Path(exe_path).parent)
    env["STEAM_COMPAT_INSTALL_PATH"] = install_path
    env["STEAM_COMPAT_APP_LIBRARY_PATH"] = install_path
    env["STEAM_COMPAT_LIBRARY_PATHS"] = str(STEAMAPPS)

    # Compat-tool paths (where Proton/quark itself lives)
    env["STEAM_COMPAT_TOOL_PATHS"] = str(COMPAT_DIR)
    env["STEAM_COMPAT_MOUNTS"] = ""  # Steam sometimes sets this empty

    # Shader/transcode caches (DXVK + audio video transcode)
    shader_path = STEAM_ROOT / "steamapps" / "shadercache" / app_id
    env["STEAM_COMPAT_SHADER_PATH"] = str(shader_path)
    env["STEAM_COMPAT_TRANSCODED_MEDIA_PATH"] = \
        str(STEAM_ROOT / "steamapps" / "transcoded")

    # Proton verb hint — some downstream code branches on this even when
    # we invoke `proton run` directly (e.g., it controls whether Proton
    # sets up the in-prefix Steam shim differently).
    env["PROTON_VERB"] = "waitforexitandrun"

    # ── Debug / driver passthrough ────────────────────────────────────
    env["WINEDEBUG"] = os.environ.get(
        "QUARK_WINEDEBUG", "+driver,+loaddll,+wgl,+x11drv,+system,err")

    # Display, session, and IPC vars that Wine/Mono read at startup.
    # DBUS_SESSION_BUS_ADDRESS in particular: missing it makes some Mono
    # initializers store NULL for the session-bus path and crash later
    # when something probes it.
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
                "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                "DBUS_SYSTEM_BUS_ADDRESS", "XDG_DATA_DIRS",
                "XDG_CONFIG_DIRS", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "XDG_CACHE_HOME", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
                "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
                "PULSE_SERVER", "PULSE_RUNTIME_PATH"):
        if var in os.environ:
            env[var] = os.environ[var]

    # Don't override EGL vendor — let Wine use GLX via UseEGL=N registry key
    env.pop("__EGL_VENDOR_LIBRARY_FILENAMES", None)

    # Optional montauk trace, started before the workload so BPF programs
    # attach in time to catch early file syscalls (PHYSFS archive scan).
    trace_proc = None
    trace_dir = None
    if trace:
        # Pattern matches the game exe basename (e.g. "Balatro.exe").
        # Linux comm is 15 chars max. Match an EARLIER process in the launch
        # chain (wine-preloader, the immediate parent of the game) so that
        # auto_track_pid catches the root before exec, and handle_fork's
        # parent→child inheritance gives us every descendant from the start.
        # Otherwise we miss the first 15-20s of allocations because the
        # game's comm doesn't match the pattern until well after Mono init.
        exe_name = "wine-preloader"
        trace_proc, trace_dir = start_trace(
            app_id, compat_tool, _LAST_LAUNCH_STAMP, exe_name)

    t0 = clock_raw_ns()

    try:
        # Capture both stdout and stderr to files. LOVE2D writes Lua errors
        # to stdout via lua's print() and via callbacks.lua's error handler,
        # so capturing stdout is essential for diagnosing fused-exe issues.
        proc = subprocess.Popen(
            [str(PROTON_BIN), "run", exe_path],
            env=env,
            cwd="/tmp",
            stdout=open(WINE_STDOUT_LOG, "w"),
            stderr=open(WINE_STDERR_LOG, "w"),
            start_new_session=True,  # own process group so we can kill the tree
        )
    except OSError as e:
        stop_trace(trace_proc)
        decode_trace(trace_dir)
        return ("launch_fail", 0, [], f"Failed to start: {e}", set())

    poll_interval = 2
    ticks = timeout // poll_interval
    milestones_seen: set = set()

    for tick in range(ticks):
        time.sleep(poll_interval)

        # Check if proton launcher exited — but DON'T stop monitoring.
        # The launcher exits quickly after spawning wine64. The game runs
        # as a background Wine process. Keep monitoring until timeout or
        # until all Wine processes exit.
        ret = proc.poll()
        if ret is not None and not hasattr(proc, '_launcher_exited'):
            proc._launcher_exited = True  # mark but don't stop

        elapsed = ns_to_ms(clock_raw_ns() - t0) / 1000
        pids = find_wine_pids()
        pid_str = f" pids={[p for p in pids]}" if pids else ""

        # Screenshot checkpoints — capture full screen at fixed elapsed times
        # so we can see what was actually rendered without the user typing it.
        # Files land in LOG_DIR/screenshots/ with the same {appid}_{stamp}
        # prefix as the archived wine_stderr.log.
        for cp in SCREENSHOT_CHECKPOINTS_S:
            if cp <= elapsed < cp + poll_interval:
                shot = take_screenshot(app_id, _LAST_LAUNCH_STAMP, cp)
                if shot:
                    log_info(f"screenshot: {shot.name}")
                break

        # Check wine_stderr for milestones.
        #
        # IMPORTANT: every milestone here must be a TRUE positive — substring
        # matches that fire on error screens, helper windows, or unrelated
        # codepaths are misleading and have caused multiple "looks playable"
        # hallucinations. The bar:
        #   - "STEAM_API" requires the bridge to actually answer, not just
        #     for the string "steam_api64" to appear in a load message
        #   - "CRASH" requires an actual exit/SIGSEGV, not "Unhandled" which
        #     appears in stub fixme lines that don't crash anything
        milestones = []
        try:
            stderr_text = WINE_STDERR_LOG.read_text(errors="replace") if WINE_STDERR_LOG.exists() else ""
            if "winex11.drv" in stderr_text:
                milestones.append("x11drv")
            if "XRandR" in stderr_text:
                milestones.append("XRandR")
            if "NO GAME" in stderr_text or "love" in stderr_text.lower():
                milestones.append("LÖVE")

            # CRASH: a real exit from the launcher with non-zero status, OR
            # an actual SIGSEGV trace. "Unhandled" alone is too noisy.
            if re.search(r'\[quark\] exit: [1-9]', stderr_text) or "SIGSEGV" in stderr_text:
                milestones.append("CRASH")
            # LOVE error screen specifically — different from a quark crash
            if 'love "callbacks.lua"' in stderr_text or "module 'engine/object'" in stderr_text:
                milestones.append("LOVE_ERROR")

            if "steam_api64" in stderr_text:
                milestones.append("STEAM_API")
            if any(f"err:service" in l for l in stderr_text.splitlines()[-5:]):
                milestones.append("SVC_ERR")
        except Exception:
            pass
        milestones_seen.update(milestones)
        ms_str = f" | {' '.join(milestones)}" if milestones else ""

        log_info(f"[{elapsed:.0f}s]{pid_str}{ms_str}")

    # Timeout
    elapsed_ns = clock_raw_ns() - t0
    elapsed = ns_to_ms(elapsed_ns) / 1000
    snaps = [snapshot_process(p) for p in find_wine_pids()]
    stderr_report = _collect_stderr_analysis()
    if stderr_report:
        print(stderr_report)
    terminate_game(proc)
    stop_trace(trace_proc)
    decode_trace(trace_dir)
    cleanup_processes()
    return ("timeout", elapsed, snaps, "", milestones_seen)

# ── Report ───────────────────────────────────────────────────────────

def print_report(verdict: str, elapsed: float, snapshots: list):
    log_info(f"verdict: {verdict} ({elapsed:.1f}s)")

    # Display driver
    stderr_text = WINE_STDERR_LOG.read_text() if WINE_STDERR_LOG.exists() else ""
    x11_loaded = "winex11.drv" in stderr_text or "x11drv" in stderr_text
    log_info(f"display: x11={x11_loaded}")

    # Hang analysis
    if snapshots:
        analysis = analyze_hang(snapshots)
        if analysis:
            log_info(f"hang analysis:")
            for line in analysis.split("\n"):
                if line.strip():
                    print(f"  {line.strip()}")

# ── Main ─────────────────────────────────────────────────────────────

def discover_all_games() -> list[tuple[str, str, str]]:
    """Find all playable Steam games. Returns [(appid, name, exe_path), ...]."""
    _skip_appids = {
        "228980", "1493710", "1887720", "1826330", "2180100",
        "961940", "1391110", "1580130", "2348590",
    }
    _skip_names = {"proton", "steamworks", "steam linux runtime", "redistribut", "sdk"}
    games = []
    seen = set()
    for manifest in sorted(STEAMAPPS.glob("appmanifest_*.acf")):
        info = parse_appmanifest(manifest)
        appid = info.get("appid", "")
        name = info.get("name", "")
        installdir = info.get("installdir", "")
        if not appid or not name or not installdir:
            continue
        if appid in seen or appid in _skip_appids:
            continue
        if any(s in name.lower() for s in _skip_names):
            continue
        install_dir = STEAMAPPS / "common" / installdir
        if not install_dir.exists():
            continue
        exe = find_game_exe(install_dir)
        if not exe:
            continue
        seen.add(appid)
        games.append((appid, name, exe))
    games.sort(key=lambda g: g[1])
    return games


def run_single_game(appid, game_name, exe_path, timeout, skip_build,
                    compat_tool: str = "quark", trace: bool = False):
    """Run one game through iterate cycle. Returns result dict."""
    # Build is only relevant for quark; stock Proton has nothing to compile.
    if compat_tool == "quark" and not build(skip=skip_build):
        return {"appid": appid, "name": game_name, "verdict": "build_fail"}

    if not install(compat_tool=compat_tool):
        return {"appid": appid, "name": game_name, "verdict": "install_fail"}

    result = launch_and_monitor(appid, game_name, exe_path, timeout,
                                compat_tool=compat_tool, trace=trace)
    verdict, elapsed, snapshots, err, milestones = result
    milestones = sorted(milestones)

    # Check wine_stderr for key errors
    stderr_text = ""
    if WINE_STDERR_LOG.exists():
        stderr_text = WINE_STDERR_LOG.read_text(errors="replace")
    steam_auth_ok = "Failed to find steamclient_init_registry" not in stderr_text
    has_crash = "Unhandled page fault" in stderr_text or "page fault" in stderr_text

    # Archive — reuse the launch stamp so archived logs and screenshots
    # captured during the run share the same {appid}_{stamp}_{tool} prefix.
    stamp = _LAST_LAUNCH_STAMP or datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_dir = LOG_DIR / "runs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{appid}_{stamp}_{compat_tool}"
    for src, suffix in [(WINE_STDERR_LOG, "wine_stderr.log"), (WINE_STDOUT_LOG, "wine_stdout.log"),
                         (LOG_DIR / "launcher_env.txt", "launcher_env.txt")]:
        if src.exists() and src.stat().st_size > 0:
            dst = archive_dir / f"{prefix}_{suffix}"
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass

    # Trace archive: copy montauk's log dir alongside if it exists.
    if trace:
        src_trace = TRACE_BASE / f"{appid}_{stamp}_{compat_tool}"
        if src_trace.exists():
            dst_trace = archive_dir / f"{prefix}_trace"
            try:
                if dst_trace.exists():
                    shutil.rmtree(dst_trace)
                shutil.copytree(src_trace, dst_trace)
                log_info(f"trace: archived → {dst_trace}")
            except OSError as e:
                log_warn(f"trace: archive copy failed: {e}")

    return {
        "appid": appid,
        "name": game_name,
        "verdict": verdict,
        "elapsed": elapsed,
        "steam_auth": steam_auth_ok,
        "crash": has_crash,
        "milestones": milestones,
        "compat_tool": compat_tool,
    }


def run_all_games(timeout, skip_build, update_baseline=False,
                  compat_tool: str = "quark", trace: bool = False) -> int:
    """Discover and test all Steam games. Print summary table.
    Returns number of regressions (0 = clean, >0 = at least one game drifted)."""
    games = discover_all_games()
    if not games:
        log_error("no games found")
        return 0

    log_info(f"found {len(games)} games to test (compat_tool={compat_tool}, trace={trace})")
    for appid, name, _ in games:
        log_info(f"  {appid:>8}  {name}")
    print()

    # Build/deploy once. Stock Proton has nothing to compile or copy.
    if compat_tool == "quark":
        if not build(skip=skip_build):
            log_error("build failed")
            return 0
    if not install(compat_tool=compat_tool):
        log_error("install failed")
        return 0

    # Verify patches only when running against quark (otherwise irrelevant).
    if compat_tool == "quark":
        log_info("running patch verification...")
        r = subprocess.run([sys.executable, str(REPO_ROOT / "tests" / "verify_patches.py")],
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if "[FAIL]" in line or "RESULTS" in line:
                print(f"  {line.strip()}")
        if r.returncode != 0:
            log_warn("patch verification had failures (continuing anyway for dev testing)")
        else:
            log_info("patch verification passed")
        print()

    baselines = load_baselines()

    results = []
    for i, (appid, name, exe) in enumerate(games):
        log_info(f"GAME {i+1}/{len(games)}: {name} ({appid})")
        res = run_single_game(appid, name, exe, timeout, skip_build=True,
                              compat_tool=compat_tool, trace=trace)
        res["drifts"] = compute_drift(res, baselines.get(appid, {}))
        res["has_baseline"] = appid in baselines
        results.append(res)
        print()

    # Summary table
    print(f"  {'Game':<40} {'Verdict':<10} {'Steam':>6} {'Crash':>6}  {'Drift':<30}")
    for r in results:
        name = r["name"][:39]
        verdict = r["verdict"]
        steam = "OK" if r.get("steam_auth") else "FAIL"
        crash = "YES" if r.get("crash") else "no"
        if not r.get("has_baseline"):
            drift = "(no baseline)"
        elif r.get("drifts"):
            drift = "; ".join(r["drifts"])[:30]
        else:
            drift = "OK"
        print(f"  {name:<40} {verdict:<10} {steam:>6} {crash:>6}  {drift:<30}")

    regressed = [r for r in results if r.get("drifts")]
    if regressed:
        print("\nREGRESSIONS:")
        for r in regressed:
            print(f"  {r['name']} ({r['appid']}):")
            for d in r["drifts"]:
                print(f"    - {d}")

    ok = sum(1 for r in results if r["verdict"] == "timeout" and not r.get("crash"))
    crashed = sum(1 for r in results if r.get("crash"))
    print(f"\n  {len(results)} games | {ok} running | {crashed} crashed | {len(regressed)} regressed")

    if update_baseline:
        for r in results:
            baselines[r["appid"]] = result_to_baseline_entry(r)
        save_baselines(baselines)
        log_info(f"baseline: wrote {len(results)} entries to {BASELINES_PATH}")

    return len(regressed)


def main():
    parser = argparse.ArgumentParser(description="quark build-test-analyze cycle")
    parser.add_argument("--appid", default=DEFAULT_APPID, help=f"Steam app ID (default: {DEFAULT_APPID})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"Timeout in seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--skip-build", action="store_true", help="Skip cargo build")
    parser.add_argument("--exe", type=str, default=None, help="Override game exe path")
    parser.add_argument("--all", action="store_true", help="Test ALL installed Steam games")
    parser.add_argument("--update-baseline", action="store_true",
                        help="Write current run results as the new baseline in tests/baselines.json")
    parser.add_argument("--compat-tool", choices=list(COMPAT_TOOLS.keys()),
                        default="quark",
                        help="Compat tool to launch through (default: quark). "
                             "Stock Proton entries enable A/B comparison traces.")
    parser.add_argument("--trace", action="store_true",
                        help="Run montauk eBPF trace in parallel; archive snapshots "
                             "alongside the run's logs for offline diff "
                             "(requires cap_bpf,cap_perfmon on montauk)")
    args = parser.parse_args()

    # Resolve compat tool launcher.
    global PROTON_BIN
    PROTON_BIN = COMPAT_TOOLS[args.compat_tool]

    if args.all:
        regressions = run_all_games(args.timeout, args.skip_build,
                                    update_baseline=args.update_baseline,
                                    compat_tool=args.compat_tool,
                                    trace=args.trace)
        sys.exit(1 if regressions else 0)

    # Find game
    game_name, exe_path = find_game(args.appid)
    if args.exe:
        exe_path = args.exe
    if not exe_path:
        log_error(f"could not find game exe for app {args.appid}")
        sys.exit(1)

    # Build (skip for stock Proton — nothing to compile)
    if args.compat_tool == "quark" and not build(skip=args.skip_build):
        sys.exit(1)

    # Install (no-op for stock Proton — verifies path exists)
    if not install(compat_tool=args.compat_tool):
        sys.exit(1)

    # Launch & monitor
    result = launch_and_monitor(args.appid, game_name, exe_path, args.timeout,
                                compat_tool=args.compat_tool, trace=args.trace)
    verdict, elapsed, snapshots, err, milestones = result
    milestones = sorted(milestones)

    if err:
        log_error(err)
        sys.exit(1)

    # Report
    print_report(verdict, elapsed, snapshots)

    # Regression check against baseline
    stderr_text = WINE_STDERR_LOG.read_text(errors="replace") if WINE_STDERR_LOG.exists() else ""
    has_crash = "Unhandled page fault" in stderr_text or "page fault" in stderr_text
    current_result = {
        "appid": args.appid,
        "name": game_name,
        "milestones": milestones,
        "crash": has_crash,
    }
    baselines = load_baselines()
    baseline = baselines.get(args.appid, {})
    drifts = compute_drift(current_result, baseline)
    if not baseline:
        log_info(f"baseline: no baseline for appid={args.appid} (run --update-baseline to create)")
    elif drifts:
        log_warn(f"REGRESSION ({game_name}):")
        for d in drifts:
            print(f"  - {d}")
    else:
        log_info(f"baseline: no drift from {baseline.get('updated', '?')} snapshot")

    if args.update_baseline:
        baselines[args.appid] = result_to_baseline_entry(current_result)
        save_baselines(baselines)
        log_info(f"baseline: updated {args.appid} in {BASELINES_PATH}")

    # Archive logs — reuse the launch stamp so archived logs and screenshots
    # captured during the run share the same {appid}_{stamp}_{tool} prefix.
    stamp = _LAST_LAUNCH_STAMP or datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_dir = LOG_DIR / "runs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{args.appid}_{stamp}_{args.compat_tool}"
    for src, suffix in [(WINE_STDERR_LOG, "wine_stderr.log"), (WINE_STDOUT_LOG, "wine_stdout.log"),
                         (LOG_DIR / "launcher_env.txt", "launcher_env.txt")]:
        if src.exists() and src.stat().st_size > 0:
            dst = archive_dir / f"{prefix}_{suffix}"
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass
    if args.trace:
        src_trace = TRACE_BASE / f"{args.appid}_{stamp}_{args.compat_tool}"
        if src_trace.exists():
            dst_trace = archive_dir / f"{prefix}_trace"
            try:
                if dst_trace.exists():
                    shutil.rmtree(dst_trace)
                shutil.copytree(src_trace, dst_trace)
            except OSError as e:
                log_warn(f"trace: archive copy failed: {e}")
    log_info(f"logs archived: {LOG_DIR}/runs/{prefix}_*")

    if drifts and not args.update_baseline:
        sys.exit(1)


if __name__ == "__main__":
    main()
