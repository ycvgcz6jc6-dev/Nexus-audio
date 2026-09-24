ARG BUILD_FROM=ghcr.io/home-assistant/base:3.24-2026.08.0
FROM rust:1.92-alpine3.23 AS inferno-builder
RUN apk add --no-cache build-base git pkgconfig openssl-dev alsa-lib-dev linux-headers
ENV RUSTFLAGS="-C target-feature=-crt-static"
WORKDIR /src
ARG INFERNO_REF=9abfab97c609bd8a5a3a4cae21b919972fd44937
RUN git init . && git remote add origin https://github.com/salanki/inferno.git && git fetch --depth 1 origin ${INFERNO_REF} && git checkout FETCH_HEAD && git submodule update --init --recursive && cargo build --locked --release -p inferno2pipe
WORKDIR /librespot
ARG LIBRESPOT_REF=383a6f6969f23b3e3cbc693747101cb9c92463dc
RUN git init . && git remote add origin https://github.com/librespot-org/librespot.git && git fetch --depth 1 origin ${LIBRESPOT_REF} && git checkout FETCH_HEAD && git submodule update --init --recursive && cargo build --locked --release --no-default-features --features "with-libmdns"

FROM ${BUILD_FROM}
ARG BUILD_VERSION=1.0.0-rc3
ARG BUILD_ARCH
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="addon" io.hass.arch="${BUILD_ARCH}" org.opencontainers.image.licenses="Apache-2.0" org.opencontainers.image.source="https://github.com/ycvgcz6jc6-dev/Nexus-audio"
RUN apk add --no-cache python3 py3-pip iproute2 dbus avahi avahi-tools ffmpeg alsa-utils shairport-sync libgcc libssl3 libcrypto3
COPY requirements.txt /tmp/requirements.txt
RUN apk add --no-cache --virtual .python-build build-base python3-dev libffi-dev openssl-dev cargo && pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt && apk del .python-build
COPY --from=inferno-builder /src/target/release/inferno2pipe /usr/local/bin/inferno2pipe
COPY --from=inferno-builder /librespot/target/release/librespot /usr/local/bin/librespot
WORKDIR /app
COPY app /app
COPY licenses /usr/share/licenses/nexus-audio
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /usr/share/licenses/nexus-audio/
COPY run.sh /run.sh
RUN chmod a+x /run.sh /usr/local/bin/inferno2pipe /usr/local/bin/librespot
CMD [ "/run.sh" ]
