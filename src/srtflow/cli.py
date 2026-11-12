import argparse
import copy
from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, SpinnerColumn
from rich.table import Table

from .backends import Backend, APIError
from .storage import config_dir, load_config, load_secrets, read_json, write_json, state_dir
from .subtitles import discover, parse, infer_language, output_name, VIDEO_EXTENSIONS
from .tasks import create, run, task_root, read_task, now

console = Console(highlight=False)


def say(text, style=None):
    console.print(str(text), style=style, markup=False)


def ask(label, default=""):
    value = input(f"{label}"+(f" [{default}]" if str(default) else "")+"：").strip()
    return value if value else str(default)


def optional(label,default=""):
    value=ask(label+"（输入 - 清空；回车保留）",default)
    return "" if value=="-" else value


def choice(title, options, default="1"):
    say("\n"+title, "bold cyan")
    for key, value in options.items():
        say(f"  {key}. {value[0]}"+("（默认）" if key == default else ""))
    while True:
        value = ask("选择", default)
        if value in options:
            return options[value][1]
        say("选项无效。", "yellow")


def yes(label, default=True):
    return ask(label+"（y/n）", "y" if default else "n").lower() in {"y", "yes"}


def positive(label, default, low, high, integer=True):
    while True:
        try:
            value = (int if integer else float)(ask(label, default))
            if low <= value <= high:
                return value
        except ValueError:
            pass
        say(f"请输入 {low}～{high} 范围的数值。", "yellow")


def path_input(label, default=""):
    value = ask(label, default)
    if len(value) > 1 and value[0] in "\"'" and value[0] == value[-1]:
        value = value[1:-1]
    return Path(value).expanduser().resolve() if value else None


def test_provider(config, name, required=False):
    if not required and not yes("使用一句固定短文本实际测试翻译，可能产生少量用量", True):
        return False
    backend = Backend(name, config["providers"][name], load_secrets().get(name), config["timeout"])
    with console.status("正在测试实际翻译……"):
        result = backend.test("ja" if config["source_language"] == "ja" else "en")
    say("测试成功："+result, "green")
    if backend.usage:
        say("服务返回用量："+json.dumps(backend.usage, ensure_ascii=False))
    return True


def configure_provider(config, name):
    secrets = load_secrets()
    credentials = dict(secrets.get(name, {}))
    old = config["providers"].get(name, {})
    if name == "deepl":
        plan = choice("DeepL API 套餐", {"1": ("Free", "free"), "2": ("Pro", "pro")}, "2" if old.get("plan") == "pro" else "1")
        options = {"plan": plan, "endpoint": "https://api-free.deepl.com" if plan == "free" else "https://api.deepl.com"}
        glossary=optional("DeepL 术语表ID，不是邮箱；没有可输入 -",old.get("glossary_id",""))
        while "@" in glossary:
            say("此处不是登录邮箱。没有术语表请输入 -。","yellow")
            glossary=optional("术语表ID","")
        options["glossary_id"]=glossary
    elif name == "baidu":
        say("产品：百度通用文本翻译 API（不是百度智能云其他翻译产品）")
        options = {"product": "general-text", "endpoint": "https://fanyi-api.baidu.com"}
        credentials["appid"] = ask("APPID", credentials.get("appid", ""))
        if not credentials["appid"]:
            raise ValueError("APPID 不能为空")
    else:
        local = name == "local"
        if local:
            say("本地接口：兼容 OpenAI Chat Completions；填写含 /v1 的基础地址（如服务要求），程序追加 /chat/completions。不安装模型。")
        endpoint = ask("本地 API 基础地址" if local else "DeepSeek API 服务地址", old.get("endpoint", "http://127.0.0.1:8000/v1" if local else "https://api.deepseek.com"))
        model=""
        while not model:
            model=ask("模型名称（填账户实际可用型号；q返回）",old.get("model",""))
            if model.lower()=="q":return False
            if not model:say("模型名称不能为空，请重新输入，或输入q返回。","yellow")
        options = {"endpoint": endpoint, "model": model, "max_tokens": positive("最大输出 token 数", old.get("max_tokens", 4096), 256, 65536)}
    if name == "local":
        options["json_mode"] = yes("服务支持 response_format=json_object", old.get("json_mode", True))
    key = getpass.getpass("密钥（隐藏输入；已配置时回车保留，本地服务可留空，输入 - 清除）：").strip()
    if key == "-":
        credentials.pop("key", None)
    elif key:
        credentials["key"] = key
    if not credentials.get("key") and name != "local":
        raise ValueError("未提供密钥")
    options["verified"] = False
    config["providers"][name] = options
    secrets[name] = credentials
    write_json(config_dir()/"secrets.json", secrets)
    write_json(config_dir()/"config.json", config)
    say("配置已保存，凭据权限为600，摘要不会显示密钥。", "green")
    say("将使用一句固定短文本实际测试，可能产生少量用量。")
    while True:
        try:
            test_provider(config, name, required=True)
            config["providers"][name]["last_test"] = now()
            config["providers"][name]["verified"] = True
            write_json(config_dir()/"config.json", config)
            return True
        except APIError as error:
            say(f"测试失败：{error.category}：{error}", "red")
            action = choice("测试未通过", {"1": ("重试测试", "retry"), "2": ("返回修改此引擎配置", "edit"), "0": ("返回向导，保留未验证配置", "cancel")})
            if action == "edit":
                return configure_provider(config, name)
            if action == "cancel":
                return False


