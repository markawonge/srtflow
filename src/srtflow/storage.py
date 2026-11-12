import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home()/".config")))/"srtflow"


def state_dir():
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home()/".local/state")))/"srtflow"


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_json(path, data):
    private_dir(path.parent)
    fd, temp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def sha(data):
    if isinstance(data, bytes):
        return hashlib.sha256(data).hexdigest()
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(8*1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextlib.contextmanager
def lock(path):
    private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("任务或输出正在被另一个进程使用") from None
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


DEFAULTS = {
    "target_language": "zh-Hans", "output_mode": "beside", "existing_policy": "skip",
    "source_policy": "ask", "background": True, "glossary_path": "",
    "default_engine": "deepl", "source_language": "ja", "input_dir": "",
    "output_dir": "", "recursive": False, "retries": 3, "timeout": 60,
    "batch_items": 20, "batch_chars": 4000, "context_items": 3,
    "request_interval": 1.0, "providers": {},
    "transcription": {
        "image": "whisper-subtitles:rocm", "model": "large-v3", "compute_type": "float16",
        "vad": True, "threshold": 0.6, "min_speech_duration_ms": 250,
        "min_silence_duration_ms": 700, "speech_pad_ms": 300, "max_speech_duration_s": 25,
        "beam_size": 5, "temperature": 0.0, "condition_on_previous_text": False,
        "word_timestamps": False, "no_speech_threshold": 0.6,
        "log_prob_threshold": -1.0, "compression_ratio_threshold": 2.4,
        "cache_dir": "~/whisper-subtitles/cache",
    },
}


def load_config():
    saved = read_json(config_dir()/"config.json", {})
    result = copy.deepcopy(DEFAULTS) | saved
    result["transcription"] = copy.deepcopy(DEFAULTS["transcription"]) | saved.get("transcription", {})
    return result


def load_secrets():
    path = config_dir()/"secrets.json"
    if path.exists() and path.stat().st_mode & 0o077:
        raise RuntimeError(f"凭据文件权限过宽，请执行 chmod 600 {path}")
    return read_json(path, {})
