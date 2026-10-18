"""Shared by install.py and the tests/ suite: the paths, the logger, Steam
discovery and Wine process hygiene. One definition each; nothing here is
reimplemented elsewhere."""

from __future__ import annotations

import os
import pwd
import re
import shutil
import socket
import subprocess
from datetime import datetime
from pathlib import Path


# PATHS

def _user_home() -> Path:
    """The invoking user's home, also under sudo."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        return Path(pwd.getpwnam(sudo_user).pw_dir)
    return Path.home()


REPO_ROOT = Path(__file__).resolve().parent
RUST_DIR = REPO_ROOT / "rust"
QUARK_BIN = REPO_ROOT / "target" / "release" / "quark"

USER_HOME = _user_home()
STEAM_ROOT = USER_HOME / ".local" / "share" / "Steam"
STEAMAPPS = STEAM_ROOT / "steamapps"
COMPAT_DIR = STEAM_ROOT / "compatibilitytools.d" / "quark"

DATA_DIR = USER_HOME / ".local" / "share" / "quark"
CACHE_DIR = USER_HOME / ".cache" / "quark"
LSTEAMCLIENT_CACHE = DATA_DIR / "lsteamclient"
LOG_DIR = Path("/tmp/quark")  # the launcher's log::LOG_DIR
WINE_SRC_DIR = CACHE_DIR / "wine-src"
WINE_OBJ_DIR = CACHE_DIR / "wine-obj"
MONTAUK_SRC = REPO_ROOT.parent / "montauk"  # quark's sibling in the monorepo

# The QEMU suite's own outputs (probes, verdicts, analyses, pulled-back guest
# runs), apart from LOG_DIR, which install.py's clean step removes.
SUITE_DIR = Path("/tmp/quark-suite")

# The QEMU suite's disks. QEMU_READY exists only after a provision finished;
# the image file alone exists from the first moment a provision starts.
QEMU_CACHE = USER_HOME / ".cache" / "quark-qemu"
QEMU_READY = QEMU_CACHE / "provisioned.ok"


# LOGGING
# Every line is `[HH:MM:SS] [LEVEL]   message`; the pad after the level tag
# keeps the message column identical for every level.

def _timestamp() -> str:
    return datetime.now().strftime("[%H:%M:%S]")


def log_info(msg: str) -> None:
    print(f"{_timestamp()} [INFO]   {msg}", flush=True)


def log_warn(msg: str) -> None:
    print(f"{_timestamp()} [WARN]   {msg}", flush=True)


def log_error(msg: str) -> None:
    print(f"{_timestamp()} [ERROR]  {msg}", flush=True)


def log_debug(msg: str) -> None:
    print(f"{_timestamp()} [DEBUG]  {msg}", flush=True)


# STEAM

def get_quark_appids() -> set[str]:
    """Appids Steam's config.vdf maps to the quark compat tool."""
    config_vdf = STEAM_ROOT / "config" / "config.vdf"
    if not config_vdf.exists():
        return set()
    try:
        content = config_vdf.read_text()
        start = content.find('"CompatToolMapping"')
        if start < 0:
            return set()
        brace_start = content.find('{', start)
        if brace_start < 0:
            return set()
        depth = 1
        i = brace_start + 1
        while i < len(content) and depth > 0:
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
            i += 1
        block = content[brace_start + 1:i - 1]
        return set(re.findall(r'"(\d+)"\s*\{[^}]*"name"\s*"quark"', block))
    except (OSError, ValueError):
        return set()


# WINE PROCESS HYGIENE
# Scoped by prefix or by liveness, never by process name: A bare
# `pkill wineserver` or a sweep of every socket takes down other Wine and
# Proton sessions.

def stop_prefix_wineserver(pfx: Path) -> None:
    """Stop the wineserver serving `pfx`, and nothing else."""
    wineserver = shutil.which("wineserver")
    if not wineserver:
        return
    try:
        subprocess.run([wineserver, "-k"], env={**os.environ, "WINEPREFIX": str(pfx)},
                       capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        log_warn(f"wineserver -k timed out for {pfx}")


def stale_wine_sockets() -> list[Path]:
    """Wineserver sockets nothing answers on. A live server, quark's or any
    other Wine/Proton session's, accepts the connect and is left alone."""
    wine_tmp = Path(f"/tmp/.wine-{os.getuid()}")
    if not wine_tmp.exists():
        return []
    stale = []
    for sock_path in wine_tmp.glob("server-*/socket"):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            try:
                s.connect(str(sock_path))
            except OSError:
                stale.append(sock_path)
    return stale


def remove_stale_wine_sockets() -> None:
    """Remove each stale socket and its lock. The server directory stays:
    Wine expects it, and recreating it races with a starting server's bind."""
    for sock in stale_wine_sockets():
        sock.unlink(missing_ok=True)
        (sock.parent / "lock").unlink(missing_ok=True)


# STEAM GAME DISCOVERY
#
# One definition of which installed Steam games are launchable and which
# exe is the game, for the harness and the scoreboard.

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
            log_info(f"{name} ({appid}): no Windows build installed, only the native one; "
                     "force quark as its compatibility tool in Steam to fetch it")
            continue
        seen.add(appid)
        games.append((appid, name, exe))
    games.sort(key=lambda g: g[1])
    return games
