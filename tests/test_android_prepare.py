#!/usr/bin/env python3
"""Source preparation must fail before modifying an unexpected Wine tree."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class PrepareTests(unittest.TestCase):
    def setUp(self):
        module_path = REPO / "android/prepare.py"
        self.assertTrue(module_path.is_file(), "Android source preparation is missing")
        spec = importlib.util.spec_from_file_location("android_prepare", module_path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory(prefix="quark prepare ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "Wine tree with spaces"
        self.source.mkdir()
        self.git("init", "-q")
        (self.source / "alpha").write_text("original\n")
        (self.source / "beta").write_text("unchanged\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.patch = self.root / "a patch.patch"
        self.patch.write_text("--- a/alpha\n+++ b/alpha\n@@ -1 +1 @@\n-original\n+patched\n")
        self.config = {"schema": 1, "wine": {"revision": self.base}, "patches": [
            {"path": self.patch.name, "sha256": hashlib.sha256(self.patch.read_bytes()).hexdigest()}
        ]}

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.source), *args], text=True,
                                       stderr=subprocess.STDOUT)

    def prepare(self):
        return self.module.prepare_source(self.source, self.config, self.root)

    def test_applies_patch_and_records_exact_identity(self):
        result = self.prepare()
        self.assertEqual((self.source / "alpha").read_text(), "patched\n")
        self.assertEqual((self.source / "beta").read_text(), "unchanged\n")
        self.assertEqual(result["wine_revision"], self.base)
        self.assertEqual(result["patches"], self.config["patches"])

    def test_rejects_wrong_revision_before_changes(self):
        self.config["wine"]["revision"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "revision"):
            self.prepare()
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_rejects_dirty_or_untracked_source(self):
        for path in [self.source / "alpha", self.source / "untracked"]:
            with self.subTest(path=path.name):
                path.write_text("user work\n")
                with self.assertRaisesRegex(ValueError, "clean"):
                    self.prepare()
                self.assertEqual(path.read_text(), "user work\n")
                if path.name == "alpha":
                    path.write_text("original\n")
                else:
                    path.unlink()

    def test_repeated_preparation_fails_without_further_changes(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, "clean"):
            self.prepare()
        self.assertEqual((self.source / "alpha").read_text(), "patched\n")

    def test_bad_second_patch_leaves_all_files_unchanged(self):
        bad = self.root / "bad.patch"
        bad.write_text("--- a/beta\n+++ b/beta\n@@ -1 +1 @@\n-not the source\n+bad\n")
        self.config["patches"].append({"path": bad.name,
            "sha256": hashlib.sha256(bad.read_bytes()).hexdigest()})
        with self.assertRaisesRegex(RuntimeError, "patch"):
            self.prepare()
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_rejects_modified_patch_before_changes(self):
        self.patch.write_text(self.patch.read_text() + "extra\n")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.prepare()
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_rejects_patch_outside_repository(self):
        self.config["patches"][0]["path"] = "../escape.patch"
        with self.assertRaisesRegex(ValueError, "patch path"):
            self.prepare()

    def test_rejects_nested_source_directory(self):
        child = self.source / "nested"
        child.mkdir()
        with self.assertRaisesRegex(ValueError, "root"):
            self.module.prepare_source(child, self.config, self.root)

    def test_load_sources_rejects_invalid_revision(self):
        config_file = self.root / "sources.json"
        self.config["wine"]["revision"] = "main"
        config_file.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError, "revision"):
            self.module.load_sources(config_file)


if __name__ == "__main__":
    unittest.main()
