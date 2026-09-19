#!/usr/bin/env python3
"""Dashboard CCTV — stdlib-only (ThreadingHTTPServer + hls.js CDN di klien).

Route:
  GET /                       dashboard (grid autoplay CH1-4)
  GET /hls/<cam>/...          file HLS live (dari recordings/live/<cam>)
  GET /api/status             JSON status per kamera + disk (poll 3 detik)
  GET /api/archive            daftar arsip MP4 per kamera
  GET /api/rtsp?cam=&stream=  URL RTSP untuk VLC (nilai segar dari .env)
  GET+POST /api/ptz           kontrol PTZ ONVIF (move/stop/home/focus)
  GET /api/archive?date=&hour=&cam=&q=&limit=  daftar arsip + filter tanggal
  GET /api/log?limit=         50 aktivitas terakhir (terbaru dulu)
  POST /api/log               catat aktivitas {event,cam,detail}
  GET /api/metrics            metrik server (cpu/mem/disk/suhu/uptime/worker)
  GET /api/motion?limit=&since=  notifikasi sensor gerak + status aktif
  POST /api/snapshot          jepret frame {cam} -> JPG (ffmpeg sekali jalan)
  GET+POST /api/record        rekam manual {cam,action=start|stop} / status
  GET /api/manual             daftar file manual (klip + snapshot) per kamera
  GET /snaps/<cam>/<file>     unduh JPG snapshot
  GET /manual/<cam>/<file>    unduh/putar MP4 rekaman manual
  GET /dvr                    iframe web UI DVR

Tidak ada dep eksternal. Jalankan:  python3 app.py --port 8080
Kredensial hanya dibaca dari .env (mode 600) di sisi server; TIDAK ditulis
ke HTML/JS sehingga tidak bocor ke browser.
"""

import argparse, base64, json, os, re, subprocess, threading, time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from functools import partial

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(ROOT, "config.json")
ENVF = os.path.join(ROOT, ".env")
LIVE = os.path.join(ROOT, "recordings", "live")
ARCH = os.path.join(ROOT, "recordings", "archive")
LOGF = os.path.join(ROOT, "nas", "logs", "activity.log")
MOTF = os.path.join(ROOT, "nas", "logs", "motion.log")
MANDIR = os.path.join(ROOT, "recordings", "manual")  # klip rekaman manual
SNAPDIR = os.path.join(ROOT, "recordings", "snaps")  # JPG snapshot
START_TS = time.time()  # waktu start dashboard (untuk metrik uptime)

# status rekaman manual: {cam: {"proc": Popen, "file": nama, "started": ts}}
RECORDINGS = {}
RECORDINGS_LOCK = threading.Lock()

# status monitor gerak (diisi thread latar; dibaca /api/motion)
MOTION = {
    "ok": False,
    "err": "belum jalan",
    "last_poll": None,
    "active": {},
    "lock": threading.Lock(),
}


