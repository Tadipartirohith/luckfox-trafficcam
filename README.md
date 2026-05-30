# Luckfox Trafficcam

AI Traffic Enforcement firmware for **Luckfox Pico Ultra (RV1106G3)**.
Records 1080p H264 video with burned-in IST timestamp, runs RKNN vehicle/plate detection, uploads to S3.

## Repository Structure

```
overlay/            ← Files copied into the firmware rootfs at build time
  opt/trafficcam/   ← Python pipeline (camera, processor, uploader, GPS, modem)
  etc/init.d/       ← S80trafficcam (service), S60ispserver (ISP daemon)
  oem/usr/bin/      ← RkLunch.sh (patched: rkaiq_3A_server instead of rkipc)
recorder/           ← trafficcam_recorder.c (HW RGN OSD + VENC, no re-encode)
flash/              ← flash.ps1 (Windows), flash.sh (Linux/Mac)
build/              ← build_overlay.sh, upload_s3.sh, setup_vm.sh
config/             ← device_config.json template (secrets injected at build)
.github/workflows/  ← GitHub Actions CI: build + S3 publish on every push
```

## Flashing a Board (Windows)

1. Download `flash.ps1` and the latest `.img` from S3:
   ```
   s3://luckfox-firmware-img/YYYY-MM-DD/
   ```
2. Open **PowerShell as Administrator**
3. Run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File flash.ps1
   ```
4. Follow the MASKROM sequence when prompted (unplug → hold BOOT → plug → release)

## Live Stream (for focus adjustment)

Open VLC → Media → Open Network Stream:
```
smb://root:luckfox@172.32.0.93/public/var/trafficcam/raw/seg_XXXX.h264
```
Or use this PowerShell one-liner:
```powershell
$seg = adb -s db9fdbc7150490d6 shell "ls -t /var/trafficcam/raw/seg_*.h264 2>/dev/null | head -1" | ForEach-Object { $_.Trim() }
Start-Process "C:\Program Files\VideoLAN\VLC\vlc.exe" -ArgumentList "`"smb://root:luckfox@172.32.0.93/public$seg`" --live-caching=1000 --demux=h264"
```

## Build Pipeline

Every `git push` to `main` triggers GitHub Actions which:
1. Restores the pre-built Luckfox SDK from S3 cache
2. Compiles `trafficcam_recorder_new`
3. Applies overlay files to rootfs
4. Injects secrets from GitHub Secrets into `config.json`
5. Repackages the firmware image
6. Uploads `luckfox-trafficcam-YYYY-MM-DD.img` + `flash.ps1` to S3

### First-time Setup (run once in WSL2)

```bash
# 1. Clone this repo
git clone https://github.com/Tadipartirohith/luckfox-trafficcam
cd luckfox-trafficcam

# 2. Set up WSL2 build environment
bash build/setup_vm.sh

# 3. Do a full base build (once — takes ~90 min)
bash build/build_overlay.sh

# 4. Push SDK cache to S3 so GitHub Actions can use it
tar --use-compress-program=zstd -cf /tmp/sdk-cache.tar.zst \
    /root/trafficcam_build/luckfox-pico/output \
    /root/trafficcam_build/luckfox-pico/sysdrv/source/objs_kernel
aws s3 cp /tmp/sdk-cache.tar.zst \
    s3://luckfox-firmware-img/sdk-cache/luckfox-sdk-cache.tar.zst \
    --region ap-south-1
```

### GitHub Secrets Required

Set these in **Settings → Secrets and variables → Actions**:

| Secret | Value |
|--------|-------|
| `AWS_ACCESS_KEY_ID` | Your AWS access key |
| `AWS_SECRET_ACCESS_KEY` | Your AWS secret key |
| `AWS_REGION` | `ap-south-1` |
| `S3_BUCKET` | `traf-test` (video upload bucket) |
| `S3_BUCKET_FIRMWARE` | `luckfox-firmware-img` (firmware bucket) |

## Key Hardware Notes

- **Device:** Luckfox Pico Ultra, RV1106G3, ADB serial `db9fdbc7150490d6`
- **ADB:** `adb -s db9fdbc7150490d6 shell`  (SSH unreliable — always use ADB)
- **Camera:** MIS5001 5MP, manual focus lens, MIPI CSI-2
- **Modem:** EC200U on `/dev/ttyS4`, Airtel SIM
- **GPS:** `/dev/ttyS3` @ 9600 baud
- **Clock:** Resets on reboot — sync at session start:
  ```powershell
  $e=[int][double]::Parse(([datetime]::UtcNow-[datetime]"1970-01-01").TotalSeconds)
  adb -s db9fdbc7150490d6 shell "date -u -s @$e"
  ```
