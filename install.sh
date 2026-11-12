#!/usr/bin/env bash
set -euo pipefail
project="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Keep the existing environment and all user configuration / task state.
if [[ ! -x "$project/.venv/bin/python" ]]; then
  python3 -m venv "$project/.venv"
fi
wheel="$project/dist/srtflow-0.3.0-py3-none-any.whl"
if [[ ! -f "$wheel" ]]; then
  echo "缺少安装包：$wheel" >&2
  exit 1
fi
if "$project/.venv/bin/python" -c 'import rich' 2>/dev/null; then
  "$project/.venv/bin/python" -m pip install --no-index --no-deps --force-reinstall "$wheel"
else
  "$project/.venv/bin/python" -m pip install "$wheel"
fi
mkdir -p "$HOME/.local/bin"
printf '#!/usr/bin/env bash\nexec %q "$@"\n' "$project/.venv/bin/srtflow" > "$HOME/.local/bin/srtflow"
chmod +x "$HOME/.local/bin/srtflow"
echo "SRTFlow 0.3 已安装：$HOME/.local/bin/srtflow"
echo "配置、凭据、任务检查点和 ROCm 镜像均沿用现有内容。"
echo "启动：$HOME/.local/bin/srtflow"
