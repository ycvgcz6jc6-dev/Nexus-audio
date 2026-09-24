ARG BUILD_FROM
FROM rust:1-alpine AS inferno-builder
RUN apk add --no-cache musl-dev git pkgconfig openssl-dev alsa-lib-dev linux-headers
WORKDIR /src
# Use the same maintained Inferno fork as spin2dante; build the existing Dante->raw-pipe receiver.
RUN git clone --depth 1 --branch dev https://github.com/salanki/inferno.git . && cargo build --release -p inferno2pipe

WORKDIR /librespot
RUN git clone --depth 1 --branch v0.6.0 https://github.com/librespot-org/librespot.git . && cargo build --release --no-default-features --features "with-libmdns"

ARG BUILD_FROM
FROM $BUILD_FROM
RUN apk add --no-cache python3 py3-pip iproute2 avahi-tools ffmpeg alsa-utils shairport-sync libgcc
RUN pip3 install --no-cache-dir --break-system-packages 'aiosendspin>=9.1,<10'
COPY --from=inferno-builder /src/target/release/inferno2pipe /usr/local/bin/inferno2pipe
COPY --from=inferno-builder /librespot/target/release/librespot /usr/local/bin/librespot
WORKDIR /app
COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh /usr/local/bin/inferno2pipe /usr/local/bin/librespot
CMD [ "/run.sh" ]
