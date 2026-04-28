#!/usr/bin/env bash
# Render.com build script
# Installs tesseract-ocr (system binary) then Python packages.
# Referenced in render.yaml as: buildCommand: bash build.sh

set -e

echo "=== Installing system packages ==="
apt-get update -qq
apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    libglib2.0-0 \
    libgl1

echo "=== Tesseract version ==="
tesseract --version

echo "=== Installing Python packages ==="
pip install --upgrade pip
pip install -r requirements.txt

echo "=== Build complete ==="