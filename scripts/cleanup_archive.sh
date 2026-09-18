#!/bin/bash
# Hapus arsip MP4 lebih tua dari 7 hari agar penyimpanan tidak membengkak.
# Aman: hanya *.mp4 di recordings/archive/; TIDAK menyentuh live/, config, .env.
# Pakai: ./scripts/cleanup_archive.sh [--dry-run] [--days N]
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCH="$ROOT/recordings/archive"
DAYS=7
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1; shift;;
    --days) DAYS="${2:?butuh angka}"; shift 2;;
    *) echo "pakai: $0 [--dry-run] [--days N]"; exit 1;;
  esac
done
LOG="/tmp/cleanup_archive.log"
LOCK="/tmp/cleanup_archive.lock"

[ -d "$ARCH" ] || { echo "$(date '+%F %T') ARCH tidak ada: $ARCH" | tee -a "$LOG"; exit 0; }

if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date '+%F %T') masih jalan, lewati" | tee -a "$LOG"; exit 0
fi
trap 'rmdir "$LOCK"' EXIT

if [ "$DRY" -eq 1 ]; then
  echo "=== DRY-RUN: *.mp4 lebih tua dari $DAYS hari yang AKAN dihapus ==="
  find "$ARCH" -type f -name "*.mp4" -mtime "+$DAYS" -printf "%T+ %s byte %p\n" | sort
  echo "=== akhir dry-run (tidak ada yang dihapus) ==="
  exit 0
fi

BEFORE=$(du -sb "$ARCH" | cut -f1)
COUNT=0; FREED=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
  if rm -f "$f"; then COUNT=$((COUNT+1)); FREED=$((FREED+sz)); fi
done < <(find "$ARCH" -type f -name "*.mp4" -mtime "+$DAYS" -print)
find "$ARCH" -mindepth 1 -type d -empty -delete
AFTER=$(du -sb "$ARCH" | cut -f1)
echo "$(date '+%F %T') hapus=$COUNT dibebaskan=${FREED}B sisa=${AFTER}B" | tee -a "$LOG"
