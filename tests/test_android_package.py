#!/usr/bin/env python3
"""Exercise archive validation with small, explicitly non-runnable fixtures."""
import hashlib
import importlib.util
import io
import json
import re
from pathlib import Path
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


def elf(machine):
    data = bytearray(256)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHIQQQIHHHHHH", data, 16, 3, machine, 1, 0, 64, 0, 0,
                     64, 56, 2, 0, 0, 0)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0, 0, 0, 256, 256, 16384)
    interpreter = b"/system/bin/linker64\0"
    struct.pack_into("<IIQQQQQQ", data, 120, 3, 4, 192, 192, 192,
                     len(interpreter), len(interpreter), 1)
    data[192:192 + len(interpreter)] = interpreter
    return bytes(data)


def pe(machine):
    data = bytearray(256)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    struct.pack_into("<4sH", data, 128, b"PE\0\0", machine)
    return bytes(data)


def hybrid_pe(metadata=True):
    data = bytearray(1024)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    struct.pack_into("<4sHHIIIHH", data, 128, b"PE\0\0", 0x8664, 1, 0, 0, 0, 240, 0x2022)
    optional = 152
    struct.pack_into("<H", data, optional, 0x20b)
    struct.pack_into("<I", data, optional + 108, 16)
    struct.pack_into("<II", data, optional + 112 + 10 * 8, 0x1000, 208)
    struct.pack_into("<8sIIII", data, optional + 240, b".rdata\0\0", 512, 0x1000, 512, 512)
    struct.pack_into("<I", data, 512, 208)
    struct.pack_into("<Q", data, 512 + 200, 0x180001100 if metadata else 0)
    return bytes(data)


def prefix(path, machine, extra=None, z_target="/"):
    with tarfile.open(path, "w:xz") as archive:
        for name, content in [(".wine/system.reg", b"WINE REGISTRY Version 2\n"),
                              (".wine/user.reg", b"WINE REGISTRY Version 2\n"),
                              (".wine/drive_c/windows/system32/example.dll", pe(machine))]:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        member = tarfile.TarInfo(".wine/dosdevices/z:")
        member.type, member.linkname = tarfile.SYMTYPE, z_target
        archive.addfile(member)
        for member in extra if isinstance(extra, list) else ([extra] if extra else []):
            if isinstance(member, tuple):
                archive.addfile(member[0], io.BytesIO(member[1]))
            else:
                archive.addfile(member)


