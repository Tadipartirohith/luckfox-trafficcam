#!/bin/bash
# setup_runner.sh — Register WSL2 Ubuntu as GitHub Actions self-hosted runner
# Run once in WSL2 to set up the build pipeline.
# Usage: bash build/setup_runner.sh <GITHUB_REPO_URL> <RUNNER_TOKEN>
# Get token: https://github.com/Tadipartirohith/luckfox-trafficcam/settings/actions/runners/new
set -euo pipefail

REPO_URL=${1:-"https://github.com/Tadipartirohith/luckfox-trafficcam"}
TOKEN=${2:-""}
RUNNER_DIR=/root/actions-runner
RUNNER_VERSION=2.317.0

if [ -z "$TOKEN" ]; then
    echo "Usage: bash setup_runner.sh <REPO_URL> <RUNNER_TOKEN>"
    echo "Get token from: $REPO_URL/settings/actions/runners/new"
    exit 1
fi

echo "=== Installing GitHub Actions runner $RUNNER_VERSION ==="
mkdir -p $RUNNER_DIR
cd $RUNNER_DIR

ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ]; then
    RUNNER_PKG="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
elif [ "$ARCH" = "aarch64" ]; then
    RUNNER_PKG="actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz"
else
    echo "Unsupported architecture: $ARCH"; exit 1
fi

if [ ! -f "$RUNNER_DIR/run.sh" ]; then
    echo "Downloading runner package..."
    curl -sLO "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_PKG}"
    tar xzf "$RUNNER_PKG"
    rm "$RUNNER_PKG"
fi

echo "=== Configuring runner ==="
./config.sh \
    --url "$REPO_URL" \
    --token "$TOKEN" \
    --name "wsl2-luckfox-builder" \
    --labels "self-hosted,linux,luckfox" \
    --work "/root/actions-runner/_work" \
    --unattended \
    --replace

echo ""
echo "=== Runner configured. ==="
echo "To start now (foreground): cd $RUNNER_DIR && ./run.sh"
echo ""
echo "To start as background service (recommended):"
echo "  cd $RUNNER_DIR && ./svc.sh install && ./svc.sh start"
echo ""
echo "The runner will auto-start on WSL2 launch."
