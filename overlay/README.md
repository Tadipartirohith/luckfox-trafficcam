# Overlay

Everything in this folder is copied verbatim into the firmware rootfs at build time.
The directory structure mirrors the device filesystem.

---

## Contents

### `etc/init.d/` — Boot services (SysVinit)

| Script | Runs at | Purpose |
|--------|---------|---------|
| `S41clocksync` | Boot S41 | Sync system clock via NTP (rdate). Retries every 10 s for 3 min in background if network not yet up. Also triggered after PPP connects. |
| `S60ispserver` | Boot S60 | Starts `rkaiq_3A_server` (ISP auto-exposure / auto-white-balance daemon). Must run before S80. |
| `S80trafficcam` | Boot S80 | Starts the Python trafficcam pipeline (`main.py`). Sets `TZ=Asia/Kolkata`, `LD_LIBRARY_PATH=/oem/usr/lib:/usr/lib`, `SSL_CERT_FILE`. |

Boot order: S41 → S60 → S80.

### `oem/usr/bin/RkLunch.sh` — ISP startup patch

The stock `RkLunch.sh` starts `rkipc`, which holds the VI channel exclusively and blocks the recorder.
This patched version starts `rkaiq_3A_server --silent` instead.

### `opt/trafficcam/` — Python pipeline

| File | Purpose |
|------|---------|
| `main.py` | Async orchestrator: wires recorder → processor → uploader. Handles NTP sync at startup and modem clock sync after PPP. |
| `pipeline/camera.py` | `ChunkRecorder` — wraps the C recorder binary, renames completed segments, enqueues for processing |
| `pipeline/processor.py` | `ChunkProcessor` — stream-copies H.264 → MP4 (timestamp already burned in via HW RGN, no re-encode) |
| `pipeline/uploader.py` | `Uploader` — uploads MP4 to S3, disk-queues on failure, retries every 5 min |
| `pipeline/health.py` | `HealthMonitor` — watchdog, temperature, disk space |
| `util/modem.py` | `ModemManager` — auto-detects carrier (Jio/Airtel/Vi/BSNL), dials PPP, syncs clock via `AT+QLTS=2` |
| `util/gps.py` | `GPSReader` — reads NMEA from `/dev/ttyS3` |
| `util/detect.py` | RKNN vehicle / number-plate detection (not yet active) |

---

## Modifying the Pipeline

Edit any file under `overlay/`, commit, and push.
GitHub Actions rebuilds the firmware and publishes a new versioned image to S3.
Flash the new image to apply changes to the board.
