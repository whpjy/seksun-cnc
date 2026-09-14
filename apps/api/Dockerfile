FROM debian:12-slim AS camotics-build

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && attempt=0; until apt-get install -y --no-install-recommends --fix-missing \
        ca-certificates build-essential git pkgconf scons python3 libssl-dev zlib1g-dev libbz2-dev \
        libsqlite3-dev libyaml-dev libsnappy-dev libleveldb-dev libmariadb-dev \
        ; do attempt=$((attempt + 1)); [ "$attempt" -ge 8 ] && exit 1; sleep 2; done

ARG CAMOTICS_COMMIT=e84665f2fa9d1151f03282ac7e01320bc65e015b
ARG CBANG_COMMIT=45929e1a2eb30c5f86fa563bd51b29c1b2eec0bd
WORKDIR /src
RUN git clone https://github.com/CauldronDevelopmentLLC/cbang.git \
    && git -C cbang checkout "${CBANG_COMMIT}" \
    && scons -C cbang strict=0
RUN git clone https://github.com/CauldronDevelopmentLLC/CAMotics.git \
    && git -C CAMotics checkout "${CAMOTICS_COMMIT}" \
    && cd CAMotics \
    && CBANG_HOME=/src/cbang scons strict=0 with_gui=0 with_tpl=0 camsim

FROM debian:12-slim

ARG FREECAD_VERSION=1.1.1
ARG FREECAD_APPIMAGE_SHA256=e2006138400b2fa85fa2e160e872d00767eb32964e85075830f7e198a3a876e1

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

# Bootstrap CA certificates, then use the HTTPS mirror with normal verification.
# FreeCAD is installed from the official, checksum-pinned AppImage below.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    sed -i 's|http://deb.debian.org/debian-security|https://deb.debian.org/debian-security|g; s|http://deb.debian.org/debian|https://deb.debian.org/debian|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::https::Verify-Peer=false -o Acquire::https::Verify-Host=false update \
    && apt-get -o Acquire::https::Verify-Peer=false -o Acquire::https::Verify-Host=false install -y --no-install-recommends ca-certificates

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && attempt=0; until apt-get install -y --no-install-recommends --fix-missing \
        build-essential cmake ninja-build libtbb-dev \
        libocct-data-exchange-dev libocct-modeling-algorithms-dev \
        libocct-ocaf-dev libocct-visualization-dev \
        python3 python3-venv python3-numpy curl file libgl1 libegl1 libxkbcommon0 \
        libfontconfig1 libdbus-1-3 libxcb1 libx11-6 libxi6 libxrender1 libxext6; do \
        attempt=$((attempt + 1)); [ "$attempt" -ge 8 ] && exit 1; sleep 2; \
    done \
    && python3 -m venv /opt/venv

RUN curl --fail --location --retry 5 \
        "https://github.com/FreeCAD/FreeCAD/releases/download/${FREECAD_VERSION}/FreeCAD_${FREECAD_VERSION}-Linux-x86_64-py311.AppImage" \
        --output /tmp/freecad.AppImage \
    && echo "${FREECAD_APPIMAGE_SHA256}  /tmp/freecad.AppImage" | sha256sum --check - \
    && chmod +x /tmp/freecad.AppImage \
    && cd /opt \
    && /tmp/freecad.AppImage --appimage-extract >/dev/null \
    && mv squashfs-root freecad \
    && rm /tmp/freecad.AppImage \
    && ln -s /opt/freecad/AppRun /usr/local/bin/FreeCADCmd

RUN ln -sfn /opt/freecad/usr/bin/freecadcmd /usr/local/bin/FreeCADCmd

COPY --from=camotics-build /src/CAMotics/camsim /usr/local/bin/camsim

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /geometry
COPY services/geometry/CMakeLists.txt ./
COPY services/geometry/src ./src
RUN cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --parallel \
    && cmake --install build --prefix /opt/seksun-cnc

WORKDIR /app
COPY apps/api/requirements-dev.txt apps/api/requirements.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY apps/api/app ./app
COPY apps/api/data ./data
COPY apps/api/cam ./cam
COPY apps/api/tools ./tools
COPY apps/api/tests ./tests
RUN useradd --system --uid 10001 --create-home cnc \
    && mkdir -p /var/lib/seksun-cnc/jobs \
    && chown -R cnc:cnc /var/lib/seksun-cnc
USER cnc
EXPOSE 8090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
