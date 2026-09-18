#!/bin/bash
# Hentikan worker HLS live (via pidfile; fallback pkill berpola anti-self-match)
for cam in depan belakang belakang2 depan2; do
  pidf="/tmp/cctv-live-$cam.pid"
  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then
    pid=$(cat "$pidf")
    if kill "$pid"; then echo "$cam: dihentikan (pid $pid)"; else echo "$cam: gagal hentikan"; fi
    rm -f "$pidf"
  elif pkill -f "[r]ecordings/live/$cam/"; then
    echo "$cam: dihentikan"
  else
    echo "$cam: tidak jalan"
  fi
done