def config_wizard():
    config = load_config()
    while True:
        mode = choice("默认配置向导", {
            "1": ("DeepL Free/Pro 凭据与实测", "deepl"),
            "2": ("百度通用文本翻译凭据与实测", "baidu"),
            "3": ("DeepSeek 凭据、模型与实测", "deepseek"),
            "4": ("默认目录、源语言、目标语言及翻译引擎", "defaults"),
            "5": ("高级设置：批次、重试、超时", "advanced"),
            "6": ("默认转写参数（现有ROCm镜像）", "transcription"),
            "7": ("本地翻译 API：兼容 OpenAI 接口", "local"),
            "0": ("保存并退出", "exit"),
        })
        if mode == "exit":
            config["target_language"] = "zh-Hans"
            write_json(config_dir()/"config.json", config)
            return
        if mode in {"deepl", "baidu", "deepseek", "local"}:
            configure_provider(config, mode)
        elif mode == "defaults":
            from .quickflow import edit_defaults
            edit_defaults(config)
        elif mode=="transcription":
            config["transcription"]=transcription_options(config["transcription"],advanced=True)
        else:
            say("并发固定为1：先保证检查点和顺序上下文。以下设置只改变后续新任务。")
            config["retries"] = positive("失败后额外重试次数", config["retries"], 0, 8)
            config["timeout"] = positive("单次请求超时秒数", config["timeout"], 5, 600)
            config["batch_items"] = positive("每批最多字幕条数（百度固定1条）", config["batch_items"], 1, 50)
            config["batch_chars"] = positive("每批原文字符预算", config["batch_chars"], 100, 10000)
            config["context_items"] = positive("相邻上下文条数", config["context_items"], 0, 10)
            config["request_interval"] = positive("请求后间隔秒数（百度按账户QPS调整）", config["request_interval"], 0, 60, False)
            from .quickflow import edit_advanced
            edit_advanced(config)
        write_json(config_dir()/"config.json", config)


def selected_indices(value, total):
    if value.lower() in {"a", "all", "全选"}:
        return list(range(total))
    if value.lower() in {"q", "取消"}:
        return []
    indices = set()
    for part in value.replace("，", ",").split(","):
        if "-" in part:
            left, right = map(int, part.split("-", 1))
            if right < left:
                raise ValueError("编号区间顺序错误")
            if left < 1 or right > total:
                raise ValueError("编号超出范围")
            indices.update(range(left-1, right))
        else:
            i = int(part)
            if i < 1 or i > total:
                raise ValueError("编号超出范围")
            indices.add(i-1)
    return sorted(indices)


