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
#
# Auto-reconnect: if the server drops (restart, WiFi blip), the receiver
# waits for the port to come back and resumes by itself. Ctrl-C exits.

set -euo pipefail

PC_IP="${1:?usage: bedcast-receive.sh PC_IP [BUFFER_MS]}"
BUFFER_MS="${2:-500}"
PORT=48100

command -v nc  >/dev/null || { echo "missing: pkg install netcat-openbsd"; exit 1; }
command -v mpv >/dev/null || { echo "missing: pkg install mpv (or mpv-x)"; exit 1; }

trap 'echo; echo "[bedcast] stopped"; exit 0' INT TERM

BUFFER_S=$(awk "BEGIN{print $BUFFER_MS/1000}")

while :; do
  until nc -z -w2 "$PC_IP" "$PORT" 2>/dev/null; do
    sleep 2
  done

  echo "[bedcast] connected to $PC_IP:$PORT (buffer ${BUFFER_MS}ms) — Ctrl-C to stop"

  # -d: don't read our stdin — without it, OpenBSD nc never exits on server EOF
  # (receiver hangs silent after a server stop/restart; found in review 2026-07-20)
  nc -d "$PC_IP" "$PORT" \
    | tail -c +17 \
    | mpv --no-terminal --force-seekable=no \
          --demuxer=rawaudio \
          --demuxer-rawaudio-rate=48000 \
          --demuxer-rawaudio-channels=2 \
          --demuxer-rawaudio-format=s16le \
          --audio-buffer="$BUFFER_S" \
          --cache=yes --cache-secs="$BUFFER_S" \
          - || true

  echo "[bedcast] stream ended — waiting for server..."
  sleep 1
done
