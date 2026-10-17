#!/usr/bin/env python3
"""Build experimental Quark-patched GameNative Wine in a private workspace."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

from prepare import REPO_ROOT, load_sources, prepare_source, run_git
from package import ARCHES, normalize_prefix, package_wcp, sha256_file

TOOLS = ["autoconf", "autoreconf", "automake", "bison", "flex", "m4", "make", "gcc", "g++",
         "perl", "pkg-config", "git", "bash", "tar", "unzip", "zstd", "cargo", "rustc", "rustup", "readelf", "nm", "awk"]


def check_prerequisites(search_path=None) -> None:
    missing = [tool for tool in TOOLS if shutil.which(tool, path=search_path) is None]
    if missing:
        raise ValueError("Missing build tools: " + ", ".join(missing))


def claim_workspace(work: Path, arch: str) -> Path:
    work = work.resolve()
    if arch not in ARCHES:
        raise ValueError("Unsupported architecture")
    if work.is_relative_to(REPO_ROOT) or REPO_ROOT.is_relative_to(work):
        raise ValueError("Build workspace must not overlap the Quark repository")
    marker = work / ".quark-android-workspace.json"
    if marker.exists():
        identity = json.loads(marker.read_text())
        if identity != {"schema": 1, "architecture": arch}:
            raise ValueError("Workspace ownership/architecture mismatch")
    else:
        if work.exists() and any(work.iterdir()):
            raise ValueError("Build workspace must be empty or already owned by this build")
        work.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"schema": 1, "architecture": arch}) + "\n")
    return work


def run_stages(stages, source: Path, env: dict, logs: Path) -> None:
    logs.mkdir(parents=True, exist_ok=True)
    for name, command in stages:
        log_path = logs / (name + ".log")
        print(f"Starting {name}; log: {log_path}", flush=True)
        try:
            with log_path.open("w") as log:
                subprocess.run([str(arg) for arg in command], cwd=source, env=env,
                               stdout=log, stderr=subprocess.STDOUT, check=True, timeout=7200)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"{name} failed (exit {exc.returncode}); see {log_path}") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"{name} failed: {exc}; see {log_path}") from exc
        print(f"Finished {name}", flush=True)


def make_stages(arch: str, source: Path, step: Path):
    stages = [("autogen", ["bash", "-e", str(source / "autogen.sh")]),
              ("wine-tools", ["bash", "-e", str(source / "build-scripts/build-step0.sh")])]
    if arch == "arm64ec":
        stages.append(("sysvshm", ["bash", "-e", str(step), "--build-sysvshm"]))
    for name, flag in [("ntsync", "--build-ntsync-android"), ("configure", "--configure"),
                       ("compile", "--build"), ("install", "--install")]:
        stages.append((name, ["bash", "-e", str(step), flag]))
    return stages


def adapt_script(original: str, android_patches=False, host_tools=False) -> str:
    """Relocate the pinned scripts' build paths; retain their compiler options."""
    adapted = original.replace("${HOME}", "${QUARK_ANDROID_WORKROOT}").replace("$HOME", "${QUARK_ANDROID_WORKROOT}")
    adapted = adapted.replace("$(nproc)", "${QUARK_ANDROID_JOBS}")
    if host_tools:
        if adapted.count("../configure ") != 1:
            raise ValueError("Unrecognized native host-tools configure command")
        adapted = adapted.replace("../configure ", "../configure --enable-win64 ")
        adapted = adapted.replace("\nmake ", " --without-freetype\nmake ", 1)
    if android_patches:
        needle = "        git apply ./android/patches/$patch"
        if adapted.count(needle) != 1:
            raise ValueError("Unrecognized upstream Android patch loop")
        replacement = '''        if git apply --check "./android/patches/$patch"; then
            git apply "./android/patches/$patch"
            printf 'QUARK_ANDROID_PATCH\\t%s\\tapplied\\n' "$patch"
        elif git apply --reverse --check "./android/patches/$patch"; then
            printf 'QUARK_ANDROID_PATCH\\t%s\\talready-present\\n' "$patch"
        else
            printf 'Android patch does not match the source: %s\\n' "$patch" >&2
            exit 1
        fi'''
        adapted = adapted.replace(needle, replacement)
    return adapted


