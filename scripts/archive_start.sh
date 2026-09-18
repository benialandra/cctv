#!/bin/bash
# Worker arsip — rekam MP4 per 10 menit per kanal DARI HLS LOKAL (tanpa sesi RTSP baru,
# karena DVR hanya mengizinkan 3 sesi RTSP yang sudah dipakai worker live).
# Butuh dashboard :8080 jalan. Pakai: ./scripts/archive_start.sh | Henti: ./scripts/archive_stop.sh
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCH="$ROOT/recordings/archive"
ENVWORKERS="$(python3 -c "import re; d = open('$ROOT/.env', encoding='utf-8').read(); m = re.search(r'^WORKERS=(.*)$', d, re.M); print(m.group(1) if m else '')")"
CAMS="${WORKERS:-${ENVWORKERS:-depan belakang belakang2}}"

for cam in $CAMS; do
  out="$ARCH/$cam"
  mkdir -p "$out"
  pidf="/tmp/cctv-arch-$cam.pid"
  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then
    echo "$cam: arsip sudah jalan (pid $(cat "$pidf"))"
    continue
  fi
  rm -f "$pidf"
  nohup ffmpeg -hide_banner -loglevel warning -nostdin \
    -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 30 \
    -i "http://127.0.0.1:8080/hls/$cam/index.m3u8" \
    -c copy -reset_timestamps 1 \
    -f segment -segment_time 600 -strftime 1 \
    "$out/%Y-%m-%d_%H-%M.mp4" >"/tmp/archive_$cam.log" 2>&1 &
  echo $! > "/tmp/cctv-arch-$cam.pid"
  echo "$cam: arsip pid $!"
done
