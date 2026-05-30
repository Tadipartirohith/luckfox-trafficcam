#!/bin/bash
# upload_s3.sh — Upload firmware + flash scripts to S3 with DDMMYY_VN versioning
# Folder format: DDMMYY_V1, DDMMYY_V2 … (never overwrites a previous build)
set -euo pipefail

BUCKET=${S3_BUCKET_FIRMWARE:-luckfox-firmware-img}
REGION=${AWS_REGION:-ap-south-1}
OUT_DIR=${OUT_DIR:-/tmp/firmware-out}
REPO_ROOT=$(dirname "$0")/..

# ── Determine version folder ───────────────────────────────────────────────
DDMMYY=$(date +%d%m%y)   # e.g. 310526 for 31 May 2026

# List all existing folders for today, find the highest V number
# grep exits 1 when no match — || true keeps pipefail happy
EXISTING_MAX=$(aws s3 ls "s3://$BUCKET/" --region "$REGION" 2>/dev/null \
    | grep -oE "${DDMMYY}_V[0-9]+" \
    | grep -oE "[0-9]+$" \
    | sort -n | tail -1 || true)

if [ -z "$EXISTING_MAX" ]; then
    NEXT_V=1
else
    NEXT_V=$((EXISTING_MAX + 1))
fi

FOLDER="${DDMMYY}_V${NEXT_V}"
IMG_NAME="luckfox-trafficcam-${FOLDER}.img"
SRC_IMG="$OUT_DIR/luckfox-trafficcam-$(date +%Y-%m-%d).img"

[ -f "$SRC_IMG" ] || { echo "ERROR: Image not found: $SRC_IMG"; exit 1; }

echo "=== Uploading to s3://$BUCKET/$FOLDER/ ==="

# Upload image (rename to versioned name)
aws s3 cp "$SRC_IMG" "s3://$BUCKET/$FOLDER/$IMG_NAME" \
    --region "$REGION" --no-progress

# Upload flash scripts alongside the image
aws s3 cp "$REPO_ROOT/flash/flash.ps1" "s3://$BUCKET/$FOLDER/flash.ps1" --region "$REGION"
aws s3 cp "$REPO_ROOT/flash/flash.sh"  "s3://$BUCKET/$FOLDER/flash.sh"  --region "$REGION"

# Update latest pointer (scripts use this to auto-download newest build)
echo "$FOLDER" | aws s3 cp - "s3://$BUCKET/latest.txt" --region "$REGION"

# Write build-info.json
GIT_SHA=$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
GIT_BRANCH=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)

cat > /tmp/build-info.json << JSON
{
  "version":    "$FOLDER",
  "date":       "$(date +%d-%m-%Y)",
  "git_sha":    "$GIT_SHA",
  "git_branch": "$GIT_BRANCH",
  "image":      "$IMG_NAME",
  "s3_image":   "s3://$BUCKET/$FOLDER/$IMG_NAME",
  "s3_flash_ps1": "s3://$BUCKET/$FOLDER/flash.ps1",
  "s3_flash_sh":  "s3://$BUCKET/$FOLDER/flash.sh"
}
JSON

aws s3 cp /tmp/build-info.json "s3://$BUCKET/$FOLDER/build-info.json" --region "$REGION"
aws s3 cp /tmp/build-info.json "s3://$BUCKET/latest-build-info.json"  --region "$REGION"

echo ""
echo "=== Upload complete ==="
echo "  Version: $FOLDER"
echo "  Image:   s3://$BUCKET/$FOLDER/$IMG_NAME"
echo "  Flash:   s3://$BUCKET/$FOLDER/flash.ps1"
echo "  Latest:  s3://$BUCKET/latest.txt → $FOLDER"
