#!/usr/bin/env python3
"""Build orchestration tests use real files and small external tool stand-ins."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import io

REPO = Path(__file__).resolve().parents[1]


class BuildTests(unittest.TestCase):
    def setUp(self):
        path = REPO / "android/build.py"
        self.assertTrue(path.is_file(), "Android build orchestration is missing")
        sys.path.insert(0, str(REPO / "android"))
        self.addCleanup(lambda: sys.path.remove(str(REPO / "android")))
        spec = importlib.util.spec_from_file_location("android_build", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory(prefix="quark build fixture ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_prerequisite_failure_names_missing_tools(self):
        with self.assertRaisesRegex(ValueError, "Missing build tools:.*autoconf"):
            self.module.check_prerequisites("")

    def test_workspace_rejects_unowned_content_without_changes(self):
        work = self.root / "user directory"
        work.mkdir()
        existing = work / "notes.txt"
        existing.write_text("keep this")
        with self.assertRaisesRegex(ValueError, "empty|owned"):
            self.module.claim_workspace(work, "arm64ec")
        self.assertEqual(existing.read_text(), "keep this")
        self.assertEqual(len(list(work.iterdir())), 1)

    def test_workspace_marker_binds_architecture(self):
        work = self.root / "owned build with spaces"
        self.module.claim_workspace(work, "arm64ec")
        self.module.claim_workspace(work, "arm64ec")
        with self.assertRaisesRegex(ValueError, "architecture"):
            self.module.claim_workspace(work, "x86_64")
        with self.assertRaisesRegex(ValueError, "repository|overlap"):
            self.module.claim_workspace(REPO, "arm64ec")

    def test_command_failure_stops_following_stages_and_retains_log(self):
        fake = self.root / "fake tool"
        fake.write_text("#!/bin/sh\nprintf 'expected failure\\n'\nexit 17\n")
        fake.chmod(0o755)
        marker = self.root / "should not exist"
        stages = [("failure", [str(fake)]),
                  ("later", [sys.executable, "-c", "from pathlib import Path; Path('should not exist').touch()"])]
        logs = self.root / "logs"
        with self.assertRaisesRegex(RuntimeError, "failure.*17"):
            self.module.run_stages(stages, self.root, os.environ.copy(), logs)
        self.assertFalse(marker.exists())
        self.assertIn("expected failure", (logs / "failure.log").read_text())

    def test_stage_order_preserves_configure_before_compile(self):
        for arch in ["arm64ec", "x86_64"]:
            stages = self.module.make_stages(arch, Path("/source"), Path("/source/build-scripts/step.sh"))
            names = [name for name, _ in stages]
            expected = ["autogen", "wine-tools"]
            if arch == "arm64ec":
                expected += ["sysvshm"]
            expected += ["ntsync", "configure", "compile", "install"]
            self.assertEqual(names, expected)

    def test_adapter_scopes_home_limits_jobs_and_checks_android_patches(self):
        original = '#!/bin/bash\nexport OUTPUT_DIR="$HOME/compiled-files"\nmake -j$(nproc)\nfor patch in "${PATCHES[@]}"; do\n        git apply ./android/patches/$patch\ndone\n'
        adapted = self.module.adapt_script(original, android_patches=True)
        self.assertNotIn("$HOME", adapted)
        self.assertIn("QUARK_ANDROID_WORKROOT", adapted)
        self.assertIn("QUARK_ANDROID_JOBS", adapted)
        self.assertIn("git apply --check", adapted)
        self.assertIn("git apply --reverse --check", adapted)
        script = self.root / "adapted.sh"
        script.write_text(adapted)
        subprocess.run(["bash", "-n", str(script)], check=True)

    def test_adapter_rejects_unrecognized_patch_loop(self):
        with self.assertRaisesRegex(ValueError, "patch loop"):
            self.module.adapt_script("#!/bin/sh\ntrue\n", android_patches=True)

    def test_host_tools_use_native_64_bit_without_multilib(self):
        original = "#!/bin/sh\nmkdir wine-tools\ncd wine-tools\n../configure --without-x --without-vulkan\nmake -j$(nproc) __tooldeps__ nls/all\n"
        try:
            adapted = self.module.adapt_script(original, host_tools=True)
        except TypeError:
            self.fail("Host tools adapter must select the native 64-bit configuration")
        self.assertIn("../configure --enable-win64 --without-x", adapted)
        self.assertIn("__tooldeps__ nls/all", adapted)

    def test_download_hash_mismatch_never_reuses_cache_file(self):
        cached = self.root / "asset.tar"
        cached.write_bytes(b"unexpected existing file")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.module.download("https://example.invalid/unused", cached, "0" * 64)
        self.assertEqual(cached.read_bytes(), b"unexpected existing file")

    def test_tar_extraction_rejects_traversal_before_writing(self):
        archive = self.root / "unsafe.tar"
        with tarfile.open(archive, "w") as t:
            m = tarfile.TarInfo("../escape")
            m.size = 3
            t.addfile(m, io.BytesIO(b"bad"))
        with self.assertRaisesRegex(ValueError, "archive"):
            self.module.extract_tar(archive, self.root / "extracted")
        self.assertFalse((self.root / "escape").exists())

    def test_termux_absolute_internal_links_are_relocated(self):
        archive = self.root / "termux.tar"
        base = "data/data/com.termux/files/usr"
        with tarfile.open(archive, "w") as t:
            m = tarfile.TarInfo(base + "/lib/target")
            m.size = 3
            t.addfile(m, io.BytesIO(b"lib"))
            link = tarfile.TarInfo(base + "/lib/link")
            link.type, link.linkname = tarfile.SYMTYPE, "/" + base + "/lib/target"
            t.addfile(link)
        dest = self.root / "termux"
        self.module.extract_tar(archive, dest, relocate_termux=True)
        link_path = dest / base / "lib/link"
        self.assertFalse(os.readlink(link_path).startswith("/"))
        self.assertEqual(link_path.read_bytes(), b"lib")


if __name__ == "__main__":
    unittest.main()