def choose_inputs(config,video=False,quick=False):
    mode = choice("选择视频" if video else "选择已有原文 SRT", {"1": ("扫描启动命令时的当前目录", "cwd"), "2": ("扫描默认输入目录", "default"), "3": ("逐行输入多个文件或目录，空行结束", "paths")})
    paths = []
    if mode == "cwd":
        paths = [Path.cwd()]
    elif mode == "default":
        if not config["input_dir"]:
            raise ValueError("未设置默认输入目录，请用 --config 设置或选择手动输入")
        paths = [Path(config["input_dir"]).expanduser()]
    else:
        while True:
            path = path_input("输入路径（空行结束）")
            if path is None:
                break
            paths.append(path)
    recursive = config["recursive"] if quick else yes("递归扫描子目录（自动排除隐藏目录、隐藏和临时文件）", config["recursive"])
    files = discover(paths, recursive, VIDEO_EXTENSIONS if video else None)
    if not files:
        raise ValueError("没有找到可用视频" if video else "没有找到可用 SRT")
    for i, file in enumerate(files, 1):
        say(f"{i}. {file}")
    while True:
        try:
            selection = selected_indices(ask("输入编号，例如1,3-5；a全选；q取消", "a"), len(files))
            if not selection:
                raise SystemExit(0)
            return [files[i] for i in selection]
        except ValueError as error:
            say(error, "yellow")


def make_plans(files, config, quick=False):
    mode = config.get("output_mode", "beside") if quick else choice("输出位置", {"1": ("各原文字幕旁边", "beside"), "2": ("默认输出目录", "default"), "3": ("指定输出目录", "manual")})
    base = None
    if mode == "default":
        if not config["output_dir"]:
            raise ValueError("未设置默认输出目录")
        base = Path(config["output_dir"]).expanduser().resolve()
    elif mode == "manual":
        base = path_input("输出目录")
        if base is None:
            raise ValueError("未输入输出目录")
    uniform = "infer" if quick else choice("本次原文语言", {"1": ("各文件参考后缀预填，再逐一确认", "per_file"), "2": ("统一日语", "ja"), "3": ("统一英语", "en")})
    plans, outputs = [], set()
    for source in files:
        # Parse before spending API usage; preserve source bytes separately later.
        parse(source.read_text(encoding="utf-8-sig"))
        language = (infer_language(source) or config["source_language"]) if uniform == "infer" else uniform
        if language == "auto":
            language = choice(f"SRT 无法可靠自动检测语言：{source.name}", {"1": ("日语", "ja"), "2": ("英语", "en")})
        if uniform == "per_file":
            default = infer_language(source) or config["source_language"]
            language = choice(f"确认原文语言：{source.name}", {"1": ("日语", "ja"), "2": ("英语", "en")}, "2" if default == "en" else "1")
        try:
            name = output_name(source)
        except ValueError:
            say(f"{source.name} 没有约定的原文语言后缀，需要确认中文文件名。", "yellow")
            name = output_name(source, ask("中文输出基础名（不要加.srt）", source.stem))
        directory = base or source.parent
        output = (directory/name).resolve()
        if output == source:
            say("中文输出不能覆盖输入字幕，请改用其他输出目录。", "yellow")
            directory = path_input("为此文件指定另一输出目录")
            if directory is None:
                raise ValueError("输出与输入冲突")
            output = (directory/name).resolve()
        while output in outputs or output in files:
            action = choice(f"命名冲突：{output}", {"1": ("为此来源使用独立子目录", "subdir"), "2": ("取消此文件", "skip")})
            if action == "skip":
                output = None
                break
            sub = ask("独立子目录名称", source.parent.name or "source")
            if not sub or Path(sub).name != sub or sub in {".", ".."}:
                raise ValueError("子目录名称无效")
            output = (directory/sub/name).resolve()
        if output is None:
            continue
        directory = output.parent
        if not directory.exists():
            if not yes(f"创建输出目录 {directory}"):
                raise ValueError("输出目录未创建")
            directory.mkdir(parents=True)
        if not os.access(directory, os.W_OK):
            raise ValueError(f"输出目录不可写：{directory}")
        policy = "skip"
        if output.exists():
            policy = config.get("existing_policy", "skip") if quick else choice(f"已有正式结果：{output}", {"1": ("跳过", "skip"), "2": ("重做，发布前备份现有文件", "backup"), "3": ("取消此文件", "cancel")})
            if policy == "cancel":
                continue
        outputs.add(output)
        plans.append({"source": str(source), "output": str(output), "language": language, "policy": policy})
    return plans


