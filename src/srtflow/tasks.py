from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import time
import uuid

from .backends import APIError, Backend
from .storage import lock, private_dir, read_json, write_json, state_dir, sha, load_secrets, file_sha
from .subtitles import parse, render

RULES = "dialogue-zh-hans-v1"


def now():
    return datetime.now(timezone.utc).isoformat()


def task_root():
    return private_dir(state_dir()/"tasks")


def create(plans, config, engine, glossary=None):
    sources = {Path(p["source"]).resolve() for p in plans}
    outputs = [Path(p["output"]).resolve() for p in plans]
    for p in plans:
        if p.get("kind")=="video":
            outputs.extend(Path(k).resolve() for k in p["source_expected"])
    if len(outputs) != len(set(outputs)) or any(p in sources for p in outputs):
        raise ValueError("输出同名冲突或输出将覆盖输入字幕")
    task_id = datetime.now().strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6]
    root = task_root()/task_id
    root.mkdir(mode=0o700)
    snapshot = {"engine": engine, "provider": config["providers"].get(engine, {}), "rules": RULES,
                "timeout": config["timeout"], "retries": config["retries"],
                "batch_items": config["batch_items"], "batch_chars": config["batch_chars"],
                "context_items": config["context_items"], "request_interval": config["request_interval"],
                "glossary": glossary or {}}
    if any(p.get("kind")=="video" for p in plans):
        snapshot["transcription"]=config["transcription"]
        snapshot["mode"]=config.get("task_mode","video_translate")
    files = []
    for index, plan in enumerate(plans):
        source, output = Path(plan["source"]), Path(plan["output"])
        if plan.get("kind")=="video":
            folder=private_dir(root/str(index))
            write_json(folder/"checkpoint.json",{"translations":{},"errors":{},"usage":[],"attempts":0})
            files.append(dict(plan)|{
                "input_sha":file_sha(source),"source_sha":None,"total":0,
                "language_requested":plan["language"],
                "existing_sha":file_sha(output) if output.exists() else None,
                "status":"skipped" if plan["policy"]=="skip" and output.exists() and snapshot["mode"]!="transcribe" else "pending",
            })
            continue
        content = source.read_bytes()
        cues = parse(content.decode("utf-8-sig"))
        folder = private_dir(root/str(index))
        copy = folder/"source.srt"
        copy.write_bytes(content)
        copy.chmod(0o600)
        write_json(folder/"checkpoint.json", {"translations": {}, "errors": {}, "usage": [], "attempts": 0})
        files.append({"source": str(source), "output": str(output), "language": plan["language"],
                      "policy": plan["policy"], "source_sha": sha(content), "total": len(cues),
                      "existing_sha": sha(output.read_bytes()) if output.exists() else None,
                      "status": "skipped" if plan["policy"] == "skip" and output.exists() else "pending"})
    spec = {"id": task_id, "created": now(), "snapshot": snapshot, "snapshot_sha": sha(snapshot), "files": files}
    write_json(root/"task.json", spec)
    write_json(root/"status.json", {"status": "pending", "updated": now(), "files": files})
    return root


def read_task(root):
    spec = read_json(root/"task.json")
    if not spec or sha(spec["snapshot"]) != spec["snapshot_sha"]:
        raise RuntimeError("任务配置快照发生变化，禁止混用旧结果；请新建任务")
    if spec["snapshot"]["rules"] != RULES:
        raise RuntimeError("翻译规则版本变化，请使用原版本程序或新建任务")
    return spec


def files_compatible(root, index, item):
    original = Path(item["source"])
    saved = root/str(index)/"source.srt"
    expected=item["input_sha"] if item.get("kind")=="video" else item["source_sha"]
    if not original.is_file() or file_sha(original) != expected:
        raise RuntimeError("输入原文已变化或不存在；禁止沿用此任务，请还原输入或新建任务")
    if sha(saved.read_bytes()) != item["source_sha"]:
        raise RuntimeError("任务中的原文副本发生变化")
    if item.get("source_published_sha"):
        published=Path(item["source_output"])
        if not published.exists() or file_sha(published)!=item["source_published_sha"]:
            raise RuntimeError("已发布的原文字幕发生变化，不可混用旧译文")
    return parse(saved.read_text(encoding="utf-8-sig"))


