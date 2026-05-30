#!/bin/bash
# flash.sh — Flash Luckfox Pico Ultra on Linux/Mac via rkdeveloptool
# Auto-downloads the latest firmware from S3 (versioned as DDMMYY_VN).
# Usage: ./flash.sh [/path/to/image.img]
#        VERSION=310526_V2 ./flash.sh      (specific version from S3)
set -euo pipefail

TOOL=${RKDEV_TOOL:-rkdeveloptool}
S3_BUCKET=${S3_BUCKET_FIRMWARE:-luckfox-firmware-img}
REGION=${AWS_REGION:-ap-south-1}
FLASH_DIR="$HOME/.luckfox-flash"
mkdir -p "$FLASH_DIR"

IMG=${1:-""}

if [ -z "$IMG" ]; then
    echo "=== Fetching firmware from S3 ==="
    if [ -n "${VERSION:-}" ]; then
        FOLDER="$VERSION"
    else
        FOLDER=$(aws s3 cp "s3://$S3_BUCKET/latest.txt" - \
                     --region "$REGION" 2>/dev/null | tr -d '[:space:]')
        [ -z "$FOLDER" ] && { echo "ERROR: Cannot reach S3. Pass image path or set VERSION="; exit 1; }
    fi
    echo "Version: $FOLDER"
    IMG_NAME="luckfox-trafficcam-${FOLDER}.img"
    IMG="$FLASH_DIR/$IMG_NAME"
    if [ ! -f "$IMG" ]; then
        echo "Downloading $IMG_NAME ..."
        aws s3 cp "s3://$S3_BUCKET/$FOLDER/$IMG_NAME" "$IMG" \
            --region "$REGION" --no-progress
    else
        echo "Using cached: $IMG"
    fi
fi

echo "Image: $IMG"
echo ""
echo ">>> DO THE MASKROM SEQUENCE:"
echo "    1. Unplug USB-C from board"
echo "    2. Hold BOOT button"
echo "    3. Plug USB-C while holding BOOT"
echo "    4. Release BOOT after 2-3 seconds"
echo ""

for i in $(seq 1 30); do
    if $TOOL ld 2>/dev/null | grep -q "Loader\|Maskrom"; then
        echo "Device found."
        break
    fi
    echo "Waiting... ${i}s"
    sleep 2
done

echo "=== Flashing (3-5 min). DO NOT UNPLUG! ==="
$TOOL uf "$IMG"

echo ""
echo "=== Flash complete! Board rebooting. ==="
