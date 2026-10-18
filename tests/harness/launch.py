"""The launch itself: build, install, launch, monitor, report, archive. One
game or every installed one. quark unless a flag names another tool."""
import os
import re
import shutil
import subprocess
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from quark_common import COMPAT_DIR, LOG_DIR, REPO_ROOT, STEAMAPPS, STEAM_ROOT, discover_all_games, find_game, log_error, log_info, log_warn
from .base import BASELINES_PATH, DEFAULT_APPID, DEFAULT_TIMEOUT, LAUNCHER_STDERR_LOG, WINE_STDERR_LOG, WINE_STDOUT_LOG, clock_raw_ns, ns_to_ms
from .baselines import compute_drift, load_baselines, result_to_baseline_entry, save_baselines
from . import metrics as metrics_mod
from .metrics import SCREENSHOT_CHECKPOINTS_S, _FirstSeen, _frametime_stats, _scheduler_label, take_screenshot
from .procs import _terminate_tree, _tree_pids, _tree_rss_mb, analyze_hang, cleanup_processes, snapshot_process, terminate_game
from .tools import COMPAT_TOOLS, _compat_data_path, build, install, tool_bin
from . import trace as trace_mod
from .trace import TRACE_BASE, _capture_stats, decode_trace, start_trace, stop_trace, trim_trace


_LAST_LAUNCH_STAMP: str = ""


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


CRASH_MARKERS = ("Unhandled exception", "Unhandled page fault", "page fault",
                 "wine: Assertion", "killed by signal", "(error)")


