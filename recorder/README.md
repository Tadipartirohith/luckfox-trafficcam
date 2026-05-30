# Recorder

`trafficcam_recorder.c` — Hardware-accelerated video recorder for the RV1106G3.

Uses Rockchip's `librockit` to capture from the MIS5001 camera sensor, burn a live IST timestamp via the hardware RGN overlay engine, encode to H.264 via the VENC hardware block, and write continuous 60-second segment files — all without any software re-encoding.

---

## What It Does

```
MIS5001 sensor
    ↓  MIPI CSI-2
VI (Video Input)
    ↓  raw YUV frames
RGN (Region) overlay  ← burns "DD-MM-YYYY HH.MM.SS IST" into every frame
    ↓
VENC (H.264 hardware encoder)
    ↓
/var/trafficcam/raw/seg_NNNN.h264   ← one file per 60 seconds
```

The timestamp is burned at the hardware level — there is no ffmpeg re-encode. Processing a 60-second segment takes ~5 seconds (stream-copy only).

---

## Output

- **Path:** `/var/trafficcam/raw/seg_NNNN.h264`
- **Resolution:** 1920×1080
- **Frame rate:** 15 fps
- **Bitrate:** ~4 Mbps
- **Segment size:** ~28–30 MB per 60 seconds
- **Timestamp format:** `DD-MM-YYYY HH.MM.SS IST` in the top-left corner

---

## How It Is Built

The binary is compiled automatically during every CI build.
The compiled binary is placed at `/opt/trafficcam/bin/trafficcam_recorder_new` on the device.

To compile manually in WSL2:

```bash
SDK=/root/trafficcam_build/luckfox-pico
TOOLCHAIN=$SDK/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin
MEDIA=$SDK/output/out/media_out

$TOOLCHAIN/arm-rockchip830-linux-uclibcgnueabihf-gcc \
    -march=armv7-a -mfpu=neon -mfloat-abi=hard -Os \
    -I"$MEDIA/include" \
    -L"$MEDIA/lib" \
    -o trafficcam_recorder_new \
    trafficcam_recorder.c \
    -lrockit -lrockchip_mpp -lrga -lpthread \
    -Wl,-rpath,/oem/usr/lib
```

Push to the board:
```bash
adb -s db9fdbc7150490d6 push trafficcam_recorder_new /opt/trafficcam/bin/trafficcam_recorder_new
adb -s db9fdbc7150490d6 shell chmod +x /opt/trafficcam/bin/trafficcam_recorder_new
```

---

## Key Libraries

| Library | Purpose |
|---------|---------|
| `librockit.so` | Rockchip media framework (VI, RGN, VENC) |
| `librockchip_mpp.so` | Media Process Platform (H.264 HW encoder) |
| `librga.so` | Rockchip GPU accelerator (used by rockit) |

All live on the device at `/oem/usr/lib/`.
