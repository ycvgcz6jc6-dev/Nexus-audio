#!/usr/bin/with-contenv bashio
set -e
# Shairport Sync's Alpine build uses Avahi over the container's system bus.
mkdir -p /run/dbus
dbus-daemon --system --fork
avahi-daemon --daemonize --no-chroot
exec python3 /app/main.py