def _stderr_has_crash() -> bool:
    """Wine's own words for a process that died, from the game's stderr."""
    if not WINE_STDERR_LOG.exists():
        return False
    text = WINE_STDERR_LOG.read_text(errors="replace")
    return any(m in text for m in CRASH_MARKERS)


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

    # Core Steam-compat vars (what Steam ALWAYS sets)
    # quark's proton-replacement and downstream Wine/Mono code read these.
    # Missing any of these makes Mono's startup hit the
    # `g_path_is_absolute(NULL)` assertion because path-construction code
    # tries to concatenate with an empty/missing base.
    compat_data = _compat_data_path(app_id, compat_tool)
    compat_data.mkdir(parents=True, exist_ok=True)  # Proton 11 expects Steam to have made it
    env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
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

    # Debug / driver passthrough
    env["WINEDEBUG"] = os.environ.get(
        "QUARK_WINEDEBUG", "+driver,+loaddll,+wgl,+x11drv,+system,err")

    # Display, session, and IPC vars that Wine/Mono read at startup.
    # DBUS_SESSION_BUS_ADDRESS in particular: Missing it makes some Mono
    # initializers store NULL for the session-bus path and crash later
    # when something probes it.
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
                "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                "DBUS_SYSTEM_BUS_ADDRESS", "XDG_DATA_DIRS",
                "XDG_CONFIG_DIRS", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "XDG_CACHE_HOME", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
                "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
                "PULSE_SERVER", "PULSE_RUNTIME_PATH", "QUARK_VERBOSE"):
        if var in os.environ:
            env[var] = os.environ[var]

    # Don't override EGL vendor — let Wine use GLX via UseEGL=N registry key
    env.pop("__EGL_VENDOR_LIBRARY_FILENAMES", None)

    # Frame times: MangoHud's per-frame log, overlay hidden, on scoreboard
    # runs only. Its CPU, GPU and thermal columns stay unused; montauk owns
    # those. Both sides of an A/B carry the layer.
    frametime_dir = None
    if metrics_mod.FRAMETIMES:
        if shutil.which("mangohud"):
            frametime_dir = LOG_DIR / "frametimes" / f"{app_id}_{_LAST_LAUNCH_STAMP}_{compat_tool}"
            frametime_dir.mkdir(parents=True, exist_ok=True)
            env["MANGOHUD"] = "1"
            # The log is written when its duration ends, so it ends ten seconds
            # before the cell's kill; a duration equal to the cell never writes.
            env["MANGOHUD_CONFIG"] = (f"no_display,output_folder={frametime_dir},"
                                      f"autostart_log=1,log_duration={max(5, timeout - 10)},log_interval=0")
        else:
            log_warn("frametimes: mangohud not on PATH; frame-time columns will be n/a")

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
        # The launcher is exec'd through the compat dir's `proton` symlink,
        # so its comm is "proton", and montauk follows its whole tree from
        # there; one name, because montauk's comma list honours only the
        # first (a two-name pattern captured nothing at all).
        exe_name = "proton"
        trace_proc, trace_dir = start_trace(
            app_id, compat_tool, _LAST_LAUNCH_STAMP, exe_name)

    t0 = clock_raw_ns()

    try:
        # Capture both stdout and stderr to files. LOVE2D writes Lua errors
        # to stdout via lua's print() and via callbacks.lua's error handler,
        # so capturing stdout is essential for diagnosing fused-exe issues.
        proc = subprocess.Popen(
            [str(tool_bin(compat_tool)), "run", exe_path],
            env=env,
            cwd="/tmp",
            # The launcher's own stderr. The launcher reopens wine_stderr.log
            # for the game with truncation, so sharing that file would wipe
            # the launcher's lines (its verbose timing among them).
            stdout=open(WINE_STDOUT_LOG, "w"),
            stderr=open(LAUNCHER_STDERR_LOG, "w"),
            start_new_session=True,  # own process group so we can kill the tree
        )
    except OSError as e:
        stop_trace(trace_proc)
        decode_trace(trace_dir)
        return ("launch_fail", 0, [], f"Failed to start: {e}", set(), {})

    watcher = _FirstSeen(exe_path, app_id, t0)
    watcher.start()
    metrics = {"appid": app_id, "tool": compat_tool, "scheduler": _scheduler_label(),
               "exec_ms": None, "window_ms": None, "procs": 0, "rss_mb": 0.0}

    def _finish_metrics(pids: set) -> dict:
        watcher.stop()
        metrics["exec_ms"] = watcher.exec_ms
        metrics["window_ms"] = watcher.window_ms
        metrics["procs"] = len(pids)
        metrics["rss_mb"] = round(_tree_rss_mb(pids), 1)
        metrics.update(_frametime_stats(frametime_dir))
        log_info(f"metrics: exec={metrics['exec_ms']}ms window={metrics['window_ms']}ms "
                 f"procs={metrics['procs']} rss={metrics['rss_mb']}MB")
        return metrics

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
        pids = sorted(_tree_pids(app_id, compat_tool))
        pid_str = f" pids={[p for p in pids]}" if pids else ""

        # The launcher waits on the game, so launcher exit plus an empty
        # process tree means the game is gone before the timeout. The
        # launcher's code alone cannot score it: steam.exe returns 0 for a
        # game that died by signal, so Wine's own crash lines in stderr
        # decide alongside it.
        if ret is not None and not pids:
            verdict = "crash" if ret != 0 or _stderr_has_crash() else "exit"
            log_warn(f"[{elapsed:.0f}s] game tree gone, launcher exit {ret}: {verdict}")
            stderr_report = _collect_stderr_analysis()
            if stderr_report:
                print(stderr_report)
            m = _finish_metrics(set())
            stop_trace(trace_proc)
            decode_trace(trace_dir)
            m.update(_capture_stats(trace_dir))
            trim_trace(trace_dir)
            cleanup_processes()
            return (verdict, elapsed, [], f"launcher exit {ret}", milestones_seen, m)

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
        # IMPORTANT: Every milestone here must be a TRUE positive — substring
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

            # CRASH: A real exit from the launcher with non-zero status, OR
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
    tree = _tree_pids(app_id, compat_tool)
    snaps = [snapshot_process(p) for p in sorted(tree)]
    stderr_report = _collect_stderr_analysis()
    if stderr_report:
        print(stderr_report)
    m = _finish_metrics(tree)
    if compat_tool == "quark":
        terminate_game(proc)
    else:
        _terminate_tree(tree - {proc.pid})
        proc.kill()
        proc.wait()
    stop_trace(trace_proc)
    decode_trace(trace_dir)
    m.update(_capture_stats(trace_dir))
    trim_trace(trace_dir)
    cleanup_processes()
    return ("timeout", elapsed, snaps, "", milestones_seen, m)


# Report
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
    verdict, elapsed, snapshots, err, milestones, metrics = result
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
                         (LOG_DIR / "launcher_env.txt", "launcher_env.txt"),
                         (LAUNCHER_STDERR_LOG, "launcher_stderr.log")]:
        if src.exists() and src.stat().st_size > 0:
            dst = archive_dir / f"{prefix}_{suffix}"
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass

    # Trace archive: Copy montauk's log dir alongside if it exists.
    if trace:
        src_trace = TRACE_BASE / f"{appid}_{stamp}_{compat_tool}"
        if src_trace.exists():
            dst_trace = archive_dir / f"{prefix}_trace"
            try:
                if dst_trace.exists():
                    shutil.rmtree(dst_trace)
                shutil.move(str(src_trace), str(dst_trace))
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
        "metrics": metrics,
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


