# Experimental GameNative Wine packages

This builds Android Wine with the applicable Quark patches and packages it as a
GameNative `.wcp`. **It does not port or run Quark's Rust launcher.** GameNative
continues to manage launching, prefixes, graphics, and its Android Steam bridge.

The package name starts with `quark-patched-0.1.0-proton-11.0-2` and ends with
`arm64ec` or `x86_64`. These are experimental builds; successful compilation and
archive validation do not establish game compatibility or performance gains.

## What is included

| Change | Decision |
| --- | --- |
| Quark's 64-bit explicit thread-stack reserve floor | Apply `patches/wine/004-ntdll-thread-stack-floor.patch`. |
| Quark's non-fatal Steam client call behavior | Apply the Android rebase under `patches/gamenative/`, preserving GameNative's call diagnostics. This deliberately differs from Proton's forced fault/assertion. |
| Steam trampoline, EAC handling, Steam placeholder PID | Keep GameNative/Proton's existing implementations. |
| PulseAudio timing patch | Do not apply: this base already uses a different callback-driven period timer. |
| GNU libstdc++ link patch | Do not apply: the Android build uses its own C++ runtime/toolchain. |

All source revisions, selected patch hashes, and toolchain archive hashes are in
[`sources.json`](sources.json). The Wine base is GameNative's
`b2e449b22ba46b9ceb3a51623253cd9693ccc409`, not Quark's desktop Wine 11.20.

The official ARM prefix archive contains old CMOD drive paths and macOS
AppleDouble metadata. After verifying its source hash, the build removes D:/E:
links and AppleDouble files and sets the usual Z: mapping to `/`. It retains
registry data, real PE modules, and legacy NE resources. Both the original and
normalized prefix hashes are recorded in the manifest.

## Build through GitHub Actions

Push your reviewed branch to your own fork, open **Actions**, choose
**Build experimental GameNative WCPs**, then select **Run workflow**. The manual
workflow builds ARM64EC and x86_64 independently. It uploads WCPs, SHA-256 files,
source/build manifests, and build logs; it does not publish a GitHub release.

The workflow does not require Android hardware. Device testing is separate.

## Build locally

Use an x86_64 Linux build host. Ubuntu 24.04 with Python 3.12 is the reference
environment. Allow about 20 GB of free disk for one active build and its cache;
retaining both architecture build trees requires more. Four jobs is the default.

On Ubuntu, install the host prerequisites:

```sh
sudo apt-get update
sudo apt-get install -y build-essential git autoconf automake libtool \
  bison flex m4 pkg-config gettext ccache unzip zstd python3 \
  libfreetype6-dev libpng-dev zlib1g-dev
rustup toolchain install 1.90.0 --profile minimal
rustup target add --toolchain 1.90.0 aarch64-linux-android x86_64-linux-android
```

Commit your Quark changes first: the build requires an exact, clean source
revision for its manifest. Build workspaces and output directories must be
outside this checkout. Run one build at a time when sharing a cache:

```sh
python3 android/build.py --arch arm64ec \
  --work-dir /tmp/quark-arm64ec-build \
  --cache-dir /tmp/quark-android-cache \
  --output-dir /tmp/quark-wcp-output --jobs 4

python3 android/build.py --arch x86_64 \
  --work-dir /tmp/quark-x86_64-build \
  --cache-dir /tmp/quark-android-cache \
  --output-dir /tmp/quark-wcp-output --jobs 4
```

The driver downloads and verifies Android NDK r27d, LLVM MinGW 20250920, the
fixed Termux dependency release, and the matching prefix pack. It fetches exact
Wine/ntsync commits, prepares the source, and calls the pinned GameNative build
scripts. It relocates their build paths into the owned workspace and limits make
jobs; it does not change your actual `HOME`. The scripts' architecture/compiler
options remain GameNative's for Android. The native host-tools configure adds
`--enable-win64`, so the Linux build host does not need 32-bit development libraries.

An existing workspace must have this build's ownership marker and architecture.
A partially built workspace is kept for diagnosis; use a **new empty work
directory** for a new invocation and reuse the cache. The driver does not clean
or reset arbitrary existing source trees. Stage logs are under `WORK_DIR/logs`.
Unknown patch failures, missing outputs, and invalid package inputs stop the
build. An already-present Android patch is accepted only after an exact reverse
application check and is recorded explicitly.

The resulting WCP is a zstd tar containing `bin/`, `lib/`, `share/`,
`prefixPack.txz`, `profile.json`, `build-manifest.json`, and Quark's license notice.
Wine's license notices are under `share/doc/quark-gamenative/`. Package checks
cover ELF/PE architecture, the Android executable interpreter, 16 KiB ELF load
alignment, required Wine/Steam bridge files, executable modes, prefix structure,
and archive links. Compression must succeed before the final WCP is published.

For an already installed, independently built tree, the packager is also usable
directly. Supply the normalized prefix and the completed build's metadata:

```sh
python3 android/package.py --arch arm64ec --tree /path/to/installed-wine \
  --prefix-pack /path/to/normalized-prefixPack.txz \
  --metadata /path/to/build-arm64ec.json --output /tmp/experimental-arm64ec.wcp
```

## Verify and test on Android

```sh
cd /tmp/quark-wcp-output
sha256sum -c quark-patched-0.1.0-proton-11.0-2-arm64ec.wcp.sha256
tar --zstd -tf quark-patched-0.1.0-proton-11.0-2-arm64ec.wcp
```

Import the matching `.wcp` through GameNative's Wine/Proton manager. Create a
fresh container for that architecture: ARM64EC with the appropriate native/FEX
setup, or x86_64 with Box64. Existing prefixes may contain files from another
Wine version, so use a disposable test container first.

Test a basic Windows program, then a game with graphics/audio/input, then the
Steam bridge where enabled. Record the device model, Android/GameNative version,
container architecture, game, package checksum, and relevant logs. These are
pending device tests until actually performed. This build makes no claim of
working anti-cheat.

Run the repository checks with:

```sh
python3 tests/run.py --layer android --layer gate
python3 tests/run.py --continue
```

Tests use temporary non-runnable fixtures to exercise package validation; those
fixtures are never build artifacts. Desktop deployment/game/QEMU tests skip when
their required installations are absent. Consult the supplied validation report
for which real builds and runtime checks have actually completed.

## Review a delivered Git archive

The source tar archive includes `.git`; extract it and use normal Git commands.
A separate bundle provides the same complete branch history:

```sh
git clone -b build/gamenative-wcp quark-gamenative.bundle quark-review
cd quark-review
git log --oneline --graph --all
git diff origin/main...HEAD
```

Add your own fork as a remote and push the feature branch when you are ready to
open the upstream PR. The packaging work does not push or publish on your behalf.
