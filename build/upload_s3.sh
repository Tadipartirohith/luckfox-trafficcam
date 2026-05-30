#!/bin/bash
# upload_s3.sh — Upload firmware image + flash scripts to S3
set -euo pipefail

DATE=$(date +%Y-%m-%d)
BUCKET=${S3_BUCKET_FIRMWARE:-luckfox-firmware-img}
REGION=${AWS_REGION:-ap-south-1}
OUT_DIR=${OUT_DIR:-/tmp/firmware-out}
IMG=$OUT_DIR/luckfox-trafficcam-${DATE}.img
REPO_ROOT=$(dirname "$0")/..

[ -f "$IMG" ] || { echo "ERROR: Image not found: $IMG"; exit 1; }

echo "=== Uploading to s3://$BUCKET/$DATE/ ==="

# Upload image
aws s3 cp "$IMG" "s3://$BUCKET/$DATE/luckfox-trafficcam-${DATE}.img" \
    --region $REGION --no-progress

# Upload flash scripts
aws s3 cp "$REPO_ROOT/flash/flash.ps1" "s3://$BUCKET/$DATE/flash.ps1" --region $REGION
aws s3 cp "$REPO_ROOT/flash/flash.sh"  "s3://$BUCKET/$DATE/flash.sh"  --region $REGION

# Update latest pointer
echo "$DATE" | aws s3 cp - "s3://$BUCKET/latest.txt" --region $REGION

# Write build-info.json
BUILD_INFO=$(cat << JSON
{
  "date": "$DATE",
  "git_sha": "$(git -C $REPO_ROOT rev-parse --short HEAD 2>/dev/null || echo unknown)",
  "git_branch": "$(git -C $REPO_ROOT rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)",
  "image": "luckfox-trafficcam-${DATE}.img",
  "s3_path": "s3://$BUCKET/$DATE/luckfox-trafficcam-${DATE}.img",
  "flash_ps1": "s3://$BUCKET/$DATE/flash.ps1",
  "flash_sh": "s3://$BUCKET/$DATE/flash.sh"
}
JSON
)
echo "$BUILD_INFO" | aws s3 cp - "s3://$BUCKET/$DATE/build-info.json" --region $REGION
echo "$BUILD_INFO" | aws s3 cp - "s3://$BUCKET/latest-build-info.json" --region $REGION

echo "=== Upload complete ==="
echo "  Image:  s3://$BUCKET/$DATE/luckfox-trafficcam-${DATE}.img"
echo "  Flash:  s3://$BUCKET/$DATE/flash.ps1"
echo "  Latest: s3://$BUCKET/latest-build-info.json"
