"""Configuration-first interactive workflow; transcription remains in the existing image."""
import copy
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from rich.panel import Panel
from rich.table import Table

from .storage import config_dir, load_config, load_secrets, read_json, write_json

ENGINES = {'deepl': 'DeepL', 'baidu': '百度通用文本翻译',
           'deepseek': 'DeepSeek', 'local': '本地翻译 API（OpenAI 兼容）'}
LANGUAGES = {'ja': '日语', 'en': '英语', 'auto': '逐文件自动检测（英/日）'}


def engine_menu(title, default='deepl', names=None):
    from . import cli
    names = list(names or ENGINES)
    options = {str(i+1): (ENGINES[name], name) for i, name in enumerate(names)}
    selected = str(names.index(default)+1) if default in names else '1'
    return cli.choice(title, options, selected)


def edit_defaults(config):
    from . import cli
    config['input_dir'] = cli.optional('默认输入目录（服务器路径）', config['input_dir'])
    config['output_mode'] = cli.choice('默认输出位置', {
        '1': ('视频相同目录；翻译已有 SRT 时为原文 SRT 相同目录', 'beside'),
        '2': ('默认输出目录', 'default')}, '2' if config['output_mode'] == 'default' else '1')
    config['output_dir'] = cli.optional('默认输出目录', config['output_dir'])
    while config['output_mode'] == 'default' and not config['output_dir']:
        cli.say('选择默认输出目录时，路径不能为空。', 'yellow')
        config['output_dir'] = cli.ask('默认输出目录（服务器路径）')
    config['recursive'] = cli.yes('递归扫描子目录（始终排除隐藏、临时文件）', config['recursive'])
    keys = list(LANGUAGES)
    config['source_language'] = cli.choice('默认源语言', {
        str(i+1): (LANGUAGES[name], name) for i, name in enumerate(keys)
    }, str(keys.index(config['source_language'])+1) if config['source_language'] in keys else '1')
    config['target_language'] = 'zh-Hans'
    cli.say('目标语言：简体中文（本版固定）。')
    config['default_engine'] = engine_menu('默认翻译引擎', config['default_engine'])


def edit_advanced(config):
    from . import cli
    config['existing_policy'] = cli.choice('已有中文输出的默认处理', {
        '1': ('跳过，避免重复处理或覆盖', 'skip'),
        '2': ('重新生成，发布前备份已有输出', 'backup')}, '2' if config['existing_policy'] == 'backup' else '1')
    config['source_policy'] = cli.choice('已有语言标记原文字幕的默认处理', {
        '1': ('遇到时单独确认', 'ask'),
        '2': ('复用（仅在明确与视频对应时使用；自动检测语言仍单独确认）', 'reuse'),
        '3': ('重新转写，发布前备份', 'backup'),
        '4': ('跳过该视频', 'cancel')}, {'ask':'1','reuse':'2','backup':'3','cancel':'4'}.get(config['source_policy'], '1'))
    config['background'] = cli.yes('默认后台运行并自动显示实时进度（SSH 断开后继续）', config['background'])
    config['glossary_path'] = cli.optional('本地术语表 JSON 路径（用于 DeepSeek／本地 API）', config['glossary_path'])


