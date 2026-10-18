"""The scoreboard: every game through every tool, same harness, one table.

This is not a test. It is the final sanity check at the end of a version,
run once by hand when a release is being prepared, and it is not part of
tests/run.py or any layer of the suite. It takes the GPU and the screen
for its whole duration, launches each game once per tool (twice with a
warm-up) and leaves its numbers in scoreboard.md and scoreboard.json for
the README. Pass or fail is not its output; the table is.

Each cell is a full iterate.py run with --metrics-json. The table reports,
per game and tool, launch-to-exec and launch-to-window from the harness's
50 ms watcher, the process count and resident set at the end of the run,
and the verdict. Runs are serial by design: Two games share one GPU.

    python3 tests/scoreboard.py                         # six games, three tools
    python3 tests/scoreboard.py --games 2379780 --tools quark,proton-exp
    python3 tests/scoreboard.py --runs 3                # median of three
"""

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

from quark_common import log_info, log_warn
from . import sched
from .tools import COMPAT_TOOLS

TESTS = Path(__file__).resolve().parent.parent
RUN = TESTS / "run.py"
OUT_MD = TESTS / "scoreboard.md"
OUT_JSON = TESTS / "scoreboard.json"

GAMES = {
    "367520": "Hollow Knight",
    "2379780": "Balatro",
    "379720": "DOOM (2016)",
    "1361510": "TMNT: Shredder's Revenge",
    "2529820": "Wedding Witch",
    "2218750": "Halls of Torment",
}
TOOLS = ["quark"]  # the field; Proton entries join it only by flag
COLUMNS = ("exec_ms", "window_ms", "procs", "rss_mb", "frametime_p99_ms", "fps_1pct_low",
           "ntsync_ops", "helper_execs")
FRAMETIMES = False
TRACE = False


def run_cell(appid: str, tool: str, timeout: int) -> dict | None:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out_path = Path(f.name)
    out_path.unlink()
    cmd = [sys.executable, str(RUN), "launch", appid, "--tool", tool,
           "--timeout", str(timeout), "--skip-build", "--metrics-json", str(out_path)]
    if FRAMETIMES:
        cmd.append("--frametimes")
    if TRACE:
        cmd.append("--trace")
    log_info(f"cell: {GAMES.get(appid, appid)} via {tool}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 120)
    except subprocess.TimeoutExpired:
        log_warn("cell: the launch did not finish; no metrics")
        return None
    if not out_path.exists():
        tail = [l for l in (r.stdout + r.stderr).strip().splitlines() if l.strip()][-3:]
        log_warn(f"cell: no metrics written (exit {r.returncode})")
        for line in tail:
            log_warn(f"  {line}")
        return None
    data = json.loads(out_path.read_text())
    out_path.unlink()
    return data


def median_cell(runs: list[dict]) -> dict:
    cell = dict(runs[-1])
    for k in COLUMNS:
        vals = [r[k] for r in runs if r.get(k) is not None]
        cell[k] = statistics.median(vals) if vals else None
    cell["verdicts"] = [r["verdict"] for r in runs]
    return cell


def fmt(v, unit: str = "") -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:,.0f}{unit}" if unit == " ms" else f"{v:,.1f}{unit}"
    return f"{v}{unit}"


