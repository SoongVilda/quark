"""Per-game regression baselines: the milestones a game reached last time,
and the drift between runs."""
import json
from datetime import datetime
from .base import BASELINES_PATH


# Milestones that indicate healthy progress — losing one = regression.
GOOD_MILESTONES = {"x11drv", "XRandR", "LÖVE", "STEAM_API"}


# Milestones that indicate failure — gaining one we didn't have = regression.
BAD_MILESTONES = {"CRASH", "LOVE_ERROR", "SVC_ERR"}


def load_baselines() -> dict:
    if not BASELINES_PATH.exists():
        return {}
    try:
        return json.loads(BASELINES_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_baselines(baselines: dict) -> None:
    BASELINES_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINES_PATH.write_text(json.dumps(baselines, indent=2, sort_keys=True) + "\n")


def compute_drift(current: dict, baseline: dict) -> list:
    """Return list of drift messages. Empty = no regression."""
    if not baseline:
        return []
    drifts = []
    base_ms = set(baseline.get("milestones", []))
    cur_ms = set(current.get("milestones", []))
    # Good milestones present before and gone now = regression
    for m in sorted((base_ms - cur_ms) & GOOD_MILESTONES):
        drifts.append(f"lost {m}")
    # Bad milestones absent before and present now = regression
    for m in sorted((cur_ms - base_ms) & BAD_MILESTONES):
        drifts.append(f"new {m}")
    if current.get("crash") and not baseline.get("crash", False):
        drifts.append("new crash")
    return drifts


def result_to_baseline_entry(result: dict) -> dict:
    return {
        "name": result.get("name", "?"),
        "milestones": sorted(result.get("milestones", [])),
        "crash": result.get("crash", False),
        "updated": datetime.now().strftime("%Y-%m-%d"),
    }
