#!/usr/bin/env python3
"""quark test runner -- one entry point for the whole suite.

Layers:
  unit    cargo test: the Rust tests under tests/rust, each compiling the
          source module it covers
  gate    checks that need no install (Steam-safe process scoping)
  deploy  the compat tool install.py deployed: package structure and patch
          verification; skipped (not failed) when nothing is deployed
  launch  a real game through iterate.py; skipped when nothing is deployed
  qemu    the full suite in a disposable VM (tests/qemu/guest.py): install,
          probes and the layers above; skipped when no image is provisioned

Usage:
  python3 tests/run.py                  # every layer
  python3 tests/run.py --layer unit     # one layer (repeatable)
  python3 tests/run.py --continue       # keep going after a failure
"""
import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quark_common import REPO_ROOT, COMPAT_DIR, QEMU_READY, log_info, log_warn, log_error

TESTS = REPO_ROOT / "tests"
PY = sys.executable

# A hung test must fail, not wait: every step is bounded. The QEMU suite's
# first run builds patched Wine on the cache disk, hence its own bound.
STEP_TIMEOUT = 900  # seconds
LAYER_TIMEOUT = {"qemu": 7200}

LAYERS = {
    "unit": [
        ("cargo test", ["cargo", "test", "--release"]),
    ],
    "android": [
        ("Android build and packaging", [PY, "-m", "unittest", "discover", "-s", str(TESTS),
                                         "-p", "test_android_*.py", "-v"]),
    ],
    "gate": [
        ("steam safety", [PY, str(TESTS / "test_iterate_steam_safety.py")]),
    ],
    "deploy": [
        ("package", [PY, str(TESTS / "test_package.py")]),
        ("patches", [PY, str(TESTS / "verify_patches.py")]),
    ],
    "launch": [
        ("iterate", [PY, str(TESTS / "iterate.py"), "--skip-build",
                     "--timeout", "20", "--appid", "2379780"]),
    ],
    "qemu": [
        ("qemu suite", [PY, str(TESTS / "qemu" / "guest.py"), "test"]),
    ],
}
NEEDS_DEPLOY = {"deploy", "launch"}


def run_step(name: str, cmd: list[str], timeout: int) -> bool:
    log_info(f"{name}: {' '.join(cmd)}")
    try:
        rc = subprocess.run(cmd, cwd=REPO_ROOT, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        log_error(f"{name}: TIMEOUT after {timeout}s (a test that cannot finish has not passed)")
        return False
    if rc != 0:
        log_error(f"{name}: FAIL (exit {rc})")
        return False
    log_info(f"{name}: PASS")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="quark test runner")
    parser.add_argument("--layer", action="append", choices=list(LAYERS),
                        help="run only this layer (repeatable)")
    parser.add_argument("--continue", dest="keep_going", action="store_true",
                        help="keep going after a failure")
    args = parser.parse_args()

    deployed = (COMPAT_DIR / "proton").exists()
    passed, failed, skipped = 0, 0, 0
    for layer in args.layer or list(LAYERS):
        steps = LAYERS[layer]
        if layer in NEEDS_DEPLOY and not deployed:
            log_warn(f"layer {layer}: SKIP, nothing deployed at {COMPAT_DIR} (run ./install.py)")
            skipped += len(steps)
            continue
        if layer == "qemu" and not QEMU_READY.exists():
            log_warn(f"layer {layer}: SKIP, no finished provision (run tests/qemu/guest.py provision)")
            skipped += len(steps)
            continue
        log_info(f"layer {layer}")
        if layer == "launch":
            from iterate import cleanup_processes
            cleanup_processes()
        for name, cmd in steps:
            if run_step(name, cmd, LAYER_TIMEOUT.get(layer, STEP_TIMEOUT)):
                passed += 1
            else:
                failed += 1
                if not args.keep_going:
                    break
        if layer == "launch":
            cleanup_processes()
        if failed and not args.keep_going:
            break

    summary = f"{passed} passed, {failed} failed, {skipped} skipped"
    if failed:
        log_error(f"FAIL: {summary}")
        return 1
    log_info(f"PASS: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