def _read_first(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except OSError:
        return ""


def server_metrics():
    """Metrik ringan dari /proc + /sys (tanpa dep eksternal)."""
    m = {"cpu_count": os.cpu_count() or 0, "app_uptime": round(time.time() - START_TS)}
    try:
        parts = _read_first("/proc/loadavg").split()
        m["load1"], m["load5"], m["load15"] = (
            float(parts[0]),
            float(parts[1]),
            float(parts[2]),
        )
    except (IndexError, ValueError):
        m["load1"] = m["load5"] = m["load15"] = None
    try:
        up = float(_read_first("/proc/uptime").split()[0])
        m["uptime"] = round(up)
    except (IndexError, ValueError):
        m["uptime"] = None
    mem = {}
    for ln in _read_first("/proc/meminfo").splitlines():
        k, _, v = ln.partition(":")
        num = v.strip().split()
        if num and num[0].isdigit():
            mem[k.strip()] = int(num[0]) * 1024  # kB -> byte
    if "MemTotal" in mem:
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        used = mem["MemTotal"] - avail
        m["mem"] = {
            "total": mem["MemTotal"],
            "used": used,
            "pct": round(100 * used / mem["MemTotal"], 1),
        }
    else:
        m["mem"] = None
    try:
        m["disk"] = disk_usage(LIVE if os.path.isdir(LIVE) else ROOT)
    except OSError:
        m["disk"] = None
    # suhu: coba thermal_zone lalu hwmon (nilai miliderajat -> °C)
    temp = None
    try:
        zones = sorted(os.listdir("/sys/class/thermal"))
    except OSError:
        zones = []
    for z in zones:
        if z.startswith("thermal_zone"):
            try:
                t = int(_read_first("/sys/class/thermal/%s/temp" % z))
                if t > 0:
                    temp = max(temp or 0, t / 1000.0)
            except ValueError:
                pass
    if temp is None:
        try:
            hwmons = sorted(os.listdir("/sys/class/hwmon"))
        except OSError:
            hwmons = []
        for h in hwmons:
            for n in ("temp1_input", "temp2_input"):
                try:
                    t = int(_read_first("/sys/class/hwmon/%s/%s" % (h, n)))
                    if t > 0:
                        temp = max(temp or 0, t / 1000.0)
                except ValueError:
                    pass
    m["temp_c"] = round(temp, 1) if temp else None
    # worker HLS per kamera (pidfile, atau playlist live masih segar) + jml ffmpeg
    workers, nffmpeg = {}, 0
    for cam in CAMS:
        alive = False
        try:
            pid = int(_read_first("/tmp/cctv-live-%s.pid" % cam))
            os.kill(pid, 0)
            alive = True
        except (ValueError, OSError):
            pass
        if not alive:  # pidfile bisa hilang (mis. /tmp dibersihkan) -> cek playlist
            try:
                m3 = os.path.join(LIVE, cam, "index.m3u8")
                alive = os.path.isfile(m3) and (time.time() - os.stat(m3).st_mtime) < 20
            except OSError:
                pass
        workers[cam] = alive
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    if b"ffmpeg" in f.read().split(b"\0")[0:1]:
                        nffmpeg += 1
            except OSError:
                pass
    except OSError:
        pass
    m["workers"], m["ffmpeg_procs"] = workers, nffmpeg
    return m


def write_log(event, detail="", cam="", ip=""):
    """Catat aktivitas ke nas/logs/activity.log (JSON lines, max ~2000 baris)."""
    try:
        os.makedirs(os.path.dirname(LOGF), exist_ok=True)
        row = {
            "ts": round(time.time()),
            "ip": ip,
            "event": event,
            "cam": cam,
            "detail": detail,
        }
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        # potong bila terlalu besar (>500KB): simpan 2000 baris terakhir
        try:
            if os.path.getsize(LOGF) > 512 * 1024:
                lines = open(LOGF, encoding="utf-8").read().splitlines()[-2000:]
                open(LOGF, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        except OSError:
            pass
    except OSError:
        pass


def read_log(limit=50):
    try:
        lines = open(LOGF, encoding="utf-8").read().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    out.reverse()  # terbaru dulu
    return out


def write_motion(kind, cam, topic):
    """Catat kejadian gerak/tamper ke nas/logs/motion.log (max ~500 baris)."""
    try:
        os.makedirs(os.path.dirname(MOTF), exist_ok=True)
        row = {"ts": round(time.time()), "kind": kind, "cam": cam, "topic": topic}
        with open(MOTF, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        try:
            if os.path.getsize(MOTF) > 128 * 1024:
                lines = open(MOTF, encoding="utf-8").read().splitlines()[-500:]
                open(MOTF, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        except OSError:
            pass
    except OSError:
        pass


def read_motion(limit=20):
    try:
        lines = open(MOTF, encoding="utf-8").read().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    out.reverse()  # terbaru dulu
    return out


def _safe_media_name(prefix, ext):
    return "%s_%s.%s" % (prefix, time.strftime("%Y-%m-%d_%H-%M-%S"), ext)


def manual_index(limit=50):
    """Daftar klip manual + snapshot per kamera. Tidak campur arsip otomatis."""
    out = {}
    for cam in CAMS:
        clips, snaps = [], []
        d = os.path.join(MANDIR, cam)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d), reverse=True):
                if f.endswith(".mp4") and not f.startswith(".") and "/" not in f:
                    try:
                        clips.append(
                            {"f": f, "size": os.path.getsize(os.path.join(d, f))}
                        )
                    except OSError:
                        pass
                if len(clips) >= limit:
                    break
        d = os.path.join(SNAPDIR, cam)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d), reverse=True):
                if f.endswith(".jpg") and not f.startswith(".") and "/" not in f:
                    try:
                        snaps.append(
                            {"f": f, "size": os.path.getsize(os.path.join(d, f))}
                        )
                    except OSError:
                        pass
                if len(snaps) >= limit:
                    break
        out[cam] = {"clips": clips, "snaps": snaps}
    return out


def take_snapshot(cam):
    """Jepret 1 frame via ffmpeg sekali-jalan (substream, cepat)."""
    if cam not in CAMS:
        raise ValueError("kamera tidak dikenal")
    url = build_url(cam, 1) or CAMS[cam]
    d = os.path.join(SNAPDIR, cam)
    os.makedirs(d, exist_ok=True)
    fn = _safe_media_name("snap", "jpg")
    p = os.path.join(d, fn)
    r = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-rtsp_transport",
            "tcp",
            "-i",
            url,
            "-frames:v",
            "1",
            "-q:v",
            "3",
            p,
        ],
        timeout=25,
        capture_output=True,
    )
    if r.returncode != 0 or not os.path.isfile(p) or os.path.getsize(p) == 0:
        try:
            os.remove(p)
        except OSError:
            pass
        raise RuntimeError("ffmpeg gagal menjepret (DVR sibuk?)")
    return fn, os.path.getsize(p)