def publish(root, index, item, cues, checkpoint):
    output = Path(item["output"])
    translated = [checkpoint["translations"][str(i)] for i in range(len(cues))]
    content = render(cues, translated).encode()
    files_compatible(root, index, item)
    if output.exists():
        current_sha = sha(output.read_bytes())
        if current_sha == item.get("output_sha") or current_sha == sha(content):
            item["output_sha"] = current_sha
            return
        if item["policy"] != "backup" or current_sha != item["existing_sha"]:
            raise RuntimeError("输出文件出现或已变化，停止发布以避免覆盖；请检查冲突后重试")
        backup = output.with_name(output.name+f".bak-{root.name}")
        if backup.exists():
            if not backup.is_file() or backup.is_symlink() or sha(backup.read_bytes()) != item["existing_sha"]:
                raise RuntimeError("备份文件冲突，请人工检查")
        else:
            shutil.copy2(output, backup)
        item["backup"] = str(backup)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name("."+output.name+f".{root.name}.tmp")
    if temp.exists() or temp.is_symlink():
        if temp.is_symlink() or not temp.is_file():
            raise RuntimeError("临时输出路径异常")
        temp.unlink()
    with temp.open("xb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, output)
    item["output_sha"] = sha(content)


def run(root, retry_only=False, backend_factory=Backend, event=None, sleeper=time.sleep):
    event = event or (lambda *a, **kw: None)
    root = Path(root)
    with lock(root/"worker.lock"):
        spec = read_task(root)
        cfg = spec["snapshot"]
        status = read_json(root/"status.json")
        status["status"] = "running"
        status.pop("api_failure", None)
        status.pop("error", None)
        def save():
            status["updated"] = now()
            write_json(root/"status.json", status)
        def emit(kind, **values):
            if kind in {"asr_file","file"}:
                status["current_file"]=values.get("source")
                status["progress"]=None
                status["stage"] = "准备转写" if kind == "asr_file" else "翻译（等待本批 API 返回）"
            if kind == "file":
                total = values["total"]; done = values["done"]
                status["progress"] = {"kind":"progress", "done":done, "total":total,
                                      "percent":min(99, done/total*100) if total else 0, "unit":"条"}
            if kind=="stage":status["stage"]=values.get("stage")
            if kind in {"asr_progress","progress"}:
                total=values["total"];done=values["done"]
                status["progress"]={"kind":kind,"done":done,"total":total,"percent":min(99,done/total*100) if total else 0,"unit":"秒语音" if kind=="asr_progress" else "条"}
                status["stage"]="转写" if kind=="asr_progress" else "翻译"
            if kind in {"source_ready","complete"} and status.get("progress"):
                status["progress"]["percent"]=100
                status["stage"]="原文发布完成" if kind=="source_ready" else "中文/原文发布完成"
            save()
            event(kind, **values)
            with (root/"events.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps({"at": now(), "event": kind, **values}, ensure_ascii=False)+"\n")
            (root/"events.jsonl").chmod(0o600)
        save()
        try:
            backend = None
            for index, item in enumerate(status["files"]):
                if item["status"] in {"success", "skipped"}:
                    if item["status"] == "success":
                        files_compatible(root, index, item)
                        final=Path(item["source_output"] if cfg.get("mode")=="transcribe" else item["output"])
                        if not final.exists() or file_sha(final) != item.get("output_sha"):
                            raise RuntimeError("已完成的正式输出发生变化，请新建任务或恢复输出")
                    continue
                if retry_only and item["status"] != "failed":
                    continue
                folder = root/str(index)
                checkpoint = read_json(folder/"checkpoint.json")
                try:
                    output_lock = private_dir(state_dir()/"output-locks")/(sha(item["output"])+".lock")
                    with lock(output_lock):
                        only_translation_retry=retry_only and (item.get("kind")!="video" or item.get("failed_stage")=="translation")
                        item["status"]="running";item.pop("error",None);save()
                        if item.get("kind")=="video":
                            from .transcribe import ensure_source
                            emit("asr_file",index=index+1,total_files=len(status["files"]),source=item["source"])
                            cues=ensure_source(root,index,item,cfg,emit,save)
                            if cfg.get("mode")=="transcribe":
                                item["status"]="success";item["output_sha"]=item["source_published_sha"]
                                emit("complete",output=item["source_output"],done=len(cues),total=len(cues))
                                save();continue
                        else:
                            cues = files_compatible(root, index, item)
                        if backend is None:
                            backend=backend_factory(cfg["engine"],cfg["provider"],load_secrets().get(cfg["engine"]),cfg["timeout"],cfg["glossary"])
                        allowed = {str(i) for i in range(len(cues))}
                        if not set(checkpoint["translations"]) <= allowed or not all(isinstance(t, str) and t.strip() for t in checkpoint["translations"].values()):
                            raise RuntimeError("检查点数据无效，禁止继续")
                        item["status"] = "running"
                        item["stage"] = "translation"
                        item.pop("error", None)
                        save()
                        emit("file", index=index+1, total_files=len(status["files"]), source=item["source"], total=len(cues), done=len(checkpoint["translations"]))
                        targets = [i for i in range(len(cues)) if str(i) not in checkpoint["translations"] and (not only_translation_retry or str(i) in checkpoint["errors"])]

                        def store():
                            write_json(folder/"checkpoint.json", checkpoint)
                            emit("progress", done=len(checkpoint["translations"]), total=len(cues), source=item["source"], stage="翻译（校验并保存）")

                        def translate_batch(ids):
                            surrounding = set()
                            for i in ids:
                                surrounding.update(range(max(0, i-cfg["context_items"]), min(len(cues), i+cfg["context_items"]+1)))
                            context = "\n".join(cues[i].text for i in sorted(surrounding-set(ids)))
                            items = [{"id": i, "text": cues[i].text} for i in ids]
                            for attempt in range(cfg["retries"]+1):
                                cursor = len(backend.usage)
                                checkpoint["attempts"] += 1
                                try:
                                    result = backend.translate(items, item["language"], context)
                                    # Validate again at the persistence boundary.
                                    from .backends import validate
                                    result = validate([{"id": i, "text": text} for i, text in zip(ids, result)], ids) if len(result) == len(ids) else validate([], ids)
                                    for i, text in zip(ids, result):
                                        checkpoint["translations"][str(i)] = text
                                        checkpoint["errors"].pop(str(i), None)
                                        checkpoint.setdefault("origins", {})[str(i)] = {
                                            "engine": cfg["engine"], "model": cfg["provider"].get("model"),
                                            "provider_sha": sha(cfg["provider"]),
                                            "revision": cfg.get("translation_revision", 0)}
                                    store()
                                    sleeper(cfg["request_interval"])
                                    return
                                except APIError as error:
                                    if error.global_error:
                                        raise
                                    if error.transient and attempt < cfg["retries"] and error.category != "size":
                                        emit("retry", category=error.category, attempt=attempt+1, count=len(ids))
                                        sleeper(min(30, 2**attempt + cfg["request_interval"]))
                                        continue
                                    if error.category in {"network", "rate", "server"}:
                                        raise APIError(error.category, f"{error}；达到重试上限，暂停以避免连续失败", global_error=True)
                                    if len(ids) > 1:
                                        midpoint = len(ids)//2
                                        translate_batch(ids[:midpoint])
                                        translate_batch(ids[midpoint:])
                                        return
                                    checkpoint["errors"][str(ids[0])] = {"category": error.category, "message": str(error)}
                                    status["api_failure"] = {"engine":cfg["engine"], "category":error.category,
                                                             "message":str(error)}
                                    store()
                                    return
                                finally:
                                    checkpoint["usage"].extend(backend.usage[cursor:])
                                    # Clear handled usage so nested batch retries cannot double count.
                                    del backend.usage[cursor:]
                                    write_json(folder/"checkpoint.json", checkpoint)

                        batch = []
                        chars = 0
                        for i in targets:
                            size = len(cues[i].text)
                            limit = 1 if cfg["engine"] == "baidu" else cfg["batch_items"]
                            if batch and (len(batch) >= limit or chars+size > cfg["batch_chars"]):
                                translate_batch(batch)
                                batch, chars = [], 0
                            batch.append(i)
                            chars += size
                        if batch:
                            translate_batch(batch)
                        if len(checkpoint["translations"]) == len(cues):
                            emit("stage", stage="最终校验与发布", source=item["source"])
                            publish(root, index, item, cues, checkpoint)
                            item["status"] = "success"
                            emit("complete", output=item["output"], done=len(cues), total=len(cues))
                        else:
                            item["status"] = "failed"
                            item["failed_stage"]="translation"
                            item["error"] = f"{len(checkpoint['errors'])} 条失败，译文尚未完整；正式中文 SRT 未发布"
                except APIError as error:
                    if error.global_error:
                        item["status"] = "paused"
                        item["error"] = str(error)
                        raise
                    raise
                except Exception as error:
                    item["status"] = "failed"
                    item["failed_stage"]=item.get("stage","translation")
                    item["error"] = str(error)
                    emit("error", source=item["source"], message=str(error))
                save()
            status["status"] = "failed" if any(f["status"] == "failed" for f in status["files"]) else ("pending" if any(f["status"] not in {"success", "skipped"} for f in status["files"]) else "success")
        except APIError as error:
            status["status"] = "paused"
            status["error"] = str(error)
            status["api_failure"] = {"engine":cfg["engine"], "category":error.category, "message":str(error)}
            status["stage"] = "API 失败：等待重新配置并测试"
            emit("pause", category=error.category, message=str(error))
        except KeyboardInterrupt:
            status["status"] = "interrupted"
            for item in status["files"]:
                if item["status"] == "running":
                    item["status"] = "pending"
            emit("interrupted")
        except Exception as error:
            status["status"] = "blocked"
            status["error"] = str(error)
            emit("error", message=str(error))
        finally:
            save()
        return 0 if status["status"] == "success" else 1


def reconfigure_translation(root, engine, provider):
    """Explicit, audited provider revision; never changes input/ASR settings."""
    import copy
    root = Path(root)
    if engine not in {'deepl', 'baidu', 'deepseek', 'local'} or not provider.get('verified') or not provider.get('last_test'):
        raise ValueError('翻译引擎必须先通过实际测试')
    with lock(root/'worker.lock'):
        spec = read_task(root)
        status = read_json(root/'status.json')
        if status.get('status') in {'pending', 'running'}:
            raise RuntimeError('任务尚在运行，不能修改翻译配置')
        original = copy.deepcopy(spec)
        previous = spec['snapshot']
        # Validate inputs before allowing any old translations to survive a revision.
        for i, item in enumerate(status['files']):
            if item.get('status') == 'skipped':
                continue
            if item.get('kind') == 'video' and not item.get('source_sha'):
                if file_sha(Path(item['source'])) != item['input_sha']:
                    raise RuntimeError('视频输入已变化，禁止混用检查点')
            else:
                files_compatible(root, i, item)
        revision = previous.get('translation_revision', 0) + 1
        history = private_dir(root/'config-revisions')
        audit = history/f'{revision:04}.json'
        # A crash after writing the audit can be retried without losing history.
        if audit.exists():
            recorded = read_json(audit)
            if recorded['previous_task']['snapshot_sha'] != original['snapshot_sha']:
                raise RuntimeError('配置修订记录冲突，请检查任务目录')
        write_json(audit, {'at':now(), 'previous_task':original, 'new_engine':engine, 'new_provider':provider})
        for i, item in enumerate(status['files']):
            path = root/str(i)/'checkpoint.json'
            checkpoint = read_json(path)
            origins = checkpoint.setdefault('origins', {})
            for cue_id in checkpoint['translations']:
                origins.setdefault(cue_id, {'engine':previous['engine'], 'model':previous['provider'].get('model'),
                                            'provider_sha':sha(previous['provider']),
                                            'revision':previous.get('translation_revision', 0)})
            write_json(path, checkpoint)
            if item.get('status') == 'paused' or (item.get('status') == 'failed' and item.get('failed_stage') == 'translation'):
                item['status'] = 'pending'
                item.pop('error', None)
        snapshot = copy.deepcopy(previous)
        snapshot.update(engine=engine, provider=copy.deepcopy(provider), translation_revision=revision)
        if engine not in {'deepseek','local'}:
            snapshot['glossary'] = {}
        spec['snapshot'] = snapshot
        spec['snapshot_sha'] = sha(snapshot)
        write_json(root/'task.json', spec)
        status['status'] = 'interrupted'
        status['stage'] = '翻译配置已更新，等待继续'
        status.pop('api_failure', None); status.pop('error', None)
        status['updated'] = now()
        write_json(root/'status.json', status)
