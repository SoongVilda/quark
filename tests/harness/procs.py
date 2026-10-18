"""Process scoping and teardown, Steam-safe. Every kill here is scoped to
quark by exe inside the compat dir, by descendant, or by the WINEPREFIX
fingerprint of an A/B run; never a bare name, never a global sweep."""
import os
import pathlib
import signal
import time
from pathlib import Path
from quark_common import COMPAT_DIR, STEAMAPPS, log_info, log_warn, remove_stale_wine_sockets
from .base import ProcSnapshot, SYSCALL_NAMES
from .tools import _compat_data_path


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
        # stat format: Pid (comm with spaces and parens) state ppid ...
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

    Steam-safe: Every kill is scoped to /proc inspection of binaries living
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

    # Phase 2: Catch reparented orphans (descendants whose parent died
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


# /proc inspection
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


def _tree_pids_by_environ(app_id: str, compat_tool: str) -> set:
    """Every process whose environment names this run's compatdata.

    Keyed on WINEPREFIX, which every tool sets to its own pfx under the
    compatdata dir the harness chose; Proton rewrites STEAM_COMPAT_DATA_PATH
    in the game's environment back to the bare app id, so that one cannot
    tell an A/B prefix from quark's. Steam's own helpers carry neither."""
    needle = f"WINEPREFIX={_compat_data_path(app_id, compat_tool) / 'pfx'}".encode()
    found = set()
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            if needle in (p / "environ").read_bytes():
                found.add(int(p.name))
        except OSError:
            continue
    return found


def _tree_pids(app_id: str, compat_tool: str) -> set:
    return _our_quark_pids() if compat_tool == "quark" else _tree_pids_by_environ(app_id, compat_tool)


def _tree_rss_mb(pids: set) -> float:
    total_kb = 0
    for pid in pids:
        try:
            for line in open(f"/proc/{pid}/status"):
                if line.startswith("VmRSS:"):
                    total_kb += int(line.split()[1])
                    break
        except OSError:
            continue
    return total_kb / 1024


def _terminate_tree(pids: set, grace_s: float = TERM_GRACE_S) -> None:
    """SIGTERM a pid set, wait up to grace_s for it to drain, SIGKILL the rest."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    t0 = time.monotonic()
    while time.monotonic() - t0 < grace_s:
        if not any(Path(f"/proc/{pid}").exists() for pid in pids):
            return
        time.sleep(0.2)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
