# flash.ps1 - Flash Luckfox Pico Ultra via MASKROM + usbipd + WSL2
# Auto-downloads the latest firmware from S3 (versioned as DDMMYY_VN).
# Run in an ADMIN PowerShell window:
#   powershell -ExecutionPolicy Bypass -File flash.ps1
#
# Optional overrides:
#   -Version   "310526_V2"              flash a specific S3 version
#   -ImagePath "C:\path\to\image.img"   flash a local image (skips S3)

param(
    [string]$ImagePath = "",
    [string]$Version   = "",
    [string]$S3Bucket  = "luckfox-firmware-img",
    [string]$Region    = "ap-south-1"
)

$WSL_TOOL   = "/root/trafficcam_build/luckfox-pico/tools/linux/Linux_Upgrade_Tool/upgrade_tool"
$FLASH_DIR  = "$env:USERPROFILE\LuckfoxFlash"
$WSL_DISTRO = "Ubuntu-22.04"
New-Item -ItemType Directory -Force $FLASH_DIR | Out-Null

# Helper: run an aws s3 cp command through WSL2 (no Windows AWS CLI needed)
function Wsl-S3Copy {
    param([string]$Src, [string]$Dst, [switch]$NoProgress)
    $np = if ($NoProgress) { "--no-progress" } else { "" }
    wsl -d $WSL_DISTRO -u root -- python3 -m awscli s3 cp $Src $Dst --region $Region $np
    return $LASTEXITCODE
}

# Helper: convert a Windows path to WSL /mnt/... path
function To-WslPath([string]$p) {
    # Convert C:\path\to\file -> /mnt/c/path/to/file
    $p = $p -replace '\\', '/'          # backslash to forward slash
    $p = $p -replace '^([A-Za-z]):', { '/mnt/' + $_.Groups[1].Value.ToLower() }
    return $p
}

# ?? Step 1: Resolve image ?????????????????????????????????????????????????
if ($ImagePath -eq "") {
    Write-Host "=== Fetching firmware from S3 ==="

    if ($Version -eq "") {
        $Version = (wsl -d $WSL_DISTRO -u root -- python3 -m awscli s3 cp `
            "s3://$S3Bucket/latest.txt" - --region $Region 2>$null).Trim()
        if (-not $Version) {
            Write-Host "ERROR: Cannot reach S3. Check WSL2 AWS credentials or use -ImagePath."
            Write-Host "       Run: wsl -d Ubuntu-22.04 -u root -- python3 -m awscli configure"
            Read-Host "Press Enter"; exit 1
        }
    }

    Write-Host "Version: $Version"
    $ImgName   = "luckfox-trafficcam-$Version.img"
    $ImagePath = "$FLASH_DIR\$ImgName"

    if (-not (Test-Path $ImagePath)) {
        Write-Host "Downloading $ImgName (~524 MB) ..."
        $WslDst = To-WslPath $ImagePath
        $rc = Wsl-S3Copy "s3://$S3Bucket/$Version/$ImgName" $WslDst -NoProgress
        if ($rc -ne 0) { Write-Host "ERROR: Download failed."; Read-Host "Press Enter"; exit 1 }
    } else {
        Write-Host "Using cached: $ImagePath"
    }
}

$IMG_WSL = To-WslPath $ImagePath
Write-Host "Image:    $ImagePath"
Write-Host "WSL path: $IMG_WSL"

# ?? Step 2: Start WSL2 ????????????????????????????????????????????????????
Write-Host "`n=== Starting WSL2 ==="
Start-Job { wsl -d Ubuntu-22.04 -- sleep 600 } | Out-Null
Start-Sleep -Seconds 5
if ((wsl -d $WSL_DISTRO -- echo ok) -ne "ok") {
    Write-Host "ERROR: WSL2 $WSL_DISTRO not available. Run: wsl --install -d Ubuntu-22.04"
    Read-Host "Press Enter"; exit 1
}
Write-Host "WSL2 ready."

# ?? Step 3: MASKROM sequence ??????????????????????????????????????????????
Write-Host ""
Write-Host ">>> DO THE MASKROM SEQUENCE NOW:"
Write-Host "    1. Unplug the USB-C cable from the board"
Write-Host "    2. Press and HOLD the BOOT button"
Write-Host "    3. Plug USB-C back in while holding BOOT"
Write-Host "    4. Release BOOT after 2-3 seconds"
Write-Host ""
Write-Host "Waiting up to 60s for device..."

$busid    = $null
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
    Write-Host "Timeout. Check USB connection and retry the MASKROM sequence."
    Read-Host "Press Enter"; exit 1
}

# ?? Step 4: Bind + Attach to WSL2 ????????????????????????????????????????
usbipd bind   --busid $busid --force 2>&1 | Out-Null
Start-Sleep -Milliseconds 500
usbipd attach --wsl $WSL_DISTRO --busid $busid 2>&1 | Out-Null
Start-Sleep -Seconds 3

# Watcher: re-attach on re-enumeration during multi-stage flash
$watcher = Start-Job -ArgumentList $busid, $WSL_DISTRO -ScriptBlock {
    param($bid, $distro)
    for ($i = 0; $i -lt 240; $i++) {
        $rk = usbipd list 2>&1 | Where-Object { $_ -match "$bid.*2207" -and $_ -notmatch "Attached" }
        if ($rk) { usbipd attach --wsl $distro --busid $bid 2>&1 | Out-Null }
        Start-Sleep -Milliseconds 500
    }
}

# ?? Step 5: Flash ?????????????????????????????????????????????????????????
Write-Host ""
Write-Host "=== Flashing (3-5 min). DO NOT UNPLUG! ==="
wsl -d $WSL_DISTRO -u root -- $WSL_TOOL uf $IMG_WSL
$rc = $LASTEXITCODE

Stop-Job  $watcher -ErrorAction SilentlyContinue
Remove-Job $watcher -ErrorAction SilentlyContinue
usbipd detach --busid $busid 2>&1 | Out-Null

if ($rc -ne 0) {
    Write-Host "ERROR: Flash failed (exit $rc). Check dmesg or retry."
    Read-Host "Press Enter"; exit 1
}

# ?? Step 6: Wait for board ????????????????????????????????????????????????
Write-Host ""
Write-Host "=== Flash complete. Waiting for board to boot (~30s) ==="
$t = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $t) {
    $dev = adb devices 2>$null | Select-String "device$"
    if ($dev) { Write-Host "Board online: $dev"; break }
    Start-Sleep -Seconds 3
}

Write-Host ""
Write-Host "=== DONE - Board flashed with $Version ==="
Read-Host "Press Enter to close"