def record_status():
    out = {}
    now = time.time()
    with RECORDINGS_LOCK:
        for cam in CAMS:
            s = RECORDINGS.get(cam)
            if s and s["proc"].poll() is None:
                out[cam] = {
                    "recording": True,
                    "file": s["file"],
                    "elapsed": round(now - s["started"]),
                }
            else:
                out[cam] = {"recording": False}
    return out


def record_start(cam):
    """Mulai rekam manual (RTSP main, copy tanpa encode)."""
    if cam not in CAMS:
        raise ValueError("kamera tidak dikenal")
    with RECORDINGS_LOCK:
        s = RECORDINGS.get(cam)
        if s and s["proc"].poll() is None:
            raise RuntimeError("sudah merekam")
        d = os.path.join(MANDIR, cam)
        os.makedirs(d, exist_ok=True)
        fn = _safe_media_name("manual", "mp4")
        p = os.path.join(d, fn)
        log = open("/tmp/rec_%s.log" % cam, "ab")
        proc = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-nostdin",
                "-rtsp_transport",
                "tcp",
                "-i",
                CAMS[cam],
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-ac",
                "1",
                "-f",
                "mp4",
                "-movflags",
                "+faststart",
                p,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        RECORDINGS[cam] = {"proc": proc, "file": fn, "started": time.time()}
        return fn


def record_stop(cam):
    """Hentikan rekaman manual (SIGTERM agar MP4 finalisasi rapi)."""
    with RECORDINGS_LOCK:
        s = RECORDINGS.get(cam)
        if not s or s["proc"].poll() is not None:
            RECORDINGS.pop(cam, None)
            raise RuntimeError("tidak sedang merekam")
        proc, fn, started = s["proc"], s["file"], s["started"]
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    dur = round(time.time() - started)
    p = os.path.join(MANDIR, cam, fn)
    with RECORDINGS_LOCK:
        RECORDINGS.pop(cam, None)
    try:
        size = os.path.getsize(p)
    except OSError:
        size = 0
    if size == 0:
        try:
            os.remove(p)
        except OSError:
            pass
        raise RuntimeError("rekaman gagal (file kosong)")
    return fn, dur, size


def cleanup_stray_manual():
    """Bunuh sisa ffmpeg rekaman manual dari proses lama (file tak final)."""
    me = os.getpid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                cl = f.read().decode("utf-8", errors="ignore")
        except OSError:
            continue
        if "ffmpeg" in cl and "recordings/manual" in cl:
            try:
                os.kill(int(pid), 15)
            except OSError:
                pass


def _motion_parse(xml):
    """Ekstrak (topic, source, state, propop) dari respons PullMessages."""
    evs = []
    for blk in re.findall(
        r"<(?:\w+:)?NotificationMessage>(.*?)</(?:\w+:)?NotificationMessage>", xml, re.S
    ):
        mt = re.search(r"<(?:\w+:)?Topic[^>]*>(.*?)</(?:\w+:)?Topic>", blk, re.S)
        ms = re.search(r'Name="Source" Value="([^"]+)"', blk)
        md = re.search(r'Name="State" Value="([^"]+)"', blk)
        mp = re.search(r'PropertyOperation="([^"]+)"', blk)
        if mt and md:
            evs.append(
                {
                    "topic": re.sub(r"\s+", "", mt.group(1)),
                    "source": ms.group(1) if ms else "",
                    "state": md.group(1).lower() == "true",
                    "prop": mp.group(1) if mp else "",
                }
            )
    return evs