def config_summary(config):
    from . import cli
    table = Table(title='当前默认配置')
    table.add_column('项目'); table.add_column('配置')
    rows = [
        ('源语言 / 目标语言', LANGUAGES.get(config['source_language'], config['source_language']) + ' → 简体中文'),
        ('默认输入目录', config['input_dir'] or '未设置（可选择当前目录或手动路径）'),
        ('输出位置', '视频相同目录／原文 SRT 相同目录' if config['output_mode'] == 'beside' else config['output_dir']),
        ('递归扫描', '开启' if config['recursive'] else '关闭'),
        ('默认翻译引擎', ENGINES.get(config['default_engine'], config['default_engine'])),
    ]
    secrets = load_secrets()
    for name, options in config['providers'].items():
        if name not in ENGINES:
            continue
        endpoint = urlsplit(options.get('endpoint', ''))
        # Never echo URL credentials or query strings, even from hand-edited configs.
        address = urlunsplit((endpoint.scheme, endpoint.netloc.rsplit('@', 1)[-1], endpoint.path, '', ''))
        configured = bool(secrets.get(name, {}).get('key')) or name == 'local'
        rows.append((ENGINES[name], f"{'已配置' if configured else '未配置密钥'}；{'已测试' if options.get('last_test') else '待测试'}；{address}" + (f"；模型 {options['model']}" if options.get('model') else '')))
    asr = config['transcription']
    rows += [('转写', f"{asr['model']}；{asr['compute_type']}；VAD {'开' if asr['vad'] else '关'}；阈值 {asr['threshold']}"),
             ('高级转写', f"最短语音 {asr['min_speech_duration_ms']}ms；静音间隔 {asr['min_silence_duration_ms']}ms；留白 {asr['speech_pad_ms']}ms；最长区间 {asr['max_speech_duration_s']}s；beam {asr['beam_size']}"),
             ('翻译请求', f"每批 {config['batch_items']}条 / {config['batch_chars']}字符；超时 {config['timeout']}秒；额外重试 {config['retries']}次；并发 1"),
             ('已有中文输出', '跳过' if config['existing_policy'] == 'skip' else '备份后重做'),
             ('执行方式', '后台运行并自动显示进度' if config['background'] else '前台运行')]
    for name, value in rows:
        table.add_row(name, str(value))
    cli.console.print(table)


def ensure_engine(config):
    from . import cli
    name = config['default_engine']
    if name not in config['providers']:
        cli.say('默认翻译引擎尚未配置，请配置并实测。', 'yellow')
        name = engine_menu('选择要配置的翻译引擎', name)
        if not cli.configure_provider(config, name):
            raise ValueError('引擎未通过实测，任务未开始')
        config['default_engine'] = name
        write_json(config_dir()/'config.json', config)
    options = config['providers'][name]
    if options.get('verified') is False or not options.get('last_test'):
        cli.say('此引擎尚无有效测试记录，需要完成一次实际测试。', 'yellow')
        if not cli.configure_provider(config, name):
            raise ValueError('引擎未通过实测，任务未开始')
    return name


def glossary_for(config, engine):
    if engine not in {'deepseek', 'local'} or not config.get('glossary_path'):
        return {}
    glossary = json.loads(Path(config['glossary_path']).expanduser().read_text(encoding='utf-8'))
    if not isinstance(glossary, dict) or not all(isinstance(k, str) and isinstance(v, str) for k,v in glossary.items()):
        raise ValueError('术语表必须是字符串到字符串的 JSON 对象')
    return glossary


