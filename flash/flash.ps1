# flash.ps1 — Flash Luckfox Pico Ultra via MASKROM + usbipd + WSL2
# Downloads latest firmware from S3 if not already present.
# Run in an ADMIN PowerShell window:
#   powershell -ExecutionPolicy Bypass -File flash.ps1
#
# Optional: override image
#   powershell -ExecutionPolicy Bypass -File flash.ps1 -ImagePath "C:\path\to\image.img"

param(
    [string]$ImagePath = "",
    [string]$S3Bucket  = "luckfox-firmware-img",
    [string]$Region    = "ap-south-1"
)

$TOOL = "/root/trafficcam_build/luckfox-pico/tools/linux/Linux_Upgrade_Tool/upgrade_tool"
$FLASH_DIR = "$env:USERPROFILE\LuckfoxFlash"
New-Item -ItemType Directory -Force $FLASH_DIR | Out-Null

# ── Step 1: Get image ──────────────────────────────────────────────────────
if ($ImagePath -eq "") {
    Write-Host "=== Fetching latest firmware from S3 ==="
    $latest = aws s3 cp "s3://$S3Bucket/latest.txt" - --region $Region 2>$null
    if (-not $latest) { Write-Host "ERROR: Cannot reach S3. Set -ImagePath manually."; exit 1 }
    $latest = $latest.Trim()
    $imgName = "luckfox-trafficcam-$latest.img"
    $ImagePath = "$FLASH_DIR\$imgName"
    if (-not (Test-Path $ImagePath)) {
        Write-Host "Downloading $imgName ..."
        aws s3 cp "s3://$S3Bucket/$latest/$imgName" $ImagePath --region $Region --no-progress
    } else {
        Write-Host "Using cached: $ImagePath"
    }
}

$IMG_WSL = "/mnt/" + ($ImagePath -replace '\\','/' -replace ':','').ToLower()
Write-Host "Image: $ImagePath"
Write-Host "WSL path: $IMG_WSL"

# ── Step 2: Start WSL2 ────────────────────────────────────────────────────
Write-Host "=== Starting WSL2 ==="
Start-Job { wsl -d Ubuntu-22.04 -- sleep 600 } | Out-Null
Start-Sleep -Seconds 5
if ((wsl -d Ubuntu-22.04 -- echo ok) -ne "ok") {
    Write-Host "ERROR: WSL2 Ubuntu-22.04 not available. Install it first."; exit 1
}
Write-Host "WSL2 ready."

# ── Step 3: MASKROM sequence ──────────────────────────────────────────────
Write-Host ""
Write-Host ">>> DO THE MASKROM SEQUENCE NOW:"
Write-Host "    1. Unplug the USB-C cable from the board"
Write-Host "    2. Press and HOLD the BOOT button"
Write-Host "    3. Plug USB-C back in while holding BOOT"
Write-Host "    4. Release BOOT after 2-3 seconds"
Write-Host ""
Write-Host "Waiting up to 60s for device..."

$busid = $null
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    $line = usbipd list 2>&1 | Where-Object { $_ -match "2207:(110B|110C|330C|350A|350B)" }
    if ($line) {
        $busid = ($line -split "\s+")[0].Trim()
        Write-Host "Device detected: $line"
        break
    }
    Start-Sleep -Milliseconds 400
}

if (-not $busid) {
    Write-Host "Timeout. Check USB connection and retry."
    Read-Host "Press Enter"; exit 1
}

# ── Step 4: Bind + Attach to WSL2 ────────────────────────────────────────
usbipd bind --busid $busid --force 2>&1 | Out-Null
Start-Sleep -Milliseconds 500
usbipd attach --wsl Ubuntu-22.04 --busid $busid 2>&1 | Out-Null
Start-Sleep -Seconds 3

# Watcher: re-attach on re-enumeration during flash
$watcher = Start-Job -ArgumentList $busid -ScriptBlock {
    param($bid)
    for ($i = 0; $i -lt 240; $i++) {
        $rk = usbipd list 2>&1 | Where-Object { $_ -match "$bid.*2207" -and $_ -notmatch "Attached" }
        if ($rk) { usbipd attach --wsl --busid $bid 2>&1 | Out-Null }
        Start-Sleep -Milliseconds 500
    }
}

# ── Step 5: Flash ─────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Flashing (3-5 min). DO NOT UNPLUG! ==="
wsl -d Ubuntu-22.04 -- $TOOL uf $IMG_WSL
$rc = $LASTEXITCODE

Stop-Job $watcher -ErrorAction SilentlyContinue
Remove-Job $watcher -ErrorAction SilentlyContinue
usbipd detach --busid $busid 2>&1 | Out-Null

if ($rc -ne 0) {
    Write-Host "ERROR: Flash failed (exit $rc)."
    Read-Host "Press Enter"; exit 1
}

# ── Step 6: Verify ────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Flash complete. Waiting for board to boot (~30s) ==="
$t = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $t) {
    $dev = adb devices 2>$null | Select-String "device$"
    if ($dev) { Write-Host "Board online: $dev"; break }
    Start-Sleep -Seconds 3
}

Write-Host ""
Write-Host "=== ALL DONE ==="
Write-Host "Board is flashed and online."
Read-Host "Press Enter to close"