def motion_loop():
    """Thread latar: langganan event ONVIF DVR, catat gerak (long-poll ringan)."""
    backoff = 10
    while True:
        try:
            host, port, user, pwd = _onvif_auth()
            base = "http://%s:%d" % (host, port)
            # 1) buat subscription pull-point (5 menit)
            sub = onvif_soap(
                "event_service",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
                '<CreatePullPointSubscription xmlns="http://www.onvif.org/ver10/events/wsdl">'
                "<InitialTerminationTime>PT5M</InitialTerminationTime>"
                "</CreatePullPointSubscription></s:Body></s:Envelope>",
                timeout=10,
            )
            mm = re.search(r"<(?:\w+:)?Address>(http[^<]+)</(?:\w+:)?Address>", sub)
            pull_url = mm.group(1).strip() if mm else base + "/onvif/event_service"
            with MOTION["lock"]:
                MOTION.update(ok=True, err="")
            backoff = 10
            last_true = {}  # source -> ts true terakhir (anti-spam)
            t_end = time.time() + 240  # perbarui subscription tiap 4 mnt
            while time.time() < t_end:
                try:
                    xml = onvif_soap(
                        "event_service",
                        '<?xml version="1.0" encoding="UTF-8"?>'
                        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
                        '<PullMessages xmlns="http://www.onvif.org/ver10/events/wsdl">'
                        "<Timeout>PT15S</Timeout><MessageLimit>50</MessageLimit>"
                        "</PullMessages></s:Body></s:Envelope>",
                        timeout=30,
                        url_override=pull_url,
                    )
                except Exception:
                    break  # subscription basi -> buat baru
                with MOTION["lock"]:
                    MOTION["last_poll"] = round(time.time())
                cam0 = next(iter(CAMS), "")
                for ev in _motion_parse(xml):
                    if "Motion" in ev["topic"]:
                        kind = "motion"
                    elif "Tamper" in ev["topic"]:
                        kind = "tamper"
                    else:
                        continue
                    with MOTION["lock"]:
                        st = MOTION["active"].get(cam0, {})
                        if ev["state"]:
                            MOTION["active"][cam0] = {
                                "active": True,
                                "ts": round(time.time()),
                                "kind": kind,
                            }
                        elif st.get("kind") == kind:
                            MOTION["active"][cam0] = {
                                "active": False,
                                "ts": round(time.time()),
                                "kind": kind,
                            }
                    # notifikasi hanya saat transisi ke true (atau heartbeat >60 dtk)
                    if ev["state"] and ev["prop"] != "Initialized":
                        now = time.time()
                        if now - last_true.get(ev["source"], 0) > 60:
                            last_true[ev["source"]] = now
                            write_motion(kind, cam0, ev["topic"])
                            write_log(kind, "sensor %s" % kind, cam0, "dvr")
        except Exception as e:
            with MOTION["lock"]:
                MOTION.update(ok=False, err=str(e)[:120])
        time.sleep(backoff)
        backoff = min(backoff * 2, 120)


