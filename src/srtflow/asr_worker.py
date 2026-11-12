"""Standalone container script: standard library + existing faster-whisper only."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile


def emit(kind, **values):
    print("SRTFLOW_EVENT "+json.dumps({"kind": kind, **values}, ensure_ascii=False), flush=True)


def atomic(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".asr-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False)
            file.flush(); os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def timestamp(seconds):
    ms=max(0,round(seconds*1000));hours,ms=divmod(ms,3600000)
    minutes,ms=divmod(ms,60000);seconds,ms=divmod(ms,1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--input",required=True)
    parser.add_argument("--job",required=True)
    args=parser.parse_args()
    job=Path(args.job)
    settings=json.loads((job/"asr-settings.json").read_text())
    fingerprint=hashlib.sha256(json.dumps(settings,sort_keys=True).encode()).hexdigest()
    checkpoint=job/"asr-checkpoint.json"
    state=json.loads(checkpoint.read_text()) if checkpoint.exists() else {"fingerprint":fingerprint,"next_region":0,"cues":[],"language":None}
    if state["fingerprint"]!=fingerprint:
        raise RuntimeError("转写输入或参数发生变化，不可混用检查点")
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import get_speech_timestamps,VadOptions
    cfg=settings["config"];rate=16000
    emit("stage",stage="音频解码（无可靠百分比）")
    audio=decode_audio(args.input,sampling_rate=rate)
    if not len(audio):raise RuntimeError("音频为空")
    duration=len(audio)/rate
    emit("stage",stage="VAD扫描（无可靠百分比）" if cfg["vad"] else "按固定时长建立语音候选区间")
    if cfg["vad"]:
        regions=get_speech_timestamps(audio,vad_options=VadOptions(
            threshold=cfg["threshold"],min_speech_duration_ms=cfg["min_speech_duration_ms"],
            min_silence_duration_ms=cfg["min_silence_duration_ms"],speech_pad_ms=cfg["speech_pad_ms"],
            max_speech_duration_s=cfg["max_speech_duration_s"]),sampling_rate=rate)
    else:
        size=int(cfg["max_speech_duration_s"]*rate)
        regions=[{"start":i,"end":min(i+size,len(audio))} for i in range(0,len(audio),size)]
    if not regions:raise RuntimeError("未检测到可转写语音，未生成空原文字幕；可降低VAD阈值后新建任务")
    region_sha=hashlib.sha256(json.dumps(regions,sort_keys=True).encode()).hexdigest()
    if state.get("regions_sha") and state["regions_sha"]!=region_sha:
        raise RuntimeError("重算的VAD区间与检查点不同，禁止续写")
    state["regions_sha"]=region_sha
    if not 0<=state["next_region"]<=len(regions):raise RuntimeError("转写检查点无效")
    total=sum(r["end"]-r["start"] for r in regions)/rate
    done=sum(r["end"]-r["start"] for r in regions[:state["next_region"]])/rate
    emit("asr_progress",done=done,total=total,full_duration=duration,regions=len(regions))
    emit("stage",stage="加载转写模型（无可靠百分比）")
    model=WhisperModel(cfg["model"],device="cuda",compute_type=cfg["compute_type"])
    language=state["language"] or settings["language"]
    if language=="auto":
        emit("stage",stage="文件级原音自动检测")
        import numpy as np
        sample=np.concatenate([audio[r["start"]:r["end"]] for r in regions[:20]])[:30*rate]
        _,info=model.transcribe(sample,language=None,condition_on_previous_text=False,vad_filter=False,word_timestamps=False)
        language=info.language
        if language not in {"ja","en"}:
            raise RuntimeError(f"自动检测为 {language}；当前翻译支持英/日，请核对后手动指定语言新建任务")
    state["language"]=language
    atomic(checkpoint,state)
    emit("language",language=language)
    for index in range(state["next_region"],len(regions)):
        region=regions[index];begin,finish=region["start"],region["end"]
        offset=begin/rate;length=(finish-begin)/rate
        segments,_=model.transcribe(audio[begin:finish],language=language,task="transcribe",
            beam_size=cfg["beam_size"],temperature=cfg["temperature"],condition_on_previous_text=cfg["condition_on_previous_text"],
            vad_filter=False,word_timestamps=False,no_speech_threshold=cfg["no_speech_threshold"],
            log_prob_threshold=cfg["log_prob_threshold"],compression_ratio_threshold=cfg["compression_ratio_threshold"])
        new=[]
        for segment in segments:
            text=" ".join(segment.text.split())
            if not math.isfinite(segment.start) or not math.isfinite(segment.end):continue
            start=max(0.,segment.start)+offset;end=min(length,segment.end)+offset
            if text and round(end*1000)>round(start*1000):new.append({"start":start,"end":end,"text":text})
        # Commit one complete region only; interrupted regions are rerun in full.
        state["cues"].extend(new);state["next_region"]=index+1
        atomic(checkpoint,state)
        done+=(finish-begin)/rate
        emit("asr_progress",done=done,total=total,full_duration=duration,regions=len(regions))
    if not state["cues"]:raise RuntimeError("没有有效字幕，未发布空原文字幕")
    content="".join(f"{i}\n{timestamp(c['start'])} --> {timestamp(c['end'])}\n{c['text']}\n\n" for i,c in enumerate(state["cues"],1))
    temporary=job/"source.srt.tmp"
    temporary.write_text(content,encoding="utf-8")
    os.replace(temporary,job/"source.srt")
    atomic(job/"asr-result.json",{"language":language,"count":len(state["cues"]),"duration":duration})
    emit("asr_ready",language=language,count=len(state["cues"]))


if __name__=="__main__":
    try:main()
    except Exception as error:
        emit("asr_error",message=f"{type(error).__name__}: {error}")
        raise SystemExit(1)
