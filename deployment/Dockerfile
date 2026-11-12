FROM rocm/dev-ubuntu-24.04:7.2.4-complete

ENV DEBIAN_FRONTEND=noninteractive
ENV PATH="/venv/bin:${PATH}"
ENV CT2_CUDA_ALLOCATOR=cub_caching
ENV HF_HOME=/cache/huggingface
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3-venv ffmpeg curl ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /venv \
    && python -m pip install --upgrade pip \
    && python -m pip install "faster-whisper==1.2.1"

WORKDIR /tmp/ct2

RUN curl -fL --retry 3 \
    https://github.com/OpenNMT/CTranslate2/releases/download/v4.8.5/rocm-python-wheels-Linux.zip \
    -o wheels.zip

RUN python - <<'PY'
from pathlib import Path
from zipfile import ZipFile
import subprocess
import sys

with ZipFile("wheels.zip") as archive:
    matches = [
        name for name in archive.namelist()
        if Path(name).name.startswith("ctranslate2-")
        and "-cp312-cp312-" in name
        and "manylinux" in name
        and name.endswith("x86_64.whl")
    ]
    if len(matches) != 1:
        raise SystemExit(f"无法唯一确定 Python 3.12 wheel：{matches}")
    name = matches[0]
    wheel = Path(name).name
    Path(wheel).write_bytes(archive.read(name))
    subprocess.check_call([
        sys.executable, "-m", "pip", "install",
        "--force-reinstall", "--no-deps", wheel
    ])
PY

RUN python -m pip check

WORKDIR /work
CMD ["bash"]

RUN python -m pip install --only-binary=:all: --no-deps "av==18.1.0" \
    && python -m pip check
