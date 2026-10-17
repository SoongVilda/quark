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
WINE_SRC_DIR = Path("/tmp/quark-wine-build/wine-src")
WINE_OBJ_DIR = Path("/tmp/quark-wine-build/wine-obj")
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
# Scoped by prefix or by liveness, never by process name: a bare
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
