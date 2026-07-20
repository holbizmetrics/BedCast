#!/data/data/com.termux/files/usr/bin/bash
# BedCast receiver launcher — Termux (Android) or any Linux/macOS with bash.
#
# Usage:  ./bedcast-receive.sh PC_IP [BUFFER_MS]
#
# Setup once (Termux):  pkg install python mpv netcat-openbsd coreutils
#   (some Termux repos name the mpv package "mpv-x")
#
# Default: the PROVEN v0 pipe (nc | mpv). The v1 timestamped receiver is
# opt-in via BEDCAST_V1=1 — its control loop was field-tested 2026-07-20 and
# found UNSTABLE on real sinks (fill/drop oscillation -> audible chop);
# redesign queued (buffer-depth control). Re-flip the default only after that.
#
# Auto-reconnect: if the server drops (restart, WiFi blip), the receiver
# waits for the port to come back and resumes by itself. Ctrl-C exits.

set -euo pipefail

PC_IP="${1:?usage: bedcast-receive.sh PC_IP [BUFFER_MS]}"
BUFFER_MS="${2:-300}"
PORT=48100
HERE="$(cd "$(dirname "$0")" && pwd)"

command -v mpv >/dev/null || { echo "missing: pkg install mpv (or mpv-x)"; exit 1; }
command -v nc  >/dev/null || { echo "missing: pkg install netcat-openbsd"; exit 1; }

trap 'echo; echo "[bedcast] stopped"; exit 0' INT TERM

BUFFER_S=$(awk "BEGIN{print $BUFFER_MS/1000}")

while :; do
  until nc -z -w2 "$PC_IP" "$PORT" 2>/dev/null; do
    sleep 2
  done

  if [ "${BEDCAST_V1:-0}" = "1" ] && command -v python >/dev/null && [ -f "$HERE/bedcast_receive.py" ]; then
    echo "[bedcast] v1 receiver (timestamped), target latency ${BUFFER_MS}ms — Ctrl-C to stop"
    python "$HERE/bedcast_receive.py" "$PC_IP" --port "$PORT" --buffer-ms "$BUFFER_MS" || true
  else
    echo "[bedcast] v0 receiver (stable default; sync drifts on restarts) — Ctrl-C to stop"
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
  fi

  echo "[bedcast] stream ended — waiting for server..."
  sleep 1
done