def transcription_options(current,advanced=False):
    cfg=copy.deepcopy(current)
    cfg["model"]=choice("模型（Turbo代表模型选择，不是额外加速开关）",{
        "1":("large-v3：沿用已验证的质量优先模型","large-v3"),
        "2":("turbo：速度优先，准确率可能变化","turbo"),
    },"2" if cfg["model"]=="turbo" else "1")
    cfg["threshold"]=positive("VAD阈值：0.5较宽松，0.6已验证，0.65较严格",cfg["threshold"],0.01,0.99,False)
    if advanced or yes("调整高级转写参数",False):
        cfg["vad"]=yes("启用VAD（关闭后固定时间分段，静音可能误识别）",cfg["vad"])
        cfg["min_silence_duration_ms"]=positive("分段静音间隔毫秒；当前默认700",cfg["min_silence_duration_ms"],100,3000)
        cfg["speech_pad_ms"]=positive("语音边界留白毫秒；当前默认300",cfg["speech_pad_ms"],0,1000)
        cfg["min_speech_duration_ms"]=positive("最短语音毫秒；当前默认250",cfg["min_speech_duration_ms"],0,1000)
        cfg["max_speech_duration_s"]=positive("单区间最多秒数；当前默认25；太短可能断句",cfg["max_speech_duration_s"],5,29,False)
        cfg["beam_size"]=positive("解码beam_size；默认5，增大可能减速",cfg["beam_size"],1,10)
        cfg["compute_type"]=choice("计算精度",{"1":("float16：已验证","float16"),"2":("float32：更多显存、可能更慢","float32"),"3":("int8_float16：需实际验证质量","int8_float16")},{"float16":"1","float32":"2","int8_float16":"3"}.get(cfg["compute_type"],"1"))
        cfg["image"]=ask("现有Docker镜像名（不自动重建）",cfg["image"])
        cfg["cache_dir"]=ask("已有模型缓存目录",cfg["cache_dir"])
    cfg["cache_dir"]=str(Path(cfg["cache_dir"]).expanduser().resolve())
    cfg["word_timestamps"]=False
    cfg["condition_on_previous_text"]=False
    cfg["temperature"]=0.0
    say("固定保留：逐区间独立识别、word_timestamps=False、condition_on_previous_text=False、temperature=0；不启用批量拼接推理。")
    return cfg