def new_task(config, mode):
    from . import cli
    from .transcribe import preflight
    from .tasks import create
    video = mode != 'translate'
    files = cli.choose_inputs(config, video=video, quick=True)
    plans = cli.video_plans(files, config, mode, quick=True) if video else cli.make_plans(files, config, quick=True)
    if not plans:
        cli.say('没有待处理文件（可能已按默认策略跳过）。', 'yellow')
        return
    if mode != 'transcribe' and all(p['policy'] == 'skip' and Path(p['output']).exists() for p in plans):
        for p in plans:
            cli.say('跳过已有结果：' + p['output'])
        return
    engine = ensure_engine(config) if mode != 'transcribe' else None
    if video:
        config = copy.deepcopy(config)
        config['transcription']['cache_dir'] = str(Path(config['transcription']['cache_dir']).expanduser().resolve())
        with cli.console.status('检查现有 Docker 镜像……'):
            config['transcription']['image_id'] = preflight(config['transcription']['image'])
        config['task_mode'] = mode
    glossary = glossary_for(config, engine)
    cli.say('\n开始前确认', 'bold cyan')
    cli.say({'video_translate':'视频 → 原文 SRT → 简体中文 SRT', 'transcribe':'视频 → 原文 SRT（不翻译）', 'translate':'原文 SRT → 简体中文 SRT'}[mode])
    if video:
        asr = config['transcription']
        cli.say(f"模型 {asr['model']}；VAD {'开' if asr['vad'] else '关'}；阈值 {asr['threshold']}；精度 {asr['compute_type']}")
    if engine:
        cli.say('翻译引擎：' + ENGINES[engine] + '；目标：简体中文')
    for p in plans:
        cli.say(p['source'])
        if video:
            cli.say('  原文 → ' + p['source_output'].replace('.auto.srt', '.{jp或en}.srt'))
        if engine:
            cli.say('  中文 → ' + p['output'])
        cli.say('  源语言：' + LANGUAGES.get(p['language'], p['language']) + '；已有结果：' + ('备份后重做' if p['policy']=='backup' else '跳过'))
    cli.say('执行方式：' + ('后台运行并自动显示进度' if config['background'] else '前台运行'))
    if not cli.yes('按回车开始；输入 n 取消', True):
        return
    with cli.console.status('保存任务及输入指纹……'):
        root = create(plans, config, engine, glossary)
    cli.say('任务已保存：' + str(root), 'green')
    cli.execute(root, background=config['background'])


def offer_recovery(root):
    from . import cli
    from .tasks import reconfigure_translation, read_task
    status = read_json(root/'status.json', {}) or {}
    failure = status.get('api_failure')
    if not failure or status.get('status') in {'pending', 'running'}:
        return False
    # A detached worker never reads stdin. Recovery is offered by an attached CLI.
    if not __import__('sys').stdin.isatty():
        cli.say(f'API 调用失败，进度已保存。重新登录运行 srtflow --resume，选择任务 {root.name}，可重新配置并测试引擎。', 'yellow')
        return False
    cli.say(f"翻译 API 失败 [{failure['category']}]：{failure['message']}\n已完成转写和译文均已保存。", 'yellow')
    if not cli.yes('现在重新配置翻译引擎并测试', True):
        cli.say('任务保持暂停，可稍后使用 srtflow --resume。')
        return False
    config = load_config()
    previous = read_task(root)['snapshot']['engine']
    name = engine_menu('选择重新配置的翻译引擎', previous)
    if not cli.configure_provider(config, name):
        cli.say('测试未通过，任务保持暂停。', 'yellow')
        return False
    cli.say('将保留已完成译文，仅继续未完成部分；如改变引擎或模型，会记录配置修订及各批次实际来源。')
    if not cli.yes('确认更新此任务的翻译配置并继续', True):
        return False
    reconfigure_translation(root, name, config['providers'][name])
    return True


def start():
    from . import cli
    cli.console.print(Panel('SRTFlow 0.3\n视频转写与简体中文字幕', title='字幕工作流', border_style='cyan'))
    existed = (config_dir()/'config.json').is_file()
    if not existed:
        cli.say('未发现配置文件，先进入默认配置向导。', 'cyan')
        cli.config_wizard()
    config = load_config()
    config_summary(config)
    if existed and cli.yes('是否修改默认配置', False):
        cli.config_wizard()
        config = load_config()
        config_summary(config)
    mode = cli.choice('选择功能', {
        '1': ('视频转写并翻译为简体中文', 'video_translate'),
        '2': ('仅转写，不翻译', 'transcribe'),
        '3': ('翻译已有原文 SRT', 'translate'),
        '4': ('继续未完成任务', 'resume'),
        '5': ('仅重试失败部分', 'retry'),
        '0': ('退出', 'exit')})
    if mode in {'resume', 'retry'}:
        cli.choose_task(mode == 'retry')
    elif mode != 'exit':
        new_task(config, mode)
