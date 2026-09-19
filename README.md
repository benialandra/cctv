# CCTV Rumah — Live HLS + Arsip MP4

Dashboard CCTV ringan tanpa dependensi Python: 1 kamera (mudah ditambah), live HLS di browser, putar ulang arsip MP4 per 10 menit, retensi otomatis 7 hari.

![Live](docs/live-1.jpg) ![Live](docs/live-2.jpg) ![Arsip](docs/arsip.jpg)

## Fitur

- **Live** — RTSP substream → H.264 720p HLS (segmen 2 detik), putar di browser via hls.js lokal (tanpa CDN)
- **Putar ulang** — daftar arsip + pemutar di dashboard, bisa seek
- **Arsip otomatis** — MP4 per 10 menit via `-c copy` (tanpa sesi RTSP tambahan, CPU ~nol)
- **Hemat sesi DVR** — arsip membaca HLS lokal, bukan RTSP langsung
- **Watchdog + cleanup** — cron tiap 5 menit memastikan semua jalan; file >7 hari dihapus otomatis
- **Panel lipat**, metrik server (popup), salin URL RTSP untuk VLC

## Struktur

```
app.py                 dashboard stdlib (:8080) — tanpa pip install
config.json            hasil scan + template RTSP (tanpa password)
templates/dashboard.html  UI Tailwind (1 frame + list rekaman)
static/hls.min.js      player HLS lokal
scripts/               hls_start/stop, archive_start/stop, cleanup_archive, ensure_up
recordings/live|archive  HLS + MP4 (tidak di-commit)
```

## Jalan cepat

```bash
cp .env.example .env   # isi kredensial DVR (JANGAN commit .env!)
./scripts/hls_start.sh      # worker live
./scripts/archive_start.sh  # worker arsip
python3 app.py --port 8080  # dashboard
```

Opsional (cron): `*/5 * * * * /data/cctv/scripts/ensure_up.sh` dan
`0 3 * * * /data/cctv/scripts/cleanup_archive.sh`.

Prasyarat: `ffmpeg` + `python3`. Kredensial hanya dibaca dari `.env` (mode 600) di sisi server.

## Catatan

- DVR ini membatasi **3 sesi RTSP bersamaan** — konfigurasi default memakai 1 kanal (`WORKERS=depan`, `SHOW=depan` di `.env`).
- Akses jarak jauh contoh memakai Cloudflare quick tunnel ke `:8080`.