def video_plans(files,config,mode,quick=False):
    from .storage import file_sha
    location=config.get("output_mode", "beside") if quick else choice("字幕输出位置",{"1":("视频相同目录","beside"),"2":("默认输出目录","default"),"3":("指定输出目录","manual")})
    base=None
    if location=="default":
        if not config["output_dir"]:raise ValueError("未设置默认输出目录")
        base=Path(config["output_dir"]).expanduser().resolve()
    elif location=="manual":
        base=path_input("输出目录")
        if base is None:raise ValueError("未指定输出目录")
    language=config["source_language"] if quick else choice("视频原音语言",{"1":("日语","ja"),"2":("英语","en"),"3":("逐文件自动检测（当前仅接受英/日结果）","auto")},"2" if config["source_language"]=="en" else "1")
    plans=[];reserved=set()
    for source in files:
        directory=base or source.parent
        if quick and mode != "transcribe" and config.get("existing_policy", "skip") == "skip" and (directory/(source.stem+".srt")).exists():
            say("跳过已有输出："+str(directory/(source.stem+".srt")))
            continue
        while True:
            chinese=(directory/(source.stem+".srt")).resolve()
            candidates=[(directory/(source.stem+"."+suffix+".srt")).resolve() for suffix in (["jp","en"] if language=="auto" else [{"ja":"jp","en":"en"}[language]])]
            paths=candidates+([chinese] if mode!="transcribe" else [])
            if not any(p in reserved for p in paths):break
            action=choice(f"同名冲突：{source}",{"1":("使用独立子目录","sub"),"2":("取消此文件","cancel")})
            if action=="cancel":directory=None;break
            name=ask("独立子目录名",source.parent.name)
            if not name or Path(name).name!=name or name in {".",".."}:raise ValueError("子目录名无效")
            directory=directory/name
        if directory is None:continue
        if not directory.exists():
            if not yes(f"创建目录 {directory}"):continue
            directory.mkdir(parents=True)
        if not os.access(directory,os.W_OK):raise ValueError(f"目录不可写：{directory}")
        existing={str(p):file_sha(p) if p.exists() else None for p in candidates}
        reuse=False;source_policy="skip"
        existing_sources=[p for p in candidates if p.exists()]
        if existing_sources:
            for p in existing_sources:say("已有原文候选："+str(p))
            if language!="auto":
                source_policy=config.get("source_policy", "ask")
                if not quick or source_policy == "ask":source_policy=choice("已有原文处理",{"1":("复用此语言标记原文（由你确认与视频对应）","reuse"),"2":("重新转写，备份后发布","backup"),"3":("取消此视频","cancel")})
                reuse=source_policy=="reuse"
                if reuse:parse(candidates[0].read_text(encoding="utf-8-sig"))
            else:
                if quick and config.get("source_policy") == "cancel":continue
                source_policy="backup" if quick and config.get("source_policy") == "backup" else choice("自动检测后的原文如已存在",{"1":("备份后重做，不猜测旧原文来源","backup"),"2":("取消此视频","cancel")})
            if source_policy=="cancel":continue
        policy="skip"
        if mode!="transcribe" and chinese.exists():
            say("同名.srt也可能是旧工具生成的原文；程序不会仅凭名称把它当作已翻译结果。","yellow")
            policy=("cancel" if config.get("existing_policy", "skip") == "skip" else "backup") if quick else choice(f"已有中文目标路径文件：{chinese}",{"1":("明确跳过整个视频","cancel"),"2":("生成中文，发布前备份该文件","backup")})
            if policy=="cancel":continue
        reserved.update(paths)
        plans.append({"kind":"video","source":str(source),"output":str(chinese),"output_dir":str(directory.resolve()),
                      "source_output":str(candidates[0]) if language!="auto" else str(directory/(source.stem+".auto.srt")),
                      "source_expected":existing,"source_existing_sha":existing[str(candidates[0])] if language!="auto" else None,
                      "reuse_source":reuse,"source_policy":source_policy,"language":language,"policy":policy})
    return plans


def summary(root):
    status = read_json(root/"status.json", {})
    table = Table(title=f"任务 {root.name}：{status.get('status', 'unknown')}")
    table.add_column("状态"); table.add_column("输入 / 正式输出 / 原因")
    for item in status.get("files", []):
        text = item["source"]
        if item.get("source_published_sha"):
            text += "\n原文："+item["source_output"]
        if item["status"] == "success":
            mode=read_json(root/"task.json",{}).get("snapshot",{}).get("mode")
            if mode!="transcribe":text += "\n中文："+item["output"]
        if item.get("error"):
            text += "\n"+item["error"]
        table.add_row(item["status"], text)
    console.print(table)
    if status.get("error"):
        say(status["error"], "yellow")
    say(f"任务和检查点：{root}\n日志：{root/'worker.log'}\n事件：{root/'events.jsonl'}")
    usage = []
    for i, _ in enumerate(status.get("files", [])):
        usage.extend(read_json(root/str(i)/"checkpoint.json", {}).get("usage", []))
    chars = sum(x["billed_characters"] for x in usage if type(x.get("billed_characters")) is int)
    tokens = sum(x.get("tokens", {}).get("total_tokens", 0) for x in usage)
    if any(type(x.get("billed_characters")) is int for x in usage):
        say(f"服务返回已知计费字符：{chars}（不含未获响应的请求，也不含向导测试）")
    if tokens:
        say(f"服务返回已知 token：{tokens}（不含未获响应的请求，也不含向导测试）")
    say("未计算费用金额。")


