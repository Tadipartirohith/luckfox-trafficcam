# Luckfox Trafficcam

AI traffic enforcement firmware for the **Luckfox Pico Ultra (RV1106G3)**.

Records 1080p H.264 video with a hardware-burned IST timestamp, detects vehicles and number plates via RKNN, and uploads clips to S3 over 4G.

---

## Repository Structure

```
flash/              ← Flashing scripts (Windows + Linux/Mac)
overlay/            ← All files baked into the firmware at build time
  etc/init.d/       ← Boot services: clock sync, ISP daemon, trafficcam service
  oem/usr/bin/      ← RkLunch.sh (starts rkaiq_3A_server instead of rkipc)
  opt/trafficcam/   ← Python pipeline: camera, processor, uploader, GPS, modem
recorder/           ← C source for the hardware recorder binary
build/              ← CI build and upload scripts
config/             ← device_config.json template (secrets injected at build)
.github/workflows/  ← GitHub Actions: auto-build and publish on every push
```

Each folder has its own README with full details.

---

## Flashing a Board

See **[flash/README.md](flash/README.md)** for the complete guide.

---

## Updating the Firmware

Edit any file under `overlay/`, `recorder/`, `flash/`, or `build/`, then push to `main`.
GitHub Actions builds a new firmware image and publishes it to S3 automatically.
Flash the new image using the steps in `flash/README.md`.

See **[build/README.md](build/README.md)** for CI setup details.

---

## Hardware Reference

| Item | Value |
|------|-------|
| SoC | RV1106G3, Cortex-A7, 256 MB RAM, 1.0 TOPS NPU |
| Camera | MIS5001 5 MP, MIPI CSI-2, manual-focus lens |
| Modem | Quectel EC200U on `/dev/ttyS4`, Airtel SIM |
| GPS | Serial on `/dev/ttyS3` @ 9600 baud |
| OS | Buildroot 2023.02.6, kernel 5.10.160, BusyBox, SysVinit |
| USB RNDIS IP | `172.32.0.93` (device) |
| ADB serial | `db9fdbc7150490d6` |

**Always use ADB** — SSH is unreliable on this board:
```powershell
adb -s db9fdbc7150490d6 shell
```

---

## Live Video Stream (for focus check)

Open VLC → **Media → Open Network Stream** and enter:
```
smb://root:luckfox@172.32.0.93/public/var/trafficcam/raw/seg_XXXX.h264
```

Or use this PowerShell one-liner to open the current recording segment automatically:
```powershell
$seg = adb -s db9fdbc7150490d6 shell "ls -t /var/trafficcam/raw/seg_*.h264 2>/dev/null | head -1" | ForEach-Object { $_.Trim() }
Start-Process "C:\Program Files\VideoLAN\VLC\vlc.exe" -ArgumentList "`"smb://root:luckfox@172.32.0.93/public$seg`" --live-caching=1000 --demux=h264"
```
