#!/bin/bash
# setup_vm.sh — One-time setup for WSL2 Ubuntu-22.04 or a fresh Ubuntu VM
# Run once before your first build.
set -euo pipefail

echo "=== Setting up Luckfox build environment ==="

# System packages
sudo apt-get update -qq
sudo apt-get install -y \
    gcc g++ gcc-12 g++-12 \
    make cmake git wget curl \
    python3 python3-pip \
    libssl-dev libncurses5-dev \
    libcrypt-dev pkg-config \
    bison flex texinfo gawk \
    lib32gcc-s1 lib32z1 \
    awscli

sudo update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-12 12
sudo update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-12 12

# Fix e2fsprogs MKDIR_P issue
sudo sed -i 's/^MKDIR_P = \/usr\/bin\/install -c -d/MKDIR_P = mkdir -p/' \
    /usr/share/automake-1.16/am/mkdir.am 2>/dev/null || true

# Clone Luckfox SDK
SDK_DIR=/root/trafficcam_build/luckfox-pico
if [ ! -d "$SDK_DIR" ]; then
    mkdir -p /root/trafficcam_build
    cd /root/trafficcam_build
    git clone https://github.com/luckfox-eng/luckfox-pico.git --depth=1
fi

# Select board config
cd $SDK_DIR
echo "luckfox_pico_ultra_defconfig" > project/.board_env

echo "=== Setup complete. Run 'bash build/build_overlay.sh' to build. ==="