class PackageTests(unittest.TestCase):
    def setUp(self):
        path = REPO / "android/package.py"
        self.assertTrue(path.is_file(), "Android WCP packaging is missing")
        spec = importlib.util.spec_from_file_location("android_package", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory(prefix="quark package fixture ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tree = self.root / "installed Wine"
        self.pack = self.root / "prefix.txz"
        self.output = self.root / "test-fixture.wcp"
        self.make_tree("x86_64")
        self.metadata = {"architecture": "x86_64", "compilation_status": "compiled",
                         "version_name": "quark-patched-test-fixture", "profile_version_name": "11.0-2.901",
                         "quark_revision": "1" * 40,
                         "wine_revision": "2" * 40, "patches": [{"path": "test.patch", "sha256": "3" * 64}],
                         "toolchains": {"test": "fixture-only"}}

    def make_tree(self, arch):
        self.arch = arch
        unix = "aarch64" if arch == "arm64ec" else "x86_64"
        machine = 183 if arch == "arm64ec" else 62
        windows = "aarch64" if arch == "arm64ec" else "x86_64"
        self.unix_dir = self.tree / "lib/wine" / (unix + "-unix")
        self.windows_dir = self.tree / "lib/wine" / (windows + "-windows")
        self.unix_dir.mkdir(parents=True, exist_ok=True)
        self.windows_dir.mkdir(parents=True, exist_ok=True)
        (self.tree / "bin").mkdir(exist_ok=True)
        for name in ["wine", "ntdll.so", "lsteamclient.so"]:
            p = self.unix_dir / name
            p.write_bytes(elf(machine))
            p.chmod(0o755)
        (self.tree / "bin/wine").symlink_to("../lib/wine/" + unix + "-unix/wine")
        server = self.tree / "bin/wineserver"
        server.write_bytes(elf(machine))
        server.chmod(0o755)
        for name in ["ntdll.dll", "lsteamclient.dll", "steam.exe"]:
            (self.windows_dir / name).write_bytes(pe(0xaa64 if arch == "arm64ec" else 0x8664))
        docs = self.tree / "share/doc/quark-gamenative"
        docs.mkdir(parents=True, exist_ok=True)
        for name in ["Wine-LICENSE", "COPYING.LIB"]:
            (docs / name).write_text("Fixture license; not a runnable Wine distribution.\n")
        prefix(self.pack, 0xaa64 if arch == "arm64ec" else 0x8664)

    def validate(self):
        return self.module.validate_tree(self.tree, self.pack, self.arch)

    def package(self, output=None):
        return self.module.package_wcp(self.tree, self.pack, self.arch,
                                       output or self.output, self.metadata)

    def test_validates_both_architectures_and_prefix_identity(self):
        self.assertEqual(self.validate()["prefix_sha256"], hashlib.sha256(self.pack.read_bytes()).hexdigest())
        import shutil
        shutil.rmtree(self.tree)
        self.make_tree("arm64ec")
        self.assertEqual(self.validate()["architecture"], "arm64ec")

    def test_packages_expected_profile_manifest_and_deterministic_payload(self):
        self.package()
        data = subprocess.check_output(["zstd", "-q", "-d", "-c", str(self.output)])
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            names = archive.getnames()
            self.assertIn("bin/wine", names)
            self.assertIn("prefixPack.txz", names)
            self.assertIn("licenses/Quark-LICENSE", names)
            profile = json.load(archive.extractfile("profile.json"))
            self.assertEqual(profile["type"], "Proton")
            # Consumer contract from GameNative's WineInfo.fromIdentifier:
            # branded names miss this pattern and fall back to bundled Wine.
            consumer = re.compile(r"^(wine|proton|Proton)\-([0-9\.]+)(?:\-([0-9\.]+))?\-(x86|x86_64|arm64ec)(?:\-([0-9]+))?$")
            parsed = consumer.fullmatch("proton-" + profile["versionName"])
            self.assertIsNotNone(parsed, "GameNative must select this package instead of falling back")
            self.assertEqual((parsed.group(2), parsed.group(4)), ("11.0", "x86_64"))
            self.assertEqual(profile["versionName"], "11.0-2.901-x86_64")
            self.assertEqual(profile["wine"], {"binPath": "bin", "libPath": "lib", "prefixPack": "prefixPack.txz"})
            manifest = json.load(archive.extractfile("build-manifest.json"))
            self.assertEqual(manifest["wine_revision"], self.metadata["wine_revision"])
            self.assertEqual(manifest["package"]["prefix_sha256"], hashlib.sha256(self.pack.read_bytes()).hexdigest())
        checksum = Path(str(self.output) + ".sha256").read_text().split()[0]
        self.assertEqual(checksum, hashlib.sha256(self.output.read_bytes()).hexdigest())
        second = self.root / "second-test-fixture.wcp"
        self.package(second)
        self.assertEqual(self.output.read_bytes(), second.read_bytes())

    def test_rejects_wrong_elf_or_pe_architecture(self):
        for p, content in [(self.unix_dir / "ntdll.so", elf(183)),
                           (self.windows_dir / "ntdll.dll", pe(0x14c))]:
            old = p.read_bytes()
            with self.subTest(path=p.name):
                p.write_bytes(content)
                with self.assertRaisesRegex(ValueError, "architecture"):
                    self.validate()
                p.write_bytes(old)

    def test_arm64ec_requires_hybrid_metadata_for_amd64_machine_field(self):
        import shutil
        shutil.rmtree(self.tree)
        self.make_tree("arm64ec")
        hybrid = self.tree / "lib/wine/arm64ec-windows/fixture.dll"
        hybrid.parent.mkdir()
        hybrid.write_bytes(hybrid_pe())
        try:
            self.validate()
        except ValueError as exc:
            self.fail(f"Valid ARM64EC hybrid images must be recognized: {exc}")
        hybrid.write_bytes(hybrid_pe(metadata=False))
        with self.assertRaisesRegex(ValueError, "architecture"):
            self.validate()

    def test_native_arm_directory_accepts_its_hybrid_emulation_module(self):
        import shutil
        shutil.rmtree(self.tree)
        self.make_tree("arm64ec")
        module = self.windows_dir / "xtajit64.dll"
        module.write_bytes(hybrid_pe())
        try:
            self.validate()
        except ValueError as exc:
            self.fail(f"ARM64EC emulation module belongs in the native directory: {exc}")
        module.write_bytes(pe(0x8664))
        with self.assertRaisesRegex(ValueError, "architecture"):
            self.validate()

    def test_winmd_metadata_keeps_its_architecture_neutral_i386_header(self):
        metadata = self.tree / "share/wine/winmd/Windows.Foundation.winmd"
        metadata.parent.mkdir(parents=True)
        metadata.write_bytes(pe(0x14c))
        try:
            self.validate()
        except ValueError as exc:
            self.fail(f"WinMD metadata is not a native x64 module: {exc}")

    def test_rejects_small_elf_page_alignment(self):
        p = self.unix_dir / "ntdll.so"
        data = bytearray(p.read_bytes())
        struct.pack_into("<Q", data, 64 + 48, 4096)
        p.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "16 KiB"):
            self.validate()

    def test_rejects_desktop_linux_loader(self):
        p = self.unix_dir / "wine"
        data = bytearray(p.read_bytes())
        data[192:212] = b"/lib64/ld-linux.so.2\0"
        p.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "Android"):
            self.validate()

    def test_rejects_missing_required_files_and_nonexecutable_loader(self):
        loader = self.unix_dir / "wine"
        loader.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "executable"):
            self.validate()
        loader.chmod(0o755)
        (self.windows_dir / "lsteamclient.dll").unlink()
        with self.assertRaisesRegex(ValueError, "required"):
            self.validate()

    def test_rejects_damaged_required_unix_and_windows_modules(self):
        for p in [self.unix_dir / "ntdll.so", self.unix_dir / "lsteamclient.so",
                  self.windows_dir / "ntdll.dll", self.windows_dir / "lsteamclient.dll",
                  self.windows_dir / "steam.exe"]:
            old = p.read_bytes()
            with self.subTest(path=p.name):
                p.write_bytes(b"damaged module")
                with self.assertRaisesRegex(ValueError, "ELF|PE"):
                    self.validate()
                p.write_bytes(old)

    def test_retains_ne_font_resources_outside_pe_module_directories(self):
        font = self.tree / "share/wine/fonts/fixture.fon"
        font.parent.mkdir(parents=True)
        data = bytearray(pe(0x14c))
        data[128:132] = b"NE\0\0"
        font.write_bytes(data)
        try:
            self.validate()
        except ValueError as exc:
            self.fail(f"Native Wine font resources are NE data, not PE modules: {exc}")

    def test_rejects_wrong_empty_and_corrupt_prefix_packs(self):
        prefix(self.pack, 0xaa64)
        with self.assertRaisesRegex(ValueError, "prefix.*architecture"):
            self.validate()
        with tarfile.open(self.pack, "w:xz"):
            pass
        with self.assertRaisesRegex(ValueError, "prefix"):
            self.validate()
        self.pack.write_bytes(b"not an xz archive")
        with self.assertRaisesRegex(ValueError, "prefix"):
            self.validate()

    def test_normalizes_stale_cmod_drive_links_in_verified_prefix(self):
        self.assertTrue(hasattr(self.module, "normalize_prefix"), "Verified prefix normalization is missing")
        links = []
        for letter, target in [("d", "/storage/emulated/0/Download"),
                               ("e", "/data/data/com.winlator.cmod/storage")]:
            member = tarfile.TarInfo(f".wine/dosdevices/{letter}:")
            member.type, member.linkname = tarfile.SYMTYPE, target
            links.append(member)
        prefix(self.pack, 0x8664, links,
               z_target="/data/user/0/com.winlator.cmod/files/imagefs/home/xuser-1/../..")
        normalized = self.root / "normalized.txz"
        digest = hashlib.sha256(self.pack.read_bytes()).hexdigest()
        result = self.module.normalize_prefix(self.pack, normalized, "x86_64", digest)
        self.assertEqual(result["source_sha256"], digest)
        self.module.validate_prefix(normalized, 0x8664)
        with tarfile.open(normalized) as archive:
            self.assertNotIn(".wine/dosdevices/d:", archive.getnames())
            self.assertNotIn(".wine/dosdevices/e:", archive.getnames())
            self.assertEqual(archive.getmember(".wine/dosdevices/z:").linkname, "/")

    def test_prefix_normalization_rejects_unexpected_source_hash(self):
        self.assertTrue(hasattr(self.module, "normalize_prefix"), "Verified prefix normalization is missing")
        output = self.root / "normalized.txz"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.module.normalize_prefix(self.pack, output, "x86_64", "0" * 64)
        self.assertFalse(output.exists())

    def test_prefix_normalization_removes_appledouble_metadata(self):
        metadata = tarfile.TarInfo(".wine/drive_c/windows/system32/._example.dll")
        metadata.size = 249
        with tarfile.open(self.root / "metadata.tar", "w") as archive:
            with tarfile.open(self.pack) as original:
                for member in original:
                    archive.addfile(member, original.extractfile(member) if member.isfile() else None)
            archive.addfile(metadata, io.BytesIO(b"\x00\x05\x16\x07" + b"\0" * 245))
        with (self.root / "metadata.tar").open("rb") as source:
            import lzma
            with lzma.open(self.pack, "wb") as target:
                target.write(source.read())
        normalized = self.root / "normalized.txz"
        try:
            result = self.module.normalize_prefix(self.pack, normalized, "x86_64",
                                                  hashlib.sha256(self.pack.read_bytes()).hexdigest())
        except ValueError as exc:
            self.fail(f"AppleDouble metadata prevents prefix normalization: {exc}")
        self.assertIn(metadata.name, result["removed_metadata_files"])
        with tarfile.open(normalized) as archive:
            self.assertNotIn(metadata.name, archive.getnames())
            self.assertIn(".wine/drive_c/windows/system32/example.dll", archive.getnames())

    def test_prefix_retains_legacy_ne_data_without_treating_it_as_pe(self):
        legacy = bytearray(pe(0x14c))
        legacy[128:132] = b"NE\0\0"
        member = tarfile.TarInfo(".wine/drive_c/windows/rundll.exe")
        member.size = len(legacy)
        prefix(self.pack, 0x8664, [(member, bytes(legacy))])
        try:
            self.validate()
        except ValueError as exc:
            self.fail(f"Legacy NE data must not be mistaken for malformed PE: {exc}")

    def test_rejects_unsafe_prefix_members_and_links(self):
        for name, target in [("../escaped", None), (".wine/escape", "/etc"),
                             (".wine/escape", "../../escape")]:
            m = tarfile.TarInfo(name)
            if target:
                m.type, m.linkname = tarfile.SYMTYPE, target
            with self.subTest(name=name, target=target):
                prefix(self.pack, 0x8664, m)
                with self.assertRaisesRegex(ValueError, "prefix"):
                    self.validate()

    def test_rejects_outer_absolute_broken_and_escaping_symlinks(self):
        link = self.tree / "share/invalid"
        for target in ["/etc/passwd", "missing", "../../prefix.txz"]:
            with self.subTest(target=target):
                link.symlink_to(target)
                with self.assertRaisesRegex(ValueError, "symlink"):
                    self.validate()
                link.unlink()

    def test_rejects_output_overlap_and_metadata_mismatch(self):
        with self.assertRaisesRegex(ValueError, "output"):
            self.package(self.tree / "inside.wcp")
        self.metadata["architecture"] = "arm64ec"
        with self.assertRaisesRegex(ValueError, "metadata"):
            self.package()
        self.assertFalse(self.output.exists())

    def test_rejects_profile_names_gamenative_cannot_parse(self):
        self.metadata["profile_version_name"] = "quark-patched-0.1.0"
        with self.assertRaisesRegex(ValueError, "profile|GameNative"):
            self.package()
        self.assertFalse(self.output.exists())

    def test_compressor_failure_publishes_no_final_package(self):
        with patch.object(self.module.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "zstd")):
            with self.assertRaisesRegex(RuntimeError, "compress"):
                self.package()
        self.assertFalse(self.output.exists())
        self.assertFalse(Path(str(self.output) + ".sha256").exists())


if __name__ == "__main__":
    unittest.main()