def render(board: dict[str, dict[str, dict]], tools: list[str]) -> str:
    ft, tr = FRAMETIMES, TRACE
    scheds = sorted({c.get("scheduler", "?") for per_tool in board.values() for c in per_tool.values()})
    lines = [f"Scheduler: {', '.join(scheds) or 'unknown'}. Ratios are quark over the tool; "
             "lower is better for times, processes and RSS.", "",
             "| Game | Tool | Verdict | Launch to exec | Launch to window | Processes | RSS |"
             + (" Frame time p99 | 1% low |" if ft else "") + (" ntsync ops | helper execs |" if tr else ""),
             "|---|---|---|---|---|---|---|" + ("---|---|" if ft else "") + ("---|---|" if tr else "")]
    for appid, per_tool in board.items():
        base = per_tool.get(tools[0])
        for tool in tools:
            c = per_tool.get(tool)
            if not c:
                lines.append(f"| {GAMES.get(appid, appid)} | {tool} | no run | | | | |")
                continue
            def ratio(k):
                if tool == tools[0] or not base or base.get(k) is None or c.get(k) in (None, 0):
                    return ""
                return f" ({base[k] / c[k]:.2f}x)"
            lines.append(
                f"| {GAMES.get(appid, appid)} | {tool} | {'/'.join(c['verdicts'])} | "
                f"{fmt(c['exec_ms'], ' ms')}{ratio('exec_ms')} | "
                f"{fmt(c['window_ms'], ' ms')}{ratio('window_ms')} | "
                f"{fmt(c['procs'])}{ratio('procs')} | {fmt(c['rss_mb'], ' MB')}{ratio('rss_mb')} |"
                + (f" {fmt(c.get('frametime_p99_ms'), ' ms')}{ratio('frametime_p99_ms')} | "
                   f"{fmt(c.get('fps_1pct_low'))} |" if ft else "")
                + (f" {fmt(c.get('ntsync_ops'))} | {fmt(c.get('helper_execs'))} |" if tr else ""))
    return "\n".join(lines) + "\n"


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--games", default=",".join(GAMES), help="comma-separated app ids")
    ap.add_argument("--tools", default="",
                    help="comma-separated Proton tools to add beside quark, e.g. proton-exp,proton11; "
                         "quark is always in the field")
    ap.add_argument("--all-tools", action="store_true",
                    help="add every Proton tool that is installed instead of a named list")
    ap.add_argument("--timeout", type=int, default=45, help="seconds per cell (default 45)")
    ap.add_argument("--runs", type=int, default=1, help="runs per cell, median reported")
    ap.add_argument("--warmup", type=int, default=1,
                    help="uncounted runs per cell first, so a cold prefix or cache is not scored (default 1)")
    ap.add_argument("--frametimes", action="store_true",
                    help="add MangoHud frame-time columns (needs mangohud installed)")
    ap.add_argument("--trace", action="store_true",
                    help="capture each cell under montauk and add the ntsync and helper-exec columns")
    ap.add_argument("--merge", action="store_true",
                    help="start from the board in scoreboard.json and add to it, so a tool or a "
                         "scheduler can be added without rerunning the rest")
    ap.add_argument("--schedulers", default="",
                    help="comma-separated schedulers to run the whole matrix under, e.g. "
                         "pandemonium,eevdf; the default is whichever is loaded now. Switching "
                         "stops or starts pandemonium.service through sudo and restores the "
                         "original at the end")


def run(args: argparse.Namespace) -> int:
    global FRAMETIMES, TRACE
    FRAMETIMES = args.frametimes
    TRACE = args.trace
    games = [g for g in args.games.split(",") if g]
    tools = list(TOOLS)
    if args.all_tools:
        tools += [t for t, p in COMPAT_TOOLS.items() if t != "quark" and p.exists()]
    else:
        for t in (x for x in args.tools.split(",") if x):
            if t not in COMPAT_TOOLS:
                log_warn(f"scoreboard: unknown tool {t}; known: {', '.join(COMPAT_TOOLS)}")
                return 2
            if t not in tools:
                tools.append(t)
    schedulers = [x for x in args.schedulers.split(",") if x] or [sched.current()]
    for name in schedulers:
        if name not in sched.KNOWN:
            log_warn(f"scoreboard: unknown scheduler {name}; known: {', '.join(sched.KNOWN)}")
            return 2
    columns = [f"{t}@{s}" if len(schedulers) > 1 else t for s in schedulers for t in tools]
    original = sched.current()
    board: dict[str, dict[str, dict]] = {}
    if args.merge and OUT_JSON.exists():
        board = json.loads(OUT_JSON.read_text())
        have = sorted({k for per_tool in board.values() for k in per_tool})
        columns = [c for c in have if c not in columns] + columns
        log_info(f"scoreboard: merging into the board of {len(board)} game(s), columns {', '.join(have)}")
    try:
        for name in schedulers:
            if not sched.switch(name):
                return 2
            for appid in games:
                board.setdefault(appid, {})
                for tool in tools:
                    key = f"{tool}@{name}" if len(schedulers) > 1 else tool
                    runs = []
                    for _ in range(args.warmup):
                        run_cell(appid, tool, args.timeout)
                    for _ in range(args.runs):
                        cell = run_cell(appid, tool, args.timeout)
                        if cell:
                            runs.append(cell)
                    if runs:
                        board[appid][key] = median_cell(runs)
                    # Persist after every cell so a stopped matrix keeps its rows.
                    OUT_JSON.write_text(json.dumps(board, indent=1) + "\n")
                    OUT_MD.write_text(render(board, columns))
    finally:
        if sched.current() != original:
            sched.switch(original)
    tools = columns

    table = render(board, tools)
    print(table)
    OUT_MD.write_text(table)
    OUT_JSON.write_text(json.dumps(board, indent=1) + "\n")
    log_info(f"scoreboard: {OUT_MD} and {OUT_JSON}")
    return 0

