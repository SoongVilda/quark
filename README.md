# quark

Experimental Android packaging support is documented in
[`android/README.md`](android/README.md). It builds Quark-patched GameNative Wine
WCPs; it does not run the desktop Quark launcher on Android.

ATTENTION: Still-in-development software, published for testing. Two games
reach their menus through it as of 2026-10-17; the rest of the list below
has not been run yet.

quark is a Proton replacement launcher for Steam on Linux, in one Rust
binary. It drops in as a Steam compatibility tool, answers the four verbs
Steam speaks to Proton (`waitforexitandrun`, `run`, `getcompatpath`,
`getnativepath`) and launches Windows games on the Wine your distribution
ships, wineserver included. It owns everything around that launch: The
prefix and its registry, DXVK and VKD3D-Proton deployed into it, the
bridge between the game's `steam_api` and the running Steam client, the
environment the game sees, the process group it runs in and, when asked,
an eBPF capture of the whole run.

The thesis is that the launcher is the part of Proton worth owning and the
Wine fork is not. Proton is a Python launcher, a Wine fork carrying
thousands of patches, a container runtime and a bundled set of translation
layers, released together. quark keeps the launcher, written to be read,
and puts seven small patches on top of stock Wine 11.20: One is Proton's
own steamclient trampoline ported to upstream, the rest are a few hundred
lines of ntdll, kernelbase and winepulse. The translation layers come from
their upstream releases. Nothing is vendored, nothing runs in a container,
and the Wine that runs the game is the one `pacman` installed, loaded
through a mount namespace so its patched modules never touch `/usr`.

