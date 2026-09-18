#!/bin/bash
# HLS live workers — 1 ffmpeg per kanal: RTSP substream -> H.264 (cap 720p @1.5M) + AAC -> HLS fmp4
# Pakai:  ./scripts/hls_start.sh     Henti:  ./scripts/hls_stop.sh
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIVE="$ROOT/recordings/live"
mkdir -p "$LIVE"

# Parse .env CAMS (format: CAMS=nama=url|nama=url...) TANPA source (nilai mengandung & dan |)
eval "$(python3 - "$ROOT/.env" <<'PYEOF'
import re, shlex, sys
d = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"^CAMS=(.*)$", d, re.M)
cams = []
if m:
    for row in m.group(1).split("|"):
        if "=" in row:
            n, u = row.split("=", 1)
            u = re.sub(r"stream=\d", "stream=1", u)  # paksa substream (hemat CPU/bandwidth)
            cams.append(n)
            print("CAMURL_%s=%s" % (n, shlex.quote(u)))
print("CAMLIST=%s" % shlex.quote(" ".join(cams)))
mw = re.search(r"^WORKERS=(.*)$", d, re.M)
workers = [w for w in mw.group(1).split() if w in cams] if mw else list(cams)
print("ENVWORKERS=%s" % shlex.quote(" ".join(workers)))
PYEOF
)"

for cam in ${WORKERS:-${ENVWORKERS:-$CAMLIST}}; do
  var="CAMURL_$cam"
  url="${!var}"
  out="$LIVE/$cam"
  mkdir -p "$out"
  pidf="/tmp/cctv-live-$cam.pid"
  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then
    echo "$cam: sudah jalan (pid $(cat "$pidf"))"
    continue
  fi
  rm -f "$pidf"
  nohup ffmpeg -hide_banner -loglevel warning -nostdin -rtsp_transport tcp \
    -i "$url" \
    -vf "scale='min(1280,iw)':-2,fps=25" \
    -c:v libx264 -preset veryfast -tune zerolatency -b:v 1500k -maxrate 1500k -bufsize 3000k -g 50 -keyint_min 50 \
    -c:a aac -b:a 64k -ac 1 \
    -f hls -hls_time 2 -hls_list_size 10 -hls_flags delete_segments+independent_segments \
    -hls_segment_type fmp4 -hls_allow_cache 0 \
    -hls_segment_filename "$out/seg_%05d.m4s" \
    "$out/index.m3u8" >"/tmp/hls_$cam.log" 2>&1 &
  echo $! > "/tmp/cctv-live-$cam.pid"
  echo "$cam: worker pid $!"
done
