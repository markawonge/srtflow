import json
import os
from pathlib import Path
import subprocess
import uuid

from .storage import private_dir,write_json,read_json,lock,state_dir,file_sha,sha
from .subtitles import parse


def preflight(image):
    result=subprocess.run(["docker","image","inspect",image,"--format","{{.Id}}"],capture_output=True,text=True)
    if result.returncode:
        raise RuntimeError("无法访问现有镜像，请检查Docker组权限和镜像名称；不自动重建镜像")
    return result.stdout.strip()


def atomic_publish(content,output,policy,expected,task_id):
    output=Path(output)
    digest=sha(content)
    if output.exists():
        current=file_sha(output)
        if current==digest:return digest
        if policy!="backup" or current!=expected:
            raise RuntimeError(f"原文输出冲突，未覆盖：{output}")
        backup=output.with_name(output.name+".bak-"+task_id)
        if backup.exists():
            if file_sha(backup)!=expected:raise RuntimeError("原文备份冲突")
        else:
            import shutil
            shutil.copy2(output,backup)
    temporary=output.with_name("."+output.name+"."+task_id+".tmp")
    if temporary.is_symlink():raise RuntimeError("原文临时路径异常")
    with temporary.open("wb") as file:
        file.write(content);file.flush();os.fsync(file.fileno())
    os.replace(temporary,output)
    return digest


def ensure_source(root,index,item,cfg,emit,save):
    source=Path(item["source"]);folder=root/str(index)
    emit("stage",stage="核对视频文件指纹",source=str(source))
    if not source.is_file() or file_sha(source)!=item["input_sha"]:
        raise RuntimeError("视频已变化或不存在；禁止沿用转写检查点")
    saved=folder/"source.srt"
    if item.get("source_sha"):
        if not saved.exists() or file_sha(saved)!=item["source_sha"]:
            raise RuntimeError("任务原文副本已变化")
    else:
        # Reusing a pre-existing, explicitly confirmed language-marked SRT is allowed.
        if item.get("reuse_source"):
            existing=Path(item["source_output"])
            if file_sha(existing)!=item["source_existing_sha"]:
                raise RuntimeError("选择复用的原文字幕已变化")
            content=existing.read_bytes();parse(content.decode("utf-8-sig"))
            saved.write_bytes(content)
            item["source_sha"]=sha(content)
        else:
            config=cfg["transcription"]
            image_id=preflight(config["image"])
            if image_id!=config["image_id"]:
                raise RuntimeError("转写镜像已变化，禁止混用；请新建任务")
            write_json(folder/"asr-settings.json",{"config":config,"language":item["language"],"input_sha":item["input_sha"]})
            cache=Path(config["cache_dir"]).expanduser().resolve();cache.mkdir(parents=True,exist_ok=True)
            script=Path(__file__).with_name("asr_worker.py")
            for path in [source.parent,folder,cache,script]:
                if ":" in str(path) or "\n" in str(path):raise RuntimeError("容器挂载路径不能含冒号或换行")
            name="srtflow-asr-"+uuid.uuid4().hex[:12]
            command=["docker","run","--rm","--init","--name",name,"--user",f"{os.getuid()}:{os.getgid()}","--device=/dev/kfd","--device=/dev/dri"]
            groups={Path("/dev/kfd").stat().st_gid}
            groups.update(p.stat().st_gid for p in Path("/dev/dri").glob("renderD*"))
            for gid in sorted(groups):command += ["--group-add",str(gid)]
            command += ["-v",f"{source.parent}:/input:ro","-v",f"{folder}:/job","-v",f"{cache}:/cache","-v",f"{script}:/app/asr_worker.py:ro",
                        config["image"],"python","/app/asr_worker.py","--input",f"/input/{source.name}","--job","/job"]
            last_error=""
            item["stage"]="transcription";save()
            # One SRTFlow GPU workload at a time, even for different tasks.
            with lock(state_dir()/"gpu.lock"):
                process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
                try:
                    for line in process.stdout:
                        if line.startswith("SRTFLOW_EVENT "):
                            data=json.loads(line[len("SRTFLOW_EVENT "):]);kind=data.pop("kind")
                            if kind=="asr_error":last_error=data["message"]
                            emit(kind,**data)
                        else:
                            emit("asr_log",message=line.rstrip()[:1000])
                    code=process.wait()
                except BaseException:
                    subprocess.run(["docker","stop","-t","5",name],capture_output=True,timeout=20)
                    try:process.wait(timeout=10)
                    except subprocess.TimeoutExpired:process.terminate()
                    raise
            if code:
                raise RuntimeError(last_error or "容器转写失败，已完成区间保留，可续跑")
            result=read_json(folder/"asr-result.json")
            if not result or not saved.exists():raise RuntimeError("转写容器未生成完整原文结果")
            item["language"]=result["language"]
            item["source_sha"]=file_sha(saved)
        save()
    content=saved.read_bytes();cues=parse(content.decode("utf-8-sig"))
    language_suffix={"ja":"jp","en":"en"}[item["language"]]
    source_output=Path(item["output_dir"])/(source.stem+"."+language_suffix+".srt")
    if source_output!=Path(item["source_output"]) and item["language_requested"]!="auto":
        raise RuntimeError("原文输出名称与计划不匹配")
    with lock(private_dir(state_dir()/"output-locks")/(sha(str(source_output))+".lock")):
        if item.get("source_published_sha") and (not source_output.exists() or file_sha(source_output)!=item["source_published_sha"]):
            raise RuntimeError("已发布原文发生变化，禁止覆盖")
        expected=item["source_expected"].get(str(source_output))
        item["source_published_sha"]=atomic_publish(content,source_output,item["source_policy"],expected,root.name)
        item["source_output"]=str(source_output);item["total"]=len(cues);item["stage"]="source_ready"
        save()
    emit("source_ready",output=str(source_output),language=item["language"],total=len(cues))
    return cues
