#!/bin/bash
# build_overlay.sh — Fast build: compile recorder + apply overlay + repackage
# Run in GitHub Actions or locally in WSL2.
# Prerequisites: base partition images must exist in $BASE_IMG_DIR (downloaded from S3).
set -euo pipefail

DATE=$(date +%Y-%m-%d)
SDK=${SDK_DIR:-/root/trafficcam_build/luckfox-pico}
BASE_IMG_DIR=${BASE_IMG_DIR:-/tmp/base_images}
OUT_DIR=${OUT_DIR:-/tmp/firmware-out}
TOOLCHAIN_PREFIX=arm-rockchip830-linux-uclibcgnueabihf
MEDIA_DIR=$SDK/sysdrv/source/media_out

echo "=== Luckfox Trafficcam Overlay Build — $DATE ==="

# 1. Compile trafficcam_recorder_new
echo "--- Compiling trafficcam_recorder_new ---"
RECORDER_SRC=$(dirname "$0")/../recorder/trafficcam_recorder.c
RECORDER_OUT=$SDK/output/out/rootfs_uclibc_rv1106/opt/trafficcam/bin/trafficcam_recorder_new
mkdir -p $(dirname $RECORDER_OUT)
${TOOLCHAIN_PREFIX}-gcc \
    -march=armv7-a -mfpu=neon -mfloat-abi=hard -Os \
    -I${MEDIA_DIR}/include \
    -L${MEDIA_DIR}/lib \
    -o $RECORDER_OUT \
    $RECORDER_SRC \
    -lrockit -lpthread -Wl,-rpath,/oem/usr/lib
chmod +x $RECORDER_OUT
echo "Recorder compiled: $(ls -lh $RECORDER_OUT)"

# 2. Apply overlay files → rootfs
echo "--- Applying overlay ---"
OVERLAY=$(dirname "$0")/../overlay
ROOTFS=$SDK/output/out/rootfs_uclibc_rv1106

# Python pipeline
cp -r $OVERLAY/opt/trafficcam/. $ROOTFS/opt/trafficcam/

# Inject real config.json (secrets from env vars)
sed -e "s|\${AWS_ACCESS_KEY_ID}|${AWS_ACCESS_KEY_ID}|g" \
    -e "s|\${AWS_SECRET_ACCESS_KEY}|${AWS_SECRET_ACCESS_KEY}|g" \
    -e "s|\${AWS_REGION}|${AWS_REGION:-ap-south-1}|g" \
    -e "s|\${S3_BUCKET}|${S3_BUCKET:-traf-test}|g" \
    $(dirname "$0")/../config/device_config.json \
    > $ROOTFS/opt/trafficcam/config.json

# Init scripts
cp $OVERLAY/etc/init.d/S80trafficcam $ROOTFS/etc/init.d/S80trafficcam
cp $OVERLAY/etc/init.d/S60ispserver  $ROOTFS/etc/init.d/S60ispserver
chmod +x $ROOTFS/etc/init.d/S80trafficcam $ROOTFS/etc/init.d/S60ispserver

# Apply overlay to OEM partition
OEM=$SDK/output/out/oem
cp $OVERLAY/oem/usr/bin/RkLunch.sh $OEM/usr/bin/RkLunch.sh
chmod +x $OEM/usr/bin/RkLunch.sh

echo "Overlay applied"

# 3. Repackage firmware
echo "--- Packaging firmware ---"
CLEAN_PATH=$(echo $PATH | tr ':' '\n' | grep -v ' ' | tr '\n' ':' | sed 's/:$//')
cd $SDK
PATH="$CLEAN_PATH" bash build.sh firmware

# 4. Copy output with date suffix
mkdir -p $OUT_DIR
IMG=$OUT_DIR/luckfox-trafficcam-${DATE}.img
cp $SDK/output/image/update.img $IMG
echo "Image: $(ls -lh $IMG)"
echo "$DATE" > $OUT_DIR/latest.txt

echo "=== Build complete: $IMG ==="
