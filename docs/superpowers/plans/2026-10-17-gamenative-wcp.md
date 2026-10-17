# GameNative WCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Build experimental Android Wine WCPs carrying applicable Quark patches,
and deliver reviewable commits with a complete repository/history archive.

**Architecture:** Keep Android build preparation and packaging separate from the
desktop launcher. Reuse pinned GameNative build scripts, apply an explicit Quark
patch selection, and validate the installed Android tree before packaging.

**Tech Stack:** Python 3 standard library, Git, Bash, tar/zstd, Android NDK r27d,
LLVM MinGW 20250920, Rust for ntsync-android, and GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-17-gamenative-wcp-design.md`

## Global Constraints

- Quark base: `41387f302a4276042e2f7f883d74251c5cb4647b`.
- GameNative Wine base: `b2e449b22ba46b9ceb3a51623253cd9693ccc409`.
- Build targets: `arm64ec` and `x86_64`; Android SDK 28; 16 KiB ELF page alignment.
- Apply Wine 004 and an Android rebase of lsteamclient 002, strictly and without fuzz.
- Retain GameNative's existing Android/Proton audio, Steam and EAC implementations.
- Do not change `install.py`, the desktop launcher, or the user's home directory.
- Call the outputs experimental Quark-patched GameNative Wine, not full Quark.
- Separate actual compilation/device results from fixture or metadata tests.
- Deliver a checkout archive including `.git` and a verified standalone Git bundle.

## Review Focus

- A wrong or dirty source checkout must fail before applying patches or compiling.
- Paths containing spaces must work; a build must not delete unrelated directories.
- A prefix pack for another architecture, or an empty/truncated prefix pack, must fail.
- Absolute/broken/escaping symlinks and traversal archive names must not enter a WCP.
- Failed subprocesses must stop the build and leave no apparently successful final WCP.

---

### Task 1: Pin inputs and prepare the patched Android source

**Files:** Create `android/sources.json`, `android/prepare.py`,
`patches/gamenative/002-lsteamclient-non-fatal-steam-call.patch`, and
`tests/test_android_prepare.py`.

**Interfaces:**
- `load_sources(path: Path) -> dict` loads the checked-in source pins.
- `prepare_source(source_dir: Path, config: dict, repo_root: Path) -> dict`
  verifies the checkout and applies the declared patches; returns patch identities.
- CLI: `python3 android/prepare.py --source DIR` prepares an existing pinned tree.

- [x] Write tests using disposable real Git repositories: wrong revision and
  dirty trees fail without changing files; a valid two-file patch applies once;
  a broken patch fails; the command works with spaces in checkout/patch paths.
- [x] Run `python3 -m unittest discover -s tests -p test_android_prepare.py -v`;
  verify failures reflect missing preparation behavior.
- [x] Pin Wine as above, ntsync-android at
  `7ce6435e5979b1cb5341aa4b299f31e8937fe121`, and prefix files at
  `6286ac456e4a8376e053ce1ce4f565bd37cc4af4`. Pin the Termux release
  `build-20260218` and its published SHA-256 digests for each architecture.
- [x] Implement preparation with argument-list subprocess calls, checked exits,
  source revision checks, and `git apply --check` before application. Reject
  partial or repeated preparation clearly rather than silently skipping patches.
- [x] Rebase only lsteamclient 002 onto the pinned GameNative sources, preserving
  its CALLDIAG timing code. Keep the desktop patch unchanged.
- [x] Run tests; then apply both real selected patches to the complete pinned
  source. Inspect the resulting diff and confirm the documented exclusions in
  the real source. Commit as `feat: prepare pinned GameNative Wine with Quark patches`.

### Task 2: Validate and package installed Android Wine trees

**Files:** Create `android/package.py` and `tests/test_android_package.py`.

**Interfaces:**
- `validate_tree(tree: Path, prefix_pack: Path, arch: str) -> dict`
  verifies the Wine tree and matching prefix pack and returns their identities.
- `package_wcp(tree: Path, prefix_pack: Path, arch: str, output: Path,
  metadata: dict) -> Path` validates inputs, writes the profile/build manifest,
  and atomically publishes a zstd WCP plus its checksum.
- CLI: `python3 android/package.py --tree DIR --prefix-pack FILE --arch ARCH
  --metadata FILE --output FILE.wcp`.

- [x] Write tests for a valid architecture-specific fixture, wrong ELF/PE
  architecture, missing Wine files, missing executable permissions, empty or
  corrupt prefix archives, unsafe members, symlink escape, paths with spaces,
  output/source overlap, and compressor failure. Name fixture outputs as tests.
- [x] Run `python3 -m unittest discover -s tests -p test_android_package.py -v`
  and observe the missing behavior fail.
- [x] Implement required Wine loader/server/ntdll/Steam bridge checks and matching
  prefix validation using the actual pinned prefix archive layout. Check relevant
  ELF and PE headers and ELF segment alignment. Reject unsafe links/members.
- [x] Generate a root-level GameNative Proton `profile.json`, `prefixPack.txz`,
  `bin`, `lib`, `share`, licenses, and build manifest. Use a distinct experimental
  version, relative Wine paths, deterministic tar metadata, and SHA-256 identities.
- [x] Write into a temporary output and rename only after compression succeeds;
  ensure failed builds cannot masquerade as final packages.
- [x] Run the tests and inspect archive members/profile by decompression.
  Commit as `feat: validate and package experimental GameNative WCPs`.

### Task 3: Build driver and manual CI workflow

**Files:** Create `android/build.py`, `.github/workflows/build-gamenative.yml`,
and `tests/test_android_build.py`; modify `tests/run.py` and `.gitignore`.

**Interfaces:**
- CLI: `python3 android/build.py --arch ARCH --work-dir DIR --output-dir DIR`.
- The driver uses Task 1's configuration/preparation and Task 2's packager.
- Build metadata records Quark/Wine/dependency revisions, patch hashes, resolved
  toolchain identities, architecture, prefix hash, and compilation status.

- [x] Write tests for prerequisite errors, safe workspace selection, checked
  subprocess failure, and the build-stage ordering. Use a fake executable only
  at the external tool boundary; test filesystem effects with real temp paths.
- [x] Run `python3 -m unittest discover -s tests -p test_android_build.py -v`
  and observe the intended failures.
- [x] Implement a dedicated, ownership-marked build workspace. Keep GameNative's
  scripts scoped to that workspace, including their fixed `$HOME`-relative output
  paths; preserve the user's actual home. Reuse the existing toolchain setup
  rather than copying architecture build logic.
- [x] Use checked Bash execution for autogen, host Wine tools, ARM64 sysvshm,
  ntsync, configure/Android patch application, compile, and install. Audit the
  upstream scripts' patch loops before choosing fail-fast handling. Require the
  expected outputs rather than treating printed warnings as successful stages.
- [x] Add a manual two-architecture CI matrix with necessary toolchains, pinned
  inputs, caches, bounded jobs/timeouts, and artifact uploads. Do not publish a
  release or write to external repositories.
- [x] Add an `android` test layer and ignore only generated Android build outputs.
  Run focused tests and validate workflow/shell syntax. Commit as
  `ci: build experimental GameNative WCP artifacts`.

### Task 4: Real build attempt, documentation, and history export

**Files:** Create `android/README.md`; update the root `README.md`. Keep build
outputs, final archives, and a validation/handoff report outside the source tree.

**Interfaces:** Commands from Tasks 1-3; `git bundle create FILE --all`;
repository tar archive including `.git`.

- [ ] Document patch decisions, dependencies, local/CI commands, package identity,
  fresh-container installation, pending Android smoke tests, and bundle restore.
- [ ] Obtain the declared toolchains and attempt the real cross-builds one target
  at a time within available disk/memory. Retain stage logs and record exact
  failures if the environment prevents completion; never hand off a fixture WCP.
- [ ] Run `python3 tests/run.py --continue`, obtaining Cargo if possible. Report
  each failure and skipped deployment/device suite by name. Validate actual
  package contents/checksums separately for every compiled architecture.
- [ ] Review the full diff against the spec and fix concrete defects. Run fresh
  checks after fixes. Device tests remain explicitly pending without a device.
- [ ] Commit documentation and actual validation results. Verify a clean tree,
  `git diff --check`, and `git fsck --full`.
- [ ] Create the source tar archive including `.git` and a full-history bundle.
  Clone the bundle into a new directory, verify the feature branch tip/history
  and tree, and hash all handoff files. Provide direct download links and concise
  instructions for reviewing the diff and opening the user's own upstream PR.

## Execution choice

Recommended: native execution in this session. Preparation, compilation, and
packaging share source identities and build state, so a single implementer can
validate their interfaces without repeated context handoffs. The current checkout
is a dedicated clean checkout on `build/gamenative-wcp`.

Status: approved by the user; native execution selected. Tasks 1–3 complete;
real build verification and history export are in progress.
