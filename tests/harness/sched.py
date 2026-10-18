"""The scheduler under the scoreboard. sched_ext loads one scheduler at a
time and `/sys/kernel/sched_ext/root/ops` names it; with none loaded the
kernel runs EEVDF. PANDEMONIUM runs as `pandemonium.service`, and its own
suite switches to EEVDF with `systemctl stop pandemonium` and back with
`start`, which is what this does. Switching needs root, so every call
here goes through sudo and the caller should have primed it."""
import os
import subprocess
import time
from pathlib import Path

from quark_common import log_info, log_warn

OPS = Path("/sys/kernel/sched_ext/root/ops")
KNOWN = ("pandemonium", "eevdf")


def current() -> str:
    """The loaded sched_ext scheduler's name, or eevdf when none is loaded."""
    try:
        return OPS.read_text().strip() or "eevdf"
    except OSError:
        return "eevdf"


def _sudo() -> list[str]:
    return [] if os.geteuid() == 0 else ["sudo"]


def switch(name: str, timeout_s: float = 15.0) -> bool:
    """Load `name` (pandemonium) or unload every sched_ext scheduler (eevdf),
    and wait until the kernel reports it. Returns whether it did."""
    if name not in KNOWN:
        log_warn(f"scheduler: unknown {name}; known: {', '.join(KNOWN)}")
        return False
    if current() == name:
        return True
    if name == "eevdf":
        subprocess.run(_sudo() + ["systemctl", "stop", "pandemonium"], capture_output=True)
    else:
        subprocess.run(_sudo() + ["systemctl", "start", "pandemonium"], capture_output=True)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if current() == name:
            log_info(f"scheduler: {name}")
            return True
        time.sleep(0.2)
    log_warn(f"scheduler: asked for {name}, kernel reports {current()}")
    return False