# THE COMMAND LINE: tests/run.py launch

def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("appid", nargs="?", default=DEFAULT_APPID,
                        help=f"Steam app id (default {DEFAULT_APPID}, Balatro)")
    parser.add_argument("--all", action="store_true", help="every installed Windows game")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"seconds to run before the verdict (default {DEFAULT_TIMEOUT})")
    parser.add_argument("--skip-build", action="store_true", help="launch the quark already built")
    parser.add_argument("--exe", default=None, help="launch this exe instead of the one found for the app")
    parser.add_argument("--trace", action="store_true",
                        help="capture the launch under montauk (needs cap_bpf and cap_perfmon on it)")
    parser.add_argument("--frametimes", action="store_true",
                        help="log frame times through MangoHud, overlay hidden, when it is installed")
    parser.add_argument("--metrics-json", default=None, help="write the verdict and metrics to this file")
    parser.add_argument("--keep-trace", action="store_true",
                        help="with --trace, keep the raw 200 MB capture after its report set is written")
    parser.add_argument("--update-baseline", action="store_true",
                        help="record this run's milestones as the game's baseline")
    parser.add_argument("--tool", choices=list(COMPAT_TOOLS), default="quark",
                        help="the compatibility tool to launch through. quark is the tool under "
                             "test; a Proton entry is an A/B run against stock Proton and nothing "
                             "else in the suite knows those exist")


def run(args: argparse.Namespace) -> int:
    metrics_mod.FRAMETIMES = args.frametimes
    trace_mod.KEEP_TRACE = args.keep_trace
    tool = args.tool

    if args.all:
        regressions = run_all_games(args.timeout, args.skip_build,
                                    update_baseline=args.update_baseline,
                                    compat_tool=tool, trace=args.trace)
        return 1 if regressions else 0

    game_name, exe_path = find_game(args.appid)
    if args.exe:
        exe_path = args.exe
    if not exe_path:
        log_error(f"could not find game exe for app {args.appid}")
        return 1

    if tool == "quark" and not build(skip=args.skip_build):
        return 1
    if not install(compat_tool=tool):
        return 1

    result = launch_and_monitor(args.appid, game_name, exe_path, args.timeout,
                                compat_tool=tool, trace=args.trace)
    verdict, elapsed, snapshots, err, milestones, run_metrics = result
    milestones = sorted(milestones)

    # The metrics file is written for every verdict: A crash under one tool
    # is a scoreboard row, not a missing cell.
    print_report(verdict, elapsed, snapshots)
    if args.metrics_json:
        out = dict(run_metrics, verdict=verdict, elapsed_s=round(elapsed, 1),
                   milestones=milestones, name=game_name, error=err)
        Path(args.metrics_json).write_text(json.dumps(out, indent=1) + "\n")
    if err:
        log_error(err)
        return 1

    stderr_text = WINE_STDERR_LOG.read_text(errors="replace") if WINE_STDERR_LOG.exists() else ""
    has_crash = "Unhandled page fault" in stderr_text or "page fault" in stderr_text
    current_result = {"appid": args.appid, "name": game_name, "milestones": milestones, "crash": has_crash}
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

    # Archive the run's logs under the launch stamp, so logs, screenshots and
    # the capture share one {appid}_{stamp}_{tool} prefix.
    stamp = _LAST_LAUNCH_STAMP or datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_dir = LOG_DIR / "runs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{args.appid}_{stamp}_{tool}"
    for src, suffix in [(WINE_STDERR_LOG, "wine_stderr.log"), (WINE_STDOUT_LOG, "wine_stdout.log"),
                        (LOG_DIR / "launcher_env.txt", "launcher_env.txt"),
                        (LAUNCHER_STDERR_LOG, "launcher_stderr.log")]:
        if src.exists() and src.stat().st_size > 0:
            try:
                shutil.copy2(src, archive_dir / f"{prefix}_{suffix}")
            except OSError:
                pass
    if args.trace:
        src_trace = TRACE_BASE / f"{args.appid}_{stamp}_{tool}"
        if src_trace.exists():
            dst_trace = archive_dir / f"{prefix}_trace"
            try:
                if dst_trace.exists():
                    shutil.rmtree(dst_trace)
                shutil.move(str(src_trace), str(dst_trace))
            except OSError as e:
                log_warn(f"trace: archive copy failed: {e}")
    log_info(f"logs archived: {LOG_DIR}/runs/{prefix}_*")

    return 1 if (verdict == "crash" or (drifts and not args.update_baseline)) else 0