v0.1.0 restarts the project as that launcher alone. The wineserver
replacement, its display helper and the fork tooling that had grown around
the launcher were removed, and the version number restarted with them.
What remains is verified the way the rest of this tree is verified: A
disposable VM runs the whole install and seven probe programs through the
launcher under a [montauk](https://github.com/wllclngn/montauk) capture before any real game is
touched, and every bug fixed on the way to v0.1.0 was found by reading a
capture, a loader trace or the registry rather than by guessing. The two
that turned a blank screen into a menu are written up under
[Diagnosing a launch](#diagnosing-a-launch).

## Status

Verified on Arch Linux with an RTX 2060 (NVIDIA 615.78.08, Wine 11.20,
kernel 7.2.11), through `tests/iterate.py`:

| Game | Engine | Result |
|---|---|---|
| Hollow Knight | Unity 6 | Reaches its language menu, Steam logged in, D3D11 through DXVK on the GPU |
| Balatro | LÖVE | Reaches its main menu, Steam API live |
| TMNT: Shredder's Revenge | FNA/.NET | Not yet run under v0.1.0 |
| Wedding Witch | Unity | Not yet run under v0.1.0 |
| Halls of Torment | Godot/.NET | Not yet run under v0.1.0 |

The QEMU suite is green: Install, seven probes (Direct3D 11 through DXVK
on lavapipe, audio enumeration, the window and message pump, the Windows
environment and registry, threads and sync objects, the Steam bridge with
no Steam running and a GL plus audio stress) and the unit, gate and deploy
layers, with every montauk capture covering the probe, steam.exe and the
wineserver.

Open:

- Stopping a game from Steam's Stop button does not reach it yet. The
  launcher ignores SIGINT and SIGTERM and its children inherit that; close
  the game itself.
- Anti-cheat is untested. The EasyAntiCheat patches apply and build, but
  the kernelbase module they live in is not deployed yet, and BattlEye has
  nothing yet.
- The two working games were launched through the harness, not yet from
  Steam's own Play button.
- DXVK, VKD3D-Proton and dxvk-nvapi are fetched from each project's latest
  release rather than pinned by hash.

## Components

| Component | Where | Role |
|---|---|---|
| **quark** | `rust/src/quark/` | The launcher. One binary crate, 2,600 lines: `launcher.rs` (the six launch phases), `pe_scanner.rs` (the game's import table, read positionally), `cli.rs` (the four verbs) and `log.rs`. Installed as `compatibilitytools.d/quark/quark` with `proton` linked to it, which is the name Steam execs. |
| **install.py** | repo root | The installer, sixteen steps that each name their inputs, outputs and failure. Builds quark, deploys the system Wine tree beside it, clones the matching Wine source, applies the patches strictly and builds the modules they touch, bakes the prefix template from Proton 11.0, builds the Steam bridge from Proton's source, downloads the translation layers and writes the compatibility-tool manifests. |
| **patches/** | `patches/wine`, `patches/lsteamclient` | Seven Wine patches and two lsteamclient patches, each with a `Subject:` and a rationale header, numbered in order by module. They apply with `git apply --check` and no fuzz, or the install fails naming the patch. |
| **Steam bridge** | built into `lib/wine/` | `steam.exe`, a Wine builtin built from Proton's steam_helper, and `lsteamclient.dll` with `lsteamclient.so`, built from Proton 11.0's lsteamclient against the system Wine's source. The game's `steam_api` reaches the running Steam client through them. |
| **quark_common.py** | repo root | One definition each of the paths, the `[HH:MM:SS] [LEVEL]` logger, Steam's quark-mapped appids and the Wine process hygiene (per-prefix `wineserver -k`, stale-socket detection). `install.py` and every test import it. |
| **tests/** | `tests/` | One runner in layers, the Rust tests beside the source they compile, the deployed-package and patch verification, the game harness and the QEMU suite. |

## How a launch works

Steam runs `proton waitforexitandrun <game.exe> [args]` with the
`STEAM_COMPAT_*` variables set. `launcher.rs` runs six phases, and a
second launch of the same game skips straight to the sixth.

**1. Locate.** The Wine tree (quark's own deployment of the system Wine,
or `QUARK_WINE_DIR`), its wineserver, the Steam install and the prefix
from `STEAM_COMPAT_DATA_PATH`. Every per-launch path derives from these;
nothing is hardcoded to a user or a home.

**2. Prefix.** A new prefix is laid out from the Wine tree's PE modules
(drivers into `system32\drivers`, where `wine.inf` registers every service
binary) and seeded with the registry and profile directories of the baked
template, Proton 11.0's `default_pfx` as `install.py` copied it. The
launcher's own registry stubs and the Steam keys are injected after the
seed, never before, since a `.reg` file that exists is never replaced. The
user the prefix belongs to is read from its registry's `ProfileList`, and
that one value drives `USER`, which is where Wine takes its own user name
from, and every profile path the launcher sets. Without a template,
`wineboot --init` runs as that user. A prefix an older quark split between
two users heals on its next launch, since shell32 re-expands its shell
folders and wineboot rewrites the volatile environment.

**3. Scan and deploy.** `pe_scanner` reads the game executable's import
table with positional reads, never the whole file, and reports its render
API, whether it imports `steam_api`, `nvapi` or `nvngx`. A launcher stub
with no D3D imports (the Unreal pattern) defers to the shipping binary
under `Binaries/Win64`. DXVK (`d3d9`, `d3d10core`, `d3d11`, `dxgi`) and
VKD3D-Proton (`d3d12`, `d3d12core`) are deployed into `system32`
unconditionally, since many games discover D3D12 through DXGI at run time;
nvapi and the DLSS modules only on an NVIDIA box whose game imports them.
A `.quark_deployed` cache holds a hash per component (the Wine tree, DXVK,
VKD3D-Proton, the Steam client files and the scan) and a component is
redeployed only when its hash moved.

**4. Steam client.** Valve's own `steamclient64.dll`, `steamclient.dll`,
`GameOverlayRenderer64.dll` and `Steam.dll` go into the prefix's
`Program Files (x86)\Steam` from Steam's `legacycompat/`, on every launch,
by size and mtime. They have to be Valve's: See
[The Steam bridge](#the-steam-bridge). quark's `steam.exe` and
`lsteamclient.dll` go into `system32`.

**5. Environment.** One definition of `WINEDLLPATH` and `LD_LIBRARY_PATH`
shared by the game and wineboot; `WINEDLLOVERRIDES` built from a base
table (the bridge builtin, the XInput builtins so controllers route
through winebus, the BattlEye and .NET installer stubs) plus `=n` for
every DLL deployed in phase 3; `WINETEMP` and `WINETMP`, the channel ntdll
imports as `TEMP` and `TMP`; the profile variables; `DXVK_LOG_LEVEL` and
`VKD3D_DEBUG` quiet unless asked; the EasyAntiCheat runtime's `v2` lib
dirs appended to `WINEDLLPATH` when the Steam tool is installed; and the
`env_config` file last, so a user's line overrides any of it. Arguments
stay `OsString` from `args_os()` to Wine, so a game path that is not UTF-8
launches rather than panicking.

**6. Launch.** Save data under the profile is snapshotted. The command is
`wine C:\windows\system32\steam.exe <game.exe> [args]`, run inside a
`bwrap` mount namespace where `/usr/lib/wine` and `/usr/share/wine` are
quark's tree, so the unmodified system `wine` binary loads the patched
modules without `/usr` ever being written. The game runs in its own
process group, under the montauk observer when `QUARK_TRACE=1`. When it
exits, the group gets SIGTERM, 500 ms for services.exe and wineboot to
flush their hives, then SIGKILL; the exit status is appended to the game's
stderr log; and any save file that existed before the launch and is gone
after it is restored from the snapshot, never one the game just wrote.

## The Steam bridge

A Windows game talks to Steam through `steam_api64.dll`, which loads
`steamclient64.dll` from the path Steam's registry keys name and calls its
exports. Under Proton those exports are served by `lsteamclient`, a Wine
builtin whose PE side is the Steamworks interface and whose unix side
`dlopen`s Steam's own `linux64/steamclient.so` and talks to the running
client over its IPC. The join between Valve's DLL and the builtin is a
trampoline in ntdll, Proton's patch ported here as `wine/001`: When a
module named `steamclient64` loads, ntdll fixes up its imports (the
`tier0_s64` and `vstdlib_s64` it links against are redirected to ntdll;
`install.py` builds stubs for them as well), skips its DllMain, loads
`lsteamclient.dll` beside it and writes trampolines from every export of
Valve's DLL to the builtin's, flushing the instruction cache after. The
module is pinned so a `FreeLibrary` can never unmap it from under a
cached pointer, and the entries survive its unload for the same reason.

That is why phase 4 deploys Valve's DLL and not the builtin under its
name. The launcher used to copy `lsteamclient.dll` to
`Program Files (x86)\Steam\steamclient64.dll`, reasoning that the real DLL
would fail its imports. With the builtin loading under Valve's name, the
DllMain the trampoline skipped was the bridge's own init: Its unix side
never loaded, no call ever reached Steam and every game sat in
`SteamAPI_Init` with a window open and nothing drawn. The loader trace
showed it in one line each way, `steam.exe` with its `steamclient:DllMain`
and the game without one; the capture's memory maps confirmed it, the
game holding `lsteamclient.dll` and no `lsteamclient.so`.

`steam.exe` is the first process: It creates the Win32 events games check
for a running Steam, calls `steamclient_init_registry` so the registry
holds the client paths, creates the "Steam" window `FindWindow` looks for
and spawns the game as its child. The wineserver's user-process count
drops to zero when the game and steam.exe exit, and the session ends
without a kill.

## Wine patches

`install.py` clones the Wine source matching the installed Wine, applies
the patches strictly and in order, and builds only ntdll, kernelbase and
winepulse from the result. The rest of the tree is the distribution's,
hardlinked. A patch that does not apply, or a module that builds but does
not produce its file, fails the install by name. There is no fuzzy apply:
It once landed a hunk inside a comment, reported success and shipped an
ntdll that broke winepulse, winegstreamer and the bridge at once.

| Patch | Mechanism |
|---|---|
| `wine/001-ntdll-steamclient-trampoline` | Proton's trampoline on upstream Wine 11.20, above. The renamed tier0/vstdlib import passes its new length, the redirect honors `PROTON_DISABLE_LSTEAMCLIENT`, the instruction cache is flushed after the jumps are written and WoW64 callers go through a 32-bit thunk. |
| `wine/002-ntdll-export-wine-unix-call` | Exports `__wine_unix_call` from the PE ntdll, which the EasyAntiCheat bridge DLLs import. |
| `wine/003-ntdll-eac-loadorder` | Builtin-first load order for the EasyAntiCheat bridge DLLs, so the runtime's bridge wins over the game's copy. |
| `wine/004-ntdll-thread-stack-floor` | A floor under small explicit stack reserves of 64-bit threads; WoW64 threads keep the reserve they ask for. |
| `wine/005-kernelbase-eac-launcher-detection` | `CreateProcess` reads the child's version resource and sets `PROTON_EAC_LAUNCHER_PROCESS` only when its ProductName is "EasyAntiCheat Launcher", removing it from every other child. Proton's approach; the game a launcher started had inherited the flag and loaded the native module instead of the bridge. |
| `wine/006-kernelbase-steam-openprocess-pid` | `OpenProcess` on Steam's placeholder PID opens the calling process. |
| `wine/007-winepulse-nonblocking-timing-update` | The timer loop never blocks on a PulseAudio timing update, and keeps at most one in flight, so a stalled server cannot queue one per period. |
| `lsteamclient/001-lsteamclient-link-stdcxx` | Links libstdc++ into the unix library. |
| `lsteamclient/002-lsteamclient-non-fatal-steam-call` | A failed Steam client call returns instead of faulting. Valve's tree forces an access violation there on purpose; this deviates. |

Dropped on the way to v0.1.0, each checked against Wine 11.20, Proton 11.0
and Proton's Wine: Everything written against the deleted wineserver
replacement, the fast-boot and standalone-plugplay pair (which kept
services.exe from starting, so no winedevice, no controllers and no
wineserver shutdown), the view band that 11.20's address-space rework made
moot, and the lsteamclient API-compat patch that Proton 11 fixed upstream.

## Anti-cheat

EasyAntiCheat follows Proton's model: The bridge DLLs come from Valve's
"Proton EasyAntiCheat Runtime" Steam tool, whose `v2/lib32` and
`v2/lib64` join `WINEDLLPATH` after the Wine tree's; patches 002 and 003
give them their export and load order; patch 005 marks the launcher
process. 005 and 006 live in kernelbase, which `install.py` builds but
does not deploy yet, so the launcher detection is not active in v0.1.0.
No EasyAntiCheat title has been run. BattlEye is not wired at all.

VAC needs nothing from quark; the game talks to the real Steam client
through the bridge.

## Requirements

- Linux 6.14 or newer with `/dev/ntsync`. On Arch, `wine` pulls in
  `ntsync-autoload`; until the next boot, `sudo modprobe ntsync`. Wine
  11.20 uses the device on its own when it is present; no variable turns
  it on.
- Wine 11.20 from the distribution. quark deploys that tree and runs its
  wineserver; `install.py` rebuilds only the modules the patches touch,
  from the matching source.
- Steam, with the multilib repo enabled, a 32-bit Vulkan driver for the
  GPU (`lib32-nvidia-utils`, `lib32-vulkan-radeon` or `lib32-vulkan-intel`)
  and `lib32-libpulse` or `lib32-pipewire`.
- Proton 11.0 installed in Steam (Library → Tools). `install.py` takes the
  prefix template from its `default_pfx` and the lsteamclient source tag
  from its version, and stops by name without it.
- `bubblewrap`, for the mount namespace. Without it the launcher execs
  `wine` directly against the unpatched system tree and says so.
- Build tools: `rust` 1.85+, `clang` and `lld` (the tier0/vstdlib stubs
  are built with them; no mingw), `llvm`, `git`, `autoconf`, `bison`,
  `flex`, `base-devel`, `rsync`, and the Wine build dependencies for ntdll,
  kernelbase and winepulse.

On Arch Linux:

```bash
sudo pacman -S --needed steam wine wine-mono bubblewrap rust clang lld llvm \
  git autoconf bison flex base-devel rsync gnutls libxslt libgphoto2 \
  libpulse alsa-lib alsa-plugins openal libxcomposite libxinerama \
  libxrandr libxcursor libxi libxxf86vm libxdamage libxfixes libxkbcommon \
  wayland fontconfig freetype2 sdl2-compat v4l-utils libusb \
  gst-plugins-base gst-plugins-good lib32-vulkan-icd-loader lib32-mesa \
  lib32-libpulse
```

Add the GPU's 32-bit Vulkan driver to that line. `wine-mono` is what .NET
and FNA games run on.

## Install

```bash
./install.py          # asks before anything destructive
./install.py --yes    # answers every prompt
```

The steps, in order, each bounded by a timeout and each failing by name:

| Step | What it does |
|---|---|
| Preflight | The tools above, the kernel version, `/dev/ntsync`, Steam's root |
| Clean | Stops each quark-mapped prefix's wineserver with `wineserver -k` (never a global kill), removes only sockets nothing answers on; `--wipe-prefixes` also removes those prefixes and shader caches |
| Sync wine source | Clones the Wine source at the installed version into `/tmp/quark-wine-build`, with a manifest gate so an unchanged source and patch set skips the rebuild |
| Build | `cargo build --release` |
| Deploy binaries | `quark` and the `proton` link into `compatibilitytools.d/quark/`, the two VDF manifests |
| Deploy Wine | The system Wine tree beside it, hardlinked where possible; `bin/wineserver` is the system wineserver |
| Patch Wine | `git apply --check` then apply, each patch, then configure and build ntdll, kernelbase and winepulse |
| Bake prefix | Proton 11.0's `default_pfx` copied as the template, its relative symlinks repaired to real files |
| Build stubs | `tier0_s64`, `vstdlib_s64` and their 32-bit twins with clang and lld |
| Steam bridge | lsteamclient and steam.exe from Proton 11.0's source, the two patches applied strictly |
| GPU translation | DXVK, VKD3D-Proton and dxvk-nvapi from their GitHub releases |
| Deploy drivers | The PE driver files from Proton or the system Wine |
| EAC runtime | The bridge DLLs from the Proton EasyAntiCheat Runtime tool, if installed |
| Prefix sync | Every prefix already mapped to quark gets real files where Proton left symlinks, then quark's layer on top |
| Finalize | `env_config` written if absent |
| Smoke test | `wine --version` inside the bwrap namespace against the deployed tree |

Then restart Steam fully (Steam → Exit, not the window's close button;
Steam reads `compatibilitytools.d` only at startup), open a game's
Properties → Compatibility, tick "Force the use of a specific Steam Play
compatibility tool" and pick **quark 0.1.0**. Steam fetches the Windows
build of a game it had installed natively.

`./install.py --trace` turns the observer on for every launch: It runs
montauk's own installer, symbolicates the patched modules to Breakpad
`.sym` files and sets `QUARK_TRACE=1` and `QUARK_TRACE_OUT=1` in
`env_config`.

### Configuration

`~/.local/share/Steam/compatibilitytools.d/quark/env_config` holds
`KEY=VALUE` lines read on every launch; a line there overrides the
launcher's own value for the same key, for every game.

| Variable | Effect |
|---|---|
| `QUARK_VERBOSE=1` | Launcher diagnostics as `[DEBUG]` lines, and Wine's `+process,err` channels in `wine_stderr.log` |
| `QUARK_TRACE=1` | Start montauk beside the game; the pattern covers the game, steam.exe and the wineserver |
| `QUARK_TRACE_OUT=1` | With `QUARK_TRACE`, keep the capture as `/tmp/quark/incidents/<game>-io.bin` |
| `QUARK_WINE_DIR=/path` | Launch with another Wine tree than the one install.py deployed |
| `WINEDEBUG=...` | Passed through as given; otherwise `-all`, or `+process,err` under `QUARK_VERBOSE` |

Any of them works for one game in Steam's launch options, as
`QUARK_VERBOSE=1 %command%`.

### Logs

Everything lands in `/tmp/quark/`: `wine_stderr.log` and
`wine_stdout.log` for the last launch (truncated each time), and
`incidents/` for the montauk capture, the process memory maps and a copy
of the game's stderr with its exit status, kept per game when tracing is
on.

## Testing

```bash
python3 tests/run.py                        # every layer
python3 tests/run.py --layer unit           # cargo test (24 tests)
python3 tests/qemu/guest.py provision       # once: the VM image
python3 tests/qemu/guest.py test            # the suite in the VM
python3 tests/iterate.py --appid 2379780 --timeout 60 --trace
python3 tests/iterate.py --appid 367520 --compat-tool proton-exp
```

`tests/run.py` is the one entry point, in layers that each end in a
bounded step: `unit` runs the Rust tests under `tests/rust/`, declared as
`[[test]]` targets that compile the source module they cover (the
launcher against fixture trees: Environment assembly, the deploy cache,
DLL deployment, registry seeding, the profile user, the driver layout,
the Steam client files and save-data restore; the PE scanner against
synthetic PE32+ images); `gate` the Steam-safe process scoping, with no
install; `deploy` the deployed package layout and `verify_patches.py`,
which derives each source check from the patch itself (the longest line
it adds to each file) and checks the binaries for the strings, symbols
and PE exports the patches compile in; `launch` a real game through
`iterate.py`; and `qemu` the suite below. A layer that cannot run reports
SKIP, never PASS.

`tests/qemu/guest.py` runs the whole thing in a disposable Arch guest over
KVM with no host root: The official cloud image, checksum-verified; a
provisioned overlay with Wine 11.20, the toolchain, Xvfb, lavapipe and a
stand-in Steam root whose "Proton" template is a stock `wineboot --init`
prefix built as steamuser; a fresh run overlay per run; and a persistent
build-cache disk so the patched Wine and montauk builds carry across runs.
Inside, `suite.py` installs quark for real, builds montauk from the
sibling tree and gives it its tracing capabilities, then launches seven
mingw probes through the launcher the way Steam would, each writing a
PASS/FAIL verdict file, each under the observer, and analyzes a failed
probe's capture to JSON beside its verdict. The results come back to
`/tmp/quark-suite/qemu/<stamp>/`.

`tests/iterate.py` drives one installed game, or every one, on the host:
Build, install, launch with the Steam environment Steam would set, stderr
milestones (`x11drv`, `XRandR`, `LÖVE`, `STEAM_API`, `CRASH`), checkpoint
screenshots, an optional montauk capture, a verdict and the logs archived
under `/tmp/quark/runs/`. `--compat-tool proton-exp` runs the same game
through stock Proton Experimental, which is the A/B that separates a quark
bug from a driver or a game bug in one run. Note that a Proton run
upgrades the shared prefix to Proton's layout; move it aside first if the
quark prefix matters.

## Diagnosing a launch

The instrument is [montauk](https://github.com/wllclngn/montauk), the
eBPF flight recorder, and the rule is that a capture gets analyzed before
any log gets read. quark's installer does not need it; the observer does.
With `QUARK_TRACE=1` and `QUARK_TRACE_OUT=1` every launch leaves
`/tmp/quark/incidents/<game>-io.bin`, and:

```bash
montauk --analyze /tmp/quark/incidents/<game>-io.bin --json        # the full report set
montauk --analyze <cap> --select ntsync --where comm=<game> --by tid,op,result --count --json
montauk --analyze <cap> --select io --where comm=steam.exe --by syscall_nr --count --json
```

Two examples from the road to v0.1.0, both games showing a window and
nothing in it:

**The split prefix.** Hollow Knight's capture showed no crash, no spin
and every game thread parked; DXVK's own log stopped after device
creation; Unity had never written its `Player.log`. The registry answered:
`Shell Folders` held `AppData`, `Local AppData` and `LocalLow` under
`C:\users\<host user>` and the rest under `C:\users\steamuser`, written
during the run. shell32 expands `%USERPROFILE%` through `GetUserNameW`,
which upstream Wine answers from `$USER`, which the launcher never set; it
had hand-written a dozen `steamuser` paths instead. One source of truth
now: The prefix's `ProfileList` names the user, `USER` carries it to Wine
and every path derives from it.

**The bridge under Valve's name.** With the profile fixed, `Player.log`
ended at "Steam initializing", where Proton's continued with "Steam logged
in". The capture's memory maps had `steam.exe` holding both halves of the
bridge and the game holding only the PE half; the loader trace had
`steamclient:DllMain` for steam.exe and `process_attach START/END` with no
DllMain for the game. Patch 001 skips the DllMain of whatever loads as
`steamclient64.dll`, and the launcher had put the builtin there. Valve's
DLL goes there now, and the next run logged in.

The instrument improved along the way as well: Python f-strings and
`finally` in montauk's static reader, the unknown-field error, `\b` in
the regex face and a multi-name trace that missed a process renamed
before attach were each filed from a quark capture and fixed in montauk.

## Reporting a problem

Run the game once with tracing on, then attach

- `/tmp/quark/wine_stderr.log`,
- `/tmp/quark/incidents/<game>-io.bin`, or the output of
  `montauk --analyze /tmp/quark/incidents/<game>-io.bin --json`,
- the game, the GPU and driver, and `wine --version`.

Whether the same game reaches its menu under Proton Experimental on the
same box (`tests/iterate.py --appid <id> --compat-tool proton-exp`) is the
single most useful extra fact.

## Project structure

```
quark/
├── rust/src/quark/        launcher.rs, pe_scanner.rs, cli.rs, log.rs, main.rs
├── install.py             the installer
├── quark_common.py        paths, logger, Steam discovery, Wine process hygiene
├── patches/wine/          001-007
├── patches/lsteamclient/  001-002
├── tests/
│   ├── run.py             the layered runner
│   ├── rust/              launcher.rs, pe_scanner.rs (the cargo tests)
│   ├── iterate.py         the game harness
│   ├── verify_patches.py  deployed package and patch verification
│   ├── test_package.py    the compat tool layout
│   └── qemu/              guest.py, suite.py, probes/*.c
├── Cargo.toml             the workspace root; the crate is rust/
├── PKGBUILD
└── LICENSE
```

## License

GPL-2.0, see the [`LICENSE`](LICENSE) file.
