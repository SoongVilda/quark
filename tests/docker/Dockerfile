# quark autonomy container: build -> install -> launch -> analyze, isolated
# from the host's Steam session. Pattern follows a builder/runner image
# split: arch base to match the CachyOS
# host toolchain, capabilities via docker run (never --privileged).
#
# The Steam CLIENT is not in here on purpose: tests/iterate.py launches
# `proton run <exe>` directly with a replicated Steam-compat environment.
# Games render on lavapipe (Mesa software Vulkan) under Xvfb — slow but
# boots DXVK titles for diagnostic windows, and removes every NVIDIA
# headless-container failure mode.
FROM archlinux:base-devel

# multilib for the lib32 runtime Wine loads at runtime (32-bit support in
# quark itself is a known gap; these are for the 64-bit wow64 fringes).
# The docker base image ships a pacman.conf WITHOUT the commented multilib
# block, so append the section rather than un-commenting it.
# Mirror discipline: the base image ships only the pkgbuild.com CDN
# mirrors, and pacman's low-speed abort fails the whole transaction when
# they stall. A deeper mirrorlist lets the per-file mirror failover work.
# (XferCommand is NOT an option: ParallelDownloads bypasses it.)
RUN printf '%s\n' \
       'Server = https://mirror.leaseweb.net/archlinux/$repo/os/$arch' \
       'Server = https://mirrors.kernel.org/archlinux/$repo/os/$arch' \
       'Server = https://mirror.rackspace.com/archlinux/$repo/os/$arch' \
       'Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch' \
       'Server = https://fastly.mirror.pkgbuild.com/$repo/os/$arch' \
       > /etc/pacman.d/mirrorlist \
    && printf '[multilib]\nInclude = /etc/pacman.d/mirrorlist\n' >> /etc/pacman.conf \
    && sed -i 's/^#ParallelDownloads.*/ParallelDownloads = 8/' /etc/pacman.conf \
    && pacman -Syu --noconfirm \
    && pacman -S --noconfirm --needed \
       # quark build
       rustup git python python-pip \
       # system wine: the substrate quark's dynamic port patches (host runs
       # wine 11.x + wine-mono; the port is rebuilt against whatever is here)
       wine wine-mono \
       # wine dynamic-port build deps (install.py compiles ntdll et al)
       mingw-w64-gcc bison flex gnutls libxslt libgphoto2 \
       libpulse alsa-lib alsa-plugins openal \
       libxcomposite libxinerama libxrandr libxcursor libxi libxxf86vm \
       libxdamage libxfixes libxkbcommon wayland mesa vulkan-icd-loader \
       fontconfig freetype2 sdl2-compat v4l-utils libusb gst-plugins-base \
       gst-plugins-good gst-plugin-pipewire \
       # software Vulkan + GL (DXVK + OpenGL titles without a GPU)
       vulkan-swrast vulkan-tools lib32-mesa lib32-vulkan-swrast \
       lib32-vulkan-icd-loader \
       # headless display + audio sink
       xorg-server-xvfb xorg-xauth pulseaudio pulseaudio-alsa \
       # montauk build + trace decode tooling
       cmake clang llvm lld libbpf bpf linux-api-headers libcap liburing \
       # harness conveniences
       rsync procps-ng \
    && pacman -Scc --noconfirm

# Match the host user so bind mounts keep their ownership. uid/gid and the
# username are build args so the image follows whoever builds it.
ARG HOST_USER=mod
ARG HOST_UID=1000
ARG HOST_GID=1000
RUN groupadd -g ${HOST_GID} ${HOST_USER} \
    && useradd -m -u ${HOST_UID} -g ${HOST_GID} -s /bin/bash ${HOST_USER} \
    # Pre-create the named-volume mountpoints with user ownership: docker
    # initializes a FRESH named volume from the image path's ownership;
    # without these the volumes come up root-owned and the user can't write.
    && mkdir -p /home/${HOST_USER}/.local/share/Steam /tmp/quark-wine-build \
    && chown -R ${HOST_UID}:${HOST_GID} /home/${HOST_USER}/.local /tmp/quark-wine-build

USER ${HOST_USER}
RUN rustup default stable

# Mount points (run.py provides every one of these):
#   quark repo, montauk repo  -> same absolute paths as the host so no
#                                path translation exists anywhere
#   steamapps/common          -> ro bind of the host's installed games
#   steam scratch             -> rw container-private Steam tree (prefix
#                                COPIES, compatibilitytools.d, shadercache)
#   wine-build cache          -> /tmp/quark-wine-build (persisted volume,
#                                mirrors a ~/.cache build cache)
#   /out                      -> results (incidents, traces, verdicts)
ENV USER=${HOST_USER}
WORKDIR /home/${HOST_USER}
ENTRYPOINT ["python3", "/entrypoint.py"]
