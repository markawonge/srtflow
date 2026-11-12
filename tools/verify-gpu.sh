#!/usr/bin/env bash
set -euo pipefail
image="${1:-whisper-subtitles:rocm}"
[[ -e /dev/kfd && -d /dev/dri ]] || { echo '需要已正常工作的 ROCm 设备 /dev/kfd 和 /dev/dri。' >&2; exit 1; }
shopt -s nullglob
render_devices=(/dev/dri/renderD*)
(( ${#render_devices[@]} > 0 )) || { echo '未找到 renderD 设备。' >&2; exit 1; }
args=(--rm -i --user "$(id -u):$(id -g)" --device=/dev/kfd --device=/dev/dri)
args+=(--group-add "$(stat -c '%g' /dev/kfd)")
for device in "${render_devices[@]}"; do args+=(--group-add "$(stat -c '%g' "$device")"); done
docker run "${args[@]}" "$image" python - <<'PY'
import sys
import av
import ctranslate2
from faster_whisper.vad import get_vad_model
print('Python:',sys.version.split()[0])
print('CTranslate2:',ctranslate2.__version__)
print('PyAV:',av.__version__)
count=ctranslate2.get_cuda_device_count()
print('GPU 数量:',count)
if count < 1: raise SystemExit('需要至少一个可用 GPU')
types=ctranslate2.get_supported_compute_types('cuda',0)
print('支持的计算类型:',sorted(types))
if 'float16' not in types: raise SystemExit('当前后端未提供 float16')
get_vad_model()
print('Silero VAD 加载成功')
PY
