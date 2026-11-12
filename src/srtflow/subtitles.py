from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class Cue:
    number: str
    timing: str
    text: str


TIMING = re.compile(r"^(\d{2,}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2,}:\d{2}:\d{2},\d{3})(?:\s+.*)?$")


def millis(value):
    hours, minutes, rest = value.split(":")
    seconds, ms = rest.split(",")
    if int(minutes) >= 60 or int(seconds) >= 60:
        raise ValueError("时间戳的分或秒超出范围")
    return ((int(hours)*60+int(minutes))*60+int(seconds))*1000+int(ms)


def parse(text):
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("SRT 为空")
    cues, seen = [], set()
    for block in re.split(r"\n[ \t]*\n", text.strip()):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].isdigit():
            raise ValueError("SRT 条目缺少数字编号、时间轴或正文")
        match = TIMING.fullmatch(lines[1])
        if not match or millis(match[2]) <= millis(match[1]):
            raise ValueError(f"第 {lines[0]} 条时间轴无效")
        if int(lines[0]) in seen:
            raise ValueError(f"重复字幕编号：{lines[0]}")
        seen.add(int(lines[0]))
        content = "\n".join(lines[2:]).strip()
        if not content:
            raise ValueError(f"第 {lines[0]} 条正文为空")
        cues.append(Cue(lines[0], lines[1], content))
    return cues


def render(cues, translations):
    if len(cues) != len(translations):
        raise ValueError("译文条数与原文不一致")
    blocks = []
    for cue, text in zip(cues, translations):
        if not isinstance(text, str) or not text.strip() or "\n\n" in text or "\r" in text or "\x00" in text:
            raise ValueError("译文为空或含破坏 SRT 结构的字符")
        blocks.append(f"{cue.number}\n{cue.timing}\n{text.strip()}\n\n")
    result = "".join(blocks)
    check = parse(result)
    if [(c.number, c.timing) for c in check] != [(c.number, c.timing) for c in cues]:
        raise ValueError("导出改变了编号或时间轴")
    return result


def infer_language(path):
    suffix = Path(path).stem.rsplit(".", 1)[-1].lower()
    return {"jp": "ja", "ja": "ja", "en": "en"}.get(suffix)


def output_name(path, confirmed_base=None):
    stem = Path(path).stem
    last = stem.rsplit(".", 1)[-1].lower()
    if last in {"jp", "ja", "en"}:
        stem = stem.rsplit(".", 1)[0]
    elif confirmed_base:
        stem = confirmed_base
    else:
        raise ValueError("缺少可靠语言后缀，需要确认中文输出文件名")
    if not stem or Path(stem).name != stem or stem in {".", ".."}:
        raise ValueError("输出名称无效")
    return stem+".srt"


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".ts", ".mts", ".m2ts", ".wmv", ".mpg", ".mpeg"}


def discover(paths, recursive=False, extensions=None):
    extensions = extensions or {".srt"}
    result = {}
    def visible(path):
        return not path.name.startswith((".", "~")) and not path.stem.lower().endswith((".tmp", ".temp", ".part", ".partial", ".download", ".crdownload"))
    def visit(directory):
        for path in sorted(directory.iterdir()):
            if not visible(path):
                continue
            if path.is_file() and path.suffix.lower() in extensions:
                result[path.resolve()] = None
            elif recursive and path.is_dir() and not path.is_symlink():
                visit(path)
    for value in paths:
        path = Path(value).expanduser().resolve(strict=True)
        if path.is_dir():
            visit(path)
        elif path.is_file() and path.suffix.lower() in extensions and visible(path):
            result[path] = None
        else:
            raise ValueError(f"不是可用的目标文件或目录：{path}")
    return sorted(result, key=str)
