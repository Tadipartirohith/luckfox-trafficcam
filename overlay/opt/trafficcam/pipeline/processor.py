"""
processor.py — Luckfox Pico Ultra / RV1106 production version.

Timestamp burn-in strategy (per-frame, live wall-clock):
  Uses ffmpeg drawtext with %{pts\:localtime\:EPOCH\:FORMAT} which computes
  localtime(chunk_start_epoch + frame_pts) per frame — timestamp advances
  frame-by-frame showing the full 0..60s wall-clock span of the recording.
  TZ=Asia/Kolkata (set in S80trafficcam) ensures localtime() returns IST.

Encode strategy (try in order, first success wins):
  1. drawtext + format=yuv420p → yuv2rkmpp pipe → HW H264 → ffmpeg mux
     Requires: /usr/local/bin/yuv2rkmpp (built from media/mpp, on firmware v2+)
  2. drawtext → libx264 (slow ~6 min/chunk; fallback if yuv2rkmpp unavailable)
  3. format=yuv420p → yuv2rkmpp (no timestamp; HW encode only)
  4. stream-copy (fastest; no timestamp in pixels)

GPS in JSON sidecar regardless of encode path.
"""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from util.detect import Detector
from util.gps import GPSReader
from util.modem import ModemManager

log = logging.getLogger(__name__)

_IST      = timezone(timedelta(hours=5, minutes=30))
_EXECUTOR = ThreadPoolExecutor(max_workers=2)

# yuv2rkmpp pipe encoder — check both locations
def _find_yuvencode() -> str | None:
    candidates = [
        "/opt/trafficcam/bin/yuv2rkmpp",  # in firmware overlay (after flash)
        "/usr/local/bin/yuv2rkmpp",        # manually pushed to current device
    ]
    for c in candidates:
        if Path(c).exists() and os.access(c, os.X_OK):
            return c
    return None

_YUVENCODE = _find_yuvencode() or "/opt/trafficcam/bin/yuv2rkmpp"

# Font search order
_FONT_CANDIDATES = [
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/ttf-dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/FreeSans.ttf",
]
_font_path: str | None = None
_font_searched = False


def _find_font() -> str | None:
    global _font_path, _font_searched
    if not _font_searched:
        _font_searched = True
        for c in _FONT_CANDIDATES:
            if Path(c).exists():
                _font_path = c
                log.info("drawtext font: %s", c)
                break
        if _font_path is None:
            log.warning("No TTF font found — timestamp NOT burned into video pixels")
    return _font_path


# ── Capability probe ──────────────────────────────────────────────────────────
_caps: dict | None = None


def _probe_caps() -> dict:
    global _caps
    if _caps is not None:
        return _caps
    caps = {
        "yuv2rkmpp": False,
        "libx264":   False,
        "drawtext":  False,
        "swscale":   False,
    }
    # Check yuv2rkmpp pipe encoder (re-probe path at runtime)
    yuv = _find_yuvencode()
    if yuv:
        global _YUVENCODE
        _YUVENCODE = yuv
        caps["yuv2rkmpp"] = True
    # Check ffmpeg capabilities
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, timeout=10
        )
        out = (r.stdout + r.stderr).decode(errors="replace")
        if "libx264" in out:
            caps["libx264"] = True
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True, timeout=10
        )
        out = (r.stdout + r.stderr).decode(errors="replace")
        if "drawtext" in out:
            caps["drawtext"] = True
        if "scale" in out or "format" in out:
            caps["swscale"] = True
    except Exception:
        pass
    _caps = caps
    log.info("capabilities: %s", caps)
    return caps


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: list, label: str, timeout: int = 600) -> bool:
    try:
        ret = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if ret.returncode == 0:
            return True
        err = ret.stderr.decode(errors="replace")[-600:]
        log.warning("cmd [%s] failed rc=%d: %s", label, ret.returncode, err)
        return False
    except subprocess.TimeoutExpired:
        log.error("cmd [%s] timed out (%ds)", label, timeout)
        return False
    except Exception as e:
        log.error("cmd [%s] exception: %s", label, e)
        return False


