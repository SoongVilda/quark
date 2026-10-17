#!/usr/bin/env python3
"""Prepare an exact GameNative Wine revision with the reviewed Quark patches."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_sources(path: Path) -> dict:
    config = json.loads(path.read_text())
    if config.get("schema") != 1:
        raise ValueError("Unsupported Android sources schema")
    revision = config.get("wine", {}).get("revision", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Wine revision must be an exact Git commit ID")
    return config


def run_git(source: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(source), *args], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout


def prepare_source(source_dir: Path, config: dict, repo_root: Path) -> dict:
    source_dir = source_dir.resolve(strict=True)
    repo_root = repo_root.resolve(strict=True)
    actual_root = Path(run_git(source_dir, "rev-parse", "--show-toplevel").strip()).resolve()
    if actual_root != source_dir:
        raise ValueError("Source directory must be the Git checkout root")
    revision = run_git(source_dir, "rev-parse", "HEAD").strip()
    if revision != config["wine"]["revision"]:
        raise ValueError(f"Unexpected Wine revision: {revision}")
    if run_git(source_dir, "status", "--porcelain", "--untracked-files=normal").strip():
        raise ValueError("Wine source must be clean; use a fresh checkout for another build")
    patch_paths = []
    identities = []
    for entry in config["patches"]:
        relative = Path(entry["path"])
        path = (repo_root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(repo_root):
            raise ValueError(f"Invalid patch path: {relative}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            raise ValueError(f"Patch SHA-256 mismatch: {relative}")
        patch_paths.append(str(path))
        identities.append({"path": entry["path"], "sha256": digest})
    if not patch_paths:
        raise ValueError("No Quark patches selected")
    # Check the complete set first; git apply is atomic unless --reject is used.
    run_git(source_dir, "apply", "--check", "--whitespace=error-all", *patch_paths)
    run_git(source_dir, "apply", "--whitespace=error-all", *patch_paths)
    return {"wine_revision": revision, "patches": identities}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = prepare_source(args.source, load_sources(REPO_ROOT / "android/sources.json"), REPO_ROOT)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Android source preparation failed: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
