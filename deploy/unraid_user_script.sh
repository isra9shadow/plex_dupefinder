#!/bin/bash
#
# Example Unraid User Script — the SINGLE entrypoint for the izumi platform.
# Install one User Script per pipeline and set its schedule in the User Scripts UI:
#   monitor → hourly   ·   daily → daily (off-peak)   ·   weekly → weekly
#
# Adjust REPO to wherever the repo is cloned on the host (see homelab-infra
# convention: /mnt/cache/repos/).
set -uo pipefail

REPO="/mnt/cache/repos/izumi"
PIPELINE="${1:-daily}"

"$REPO/deploy/run.sh" "$PIPELINE" --config "$REPO/config/config.json"
