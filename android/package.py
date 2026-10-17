#!/usr/bin/env python3
"""Validate an installed Android Wine tree and produce a GameNative WCP."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import struct
import subprocess
import tarfile
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHES = {
    "x86_64": {"unix": "x86_64", "windows": "x86_64", "elf": 62, "pe": 0x8664, "prefix_pe": 0x8664},
    "arm64ec": {"unix": "aarch64", "windows": "aarch64", "elf": 183, "pe": 0xaa64, "prefix_pe": 0xaa64},
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def pe_machine(source, size: int, allow_ne: bool = False):
    header = source.read(64)
    if len(header) != 64 or header[:2] != b"MZ":
        raise ValueError("Invalid PE header")
    offset = struct.unpack_from("<I", header, 60)[0]
    if offset < 64 or offset > size - 6:
        raise ValueError("Truncated PE header")
    source.seek(offset)
    signature = source.read(6)
    if allow_ne and signature[:2] == b"NE":
        return None  # Old Wine templates retain inert 16-bit Windows resources.
    if len(signature) != 6 or signature[:4] != b"PE\0\0":
        raise ValueError("Invalid PE signature")
    machine = struct.unpack_from("<H", signature, 4)[0]
    if machine != 0x8664:
        return machine
    # Linked ARM64EC images use IMAGE_FILE_MACHINE_AMD64, unlike their object
    # files. The PE32+ load config's CHPEMetadataPointer distinguishes them.
    source.seek(offset + 4)
    coff = source.read(20)
    if len(coff) != 20:
        raise ValueError("Truncated PE COFF header")
    sections = struct.unpack_from("<H", coff, 2)[0]
    optional_size = struct.unpack_from("<H", coff, 16)[0]
    if optional_size < 200:
        return machine
    optional = source.read(optional_size)
    if len(optional) != optional_size:
        raise ValueError("Truncated PE optional header")
    if struct.unpack_from("<H", optional)[0] != 0x20b or struct.unpack_from("<I", optional, 108)[0] <= 10:
        return machine
    load_rva, load_size = struct.unpack_from("<II", optional, 112 + 10 * 8)
    if not load_rva or load_size < 208:
        return machine
    section_offset = offset + 24 + optional_size
    if sections > 96 or section_offset + sections * 40 > size:
        raise ValueError("Invalid PE section table")
    for index in range(sections):
        source.seek(section_offset + index * 40)
        section = source.read(40)
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from("<IIII", section, 8)
        delta = load_rva - virtual_address
        if 0 <= delta < max(virtual_size, raw_size):
            if delta + 208 > raw_size or raw_offset + delta + 208 > size:
                raise ValueError("Truncated PE load configuration")
            source.seek(raw_offset + delta + 200)
            if struct.unpack("<Q", source.read(8))[0]:
                return 0xa641
            break
    return machine


def check_elf(path: Path, machine: int, executable: bool = False) -> None:
    size = path.stat().st_size
    with path.open("rb") as source:
        header = source.read(64)
        if len(header) != 64 or header[:7] != b"\x7fELF\x02\x01\x01":
            raise ValueError(f"Invalid 64-bit ELF: {path.name}")
        if struct.unpack_from("<H", header, 18)[0] != machine:
            raise ValueError(f"Wrong ELF architecture: {path.name}")
        offset = struct.unpack_from("<Q", header, 32)[0]
        stride, count = struct.unpack_from("<HH", header, 54)
        if stride != 56 or not count or offset + stride * count > size:
            raise ValueError(f"Invalid ELF program headers: {path.name}")
        loads, interpreter = 0, None
        for number in range(count):
            source.seek(offset + stride * number)
            values = struct.unpack("<IIQQQQQQ", source.read(56))
            kind, _, file_offset, virtual, _, file_size, _, alignment = values
            if file_offset + file_size > size:
                raise ValueError(f"Truncated ELF segment: {path.name}")
            if kind == 1:
                loads += 1
                if alignment < 16384 or alignment & (alignment - 1) or file_offset % alignment != virtual % alignment:
                    raise ValueError(f"ELF must support 16 KiB pages: {path.name}")
            elif kind == 3:
                if file_size > 256:
                    raise ValueError(f"Invalid ELF interpreter: {path.name}")
                source.seek(file_offset)
                interpreter = source.read(file_size).rstrip(b"\0")
        if not loads:
            raise ValueError(f"ELF has no loadable segments: {path.name}")
        if executable and interpreter != b"/system/bin/linker64":
            raise ValueError(f"Wine executable must use the Android linker: {path.name}")


def validate_prefix(path: Path, expected_machine: int) -> str:
    native = False
    names, links = set(), []
    try:
        with tarfile.open(path, "r:xz") as archive:
            for member in archive:
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != ".wine":
                    raise ValueError(f"Unsafe prefix member: {member.name}")
                canonical = str(name)
                if canonical in names:
                    raise ValueError(f"Duplicate prefix member: {member.name}")
                names.add(canonical)
                if member.issym():
                    if canonical == ".wine/dosdevices/z:" and member.linkname == "/":
                        continue  # Standard GameNative/Wine drive mapping.
                    target = posixpath.normpath(str(name.parent / member.linkname))
                    if member.linkname.startswith("/") or not target.startswith(".wine/"):
                        raise ValueError(f"Unsafe prefix symlink: {member.name}")
                    links.append(target)
                elif not (member.isdir() or member.isfile()):
                    raise ValueError(f"Unsupported prefix member: {member.name}")
                if member.isfile() and member.name.endswith((".dll", ".exe", ".drv")):
                    with archive.extractfile(member) as source:
                        machine = pe_machine(source, member.size, allow_ne=True)
                    if machine is None:
                        continue
                    if machine not in (0x14c, expected_machine):
                        raise ValueError(f"Wrong prefix architecture: {member.name}")
                    native |= machine == expected_machine
            if not native or not {".wine/system.reg", ".wine/user.reg"}.issubset(names):
                raise ValueError("Incomplete prefix pack: registry and native PE files are required")
            if any(target not in names for target in links):
                raise ValueError("Broken prefix symlink")
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ValueError(f"Invalid prefix pack: {exc}") from exc
    return sha256_file(path)


def normalize_prefix(source: Path, output: Path, arch: str, expected_sha256: str) -> dict:
    """Remove stale CMOD drive mappings only from a hash-verified prefix pack."""
    if sha256_file(source) != expected_sha256:
        raise ValueError("Official prefix SHA-256 mismatch")
    if arch not in ARCHES or source.resolve() == output.resolve() or output.exists():
        raise ValueError("Invalid normalized prefix output or architecture")
    output.parent.mkdir(parents=True, exist_ok=True)
    removed = []
    removed_metadata = []
    with tempfile.TemporaryDirectory(prefix=".quark-prefix-", dir=output.parent) as work:
        normalized = Path(work) / "prefix.txz"
        with tarfile.open(source, "r:xz") as original, tarfile.open(normalized, "w:xz", preset=1) as archive:
            for member in original:
                name = str(PurePosixPath(member.name))
                if member.isfile() and PurePosixPath(name).name.startswith("._"):
                    with original.extractfile(member) as content:
                        if content.read(4) == b"\x00\x05\x16\x07":
                            removed_metadata.append(name)
                            continue
                if member.issym() and name in (".wine/dosdevices/d:", ".wine/dosdevices/e:"):
                    removed.append({"path": name, "target": member.linkname})
                    continue
                if member.issym() and name == ".wine/dosdevices/z:":
                    member.linkname = "/"
                content = original.extractfile(member) if member.isfile() else None
                archive.addfile(_tar_filter(member), content)
                if content:
                    content.close()
        digest = validate_prefix(normalized, ARCHES[arch]["prefix_pe"])
        normalized.replace(output)
    return {"source_sha256": expected_sha256, "normalized_sha256": digest,
            "removed_drive_links": removed, "removed_metadata_files": removed_metadata,
            "z_drive": "/"}


def validate_tree(tree: Path, prefix_pack: Path, arch: str) -> dict:
    if arch not in ARCHES:
        raise ValueError(f"Unsupported architecture: {arch}")
    tree = tree.resolve(strict=True)
    target = ARCHES[arch]
    unix = f"lib/wine/{target['unix']}-unix"
    windows = f"lib/wine/{target['windows']}-windows"
    required = ["bin/wine", "bin/wineserver", f"{unix}/wine", f"{unix}/ntdll.so",
                f"{unix}/lsteamclient.so", f"{windows}/ntdll.dll",
                f"{windows}/lsteamclient.dll", f"{windows}/steam.exe",
                "share/doc/quark-gamenative/Wine-LICENSE", "share/doc/quark-gamenative/COPYING.LIB"]
    for name in required:
        if not (tree / name).is_file():
            raise ValueError(f"Missing required Wine file: {name}")
    for name in ["bin/wine", "bin/wineserver"]:
        if not (tree / name).stat().st_mode & 0o111:
            raise ValueError(f"Wine loader/server must be executable: {name}")
        check_elf(tree / name, target["elf"], executable=True)
    for name in ["ntdll.so", "lsteamclient.so"]:
        check_elf(tree / unix / name, target["elf"])
    for name in ["ntdll.dll", "lsteamclient.dll", "steam.exe"]:
        path = tree / windows / name
        with path.open("rb") as source:
            if pe_machine(source, path.stat().st_size) != target["pe"]:
                raise ValueError(f"Wrong required PE architecture: {name}")
    identities = {}
    pe_arches = {"i386-windows": 0x14c, "x86_64-windows": 0x8664,
                 "aarch64-windows": 0xaa64, "arm64ec-windows": 0xa641}
    for path in sorted(tree.rglob("*")):
        relative = path.relative_to(tree).as_posix()
        if path.is_symlink():
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"Broken Wine symlink: {relative}") from exc
            link = os.readlink(path)
            if Path(link).is_absolute() or not resolved.is_relative_to(tree):
                raise ValueError(f"Escaping Wine symlink: {relative}")
            identities[relative] = {"symlink": link}
        elif path.is_file():
            with path.open("rb") as source:
                magic = source.read(4)
                if magic == b"\x7fELF":
                    check_elf(path, target["elf"])
                elif magic[:2] == b"MZ":
                    source.seek(0)
                    is_font = relative.startswith("share/wine/fonts/") and path.suffix.lower() == ".fon"
                    machine = pe_machine(source, path.stat().st_size, allow_ne=is_font)
                    if machine is None:
                        identities[relative] = {"sha256": sha256_file(path)}
                        continue
                    expected = next((v for k, v in pe_arches.items() if k in path.parts), target["pe"])
                    allowed = {expected}
                    if arch == "arm64ec" and "aarch64-windows" in path.parts:
                        allowed.add(0xa641)  # Native directory also contains ARM64EC xtajit64.
                    if relative.startswith("share/wine/winmd/") and path.suffix == ".winmd":
                        allowed = {0x14c}  # Architecture-neutral Windows Runtime metadata.
                    if machine not in allowed:
                        raise ValueError(f"Wrong PE architecture: {relative}")
            identities[relative] = {"sha256": sha256_file(path)}
        elif not path.is_dir():
            raise ValueError(f"Unsupported Wine file: {relative}")
    return {"architecture": arch, "prefix_sha256": validate_prefix(prefix_pack, target["prefix_pe"]),
            "files": identities}


def _tar_filter(member: tarfile.TarInfo) -> tarfile.TarInfo:
    member.uid = member.gid = member.mtime = 0
    member.uname = member.gname = ""
    member.mode &= 0o777
    return member


def package_wcp(tree: Path, prefix_pack: Path, arch: str, output: Path, metadata: dict) -> Path:
    tree, prefix_pack, output = tree.resolve(), prefix_pack.resolve(), output.resolve()
    if output.is_relative_to(tree) or output == prefix_pack or output.suffix != ".wcp":
        raise ValueError("Invalid output path; choose a .wcp outside the input tree")
    if output.exists() or Path(str(output) + ".sha256").exists():
        raise ValueError("The output or its checksum already exists; choose a new output path")
    required = {"architecture", "compilation_status", "version_name", "profile_version_name", "quark_revision",
                "wine_revision", "patches", "toolchains"}
    if not required.issubset(metadata) or metadata["architecture"] != arch or metadata["compilation_status"] != "compiled":
        raise ValueError("Invalid build metadata: matching completed compilation is required")
    profile_version = metadata["profile_version_name"]
    if not isinstance(profile_version, str) or not re.fullmatch(r"\d+(?:\.\d+)*(?:-\d+(?:\.\d+)*)?", profile_version):
        raise ValueError("GameNative requires a numeric profile version; keep branding in the filename and description")
    validation = validate_tree(tree, prefix_pack, arch)
    manifest = {**metadata, "package": validation}
    profile = {"type": "Proton", "versionName": f"{profile_version}-{arch}", "versionCode": 1,
               "description": f"Experimental Quark-patched GameNative Wine ({arch}); Android SDK 28, 16 KiB pages. Use a fresh matching container. Does not run the Quark launcher.",
               "files": [], "wine": {"binPath": "bin", "libPath": "lib", "prefixPack": "prefixPack.txz"}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".quark-wcp-", dir=output.parent) as work:
        stage = Path(work)
        (stage / "profile.json").write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n")
        (stage / "build-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        payload = stage / "payload.tar"
        with tarfile.open(payload, "w", format=tarfile.PAX_FORMAT, dereference=False) as archive:
            for name in ["bin", "lib", "share"]:
                paths = [tree / name, *sorted((tree / name).rglob("*"))]
                for path in paths:
                    archive.add(path, arcname=path.relative_to(tree).as_posix(), recursive=False, filter=_tar_filter)
            for name in ["profile.json", "build-manifest.json"]:
                archive.add(stage / name, arcname=name, filter=_tar_filter)
            archive.add(prefix_pack, arcname="prefixPack.txz", filter=_tar_filter)
            archive.add(REPO_ROOT / "LICENSE", arcname="licenses/Quark-LICENSE", filter=_tar_filter)
        compressed = stage / "output.wcp"
        try:
            subprocess.run(["zstd", "-q", "-T2", "-10", "--no-progress", str(payload), "-o", str(compressed)],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"WCP compression failed: {exc}") from exc
        checksum = sha256_file(compressed)
        checksum_file = stage / "output.sha256"
        checksum_file.write_text(f"{checksum}  {output.name}\n")
        compressed.replace(output)
        checksum_file.replace(Path(str(output) + ".sha256"))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", required=True, type=Path)
    parser.add_argument("--prefix-pack", required=True, type=Path)
    parser.add_argument("--arch", required=True, choices=ARCHES)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = package_wcp(args.tree, args.prefix_pack, args.arch, args.output,
                             json.loads(args.metadata.read_text()))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"WCP packaging failed: {exc}\n")
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
