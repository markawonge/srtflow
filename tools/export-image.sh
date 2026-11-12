#!/usr/bin/env bash
set -euo pipefail
destination="${1:-$HOME/srtflow-migration}"
image="${2:-whisper-subtitles:rocm}"
mkdir -p -- "$destination"
archive="$destination/whisper-subtitles-rocm.tar.gz"
[[ ! -e "$archive" && ! -e "$archive.partial" ]] || { echo "目标已存在：$archive 或 .partial，请选择其他目录。" >&2; exit 1; }
docker image inspect "$image" --format '{{.Id}}' > "$destination/image-id.txt"
docker save "$image" | gzip > "$archive.partial"
mv -- "$archive.partial" "$archive"
(cd -- "$destination" && sha256sum whisper-subtitles-rocm.tar.gz > SHA256SUMS)
echo "镜像及校验文件已保存到：$destination"