def execute(root, retry_only=False, background=None):
    if background is None:
        background = load_config()["background"]
    from .quickflow import offer_recovery
    while True:
        _execute_once(root, retry_only, background)
        if not offer_recovery(root):
            return
        retry_only = False


def _execute_once(root, retry_only=False, background=None):
    if background is None:
        background = choice("执行方式", {"1": ("后台 tmux：SSH断开后继续，推荐", True), "2": ("前台：断线后从检查点恢复", False)})
    if background:
        if not shutil.which("tmux"):
            say("未找到 tmux，请先安装：sudo apt install tmux。任务已保存，可随后 --resume。", "yellow")
            return
        session = "sf-"+root.name
        if subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0:
            say(f"已有后台会话：{session}；请用 --status 查看。", "yellow")
            return
        cmd = [sys.executable, "-m", "srtflow.cli", "--worker", root.name]
        if retry_only:
            cmd.append("--retry-only")
        shell = "exec "+shlex.join(cmd)+" >> "+shlex.quote(str(root/"worker.log"))+" 2>&1"
        subprocess.run(["tmux", "new-session", "-d", "-s", session, shell], check=True)
        say(f"后台任务已启动：{root.name}\n重新登录查看：srtflow --status {root.name}\n实时进度：srtflow --watch {root.name}\n进入后台：tmux attach -t {session}\n从tmux脱离：按 Ctrl+B，松开，再按 D。", "green")
        # SRTFLOW_PROGRESS_V2
        say("现在自动显示任务进度。Ctrl+C只退出查看，后台任务继续运行。", "cyan")
        try:
            subprocess.run([sys.executable, "-u", "-m", "srtflow.cli", "--watch", root.name], check=False, env=dict(os.environ, SRTFLOW_MONITOR_CHILD="1"))
        except KeyboardInterrupt:
            say("已退出进度查看，后台任务仍继续运行。", "yellow")
            return
    else:
        worker(root, retry_only)
        summary(root)


def worker(root, retry_only=False):
    progress = Progress(SpinnerColumn(),TextColumn("{task.description}"), BarColumn(), TextColumn("{task.fields[saved]}/{task.fields[items]}{task.fields[unit]}"), TextColumn("{task.fields[percentage_label]}"), TimeElapsedColumn(), console=console)
    task = None
    def event(kind, **values):
        nonlocal task
        if kind=="asr_file":
            if task is not None:progress.remove_task(task)
            task=None
            say(f"转写文件 {values['index']}/{values['total_files']}：{values['source']}","cyan")
        elif kind=="asr_progress":
            fraction=min(99,values["done"]/values["total"]*100) if values["total"] else 0
            if task is not None:progress.remove_task(task)
            task=progress.add_task("转写（VAD保留语音）",total=100,completed=fraction,saved=f"{values['done']:.1f}",items=f"{values['total']:.1f}",unit="秒",percentage_label=f"{fraction:.0f}%")
        elif kind=="source_ready":
            if task is not None:progress.update(task,total=100,completed=100,description="原文校验并发布完成",percentage_label="100%")
            say("原文字幕："+values["output"],"green")
        elif kind == "file":
            if task is not None:
                progress.remove_task(task)
            fraction = min(99, values["done"]/values["total"]*100)
            task = progress.add_task(f"翻译 {values['index']}/{values['total_files']}", total=100, completed=fraction, saved=values["done"], items=values["total"],unit="条",percentage_label=f"{fraction:.0f}%")
            say(values["source"], "cyan")
        elif kind == "progress" and task is not None:
            fraction=min(99,values["done"]/values["total"]*100)
            progress.update(task, completed=fraction, saved=values["done"],percentage_label=f"{fraction:.0f}%")
        elif kind == "complete" and task is not None:
            progress.update(task, total=100,completed=100, saved=values["total"],items=values["total"],unit="条",percentage_label="100%", description="校验并发布完成")
            say("生成："+values["output"], "green")
        elif kind=="stage":
            if task is not None:progress.remove_task(task)
            task=progress.add_task(values["stage"],total=None,saved="—",items="—",unit="",percentage_label="—")
        elif kind in {"retry", "error", "pause", "interrupted", "asr_error"}:
            say(kind+" "+json.dumps(values, ensure_ascii=False), "yellow")
    with progress:
        return run(root, retry_only, event=event)


