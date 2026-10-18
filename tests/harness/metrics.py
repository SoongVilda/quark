"""What a launch is measured with: the 50 ms watcher for the game's process
and window, checkpoint screenshots, the frame-time log when MangoHud is
present, and the scheduler label."""
import shutil
import subprocess
import threading
import time
from pathlib import Path
from quark_common import LOG_DIR
from .base import clock_raw_ns, ns_to_ms


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


FRAMETIMES = False  # set by --frametimes; MangoHud's log is read, nothing else of it


def _scheduler_label() -> str:
    """The loaded sched_ext scheduler's name, or eevdf when none is loaded."""
    try:
        name = Path("/sys/kernel/sched_ext/root/ops").read_text().strip()
        return name or "eevdf"
    except OSError:
        return "eevdf"


def _frametime_stats(log_dir) -> dict:
    """p99 frame time and the 1% low from the newest MangoHud CSV in log_dir,
    through sublimation. Column 2 of the log is frametime in ms; the first
    two lines are MangoHud's header and the column names."""
    out = {"frametime_p99_ms": None, "fps_1pct_low": None}
    if not log_dir:
        return out
    csvs = sorted(Path(log_dir).glob("*.csv"), key=lambda p: p.stat().st_mtime)
    if not csvs:
        return out
    try:
        cols = subprocess.run(["sublimation", "field", "2", "-d", ",", str(csvs[-1])],
                              capture_output=True, text=True, timeout=60).stdout.splitlines()[2:]
        values = "\n".join(c for c in cols if c.strip()) + "\n"
        if not values.strip():
            return out
        p99 = subprocess.run(["sublimation", "quantile", "0.99"], input=values,
                             capture_output=True, text=True, timeout=60).stdout.strip()
        out["frametime_p99_ms"] = round(float(p99), 2)
        out["fps_1pct_low"] = round(1000.0 / float(p99), 1) if float(p99) > 0 else None
    except (subprocess.TimeoutExpired, ValueError, OSError, IndexError):
        pass
    return out


class _FirstSeen(threading.Thread):
    """Polls at 50 ms for the game's process and at 100 ms for its X window.

    Wine names the process and the window class after the exe, so both are
    matched on the exe's basename, case-insensitive, comm truncated to 15.
    Resolution is the poll period; montauk's exec events carry no timestamp
    yet, so this is the launch-to-exec source until they do."""

    def __init__(self, exe_path: str, app_id: str, t0_ns: int):
        super().__init__(daemon=True)
        self.exe = Path(exe_path).name.lower()
        self.comm = self.exe[:15]
        # Proton names the window class steam_app_<appid>; Wine, the exe.
        self.classes = (f'"{self.exe}"', f'"steam_app_{app_id}"')
        self.t0 = t0_ns
        self.exec_ms: float | None = None
        self.window_ms: float | None = None
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _scan_procs(self) -> bool:
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            try:
                if (p / "comm").read_text().strip().lower() == self.comm:
                    return True
            except OSError:
                continue
        return False

    def _scan_windows(self) -> bool:
        try:
            out = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True,
                                 text=True, timeout=2).stdout.lower()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        return any(c in out for c in self.classes)

    def run(self) -> None:
        tick = 0
        while not self._stop.is_set():
            now = ns_to_ms(clock_raw_ns() - self.t0)
            if self.exec_ms is None and self._scan_procs():
                self.exec_ms = now
            if self.window_ms is None and tick % 2 == 0 and self._scan_windows():
                self.window_ms = now
            if self.exec_ms is not None and self.window_ms is not None:
                return
            tick += 1
            time.sleep(0.05)