def load_cams():
    env = {}
    for line in open(ENVF, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    cams = {}
    for row in env.get("CAMS", "").split("|"):
        if "=" not in row:
            continue
        nama, url = row.split("=", 1)
        cams[nama] = url
    # SHOW di .env membatasi kanal yang ditampilkan (kosong = semua)
    show = [s for s in env.get("SHOW", "").split() if s]
    if show:
        cams = {n: cams[n] for n in show if n in cams}
    return env, cams


ENV, CAMS = load_cams()


def rtsp_url(nama, stream=0):
    """RTSP lengkap untuk kamera `nama` (stream 0=main, 1=sub).
    Kanal & host diambil dari URL template kanal pertama .env,
    channel diubah sesuai index kamera."""
    url = CAMS.get(nama)
    if not url:
        return None
    # jika query tidak menyatakan channel, sematkan sesuai indeks
    return url


def cam_index(nama):
    return list(CAMS).index(nama) + 1


def ffprobe():
    for p in ("/usr/bin/ffprobe", "/usr/local/bin/ffprobe"):
        if os.path.exists(p):
            return p
    return "ffprobe"


def cam_meta(nama):
    """baca playlist live -> codec/res/bitrate (tanpa probing RTSP tiap detik)."""
    m3 = os.path.join(LIVE, nama, "index.m3u8")
    info = {"live": False, "seg_old": None, "res": "-", "bitrate": "-"}
    if not os.path.exists(m3):
        return info
    try:
        st = os.stat(m3)
    except OSError:
        return info
    info["seg_old"] = (
        round(time.time() - st.st_mtime, 1) if time.time() > st.st_mtime else 0
    )
    info["live"] = info["seg_old"] < 20
    try:
        txt = open(m3, encoding="utf-8", errors="ignore").read()
        m = re.search(r"RESOLUTION=(\d+x\d+)", txt)
        if m:
            info["res"] = m.group(1)
        b = re.search(r"BANDWIDTH=(\d+)", txt)
        if b:
            info["bitrate"] = "{:.0f}k".format(int(b.group(1)) / 1024)
    except Exception:
        pass
    return info


def disk_usage(path):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return {
        "free": free,
        "total": total,
        "pct": round(100 * (total - free) / total, 1) if total else 0,
    }


def archive_index(date=None, cam=None, hour=None, q=None, limit=40):
    """Daftar arsip MP4. Filter opsional: date=YYYY-MM-DD, hour=HH, q=substring."""
    out = {}
    if not os.path.isdir(ARCH):
        return out
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 40
    for camname in sorted(os.listdir(ARCH)):
        if cam and camname != cam:
            continue
        d = os.path.join(ARCH, camname)
        if not os.path.isdir(d):
            continue
        files = []
        for f in os.listdir(d):
            if not f.endswith(".mp4"):
                continue
            if date and not f.startswith(date):
                continue
            if hour and f[11:13] != hour:
                continue
            if q and q.lower() not in f.lower():
                continue
            p = os.path.join(d, f)
            try:
                files.append(
                    {
                        "f": f,
                        "size": os.path.getsize(p),
                        "mtime": round(os.path.getmtime(p)),
                    }
                )
            except OSError:
                pass
        files.sort(key=lambda x: x["f"])
        # tanpa filter: 40 terbaru; dengan filter: semua yang cocok (max limit)
        out[camname] = files[-limit:] if not (date or hour or q) else files[:limit]
    return out


# ============================ HTML ============================
def html_dash():
    tmpl = open(
        os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8"
    ).read()
    # substitusi token "__CAMS__" (JANGAN %-format: CSS/JS berisi '%' literal)
    cams_js = ",".join(json.dumps(n) for n in CAMS)
    return tmpl.replace('"__CAMS__"', cams_js)


_HLSJS = "https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"


def build_url(cam_nama, stream):
    url = CAMS.get(cam_nama)
    if not url:
        return ""
    # paksa substream bila diminta
    if stream == 1:
        url = re.sub(r"stream=\d", "stream=1", url)
    # override host (mis. domain sendiri) via RTSP_HOST di .env; kosong = IP DVR asli
    host = (ENV.get("RTSP_HOST") or "").strip()
    if host:
        url = re.sub(r"(rtsp://[^@]+@)[^/:]+", r"\g<1>" + host, url)
    return url


def dvr_host():
    vals = list(CAMS.values())
    m = re.search(r"rtsp://[^@]+@([^:/]+)", vals[0]) if vals else None
    return m.group(1) if m else "192.168.1.5"


def _onvif_auth():
    """user/pass ONVIF diambil dari URL RTSP pertama di .env (server-side)."""
    vals = list(CAMS.values())
    m = re.search(r"rtsp://([^:]+):([^@]+)@", vals[0]) if vals else None
    user = m.group(1) if m else "admin"
    pwd = m.group(2) if m else ""
    try:
        port = int((ENV.get("ONVIF_PORT") or "8899").strip() or "8899")
    except ValueError:
        port = 8899
    return dvr_host(), port, user, pwd


def onvif_soap(service, body_xml, timeout=6, url_override=None):
    """Kirim SOAP ke DVR (Basic Auth). service: ptz_service|image_service|..."""
    host, port, user, pwd = _onvif_auth()
    url = url_override or "http://%s:%d/onvif/%s" % (host, port, service)
    req = urllib.request.Request(
        url,
        data=body_xml.encode("utf-8"),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
    )
    cred = ("%s:%s" % (user, pwd)).encode("utf-8")
    req.add_header("Authorization", "Basic " + base64.b64encode(cred).decode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def _clamp(v, lo=-1.0, hi=1.0):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(lo, min(hi, v))


def ptz_do(cmd, x=0.0, y=0.0, z=0.0, profile="000", vsrc="V_SRC_000"):
    """Eksekusi perintah PTZ/fokus via ONVIF. Mengembalikan True bila DVR jawab OK."""
    x, y, z = _clamp(x), _clamp(y), _clamp(z)
    if cmd == "move":
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
            '<ContinuousMove xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
            "<ProfileToken>%s</ProfileToken><Velocity>"
            '<PanTilt xmlns="http://www.onvif.org/ver10/schema" x="%s" y="%s"/>'
            '<Zoom xmlns="http://www.onvif.org/ver10/schema" x="%s"/>'
            "</Velocity></ContinuousMove></s:Body></s:Envelope>"
        ) % (profile, x, y, z)
        onvif_soap("ptz_service", body)
    elif cmd == "stop":
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
            '<Stop xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
            "<ProfileToken>%s</ProfileToken><PanTilt>true</PanTilt><Zoom>true</Zoom>"
            "</Stop></s:Body></s:Envelope>"
        ) % profile
        onvif_soap("ptz_service", body)
        try:  # hentikan juga motor fokus bila sedang jalan
            onvif_soap(
                "image_service",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
                '<Stop xmlns="http://www.onvif.org/ver20/imaging/wsdl">'
                "<VideoSourceToken>%s</VideoSourceToken>"
                "</Stop></s:Body></s:Envelope>" % vsrc,
            )
        except Exception:
            pass
    elif cmd == "home":
        onvif_soap(
            "ptz_service",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
            '<GotoHomePosition xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
            "<ProfileToken>%s</ProfileToken>"
            "</GotoHomePosition></s:Body></s:Envelope>" % profile,
        )
    elif cmd == "focus":
        # Imaging Move — Continuous focus (x: -1 jauh .. +1 dekat)
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
            '<Move xmlns="http://www.onvif.org/ver20/imaging/wsdl">'
            '<VideoSourceToken>%s</VideoSourceToken><Focus><Continuous x="%s"/>'
            "</Focus></Move></s:Body></s:Envelope>"
        ) % (vsrc, x)
        onvif_soap("image_service", body)
    else:
        raise ValueError("cmd tidak dikenal: %s" % cmd)
    return True


MIME = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".m4s": "video/mp4",
    ".mp4": "video/mp4",
    ".ts": "video/MP2T",
}


