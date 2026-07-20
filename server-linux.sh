#!/usr/bin/env bash
# BedCast v0 server — Linux (PulseAudio or PipeWire).
#
# Streams what your speakers are playing (the default sink's monitor source)
# as raw S16LE 48kHz stereo PCM over TCP, with the 16-byte BEDCAST0 header.
# Wire-compatible with the Windows server — same receiver works unchanged.
#
# Usage:   ./server-linux.sh [--port N] [--monitor SOURCE_NAME]
# Needs:   pactl + parec (package: pulseaudio-utils; PipeWire boxes: pipewire-pulse
#          provides both), and netcat (any flavor) or ncat.
# Status:  UNTESTED on real hardware as of 2026-07-20 — syntax-checked only.
#          Wire format verified against the Windows server's output. If you run
#          this and it works (or doesn't), please open an issue saying so.

set -euo pipefail

PORT=48100
MONITOR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --port)    PORT="$2"; shift 2 ;;
    --monitor) MONITOR="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1 (try --help)"; exit 1 ;;
  esac
done

command -v parec >/dev/null || { echo "missing parec (install pulseaudio-utils / pipewire-pulse)"; exit 1; }
command -v pactl >/dev/null || { echo "missing pactl (install pulseaudio-utils / pipewire-pulse)"; exit 1; }

# Default monitor = default sink + ".monitor"
if [ -z "$MONITOR" ]; then
  MONITOR="$(pactl get-default-sink).monitor"
fi

# Pick a listener: ncat > OpenBSD nc (-l PORT) > traditional nc (-l -p PORT)
listen_cmd() {
  if command -v ncat >/dev/null; then
    echo "ncat -l $PORT"
  elif nc -h 2>&1 | grep -q -- '-l.*-p'; then
    echo "nc -l -p $PORT"
  else
    echo "nc -l $PORT"
  fi
}

# 16-byte header: "BEDCAST0" + rate u32 LE (48000 = 0x0000BB80) + ch + bits + 2 reserved
emit_header() {
  printf 'BEDCAST0\x80\xbb\x00\x00\x02\x10\x00\x00'
}

echo "[bedcast] monitor source: $MONITOR"
echo "[bedcast] listening on :$PORT — one client at a time, Ctrl-C to stop"

while true; do
  { emit_header
    parec -d "$MONITOR" --format=s16le --rate=48000 --channels=2 --latency-msec=50
  } | $(listen_cmd) || true
  echo "[bedcast] client gone — waiting for next"
  sleep 1
done
