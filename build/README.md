# Build System

The build system compiles the recorder binary, applies overlay files, repackages the firmware image, and publishes it to S3.

---

## How It Works

Every push to `main` touching these paths triggers a GitHub Actions build:

```
overlay/**
recorder/**
flash/**
build/build_overlay.sh
build/upload_s3.sh
```

The build runs on a **self-hosted runner** (WSL2 Ubuntu-22.04 on the development machine) because the Luckfox SDK is ~2 GB and cannot fit on GitHub-hosted runners.

### Build steps

1. `build_overlay.sh` — compiles the recorder, applies all overlay files to the SDK rootfs, runs `build.sh firmware` to produce `update.img`
2. `upload_s3.sh` — determines the next version number (`DDMMYY_VN`), uploads the image and flash scripts, updates `latest.txt`

---

## First-Time Runner Setup

Run this **once** in WSL2 to register the machine as a GitHub Actions runner:

```bash
# 1. Get a runner token from GitHub
#    Go to: https://github.com/Tadipartirohith/luckfox-trafficcam/settings/actions/runners/new
#    Copy the token shown on that page

# 2. Configure the runner (already downloaded to /root/actions-runner/)
cd /root/actions-runner
RUNNER_ALLOW_RUNASROOT=1 ./config.sh \
  --url https://github.com/Tadipartirohith/luckfox-trafficcam \
  --token <PASTE_TOKEN_HERE> \
  --name wsl2-luckfox-builder \
  --labels self-hosted,linux,luckfox \
  --unattended

# 3. Start the runner
RUNNER_ALLOW_RUNASROOT=1 nohup ./run.sh > /tmp/runner.log 2>&1 &
```

The runner must be running whenever you want CI builds to execute.
Check status: `tail -5 /tmp/runner.log`

---

## Triggering a Build Manually

Via GitHub UI: go to **Actions → Build & Publish Firmware → Run workflow**.

Via CLI (from WSL2):
```bash
curl -X POST \
  -H "Authorization: token <GITHUB_PAT>" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/Tadipartirohith/luckfox-trafficcam/actions/workflows/build.yml/dispatches" \
  -d '{"ref":"main"}'
```

---

## GitHub Secrets

Set these under **Settings → Secrets and variables → Actions**:

| Secret | Description |
|--------|-------------|
| `AWS_ACCESS_KEY_ID` | AWS access key for S3 |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key |
| `AWS_REGION` | `ap-south-1` |
| `S3_BUCKET` | Video upload bucket (`traf-test`) |
| `S3_BUCKET_FIRMWARE` | Firmware bucket (`luckfox-firmware-img`) |

---

## Build Scripts

| Script | Purpose |
|--------|---------|
| `build_overlay.sh` | Compile recorder, apply overlay, repackage firmware |
| `upload_s3.sh` | Version and upload image + flash scripts to S3 |
| `setup_runner.sh` | Helper to register WSL2 as GitHub Actions runner |
| `setup_vm.sh` | Install build dependencies in a fresh Ubuntu VM |

### Running a build locally (WSL2)

```bash
cd /root/luckfox-trafficcam
export SDK_DIR=/root/trafficcam_build/luckfox-pico
export OUT_DIR=/tmp/firmware-out
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=ap-south-1
export S3_BUCKET=traf-test
bash build/build_overlay.sh
```

---

## S3 Bucket Layout

```
s3://luckfox-firmware-img/
  DDMMYY_V1/
    luckfox-trafficcam-DDMMYY_V1.img   (firmware image ~524 MB)
    flash.ps1                            (Windows flash script)
    flash.sh                             (Linux/Mac flash script)
    build-info.json                      (git SHA, branch, date)
  DDMMYY_V2/
    ...
  latest.txt                             (contains the latest folder name)
  latest-build-info.json
```

Version naming: `DDMMYY_VN` where DD/MM/YY is the build date and N increments each build.
New builds never overwrite old ones.
