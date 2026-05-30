# Flashing the Luckfox Pico Ultra

This folder contains the one-click flash scripts for Windows (`flash.ps1`) and Linux/Mac (`flash.sh`).
Both scripts auto-download the latest firmware from S3 and handle the full MASKROM flashing sequence.

---

## Prerequisites

Install these on your Windows machine before flashing:

| Tool | Purpose | Install |
|------|---------|---------|
| **AWS CLI** | Download firmware from S3 | `winget install Amazon.AWSCLI` or [aws.amazon.com/cli](https://aws.amazon.com/cli/) |
| **usbipd-win** | Pass USB device to WSL2 | `winget install usbipd` or [github.com/dorssel/usbipd-win](https://github.com/dorssel/usbipd-win) |
| **WSL2 Ubuntu-22.04** | Runs the Luckfox upgrade tool | `wsl --install -d Ubuntu-22.04` |

WSL2 must have the Luckfox SDK at `/root/trafficcam_build/luckfox-pico/` (the upgrade tool lives inside it).

---

## Step-by-Step: Flash on Windows

### 1. Download flash.ps1 from S3

Open any PowerShell window (does not need to be admin yet):

```powershell
# Always downloads the latest version
$v = (aws s3 cp s3://luckfox-firmware-img/latest.txt - --region ap-south-1).Trim()
aws s3 cp "s3://luckfox-firmware-img/$v/flash.ps1" "$env:USERPROFILE\Desktop\flash.ps1" --region ap-south-1
Write-Host "Downloaded version: $v"
```

### 2. Open an Administrator PowerShell

Press **Win + S**, type `PowerShell`, right-click **Windows PowerShell** → **Run as administrator**, accept the UAC prompt.

### 3. Run the flash script

In the admin PowerShell window:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Desktop\flash.ps1"
```

The script will:
- Download the firmware image from S3 (~524 MB, cached after first download)
- Start WSL2 automatically
- Prompt you to do the MASKROM sequence

### 4. MASKROM sequence

When the script prints **"DO THE MASKROM SEQUENCE NOW"**:

1. **Unplug** the USB-C cable from the board
2. **Press and hold** the small **BOOT** button on the board
3. **Plug the USB-C cable back in** while still holding BOOT
4. **Release BOOT** after 2–3 seconds

The script detects the Rockchip USB device automatically and begins flashing.

> **Do not unplug** during flashing. It takes 3–5 minutes.

### 5. Done

The script prints `DONE — Board flashed with DDMMYY_VN` and confirms the board is online via ADB.
No further manual steps are needed — the board is fully operational immediately after reboot.

---

## Flashing a Specific Version

To flash a specific firmware build instead of the latest:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Desktop\flash.ps1" -Version "310526_V1"
```

To use a firmware image you already have locally:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Desktop\flash.ps1" -ImagePath "C:\path\to\image.img"
```

---

## S3 Firmware Versions

Firmware images are stored at:
```
s3://luckfox-firmware-img/
  DDMMYY_V1/          ← e.g. 310526_V1 = first build on 31 May 2026
    luckfox-trafficcam-310526_V1.img
    flash.ps1
    flash.sh
    build-info.json
  DDMMYY_V2/          ← second build on the same day
  latest.txt          ← always contains the folder name of the newest build
  latest-build-info.json
```

Each build gets a new version folder. Old builds are never overwritten.

To see all available versions:
```powershell
aws s3 ls s3://luckfox-firmware-img/ --region ap-south-1
```

To check what version is currently latest:
```powershell
aws s3 cp s3://luckfox-firmware-img/latest.txt - --region ap-south-1
```

---

## Flash on Linux / Mac

```bash
# Download and run (uses rkdeveloptool)
VERSION=$(aws s3 cp s3://luckfox-firmware-img/latest.txt - --region ap-south-1 | tr -d '[:space:]')
aws s3 cp "s3://luckfox-firmware-img/$VERSION/flash.sh" ./flash.sh --region ap-south-1
chmod +x flash.sh
./flash.sh
```

`rkdeveloptool` must be installed: [github.com/rockchip-linux/rkdeveloptool](https://github.com/rockchip-linux/rkdeveloptool)

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Script closes immediately | Run via admin PowerShell with `-ExecutionPolicy Bypass`, not by double-clicking |
| "Cannot reach S3" | Check AWS CLI is installed and `aws configure` has been run |
| Device not detected in 60s | Retry the MASKROM sequence — hold BOOT before plugging in |
| "WSL2 not available" | Run `wsl --install -d Ubuntu-22.04` then restart |
| Flash fails at "Test Device" | Board re-enumerated mid-flash; re-run the script (board stays in loader mode) |
