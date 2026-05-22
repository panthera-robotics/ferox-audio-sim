#!/usr/bin/env bash
# Build the ferox-audio-sim Docker image (host-side audio bridge).
#
# Usage:
#   ./scripts/build.sh
#
# Produces the image  ferox/audio_sim:humble  from docker/Dockerfile.
# Start it afterwards with  ./scripts/start.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

IMAGE="ferox/audio_sim:humble"

echo "Building ${IMAGE} from docker/Dockerfile ..."
time docker build -f docker/Dockerfile -t "${IMAGE}" .

echo ""
echo "Built ${IMAGE}. Start the bridge with:"
echo "    ./scripts/start.sh"