class H(BaseHTTPRequestHandler):
    server_version = "cctv/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", nocache=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if nocache:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path, ctype):
        """kirim file dengan dukungan Range (agar seek video browser jalan)."""
        size = os.path.getsize(path)
        rh = (self.headers.get("Range") or "").strip()
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", rh)
        if m:
            s, e = m.groups()
            if s:
                start = int(s)
                end = int(e) if e else size - 1
            elif e:
                start = max(0, size - int(e))
            end = min(end, size - 1)
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def _handle_ptz(self, params):
        """params: dict cam/cmd/x/y/z. Mengembalikan (code, dict)."""
        cam = (params.get("cam") or "").strip() or next(iter(CAMS), "")
        if cam not in CAMS:
            return 404, {"ok": False, "err": "kamera tidak dikenal"}
        cmd = (params.get("cmd") or "").strip().lower()
        # alias dari UI: arah mata angin / zoom / fokus
        aliases = {
            "up": ("move", 0, 0.6, 0),
            "down": ("move", 0, -0.6, 0),
            "left": ("move", -0.6, 0, 0),
            "right": ("move", 0.6, 0, 0),
            "up-left": ("move", -0.5, 0.5, 0),
            "upleft": ("move", -0.5, 0.5, 0),
            "up-right": ("move", 0.5, 0.5, 0),
            "upright": ("move", 0.5, 0.5, 0),
            "down-left": ("move", -0.5, -0.5, 0),
            "downleft": ("move", -0.5, -0.5, 0),
            "down-right": ("move", 0.5, -0.5, 0),
            "downright": ("move", 0.5, -0.5, 0),
            "zoom-in": ("move", 0, 0, 0.6),
            "zoomin": ("move", 0, 0, 0.6),
            "zoom-out": ("move", 0, 0, -0.6),
            "zoomout": ("move", 0, 0, -0.6),
            "focus-in": ("focus", 0.6, 0, 0),
            "focusin": ("focus", 0.6, 0, 0),
            "focus-out": ("focus", -0.6, 0, 0),
            "focusout": ("focus", -0.6, 0, 0),
        }
        if cmd in aliases:
            acmd, ax, ay, az = aliases[cmd]
            # kecepatan manual dari query tetap dihormati bila ada
            try:
                sp = float(params.get("speed", ""))
                if 0 < sp <= 1:
                    ax, ay, az = (v and (v / abs(v) * sp) for v in (ax, ay, az))
            except (TypeError, ValueError):
                pass
            cmd, params = acmd, dict(params, x=ax, y=ay, z=az)
        if cmd not in ("move", "stop", "home", "focus"):
            return 400, {
                "ok": False,
                "err": "cmd harus move|stop|home|focus (+ arah/zoom)",
            }
        try:
            if cmd == "focus":
                fx = params.get("x", params.get("speed", 0.5))
                ptz_do("focus", x=float(fx))
            elif cmd == "move":
                ptz_do(
                    "move",
                    x=float(params.get("x", 0)),
                    y=float(params.get("y", 0)),
                    z=float(params.get("z", 0)),
                )
            else:
                ptz_do(cmd)
        except ValueError as e:
            return 400, {"ok": False, "err": str(e)}
        except Exception as e:
            return 502, {"ok": False, "err": "DVR tidak merespons: %s" % e}
        return 200, {"ok": True, "cam": cam, "cmd": cmd}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/":
                self._send(200, html_dash(), nocache=True)
            elif u.path == "/api/status":
                out = {n: cam_meta(n) for n in CAMS}
                try:
                    out["_disk"] = disk_usage(LIVE if os.path.isdir(LIVE) else ROOT)
                except OSError:
                    pass
                self._send(200, json.dumps(out), "application/json", True)
            elif u.path == "/api/archive":
                date = (q.get("date", [""])[0] or "").strip()
                if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                    self._send(
                        400,
                        json.dumps({"ok": False, "err": "format tanggal YYYY-MM-DD"}),
                        "application/json",
                        True,
                    )
                    return
                hour = (q.get("hour", [""])[0] or "").strip()
                if hour and not re.match(r"^([01]\d|2[0-3])$", hour):
                    self._send(
                        400,
                        json.dumps({"ok": False, "err": "format jam HH (00-23)"}),
                        "application/json",
                        True,
                    )
                    return
                camf = (q.get("cam", [""])[0] or "").strip() or None
                qq = (q.get("q", [""])[0] or "").strip() or None
                try:
                    lim = int(q.get("limit", ["40"])[0])
                except ValueError:
                    lim = 40
                if date or hour or qq:
                    lim = lim if 1 <= lim <= 1000 else 500
                self._send(
                    200,
                    json.dumps(
                        archive_index(
                            date=date or None,
                            cam=camf,
                            hour=hour or None,
                            q=qq,
                            limit=lim,
                        )
                    ),
                    "application/json",
                    True,
                )
            elif u.path == "/api/log":
                try:
                    lim = int(q.get("limit", ["50"])[0])
                except ValueError:
                    lim = 50
                self._send(
                    200,
                    json.dumps(read_log(max(1, min(lim, 200)))),
                    "application/json",
                    True,
                )
            elif u.path == "/api/manual":
                self._send(200, json.dumps(manual_index()), "application/json", True)
            elif u.path == "/api/record":
                self._send(200, json.dumps(record_status()), "application/json", True)
            elif u.path == "/api/metrics":
                try:
                    self._send(
                        200, json.dumps(server_metrics()), "application/json", True
                    )
                except Exception as e:
                    self._send(
                        500,
                        json.dumps({"ok": False, "err": str(e)[:120]}),
                        "application/json",
                        True,
                    )
            elif u.path == "/api/motion":
                try:
                    lim = int(q.get("limit", ["20"])[0])
                except ValueError:
                    lim = 20
                try:
                    since = int(q.get("since", ["0"])[0])
                except ValueError:
                    since = 0
                evs = read_motion(max(1, min(lim, 100)))
                if since:
                    evs = [e for e in evs if e.get("ts", 0) > since]
                with MOTION["lock"]:
                    self._send(
                        200,
                        json.dumps(
                            {
                                "monitor": (
                                    "ok" if MOTION["ok"] else "error: " + MOTION["err"]
                                ),
                                "last_poll": MOTION["last_poll"],
                                "active": MOTION["active"],
                                "events": evs,
                            }
                        ),
                        "application/json",
                        True,
                    )
            elif u.path == "/api/rtsp":
                cam = q.get("cam", [""])[0]
                try:
                    stream = int(q.get("stream", ["1"])[0])
                except ValueError:
                    stream = 1
                url = build_url(cam, stream)
                if not url:
                    self._send(404, "kamera tidak dikenal")
                else:
                    self._send(200, url, "text/plain; charset=utf-8", True)
            elif u.path == "/api/ptz":
                flat = {k: (v[0] if isinstance(v, list) else v) for k, v in q.items()}
                code, obj = self._handle_ptz(flat)
                if code == 200 and obj.get("cmd") not in ("stop",):
                    ip = self.client_address[0] if self.client_address else ""
                    write_log("ptz", obj.get("cmd", ""), obj.get("cam", ""), ip)
                self._send(code, json.dumps(obj), "application/json", True)
            elif u.path == "/dvr":
                self.send_response(302)
                self.send_header("Location", "http://" + dvr_host() + "/")
                self.end_headers()
            elif u.path == "/hls.js":
                p = os.path.join(ROOT, "static", "hls.min.js")
                if not os.path.isfile(p):
                    self._send(404, "hls.js belum ada")
                    return
                with open(p, "rb") as f:
                    self._send(200, f.read(), "application/javascript")
            elif u.path.startswith("/dl/"):
                parts = u.path[len("/dl/") :].split("/", 1)
                if len(parts) != 2 or parts[0] not in CAMS:
                    self._send(404, "tidak ada")
                    return
                cam, fn = (urllib.parse.unquote(p) for p in parts)
                if "/" in fn or fn.startswith(".") or not fn.endswith(".mp4"):
                    self._send(403, "ditolak")
                    return
                p = os.path.realpath(os.path.join(ARCH, cam, fn))
                if not p.startswith(
                    os.path.realpath(ARCH) + os.sep
                ) or not os.path.isfile(p):
                    self._send(404, "tidak ada")
                    return
                self._serve_file(p, "video/mp4")
            elif u.path.startswith("/manual/"):
                parts = u.path[len("/manual/") :].split("/", 1)
                if len(parts) != 2 or parts[0] not in CAMS:
                    self._send(404, "tidak ada")
                    return
                cam, fn = (urllib.parse.unquote(p) for p in parts)
                if "/" in fn or fn.startswith(".") or not fn.endswith(".mp4"):
                    self._send(403, "ditolak")
                    return
                p = os.path.realpath(os.path.join(MANDIR, cam, fn))
                if not p.startswith(
                    os.path.realpath(MANDIR) + os.sep
                ) or not os.path.isfile(p):
                    self._send(404, "tidak ada")
                    return
                self._serve_file(p, "video/mp4")
            elif u.path.startswith("/snaps/"):
                parts = u.path[len("/snaps/") :].split("/", 1)
                if len(parts) != 2 or parts[0] not in CAMS:
                    self._send(404, "tidak ada")
                    return
                cam, fn = (urllib.parse.unquote(p) for p in parts)
                if "/" in fn or fn.startswith(".") or not fn.endswith(".jpg"):
                    self._send(403, "ditolak")
                    return
                p = os.path.realpath(os.path.join(SNAPDIR, cam, fn))
                if not p.startswith(
                    os.path.realpath(SNAPDIR) + os.sep
                ) or not os.path.isfile(p):
                    self._send(404, "tidak ada")
                    return
                self._serve_file(p, "image/jpeg")
            elif u.path.startswith("/hls/"):
                parts = u.path[len("/hls/") :].split("/", 1)
                if len(parts) != 2 or parts[0] not in CAMS:
                    self._send(404, "tidak ada")
                    return
                cam, fn = parts
                if "/" in fn or fn.startswith("."):
                    self._send(403, "ditolak")
                    return
                p = os.path.join(LIVE, cam, fn)
                if not os.path.isfile(p):
                    self._send(404, "belum ada (worker HLS mati?)")
                    return
                ext = os.path.splitext(fn)[1].lower()
                with open(p, "rb") as f:
                    data = f.read()
                self._send(
                    200,
                    data,
                    MIME.get(ext, "application/octet-stream"),
                    nocache=(ext == ".m3u8"),
                )
            else:
                self._send(404, "tidak ada")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path not in ("/api/ptz", "/api/log", "/api/snapshot", "/api/record"):
            self._send(404, "tidak ada")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8", errors="ignore") if n else "{}"
            try:
                params = json.loads(raw or "{}")
            except json.JSONDecodeError:
                params = dict(urllib.parse.parse_qsl(raw))
            if not isinstance(params, dict):
                params = {}
            params = {str(k): str(v) for k, v in params.items()}
            ip = self.client_address[0] if self.client_address else ""
            if u.path == "/api/log":
                ev = (params.get("event") or "")[:32]
                if ev not in (
                    "live",
                    "replay",
                    "ptz",
                    "rtsp",
                    "search",
                    "buka",
                    "motion",
                    "tamper",
                    "snap",
                    "rec",
                ):
                    self._send(
                        400,
                        json.dumps({"ok": False, "err": "event tidak dikenal"}),
                        "application/json",
                        True,
                    )
                    return
                write_log(
                    ev, params.get("detail", "")[:200], params.get("cam", "")[:32], ip
                )
                self._send(200, json.dumps({"ok": True}), "application/json", True)
                return
            if u.path == "/api/snapshot":
                cam = (params.get("cam") or "").strip() or next(iter(CAMS), "")
                try:
                    fn, size = take_snapshot(cam)
                except ValueError:
                    self._send(
                        404,
                        json.dumps({"ok": False, "err": "kamera tidak dikenal"}),
                        "application/json",
                        True,
                    )
                    return
                except Exception as e:
                    self._send(
                        502,
                        json.dumps({"ok": False, "err": str(e)[:150]}),
                        "application/json",
                        True,
                    )
                    return
                write_log("snap", fn, cam, ip)
                self._send(
                    200,
                    json.dumps(
                        {
                            "ok": True,
                            "cam": cam,
                            "file": fn,
                            "size": size,
                            "url": "/snaps/%s/%s" % (cam, fn),
                        }
                    ),
                    "application/json",
                    True,
                )
                return
            if u.path == "/api/record":
                cam = (params.get("cam") or "").strip() or next(iter(CAMS), "")
                action = (params.get("action") or "status").strip().lower()
                try:
                    if action == "start":
                        fn = record_start(cam)
                        write_log("rec", "mulai " + fn, cam, ip)
                        self._send(
                            200,
                            json.dumps(
                                {"ok": True, "cam": cam, "recording": True, "file": fn}
                            ),
                            "application/json",
                            True,
                        )
                    elif action == "stop":
                        fn, dur, size = record_stop(cam)
                        write_log("rec", "selesai %s (%s dtk)" % (fn, dur), cam, ip)
                        self._send(
                            200,
                            json.dumps(
                                {
                                    "ok": True,
                                    "cam": cam,
                                    "recording": False,
                                    "file": fn,
                                    "dur": dur,
                                    "size": size,
                                }
                            ),
                            "application/json",
                            True,
                        )
                    else:
                        self._send(
                            400,
                            json.dumps({"ok": False, "err": "action harus start|stop"}),
                            "application/json",
                            True,
                        )
                except (ValueError, RuntimeError) as e:
                    code = 404 if "dikenal" in str(e) else 409
                    self._send(
                        code,
                        json.dumps({"ok": False, "err": str(e)}),
                        "application/json",
                        True,
                    )
                return
            code, obj = self._handle_ptz(params)
            if code == 200 and obj.get("cmd") not in ("stop",):
                write_log("ptz", obj.get("cmd", ""), obj.get("cam", ""), ip)
            self._send(code, json.dumps(obj), "application/json", True)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._send(
                    500,
                    json.dumps({"ok": False, "err": str(e)}),
                    "application/json",
                    True,
                )
            except (BrokenPipeError, ConnectionResetError):
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    os.makedirs(LIVE, exist_ok=True)
    os.makedirs(ARCH, exist_ok=True)
    os.makedirs(MANDIR, exist_ok=True)
    os.makedirs(SNAPDIR, exist_ok=True)
    cleanup_stray_manual()
    threading.Thread(target=motion_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print("cctv dashboard :%d kanal=%s" % (a.port, ",".join(CAMS)), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
