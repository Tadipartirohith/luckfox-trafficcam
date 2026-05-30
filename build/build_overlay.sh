#!/bin/bash
# build_overlay.sh — Fast build: compile recorder + apply overlay + repackage firmware
# Runs on the self-hosted WSL2 runner which already has the full Luckfox SDK built.
# Prerequisites: SDK fully built at $SDK_DIR (default: /root/trafficcam_build/luckfox-pico)
set -euo pipefail

DATE=$(date +%Y-%m-%d)
SDK=${SDK_DIR:-/root/trafficcam_build/luckfox-pico}
OUT_DIR=${OUT_DIR:-/tmp/firmware-out}
REPO=${REPO_DIR:-$(dirname "$0")/..}
TOOLCHAIN_PREFIX=arm-rockchip830-linux-uclibcgnueabihf
TOOLCHAIN_BIN=$SDK/tools/linux/toolchain/$TOOLCHAIN_PREFIX/bin
MEDIA_DIR=$SDK/output/out/media_out

export PATH="$TOOLCHAIN_BIN:$PATH"

echo "=== Luckfox Trafficcam Overlay Build — $DATE ==="
echo "SDK:  $SDK"
echo "Repo: $REPO"
echo "Out:  $OUT_DIR"

# Sanity checks
[ -d "$SDK/output/out/rootfs_uclibc_rv1106" ] || { echo "ERROR: rootfs not found. Run full SDK build first."; exit 1; }
[ -d "$SDK/output/out/oem" ] || { echo "ERROR: OEM partition not found."; exit 1; }
[ -d "$MEDIA_DIR/include" ] || { echo "ERROR: media headers not found at $MEDIA_DIR"; exit 1; }
which ${TOOLCHAIN_PREFIX}-gcc > /dev/null 2>&1 || { echo "ERROR: toolchain not found at $TOOLCHAIN_BIN"; exit 1; }

# ── 1. Compile trafficcam_recorder_new ────────────────────────────────────
echo "--- Compiling trafficcam_recorder_new ---"
RECORDER_SRC=$REPO/recorder/trafficcam_recorder.c
RECORDER_OUT=$SDK/output/out/rootfs_uclibc_rv1106/opt/trafficcam/bin/trafficcam_recorder_new
mkdir -p "$(dirname "$RECORDER_OUT")"

${TOOLCHAIN_PREFIX}-gcc \
    -march=armv7-a -mfpu=neon -mfloat-abi=hard -Os \
    -I"$MEDIA_DIR/include" \
    -L"$MEDIA_DIR/lib" \
    -o "$RECORDER_OUT" \
    "$RECORDER_SRC" \
    -lrockit -lrockchip_mpp -lrga -lpthread \
    -Wl,-rpath,/oem/usr/lib

chmod +x "$RECORDER_OUT"
echo "Recorder compiled: $(ls -lh "$RECORDER_OUT")"

# ── 2. Apply overlay files to rootfs ──────────────────────────────────────
echo "--- Applying overlay ---"
OVERLAY=$REPO/overlay
ROOTFS=$SDK/output/out/rootfs_uclibc_rv1106

# Python pipeline
cp -r "$OVERLAY/opt/trafficcam/." "$ROOTFS/opt/trafficcam/"

# Inject real config.json (secrets from env vars)
sed -e "s|\${AWS_ACCESS_KEY_ID}|${AWS_ACCESS_KEY_ID:-}|g" \
    -e "s|\${AWS_SECRET_ACCESS_KEY}|${AWS_SECRET_ACCESS_KEY:-}|g" \
    -e "s|\${AWS_REGION}|${AWS_REGION:-ap-south-1}|g" \
    -e "s|\${S3_BUCKET}|${S3_BUCKET:-traf-test}|g" \
    "$REPO/config/device_config.json" \
    > "$ROOTFS/opt/trafficcam/config.json"

# Init scripts
cp "$OVERLAY/etc/init.d/S41clocksync"  "$ROOTFS/etc/init.d/S41clocksync"
cp "$OVERLAY/etc/init.d/S60ispserver"  "$ROOTFS/etc/init.d/S60ispserver"
cp "$OVERLAY/etc/init.d/S80trafficcam" "$ROOTFS/etc/init.d/S80trafficcam"
chmod +x "$ROOTFS/etc/init.d/S41clocksync"          "$ROOTFS/etc/init.d/S60ispserver"          "$ROOTFS/etc/init.d/S80trafficcam"

# RkLunch.sh (OEM partition)
OEM=$SDK/output/out/oem
cp "$OVERLAY/oem/usr/bin/RkLunch.sh" "$OEM/usr/bin/RkLunch.sh"
chmod +x "$OEM/usr/bin/RkLunch.sh"

echo "Overlay applied"

# ── 3. Repackage firmware ─────────────────────────────────────────────────
echo "--- Packaging firmware ---"
CLEAN_PATH=$(echo "$PATH" | tr ':' '\n' | grep -v ' ' | tr '\n' ':' | sed 's/:$//')
cd "$SDK"
PATH="$CLEAN_PATH" bash build.sh firmware

# ── 4. Copy output with date suffix ───────────────────────────────────────
mkdir -p "$OUT_DIR"
IMG="$OUT_DIR/luckfox-trafficcam-${DATE}.img"
cp "$SDK/output/image/update.img" "$IMG"
echo "$DATE" > "$OUT_DIR/latest.txt"

echo "=== Build complete: $(ls -lh "$IMG") ==="
