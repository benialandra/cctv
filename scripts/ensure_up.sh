#!/bin/bash
# Watchdog: pastikan dashboard + worker live + arsip selalu jalan.
# Dipasang di cron tiap 5 menit. Diam bila semua sehat, mencatat bila menstart.
export PATH=/usr/local/bin:/usr/bin:/bin
ROOT=/data/cctv
LOG=/tmp/ensure_up.log

up_dash() { curl -m 5 -s -o /dev/null http://127.0.0.1:8080/; }
alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

{
echo "--- $(date '+%F %T') ---"
if ! up_dash; then
  python3 -c "import subprocess; subprocess.Popen(['python3','$ROOT/app.py','--port','8080'],stdout=open('/tmp/dash.log','w'),stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True)"
  echo "dashboard: distart ulang"
  sleep 4
fi
if ! alive /tmp/cctv-live-depan.pid; then
  rm -f /tmp/cctv-live-depan.pid
  bash "$ROOT/scripts/hls_start.sh" | sed 's/^/live: /'
fi
if ! alive /tmp/cctv-arch-depan.pid; then
  rm -f /tmp/cctv-arch-depan.pid
  bash "$ROOT/scripts/archive_start.sh" | sed 's/^/arsip: /'
fi
} >>"$LOG" 2>&1
