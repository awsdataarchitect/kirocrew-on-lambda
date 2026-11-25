#!/usr/bin/env bash
# Assemble webfn/dist: the Python app, the Linux arm64 OpenCode binary and boto3 for Python 3.15 (Web Functions run arm64 only).
# Needs: node/npm (npm pack), uv. Output is ignored by git; deploy.sh runs this first.
set -euo pipefail
cd "$(dirname "$0")"
OPENCODE_VERSION=${OPENCODE_VERSION:-1.19.5}   # KiroCrew launches OpenCode on major 1 only
rm -rf dist && mkdir -p dist/bin
cp app/main.py dist/main.py
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
(cd "$tmp" && npm pack --silent "opencode-linux-arm64@${OPENCODE_VERSION}" >/dev/null && tar -xzf ./*.tgz)
cp "$tmp/package/bin/opencode" dist/bin/opencode
chmod 755 dist/bin/opencode
uv pip install -q --python-version 3.15 --python-platform aarch64-manylinux_2_28 --only-binary :all: --target dist boto3
echo "webfn/dist ready: $(du -sm dist | cut -f1) MB unzipped (limit 512 MB)"
