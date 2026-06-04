#!/usr/bin/env bash
# Regenerate Plan Change PNGs from .mmd sources (requires Node/npx).
set -euo pipefail
cd "$(dirname "$0")"
for f in *.mmd; do
  n="${f%.mmd}"
  npx -y @mermaid-js/mermaid-cli@11 -p ../puppeteer-config.json -i "$f" -o "../plan_change_${n}.png" -b transparent
done
echo "Wrote plan_change_*.png in $(dirname "$PWD")"
