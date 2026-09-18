#!/bin/bash
# Hentikan worker arsip MP4 (via pidfile; fallback pkill berpola anti-self-match)
for cam in depan belakang belakang2 depan2; do
  pidf="/tmp/cctv-arch-$cam.pid"
  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then
    pid=$(cat "$pidf")
    if kill "$pid"; then echo "$cam: arsip dihentikan (pid $pid)"; else echo "$cam: gagal hentikan"; fi
    rm -f "$pidf"
  elif pkill -f "[r]ecordings/archive/$cam/"; then
    echo "$cam: arsip dihentikan"
  else
    echo "$cam: arsip tidak jalan"
  fi
done