def download(url: str, destination: Path, expected_sha256: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        print(f"Downloading {destination.name}", flush=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".download-", delete=False) as temporary:
            part = Path(temporary.name)
            try:
                with urllib.request.urlopen(url, timeout=120) as response:
                    while chunk := response.read(1024 * 1024):
                        temporary.write(chunk)
                temporary.flush()
                if sha256_file(part) != expected_sha256:
                    raise ValueError(f"Download SHA-256 mismatch: {destination.name}")
                part.replace(destination)
            finally:
                part.unlink(missing_ok=True)
    if sha256_file(destination) != expected_sha256:
        raise ValueError(f"Cached SHA-256 mismatch: {destination.name}")
    return destination


def extract_tar(archive_path: Path, destination: Path, relocate_termux=False) -> None:
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError(f"Unsafe archive member: {member.name}")
            if member.issym():
                link = member.linkname
                if relocate_termux and link.startswith("/data/data/com.termux/files/usr/"):
                    link = posixpath.relpath(link.lstrip("/"), str(name.parent))
                    member.linkname = link
                target = PurePosixPath(posixpath.normpath(str(name.parent / link)))
                if target.is_absolute() or ".." in target.parts:
                    raise ValueError(f"Unsafe archive symlink: {member.name}")
            elif member.islnk():
                link = PurePosixPath(member.linkname)
                if link.is_absolute() or ".." in link.parts:
                    raise ValueError(f"Unsafe archive hardlink: {member.name}")
            elif not (member.isfile() or member.isdir()):
                raise ValueError(f"Unsupported archive member: {member.name}")
        destination.mkdir(parents=True, exist_ok=False)
        archive.extractall(destination, members=members, filter="data")


def cached_toolchain(archive: Path, cache: Path, folder: str) -> Path:
    root = cache / "extracted"
    root.mkdir(parents=True, exist_ok=True)
    destination = root / folder
    digest = sha256_file(archive)
    marker = destination / ".quark-archive-sha256"
    if destination.exists():
        if not marker.is_file() or marker.read_text().strip() != digest:
            raise ValueError(f"Unverified existing toolchain directory: {destination}")
        return destination
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=root) as work:
        unpacked = Path(work) / "unpacked"
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as source:
                if any(PurePosixPath(n).is_absolute() or ".." in PurePosixPath(n).parts for n in source.namelist()):
                    raise ValueError("Unsafe toolchain ZIP archive")
            unpacked.mkdir()
            subprocess.run(["unzip", "-q", str(archive), "-d", str(unpacked)], check=True)
        else:
            extract_tar(archive, unpacked)
        (unpacked / folder).rename(destination)
        marker.write_text(digest + "\n")
    return destination


def checkout(repository: str, revision: str, destination: Path, logs: Path) -> None:
    if destination.exists():
        raise ValueError(f"Source directory already exists; use a fresh build workspace: {destination}")
    run_stages([("source-init-" + destination.name, ["git", "init", "-q", str(destination)]),
                ("source-fetch-" + destination.name, ["git", "-C", str(destination), "fetch", "--depth", "1", repository, revision]),
                ("source-checkout-" + destination.name, ["git", "-C", str(destination), "checkout", "--detach", "FETCH_HEAD"])],
               destination.parent, os.environ.copy(), logs)


def _tool_version(command, env) -> str:
    return subprocess.check_output(command, text=True, env=env, stderr=subprocess.STDOUT).splitlines()[0]