def _drawtext_vf(font: str, epoch: int, extra: str = "") -> str:
    """
    Build ffmpeg drawtext filter string with per-frame timestamp.

    epoch = UTC unix timestamp of the chunk start.
    TZ=Asia/Kolkata (inherited from S80trafficcam env) makes localtime() IST.

    %{pts\:localtime\:EPOCH\:FORMAT} is evaluated per-frame by ffmpeg:
      - pts = presentation timestamp in seconds (0.0, 0.067, 0.133 ...)
      - ffmpeg computes localtime(epoch + pts) → frame's actual IST wall-clock
      - FORMAT uses %H.%M.%S (dots) to avoid extra colon-escaping

    This ensures timestamp advances each frame: 0s shows start time,
    59.9s shows start+59s — the full 60-second wall-clock span.
    """
    text = f"%{{pts\\:localtime\\:{epoch}\\:%d-%m-%Y %H.%M.%S IST}}"
    return (
        f"drawtext=fontfile={font}"
        f":text='{text}'"
        f":fontsize=28:fontcolor=white"
        f":box=1:boxcolor=black@0.6:boxborderw=6"
        f":x=10:y=10"
        f"{extra}"
    )


def _encode_pipe(raw_path: str, out_path: str,
                 font: str, epoch: int,
                 W: int, H: int, fps: int) -> bool:
    """
    Hardware encode pipeline:
      ffmpeg decode + drawtext → raw YUV → yuv2rkmpp (MPP HW encoder) → H264
      Then ffmpeg muxes to MP4.
    """
    tmp_h264 = out_path + ".pipe.h264"
    try:
        vf = _drawtext_vf(font, epoch, ",format=yuv420p")
        ffmpeg_cmd = [
            "ffmpeg", "-y", "-i", raw_path,
            "-vf", vf,
            "-f", "rawvideo", "-an", "pipe:1",
        ]
        yuv2rkmpp_cmd = [
            _YUVENCODE, str(W), str(H), str(fps), "4000"
        ]

        env = os.environ.copy()
        # Ensure device-native MPP library is used (not the build-time one)
        env["LD_LIBRARY_PATH"] = "/oem/usr/lib:/usr/lib"

        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        enc_proc = subprocess.Popen(
            yuv2rkmpp_cmd,
            stdin=ffmpeg_proc.stdout,
            stdout=open(tmp_h264, "wb"),
            stderr=subprocess.DEVNULL,
            env=env,
        )
        ffmpeg_proc.stdout.close()  # allow ffmpeg to receive SIGPIPE if enc exits

        try:
            enc_proc.wait(timeout=300)
            ffmpeg_proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            log.error("pipe encode timed out")
            enc_proc.kill(); ffmpeg_proc.kill()
            return False

        if enc_proc.returncode != 0 or not Path(tmp_h264).stat().st_size:
            log.warning("yuv2rkmpp pipe failed or empty output")
            return False

        # Mux H264 to MP4
        mux_cmd = [
            "ffmpeg", "-y", "-r", str(fps),
            "-i", tmp_h264,
            "-c:v", "copy",
            "-movflags", "+faststart",
            str(out_path),
        ]
        return _run(mux_cmd, "mux-pipe-h264")

    finally:
        try:
            os.unlink(tmp_h264)
        except Exception:
            pass


