#!/usr/bin/env python3
"""
quark package validation.

Verifies the compat tool install.py deploys is structurally correct and can
initialize a Wine prefix.

Usage:
    ./tests/test_package.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quark_common import COMPAT_DIR

BIN_DIR = COMPAT_DIR / "bin"
LIB_WIN = COMPAT_DIR / "lib" / "wine" / "x86_64-windows"
LIB_UNIX = COMPAT_DIR / "lib" / "wine" / "x86_64-unix"
SHARE_DIR = COMPAT_DIR / "share" / "wine"


# TEST INFRASTRUCTURE

class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def ok(self, name: str):
        self.passed += 1
        print(f"  PASS  {name}")

    def fail(self, name: str, reason: str):
        self.failed += 1
        self.errors.append((name, reason))
        print(f"  FAIL  {name}: {reason}")

    def summary(self) -> int:
        total = self.passed + self.failed
        print()
        print(f"  {self.passed}/{total} passed")
        if self.errors:
            print()
            for name, reason in self.errors:
                print(f"  FAIL  {name}: {reason}")
        print()
        return 0 if self.failed == 0 else 1


# STRUCTURAL TESTS

def test_structure(r: TestResult):
    """Verify the package directory layout is what Steam expects."""

    # Top-level files
    for name in ("compatibilitytool.vdf", "toolmanifest.vdf", "proton", "quark"):
        path = COMPAT_DIR / name
        if path.exists():
            r.ok(f"exists: {name}")
        else:
            r.fail(f"exists: {name}", "missing")

    # Launcher is executable ELF binary (not a Python script)
    proton = COMPAT_DIR / "proton"
    if proton.exists() and proton.stat().st_mode & 0o111:
        r.ok("proton is executable")
        with open(proton, "rb") as f:
            magic = f.read(4)
        if magic == b"\x7fELF":
            r.ok("proton is ELF binary")
        else:
            r.fail("proton is ELF binary", f"got magic {magic!r}, expected ELF")

    # bin/: wine is copied from system Wine; wine64 and wineserver are links
    # (to wine and to the system wineserver). ntdll execs the preloader beside
    # its own loader in lib/wine/x86_64-unix, so that is where it must be.
    for rel in ("bin/wine", "bin/wine64", "bin/wineserver",
                "lib/wine/x86_64-unix/wine", "lib/wine/x86_64-unix/wine-preloader"):
        path = COMPAT_DIR / rel
        if path.exists() and path.stat().st_mode & 0o111:
            r.ok(rel)
        else:
            r.fail(rel, "missing or not executable")
    for link, target in (("wine64", "wine"), ("wineserver", "/usr/bin/wineserver")):
        path = BIN_DIR / link
        if path.is_symlink() and os.readlink(path) == target:
            r.ok(f"bin/{link} -> {target}")
        else:
            r.fail(f"bin/{link} -> {target}", "not a link to the expected target")


def test_dlls(r: TestResult):
    """Verify core gaming DLLs are present."""
    critical_dlls = [
        "ntdll.dll", "kernel32.dll", "kernelbase.dll",
        "user32.dll", "gdi32.dll", "win32u.dll",
        "advapi32.dll", "ole32.dll", "rpcrt4.dll",
        "d3d11.dll", "d3d12.dll", "dxgi.dll",
        "dinput8.dll", "xinput1_3.dll",
        "ws2_32.dll", "bcrypt.dll",
        "winevulkan.dll", "opengl32.dll",
        "winmm.dll", "mmdevapi.dll",
        "msvcrt.dll", "ucrtbase.dll",
    ]
    for dll in critical_dlls:
        path = LIB_WIN / dll
        if path.exists():
            r.ok(f"dll: {dll}")
        else:
            r.fail(f"dll: {dll}", "missing")


def test_programs(r: TestResult):
    """Verify essential Wine programs are present."""
    critical_progs = [
        "wineboot.exe", "winedevice.exe", "explorer.exe",
        "services.exe", "rpcss.exe", "plugplay.exe",
        "rundll32.exe", "cmd.exe",
    ]
    for prog in critical_progs:
        path = LIB_WIN / prog
        if path.exists():
            r.ok(f"prog: {prog}")
        else:
            r.fail(f"prog: {prog}", "missing")


def test_drivers(r: TestResult):
    """Verify Unix-side driver .so files are present."""
    critical_drivers = [
        "ntdll.so", "win32u.so", "opengl32.so",
        "winevulkan.so",
    ]
    for drv in critical_drivers:
        path = LIB_UNIX / drv
        if path.exists():
            r.ok(f"driver: {drv}")
        else:
            r.fail(f"driver: {drv}", "missing")


def test_nls(r: TestResult):
    """Verify locale/NLS data is present."""
    nls_dir = SHARE_DIR / "nls"
    if nls_dir.exists():
        nls_count = len(list(nls_dir.glob("*.nls")))
        if nls_count > 0:
            r.ok(f"nls: {nls_count} files")
        else:
            r.fail("nls", "directory exists but empty")
    else:
        r.fail("nls", "share/wine/nls/ missing")


def test_vdf_contents(r: TestResult):
    """Verify VDF files have correct content."""
    compat_vdf = COMPAT_DIR / "compatibilitytool.vdf"
    if compat_vdf.exists():
        text = compat_vdf.read_text()
        if "quark" in text and "display_name" in text:
            r.ok("compatibilitytool.vdf content")
        else:
            r.fail("compatibilitytool.vdf content", "missing expected fields")
    else:
        r.fail("compatibilitytool.vdf content", "file missing")

    manifest_vdf = COMPAT_DIR / "toolmanifest.vdf"
    if manifest_vdf.exists():
        text = manifest_vdf.read_text()
        if "/proton" in text and "commandline" in text:
            r.ok("toolmanifest.vdf content")
        else:
            r.fail("toolmanifest.vdf content", "missing expected fields")
    else:
        r.fail("toolmanifest.vdf content", "file missing")


# PREFIX INIT TEST

def test_prefix_init(r: TestResult):
    """Test that Wine can initialize a fresh prefix using the deployed tree."""
    wine = BIN_DIR / "wine"
    if not wine.exists():
        r.fail("prefix init", "bin/wine not found")
        return

    with tempfile.TemporaryDirectory(prefix="quark_test_") as tmpdir:
        pfx = Path(tmpdir) / "pfx"
        env = dict(os.environ)
        env["WINEPREFIX"] = str(pfx)
        env["WINEDLLPATH"] = str(LIB_WIN)
        env["WINEDEBUG"] = "-all"
        env["PATH"] = f"{BIN_DIR}:{env.get('PATH', '')}"
        env["LD_LIBRARY_PATH"] = f"{COMPAT_DIR / 'lib'}:{env.get('LD_LIBRARY_PATH', '')}"

        try:
            result = subprocess.run(
                [str(wine), "wineboot", "--init"],
                env=env, timeout=60,
                capture_output=True, text=True,
            )
            if (pfx / "system.reg").exists():
                r.ok("prefix init (system.reg created)")
            else:
                r.fail("prefix init", f"system.reg not created. "
                       f"rc={result.returncode} stderr={result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            r.fail("prefix init", "timed out after 60s")
        except Exception as e:
            r.fail("prefix init", str(e))
        finally:
            # The temp prefix's wineserver outlives wineboot; stop it before
            # the directory it serves is removed.
            subprocess.run([str(BIN_DIR / "wineserver"), "-k"], env=env,
                           capture_output=True, timeout=10)


# MAIN

def run_tests() -> int:
    r = TestResult()

    if not COMPAT_DIR.exists():
        print(f"Package not found at {COMPAT_DIR}")
        print("Run: ./install.py")
        return 1

    print()
    print("  quark package tests")
    print()

    test_structure(r)
    test_dlls(r)
    test_programs(r)
    test_drivers(r)
    test_nls(r)
    test_vdf_contents(r)
    test_prefix_init(r)

    return r.summary()
