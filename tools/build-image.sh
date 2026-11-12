#!/usr/bin/env bash
set -euo pipefail
project="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
docker build --progress=plain -t "${1:-whisper-subtitles:rocm}" \
  -f "$project/deployment/Dockerfile" "$project/deployment"