def _encode(raw_path: str, out_path: str,
            start_epoch: int, fps: int, W: int, H: int) -> bool:
    """
    Encode raw H264 to MP4 with burned-in per-frame timestamp.
    Tries strategies in order; returns True on first success.
    """
    font  = _find_font()
    caps  = _probe_caps()
    input_args = ["-y", "-i", raw_path]
    out_args   = ["-movflags", "+faststart", str(out_path)]

    # ── Strategy 1: HW encode via yuv2rkmpp pipe + drawtext ──────────────────
    if caps["yuv2rkmpp"] and caps["drawtext"] and font:
        if _encode_pipe(raw_path, out_path, font, start_epoch, W, H, fps):
            log.info("Encoded with yuv2rkmpp pipe + drawtext (per-frame timestamp)")
            return True
        log.warning("yuv2rkmpp pipe failed, trying libx264")

    # ── Strategy 2: stream-copy — preserves HW RGN OSD timestamp (instant) ─────
    # trafficcam_recorder_new burns the timestamp into H264 pixels via RGN OSD.
    # Stream-copy is lossless and ~100x faster than libx264 for 1080p@30fps.
    cmd = (["ffmpeg"] + input_args + ["-c:v", "copy"] + out_args)
    if _run(cmd, "stream-copy"):
        log.info("stream-copy (HW RGN timestamp preserved in video pixels)")
        return True

    # ── Strategy 3: libx264 + drawtext (slow; SW timestamp fallback) ──────────
    if caps["libx264"] and caps["drawtext"] and font:
        vf = _drawtext_vf(font, start_epoch)
        cmd = (["ffmpeg"] + input_args +
               ["-vf", vf,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28"] +
               out_args)
        if _run(cmd, "libx264+drawtext", timeout=900):
            log.info("Encoded with libx264 + drawtext (per-frame timestamp; slow)")
            return True

    # ── Strategy 4: yuv2rkmpp, no drawtext ───────────────────────────────────
    if caps["yuv2rkmpp"]:
        tmp_h264 = out_path + ".notxt.h264"
        try:
            env = os.environ.copy()
            env["LD_LIBRARY_PATH"] = "/oem/usr/lib:/usr/lib"
            ff = subprocess.Popen(
                ["ffmpeg", "-y", "-i", raw_path,
                 "-vf", "format=yuv420p", "-f", "rawvideo", "-an", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            enc = subprocess.Popen(
                [_YUVENCODE, str(W), str(H), str(fps), "4000"],
                stdin=ff.stdout,
                stdout=open(tmp_h264, "wb"),
                stderr=subprocess.DEVNULL,
                env=env,
            )
            ff.stdout.close()
            enc.wait(timeout=120)
            ff.wait(timeout=30)
            if Path(tmp_h264).stat().st_size > 0:
                mux = ["ffmpeg", "-y", "-r", str(fps),
                       "-i", tmp_h264, "-c:v", "copy",
                       "-movflags", "+faststart", str(out_path)]
                if _run(mux, "mux-notxt"):
                    log.info("Encoded with yuv2rkmpp (no timestamp in pixels)")
                    return True
        except Exception as e:
            log.warning("yuv2rkmpp no-text failed: %s", e)
        finally:
            try: os.unlink(tmp_h264)
            except Exception: pass

    log.error("All encode strategies failed for %s", raw_path)
    return False


class ChunkProcessor:
    def __init__(self, config: dict, raw_queue: asyncio.Queue,
                 upload_queue: asyncio.Queue,
                 gps: GPSReader, modem: ModemManager):
        self._cfg      = config
        self._raw_q    = raw_queue
        self._upload_q = upload_queue
        self._gps      = gps
        self._modem    = modem
        cam = config["camera"]
        self._fps        = int(cam.get("fps", 15))
        self._resolution = cam["resolution"]
        # Parse W×H from resolution string "1920x1080"
        try:
            self._W, self._H = (int(x) for x in self._resolution.split("x"))
        except Exception:
            self._W, self._H = 1920, 1080
        stor = config["storage"]
        self._proc_dir   = Path(stor["processed_dir"])
        det = config.get("detection", {})
        self._sample_n   = int(det.get("sample_every_n_seconds", 2))
        self._detector   = Detector(config)
        _probe_caps()  # warm up at startup

    async def run(self):
        self._proc_dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        self._loop = loop
        while True:
            raw_path = await self._raw_q.get()
            try:
                await loop.run_in_executor(_EXECUTOR, self._process, raw_path)
            except Exception as e:
                log.error("Processor error for %s: %s", raw_path, e, exc_info=True)
            finally:
                self._raw_q.task_done()

    def _process(self, raw_path: str):
        chunk_id = str(uuid.uuid4())

        # Parse start timestamp from filename: raw_{epoch}_{N}.h264
        try:
            epoch_str = Path(raw_path).stem.split("_")[1]
            start_epoch = int(epoch_str)          # UTC epoch of chunk start
            start_ts    = datetime.fromtimestamp(start_epoch, tz=timezone.utc)
        except Exception:
            start_ts    = datetime.now(tz=timezone.utc)
            start_epoch = int(start_ts.timestamp())

        # GPS location
        lat, lon = self._gps.get_coordinates()
        location_source = "gps"
        if lat is None:
            lat, lon = self._modem.get_location()
            location_source = "modem_lbs" if lat is not None else "none"

        # Sample frames for detection
        frames_dir = Path(f"/tmp/{chunk_id}_frames")
        frames_dir.mkdir(parents=True, exist_ok=True)
        sample_interval = self._fps * self._sample_n
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", raw_path,
                 "-vf", f"select='not(mod(n\\,{sample_interval}))'",
                 "-vsync", "0", "-q:v", "2",
                 str(frames_dir / "frame_%04d.jpg")],
                capture_output=True, check=False, timeout=120
            )
        except Exception as e:
            log.warning("Frame extraction failed: %s", e)

        detections = []
        for i, fp in enumerate(sorted(frames_dir.glob("frame_*.jpg"))):
            try:
                vehicles, plates = self._detector.detect(str(fp))
                if vehicles or plates:
                    detections.append({
                        "frame_index": i * sample_interval,
                        "vehicles":    vehicles,
                        "plates":      plates,
                    })
            except Exception as e:
                log.warning("Detection error on %s: %s", fp.name, e)
        for fp in frames_dir.glob("*"):
            fp.unlink(missing_ok=True)
        try:
            frames_dir.rmdir()
        except Exception:
            pass

        # Encode with per-frame timestamp burned in
        out_path = self._proc_dir / f"{chunk_id}.mp4"
        if not _encode(raw_path, str(out_path),
                       start_epoch, self._fps, self._W, self._H):
            log.error("Failed to produce output for %s", raw_path)
            return

        sha = _sha256(str(out_path))

        # Sidecar JSON
        sidecar = {
            "chunk_id":        chunk_id,
            "device_id":       _device_id(),
            "start_timestamp": start_ts.isoformat(),
            "start_ist":       start_ts.astimezone(_IST).isoformat(),
            "processed_at":    datetime.now(timezone.utc).isoformat(),
            "gps": {
                "lat":     lat, "lon":    lon,
                "has_fix": lat is not None,
                "source":  location_source,
            },
            "video_file":      f"{chunk_id}.mp4",
            "video_sha256":    sha,
            "duration_s":      self._cfg["camera"]["chunk_duration_seconds"],
            "resolution":      self._resolution,
            "fps":             self._fps,
            "sample_interval_s": self._sample_n,
            "detections":      detections,
        }
        json_path = self._proc_dir / f"{chunk_id}.json"
        json_path.write_text(json.dumps(sidecar, indent=2))

        log.info("Processed %s (%d detections)", chunk_id[:8], len(detections))

        asyncio.run_coroutine_threadsafe(
            self._upload_q.put({
                "chunk_id":     chunk_id,
                "video":        str(out_path),
                "sidecar":      str(json_path),
                "sidecar_data": sidecar,
            }),
            self._loop
        )

        try:
            os.unlink(raw_path)
        except Exception:
            pass


_DEVICE_ID_CACHE: str | None = None


def _device_id() -> str:
    global _DEVICE_ID_CACHE
    if _DEVICE_ID_CACHE:
        return _DEVICE_ID_CACHE
    serial = ""
    for src in ["/proc/cpuinfo", "/etc/machine-id"]:
        try:
            with open(src) as f:
                for line in f:
                    if "Serial" in line or src == "/etc/machine-id":
                        serial = line.strip().split(":")[-1].strip()
                        break
            if serial:
                break
        except Exception:
            pass
    if not serial:
        import uuid as _uuid
        serial = str(_uuid.uuid4())
    _DEVICE_ID_CACHE = hashlib.sha256(serial.encode()).hexdigest()[:16]
    return _DEVICE_ID_CACHE
