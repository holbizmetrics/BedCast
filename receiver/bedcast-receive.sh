#!/data/data/com.termux/files/usr/bin/bash
# BedCast v0 receiver — Termux (Android). No APK needed.
#
# Usage:  ./bedcast-receive.sh PC_IP [BUFFER_MS]
#
# Setup once:  pkg install mpv netcat-openbsd coreutils
#   (some Termux repos name the package "mpv-x")
#
# The 16-byte BEDCAST0 header is stripped (tail -c +17); the rest is
# endless S16LE 48kHz stereo PCM, played by mpv with a jitter buffer.
# Latency is FINE — compensate once in the PC video player (VLC: j/k).

set -euo pipefail

PC_IP="${1:?usage: bedcast-receive.sh PC_IP [BUFFER_MS]}"
BUFFER_MS="${2:-500}"
PORT=48100

command -v nc  >/dev/null || { echo "missing: pkg install netcat-openbsd"; exit 1; }
command -v mpv >/dev/null || { echo "missing: pkg install mpv (or mpv-x)"; exit 1; }

echo "[bedcast] connecting to $PC_IP:$PORT (buffer ${BUFFER_MS}ms) — Ctrl-C to stop"

nc "$PC_IP" "$PORT" \
  | tail -c +17 \
  | mpv --no-terminal --force-seekable=no \
        --demuxer=rawaudio \
        --demuxer-rawaudio-rate=48000 \
        --demuxer-rawaudio-channels=2 \
        --demuxer-rawaudio-format=s16le \
        --audio-buffer=$(awk "BEGIN{print $BUFFER_MS/1000}") \
        --cache=yes --cache-secs=$(awk "BEGIN{print $BUFFER_MS/1000}") \
        -