def choose_task(retry=False):
    roots = sorted(task_root().glob("*/task.json"), reverse=True)
    if not roots:
        raise ValueError("没有历史任务")
    for i, path in enumerate(roots, 1):
        status = read_json(path.parent/"status.json", {})
        say(f"{i}. {path.parent.name}  {status.get('status')}")
    index = positive("选择任务编号", 1, 1, len(roots))-1
    root = roots[index].parent
    spec = read_task(root)
    summary(root)
    say("将使用原任务快照，忽略新默认配置；仅从本地凭据文件读取当前密钥。")
    say("实际设置："+json.dumps(spec["snapshot"], ensure_ascii=False))
    if read_json(root/"status.json", {}).get("api_failure"):
        from .quickflow import offer_recovery
        if offer_recovery(root):execute(root, False)
        return
    if yes("继续此任务" if not retry else "仅重试记录为失败的部分"):
        execute(root, retry)


def get_root(task_id):
    if not task_id or Path(task_id).name != task_id or task_id in {".", ".."}:
        raise ValueError("任务ID无效")
    root = task_root()/task_id
    if not (root/"task.json").exists():
        raise ValueError("任务不存在")
    return root


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="SRTFlow 0.3：视频转写与字幕翻译")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--config", action="store_true")
    group.add_argument("--resume", action="store_true")
    group.add_argument("--retry", action="store_true")
    group.add_argument("--status", metavar="TASK_ID")
    group.add_argument("--watch", metavar="TASK_ID")
    group.add_argument("--worker", metavar="TASK_ID", help=argparse.SUPPRESS)
    parser.add_argument("--retry-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.config:
            config_wizard()
        elif args.resume or args.retry:
            choose_task(args.retry)
        elif args.status:
            summary(get_root(args.status))
        elif args.watch:
            from .monitor import follow
            root = get_root(args.watch)
            follow(root, console, summary)
            if not os.environ.get("SRTFLOW_MONITOR_CHILD"):
                from .quickflow import offer_recovery
                if offer_recovery(root):execute(root, background=True)
        elif args.worker:
            return worker(get_root(args.worker), args.retry_only)
        else:
            from .quickflow import start
            start()

    except (KeyboardInterrupt, EOFError):
        if args.watch:
            say("\n已退出进度查看，后台任务仍继续运行。", "yellow")
            return 0
        say("\n已退出；任务已保存的批次可通过 --resume 恢复。", "yellow")
        return 130
    except Exception as error:
        # Do not print raw request objects, headers or provider bodies.
        message = str(error)
        try:
            stored_secrets = load_secrets()
        except Exception:
            stored_secrets = {}
        for credentials in stored_secrets.values():
            for value in credentials.values():
                if isinstance(value, str) and value:
                    message = message.replace(value, "***")
        say(f"操作未完成：{message}", "red")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
