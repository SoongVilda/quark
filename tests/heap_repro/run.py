#!/usr/bin/env python3
"""Fast local repro loop for the #18 heap corruption — no Steam, no game.

Cross-compiles repro.exe (mingw), launches it under quark's proton entrypoint
directly with the montauk observer armed, waits for crash/exit, and runs the
abortpm report. Seconds per iteration vs a 10-minute Steam game run.

    python3 tests/heap_repro/run.py            # build + run once
    python3 tests/heap_repro/run.py --runs 5   # run N times (it's racy)
    python3 tests/heap_repro/run.py --no-build  # skip recompile
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
from quark_common import (
    REPO_ROOT, STEAM_ROOT, STEAMAPPS, COMPAT_DIR, log_info, log_warn, log_error,
)

MONTAUK_BUILD = (REPO_ROOT / "../montauk/build").resolve()
PROTON = COMPAT_DIR / "proton"
PROBE_SRC = HERE.parent / "qemu" / "probes" / "gl_heap_stress.c"
EXE = HERE / "repro.exe"
# A throwaway prefix so we never touch a real game's compatdata.
PFX = STEAMAPPS / "compatdata/heaprepro"
TRACE_OUT = "/tmp/quark/incidents/repro-io.bin"


class _Stop(Exception):
    """Raised from a SIGTERM/SIGINT handler so teardown runs on `timeout`/Ctrl-C."""


def build() -> bool:
    log_info("building repro.exe (mingw)")
    rc = subprocess.run([
        "x86_64-w64-mingw32-gcc", "-O2", '-DPROBE_NAME="gl_heap_stress"', "-o", str(EXE), str(PROBE_SRC),
        "-lopengl32", "-lgdi32", "-luser32", "-lole32", "-lwinmm",
    ]).returncode
    if rc != 0:
        log_error(f"mingw build failed rc={rc}")
        return False
    return True


def _kill_by_prefix_env(prefix_path: str) -> int:
    """SIGKILL every process whose environ references the throwaway prefix.

    The wineserver forks+setsids into its own session so it detaches from the
    launcher's process group and survives a group kill. But every process the
    launch spawned (the wineserver,
    montauk, wine, repro.exe) inherits STEAM_COMPAT_DATA_PATH / WINEPREFIX
    pointing at this prefix, which is unique to the repro. Matching on it reaps
    the detached wineserver without ever touching a real game's wineserver."""
    needle = prefix_path.encode()
    killed = 0
    me = os.getpid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        p = int(pid)
        if p == me:
            continue
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                if needle not in f.read():
                    continue
            os.kill(p, signal.SIGKILL)
            killed += 1
        except (OSError, ProcessLookupError):
            continue
    return killed


def teardown(proc) -> None:
    """Reap the whole launch — including the detached, lingering wineserver."""
    # 1. Kill the launcher's own session group (wine64 + non-detached children).
    if proc is not None and proc.poll() is None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                continue
    # 2. The reliable layer: reap anything (detached wineserver, montauk, wine)
    #    still bound to the throwaway prefix.
    n = _kill_by_prefix_env(str(PFX))
    if n:
        log_info(f"teardown: reaped {n} lingering process(es) bound to {PFX.name}")
    # 3. Belt-and-suspenders: a montauk observer still streaming our trace.
    subprocess.run(["pkill", "-KILL", "-f", f"trace-out {TRACE_OUT}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_once(idx: int, timeout_s: int) -> str:
    """Launch repro.exe under quark; return 'CRASH', 'CLEAN', 'STALL' or
    'UNKNOWN'. Always reaps the spawned tree, even on stall/timeout/signal."""
    PFX.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["STEAM_COMPAT_DATA_PATH"] = str(PFX)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(STEAM_ROOT)
    env["STEAM_COMPAT_APP_ID"] = "0"
    env["SteamAppId"] = "0"
    env["PROTON_VERB"] = "waitforexitandrun"
    env["WINE_NTSYNC"] = "1"
    env["QUARK_OBSERVER"] = "1"
    env["QUARK_OBSERVER_TRACE_OUT"] = "1"
    env["MONTAUK_HEAP_STACK_SIZE"] = "288"
    for v in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR"):
        if v in os.environ:
            env[v] = os.environ[v]

    stderr_log = f"/tmp/quark/heaprepro-{idx}-stderr.log"
    Path("/tmp/quark").mkdir(parents=True, exist_ok=True)
    log_info(f"run {idx}: launching repro.exe under quark (timeout {timeout_s}s)")
    t0 = time.monotonic()
    stalled = False
    proc = None
    with open(stderr_log, "w") as ef:
        # start_new_session: the launcher leads its own session/pgroup so the
        # whole non-detached tree is killable together and a SIGTERM to this
        # script does not race-kill half of it.
        proc = subprocess.Popen([str(PROTON), "run", str(EXE)],
                                env=env, cwd="/tmp", stdout=subprocess.DEVNULL,
                                stderr=ef, start_new_session=True)
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            stalled = True
            log_warn(f"run {idx}: exceeded {timeout_s}s — STALL (the #18 stall variant); reaping")
        finally:
            teardown(proc)

    dt = time.monotonic() - t0
    txt = Path(stderr_log).read_text(errors="replace")
    crashed = any(s in txt for s in (
        "sysmalloc", "double free or corruption", "!prev",
        "Assertion", "free_dce"))
    clean = "repro: completed without crash" in txt
    if crashed:
        verdict = "CRASH"
    elif stalled:
        verdict = "STALL"
    elif clean:
        verdict = "CLEAN"
    else:
        verdict = "UNKNOWN"
    rc = proc.returncode if proc is not None else None
    log_info(f"run {idx}: {verdict} in {dt:.1f}s (rc={rc})")
    return verdict


def analyze():
    """Run abortpm on the freshest repro trace, if montauk produced one."""
    inc = Path("/tmp/quark/incidents")
    traces = sorted(inc.glob("*.bin"), key=lambda p: p.stat().st_mtime) if inc.exists() else []
    if not traces:
        log_warn("no montauk trace produced (observer may not have attached)")
        return
    trace = traces[-1]
    analyzer = MONTAUK_BUILD / "montauk_analyze"
    if not analyzer.exists():
        log_warn(f"montauk_analyze not at {analyzer}")
        return
    log_info(f"abortpm on {trace.name}")
    subprocess.run([str(analyzer), str(trace), "--report", "abortpm"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--timeout", type=int, default=120,
                    help="per-run wall-clock limit (s); exceeding it is a STALL")
    args = ap.parse_args()

    if not PROTON.exists():
        log_error(f"{PROTON} missing — install quark first")
        return 1
    if not args.no_build and not build():
        return 1

    # Turn SIGTERM/SIGINT (from a `timeout` wrapper or Ctrl-C) into an exception
    # so the active run's teardown still reaps the spawned tree.
    def _stop(_signum, _frame):
        raise _Stop()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    hits = 0
    for i in range(1, args.runs + 1):
        try:
            verdict = run_once(i, args.timeout)
        except _Stop:
            log_warn(f"run {i}: interrupted — tree reaped, stopping")
            break
        # A crash or a stall is a #18 hit worth analyzing; both stop the loop.
        if verdict in ("CRASH", "STALL"):
            hits += 1
            analyze()
            break
    log_info(f"done: {hits}/{args.runs} hit (#18 crash or stall)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
