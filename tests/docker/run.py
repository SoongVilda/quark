#!/usr/bin/env python3
"""Host-side launcher for the quark autonomy container.

    python3 tests/docker/run.py                  # build image if needed, full sweep
    python3 tests/docker/run.py --reseed         # wipe scratch volume (fresh prefixes)
    python3 tests/docker/run.py -- --appid 2379780 --trace   # args after -- go to iterate.py

Requires: docker (user in docker group — no sudo), /dev/ntsync on the host.
The host Steam tree is mounted READ-ONLY; the container writes only to its
named volumes and the results dir. Nothing in here can touch the host Steam
client, its processes, or the real prefixes.
"""

import os
import pwd
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
from quark_common import REPO_ROOT as QUARK_REPO, STEAM_ROOT, log_info, log_error

MONTAUK_REPO = (QUARK_REPO / "../montauk").resolve()
USER = pwd.getpwuid(os.getuid()).pw_name
IMAGE = "quark-autonomy"
SCRATCH_VOL = "quark-autonomy-scratch"
WINEBUILD_VOL = "quark-autonomy-winebuild"
OUT_ROOT = Path(os.environ.get("QUARK_AUTONOMY_OUT",
                               Path.home() / ".cache/quark-autonomy/out"))
SWEEP_TIMEOUT = os.environ.get("SWEEP_TIMEOUT", "90")


def main() -> int:
    args = sys.argv[1:]

    if args and args[0] == "--reseed":
        subprocess.run(["docker", "volume", "rm", "-f", SCRATCH_VOL],
                       stdout=subprocess.DEVNULL)
        log_info("scratch volume wiped — next run reseeds prefixes from host snapshot")
        args = args[1:]

    sweep_args = "--all --trace"
    if args and args[0] == "--":
        sweep_args = " ".join(args[1:])

    if not Path("/dev/ntsync").exists():
        log_error("/dev/ntsync missing on host (modprobe ntsync)")
        return 1

    # Always build: docker's layer cache makes an unchanged build a ~1s
    # no-op, and a changed Dockerfile takes effect without manual rmi.
    log_info(f"building image {IMAGE} (cached layers are free)")
    rc = subprocess.run([
        "docker", "build", "-t", IMAGE,
        "--build-arg", f"HOST_USER={USER}",
        "--build-arg", f"HOST_UID={os.getuid()}",
        "--build-arg", f"HOST_GID={os.getgid()}",
        str(HERE),
    ]).returncode
    if rc != 0:
        log_error(f"image build failed rc={rc}")
        return rc

    run_out = OUT_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_out.mkdir(parents=True, exist_ok=True)

    # Capability set follows a standard QEMU/docker runner pattern plus
    # DAC_READ_SEARCH for montauk's /proc reads. No --privileged, no host X,
    # no host pulse.
    cmd = [
        "docker", "run", "--rm",
        "--name", "quark-autonomy",
        "--device", "/dev/ntsync",
        "--cap-add", "SYS_ADMIN", "--cap-add", "BPF", "--cap-add", "PERFMON",
        "--cap-add", "SYS_RESOURCE", "--cap-add", "SYS_PTRACE",
        "--cap-add", "DAC_READ_SEARCH",
        "--security-opt", "seccomp=unconfined",
        "-v", f"{QUARK_REPO}:{QUARK_REPO}",
        "-v", f"{MONTAUK_REPO}:{MONTAUK_REPO}",
        "-v", f"{STEAM_ROOT / 'steamapps/common'}:/host-steam/common:ro",
        "-v", f"{STEAM_ROOT / 'steamapps/compatdata'}:/host-steam/compatdata:ro",
        "-v", f"{STEAM_ROOT / 'steamapps'}:/host-steam/appmanifests:ro",
        "-v", f"{SCRATCH_VOL}:/home/{USER}/.local/share/Steam",
        "-v", f"{WINEBUILD_VOL}:/tmp/quark-wine-build",
        "-v", f"{run_out}:/out",
        "-v", f"{HERE / 'entrypoint.py'}:/entrypoint.py:ro",
        "-e", f"QUARK_REPO={QUARK_REPO}",
        "-e", f"MONTAUK_REPO={MONTAUK_REPO}",
        "-e", f"SWEEP_TIMEOUT={SWEEP_TIMEOUT}",
        "-e", f"SWEEP_ARGS={sweep_args}",
        IMAGE,
    ]
    log_info(f"results: {run_out}")
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
