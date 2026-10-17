# Experimental GameNative WCP build

Status: approved by the user on 2026-10-17; native implementation is in progress.

## Goal and interpretation

Produce an experimental, importable GameNative `.wcp` and reviewable changes in
the Quark repository. Deliver a repository archive containing `.git` and a
self-contained Git bundle with the upstream ancestry and every new commit.
The user will review the result and may open a PR to `wllclngn/quark`.

This implements the recommended first option discussed in chat: an Android Wine
build carrying applicable Quark patches. It does not run the Quark launcher or
claim full Quark support. A full launcher port would also require changing
GameNative's launch flow and adapting Quark's desktop Steam, prefix, graphics,
and mount-namespace assumptions. That is a separate project.

The device and GameNative version have been requested. Until supplied, build
both supported package architectures: `arm64ec` (native ARM64 Wine plus FEX) and
`x86_64` (Box64). Runtime compatibility is established by device tests, not by
successful archive creation.

## Source findings

- Quark base: `41387f302a4276042e2f7f883d74251c5cb4647b`.
- GameNative Wine base: `b2e449b22ba46b9ceb3a51623253cd9693ccc409`
  from `GameNative/proton-wine`, branch `proton_11.0-2`.
- GameNative's existing Android workflow uses Android NDK r27d, SDK 28,
  16 KiB page alignment, Termux dependency trees, LLVM MinGW 20250920, and
  `GameNative/ntsync-android`. Its architecture-specific build scripts already
  compile and install the Android Wine trees.
- A GameNative Proton WCP is a zstd-compressed tar containing `bin`, `lib`,
  `share`, `prefixPack.txz`, and `profile.json` at its root. The profile names
  the Wine binary/library paths and prefix pack. It has no Quark launcher hook.
- The Quark checkout was imported through the connected GitHub API after shell
  networking failed. Every blob, tree, and the original commit was checked
  against its upstream Git object ID; `git fsck --full` passes. It is not a
  shallow repository.

## Patch selection

Use a fixed, reviewed patch selection rather than applying all Quark patches
and ignoring failures. The GameNative base is Proton-derived and contains
changes Quark otherwise ports onto upstream Wine.

| Quark change | Android build decision |
| --- | --- |
| Wine 004: 64-bit explicit thread-stack floor | Apply the existing patch strictly. `git apply --check` succeeds on the pinned GameNative source, including its ARM64EC stack setup. |
| lsteamclient 002: non-fatal Steam client call | Add an Android-specific rebased patch preserving GameNative's existing call diagnostics. Remove the intentional fault and assertion, matching Quark's existing behavior. |
| lsteamclient 001: link GNU libstdc++ | Exclude. The Android build has its own C++ toolchain/runtime; do not import a desktop GNU runtime assumption. |
| Wine 007: non-blocking PulseAudio timing update | Exclude. GameNative already has a different callback-driven timer and a no-op `pulse_timer_loop`; the blocking loop targeted by this patch is absent. |
| Wine 001-003, 005-006: Steam trampoline and EAC/Steam compatibility | Retain GameNative/Proton's implementations. Audit the pinned source and document the overlap; do not replace its ARM64EC or Steam bridge code with upstream-Wine ports. |

The build must fail if its expected base revision or patch applicability changes.
It must not silently skip a failed patch, use fuzzy application, or label an
unchanged GameNative release as a new Quark build. The package description and
documentation will identify it as an experimental Quark-patched GameNative Wine.

## Architecture and repository changes

1. Add an Android build configuration recording the pinned Wine base and
   dependency source revisions. Keep Android work separate from `install.py`
   and the desktop Rust launcher.
2. Add a small build driver that checks prerequisites, obtains the pinned
   GameNative source in a dedicated build directory, applies the declared
   patches, and calls GameNative's existing build scripts. Never clean an
   arbitrary user-supplied directory or rewrite the user's home directory.
3. Add a WCP packager that takes an installed Android Wine tree and the matching
   prefix pack, validates them, writes the GameNative profile and build manifest,
   and creates the final archive. Use a distinct experimental version name.
4. Add a manually triggered GitHub Actions build for both architectures. It
   reuses GameNative's documented toolchain/dependency setup, builds the patched
   source, and uploads WCPs, SHA-256 checksums, and manifests as artifacts.
   It does not publish a release automatically.
5. Add focused Python tests for patch preparation, input validation, archive
   layout, architecture mismatch rejection, and failure propagation; integrate
   them into Quark's existing test runner.
6. Document build commands, installation, the exact patch selection, known
   limitations, device smoke tests, and how to restore the Git bundle.

Avoid vendoring the GameNative Wine repository or duplicating its full build
scripts. Record exact source revisions, the Quark revision, applied patch hashes,
toolchain versions, architecture, prefix-pack identity, and output checksums in
build metadata. Retain the applicable licenses and source/build information.

## Validation and completion criteria

- Run the existing Steam-safety tests before and after changes. The baseline's
  four tests currently pass. Cargo is absent in this environment; obtain the
  required tools before claiming Rust tests were run.
- Test patch application against the real pinned source, including the rebased
  lsteamclient patch. Verify the expected Android source behavior for exclusions.
- Run focused tests and inspect the produced tar/profile. Validate actual
  ELF/PE architectures, required Wine files, executable permissions, safe archive
  paths, and the matching prefix pack. A fixture package only tests packaging;
  it is not a usable deliverable.
- Attempt real cross-compilation and create the two WCPs where the environment
  permits. Report missing toolchains, network failures, or compile errors
  explicitly. A workflow that has not run is not evidence of a working build.
- On a device, import into GameNative, create a fresh matching container, launch
  a basic Windows program, then test a game and the Steam bridge where enabled.
  Report these tests as pending until run on Android; do not claim improved
  performance or working anti-cheat from desktop tests or package inspection.
- Commit coherent changes on a feature branch based on the original upstream
  commit. Verify a clean working tree and `git fsck --full`.
- Export a tar archive containing the checkout and `.git`, plus a standalone
  bundle made with `git bundle create ... --all`. Verify the bundle by cloning
  it into a new directory and checking the branch tip, commit history, and tree.
  Include a short handoff with validation results and any remaining blockers.

## Review boundary

The user approved this design and the implementation plan and selected native
execution. Implementation refinements and actual verification results are recorded
in the final validation report. Android device testing remains a separate step.