def build(arch: str, work: Path, output_dir: Path, cache: Path, jobs: int) -> Path:
    check_prerequisites()
    config = load_sources(REPO_ROOT / "android/sources.json")
    if run_git(REPO_ROOT, "status", "--porcelain", "--untracked-files=normal").strip():
        raise ValueError("Commit Quark changes before building so the package has an exact source revision")
    quark_revision = run_git(REPO_ROOT, "rev-parse", "HEAD").strip()
    if jobs < 1 or jobs > 64:
        raise ValueError("Choose 1–64 build jobs")
    work = claim_workspace(work, arch)
    output_dir = output_dir.resolve()
    if output_dir.is_relative_to(REPO_ROOT):
        raise ValueError("Build output must be outside the Quark repository")
    rust_env = {**os.environ, "RUSTUP_TOOLCHAIN": config["rust"]["version"]}
    installed = subprocess.check_output(["rustup", "target", "list", "--installed", "--toolchain", config["rust"]["version"]], text=True)
    if not {"aarch64-linux-android", "x86_64-linux-android"}.issubset(installed.split()):
        raise ValueError("Install Rust Android targets first: rustup target add --toolchain 1.90.0 aarch64-linux-android x86_64-linux-android")
    logs = work / "logs"
    cache = cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    native = ARCHES[arch]["unix"]
    termux = config["termux"]["architectures"][arch]
    termux_archive = download(f"https://github.com/GameNative/termux-on-gha/releases/download/{config['termux']['release']}/{termux['file']}",
                              cache / termux["file"], termux["sha256"])
    ndk_archive = download(config["ndk"]["url"], cache / Path(config["ndk"]["url"]).name, config["ndk"]["sha256"])
    mingw_archive = download(config["llvm_mingw"]["url"], cache / Path(config["llvm_mingw"]["url"]).name, config["llvm_mingw"]["sha256"])
    ndk = cached_toolchain(ndk_archive, cache, "android-ndk-r27d")
    mingw = cached_toolchain(mingw_archive, cache, config["llvm_mingw"]["directory"])
    runtime = work / "runtime"
    (runtime / "Android/Sdk/ndk").mkdir(parents=True, exist_ok=True)
    (runtime / "toolchains").mkdir(parents=True, exist_ok=True)
    (runtime / "Android/Sdk/ndk" / config["ndk"]["version"]).symlink_to(ndk, target_is_directory=True)
    (runtime / "toolchains" / config["llvm_mingw"]["directory"]).symlink_to(mingw, target_is_directory=True)
    extract_tar(termux_archive, runtime / "termuxfs" / native, relocate_termux=True)
    source = work / "source"
    checkout(config["wine"]["repository"], config["wine"]["revision"], source, logs)
    preparation = prepare_source(source, config, REPO_ROOT)
    ntsync = work / "ntsync-android"
    checkout(config["ntsync"]["repository"], config["ntsync"]["revision"], ntsync, logs)
    adapters = {}
    for path in [source / "android/android_sysvshm/build-aarch64.sh", ntsync / "build-scripts/build-android.sh"]:
        path.write_text(adapt_script(path.read_text()))
        adapters[str(path.relative_to(work))] = sha256_file(path)
    original_step = source / "build-scripts" / f"build-step-{arch}.sh"
    step = original_step.with_name("quark-" + original_step.name)
    step.write_text(adapt_script(original_step.read_text(), android_patches=True))
    adapters[str(step.relative_to(work))] = sha256_file(step)
    step0 = source / "build-scripts/build-step0.sh"
    step0.write_text(adapt_script(step0.read_text(), host_tools=True))
    adapters[str(step0.relative_to(work))] = sha256_file(step0)
    # Upstream scripts contain unquoted path expansions. A private, space-free
    # alias lets caller paths contain spaces without changing their build logic.
    with tempfile.TemporaryDirectory(prefix="quark-android-", dir="/tmp") as alias_dir:
        alias = Path(alias_dir) / "work"
        alias.symlink_to(work, target_is_directory=True)
        alias_source = alias / "source"
        env = {**rust_env, "QUARK_ANDROID_WORKROOT": str(alias / "runtime"), "QUARK_ANDROID_JOBS": str(jobs),
               "PWD": str(alias_source), "NTSYNC_ANDROID_DIR": str(alias / "ntsync-android"), "CCACHE_DIR": str(work / "ccache")}
        run_stages(make_stages(arch, alias_source, alias / step.relative_to(work)), alias_source, env, logs)
    tree = runtime / ("compiled-files-aarch64" if arch == "arm64ec" else "compiled-files-x86_64")
    docs = tree / "share/doc/quark-gamenative"
    docs.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / "LICENSE", docs / "Wine-LICENSE")
    shutil.copyfile(source / "COPYING.LIB", docs / "COPYING.LIB")
    entry = config["prefix"]["architectures"][arch]
    official = download(f"https://raw.githubusercontent.com/GameNative/bionic-prefix-files/{config['prefix']['revision']}/{entry['file']}",
                        cache / entry["file"], entry["sha256"])
    normalized = work / "prefixPack.txz"
    prefix_identity = normalize_prefix(official, normalized, arch, entry["sha256"])
    patch_results = [line.split("\t")[1:] for line in (logs / "configure.log").read_text().splitlines()
                     if line.startswith("QUARK_ANDROID_PATCH\t")]
    metadata = {**preparation, "architecture": arch, "compilation_status": "compiled", "version_name": config["version_name"],
                "quark_revision": quark_revision, "source_inputs": config,
                "prefix_normalization": prefix_identity, "build_adapters": adapters, "android_patches": patch_results,
                "toolchains": {"rust": _tool_version(["rustc", "--version"], rust_env),
                               "cargo": _tool_version(["cargo", "--version"], rust_env),
                               "autoconf": _tool_version(["autoconf", "--version"], rust_env),
                               "host_cc": _tool_version(["gcc", "--version"], rust_env),
                               "ndk": _tool_version([str(ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin/clang"), "--version"], rust_env),
                               "llvm_mingw": _tool_version([str(mingw / "bin/clang"), "--version"], rust_env)}}
    lock = ntsync / "Cargo.lock"
    if lock.exists():
        metadata["ntsync_cargo_lock_sha256"] = sha256_file(lock)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"build-{arch}.json").write_text(json.dumps(metadata, indent=2) + "\n")
    output = output_dir / f"{config['version_name']}-{arch}.wcp"
    return package_wcp(tree, normalized, arch, output, metadata)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", required=True, choices=ARCHES)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    try:
        result = build(args.arch, args.work_dir, args.output_dir,
                       args.cache_dir or args.work_dir / "cache", args.jobs)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, tarfile.TarError) as exc:
        parser.exit(1, f"Android build failed: {exc}\n")
    print(f"WCP built: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
