#!/usr/bin/env python3
"""In-container bootstrap: seed -> install -> sweep -> collect.

Runs as the container user; the only privileges are the capabilities
run.py granted (BPF/PERFMON/SYS_ADMIN for montauk's tracer) and the
/dev/ntsync device. The host Steam tree is read-only; every write lands
in the container-private scratch tree or /out.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# The quark repo is mounted at its host path, so the shared module imports
# from there even though this file is mounted alone as /entrypoint.py.
QUARK_REPO = Path(os.environ["QUARK_REPO"])
sys.path.insert(0, str(QUARK_REPO))
from quark_common import LOG_DIR, log_info, log_warn, log_error

MONTAUK_REPO = Path(os.environ["MONTAUK_REPO"])
HOME = Path.home()
SCRATCH = HOME / ".local/share/Steam"      # container-private rw tree (volume)
HOST_COMMON = Path("/host-steam/common")   # ro bind of the host's games
HOST_COMPATDATA = Path("/host-steam/compatdata")  # ro bind of host prefixes
HOST_MANIFESTS = Path("/host-steam/appmanifests")
OUT = Path("/out")
SWEEP_TIMEOUT = os.environ.get("SWEEP_TIMEOUT", "90")
SWEEP_ARGS = os.environ.get("SWEEP_ARGS", "--all --trace").split()


def seed_scratch() -> None:
    """Games symlink to the host read-only; prefixes are SEEDED AS COPIES
    once per volume lifetime, so runs are reproducible and the host
    prefixes stay untouchable. A wiped volume = factory reset."""
    (SCRATCH / "steamapps").mkdir(parents=True, exist_ok=True)
    (SCRATCH / "compatibilitytools.d").mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    common_link = SCRATCH / "steamapps/common"
    if not common_link.exists():
        common_link.symlink_to(HOST_COMMON)

    compatdata = SCRATCH / "steamapps/compatdata"
    if not compatdata.exists():
        log_info("seeding compatdata copies from host snapshot (first run on this volume)")
        compatdata.mkdir(parents=True)
        rc = subprocess.run(
            ["rsync", "-a", f"{HOST_COMPATDATA}/", f"{compatdata}/"]).returncode
        if rc != 0:
            log_error(f"compatdata seed failed (rsync rc={rc})")
            sys.exit(1)

    # appmanifests drive iterate.py's --all discovery
    for manifest in HOST_MANIFESTS.glob("appmanifest_*.acf"):
        dst = SCRATCH / "steamapps" / manifest.name
        if not dst.exists():
            shutil.copy2(manifest, dst)

    # The launcher's Steam discovery tries the ~/.steam/root symlink the
    # native client normally creates; point it at the scratch tree.
    dot_steam = HOME / ".steam"
    dot_steam.mkdir(exist_ok=True)
    for link in ("root", "steam"):
        target = dot_steam / link
        if not target.exists():
            target.symlink_to(SCRATCH)


def start_display_and_audio() -> None:
    """Xvfb gives Wine a real X server; pulse with a null sink keeps
    mmdevapi's audio path honest (audio_client_ti threads are participants
    in the open abort class — do not silence them away)."""
    subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1920x1080x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.environ["DISPLAY"] = ":99"
    time.sleep(0.5)

    pulse = subprocess.run(
        ["pulseaudio", "--start", "--exit-idle-time=-1"],
        capture_output=True).returncode
    sink = subprocess.run(
        ["pactl", "load-module", "module-null-sink", "sink_name=quark_sink"],
        capture_output=True).returncode if pulse == 0 else 1
    if pulse != 0 or sink != 0:
        log_warn("pulseaudio null sink unavailable — audio path degraded")


def build_montauk() -> None:
    log_info("building montauk")
    cfg = subprocess.run(
        ["cmake", "-B", "build", "-DCMAKE_BUILD_TYPE=Release"],
        cwd=MONTAUK_REPO, stdout=subprocess.DEVNULL).returncode
    bld = subprocess.run(
        ["cmake", "--build", "build", f"-j{os.cpu_count()}"],
        cwd=MONTAUK_REPO, stdout=subprocess.DEVNULL).returncode if cfg == 0 else 1
    if bld != 0:
        log_warn("montauk build failed — sweep continues without observer")
    os.environ["PATH"] = f"{MONTAUK_REPO / 'build'}:{os.environ['PATH']}"


def install_quark() -> int:
    """install.py's only sudo path is the observer setcap, which is
    unnecessary here: the container itself carries the BPF capabilities."""
    log_info("installing quark (this builds the wine dynamic port on first run)")
    return subprocess.run(
        [sys.executable, "install.py", "--yes", "--verbose", "--observer"],
        cwd=QUARK_REPO, stdin=subprocess.DEVNULL).returncode


def collect_results() -> None:
    """LOG_DIR holds incidents + logs; baselines ride along.
    /out is the only thing the host reads."""
    log_info(f"collecting results to {OUT}")
    subprocess.run(["rsync", "-a", f"{LOG_DIR}/", f"{OUT}/quark/"],
                   stderr=subprocess.DEVNULL)
    baselines = QUARK_REPO / "tests/baselines.json"
    if baselines.exists():
        shutil.copy2(baselines, OUT / "baselines.json")


def main() -> int:
    seed_scratch()
    start_display_and_audio()
    build_montauk()

    rc = install_quark()
    if rc != 0:
        log_warn(f"install.py exited rc={rc} — aborting sweep, collecting what exists")
        if LOG_DIR.exists():
            shutil.copytree(LOG_DIR, OUT / "install-failure-logs",
                            dirs_exist_ok=True)
        return rc

    # The sweep. iterate.py resolves the Steam tree from $USER's home,
    # which is the scratch tree here — the host is unreachable read-only.
    log_info(f"sweep: iterate.py {' '.join(SWEEP_ARGS)} --timeout {SWEEP_TIMEOUT}")
    sweep_rc = subprocess.run(
        [sys.executable, "tests/iterate.py", *SWEEP_ARGS,
         "--timeout", SWEEP_TIMEOUT],
        cwd=QUARK_REPO).returncode

    collect_results()
    log_info(f"done (sweep rc={sweep_rc})")
    return sweep_rc


if __name__ == "__main__":
    sys.exit(main())
