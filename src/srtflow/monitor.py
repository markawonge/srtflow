"""Fresh on-disk task snapshots, rendered without clearing the terminal."""
import subprocess
import shutil
import time

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.text import Text

from .storage import read_json, lock


def snapshot(root):
    # Atomic replacement of status.json by the worker makes this a coherent read.
    status = read_json(root / 'status.json', {}) or {}
    files = status.get('files', [])
    done = total = 0
    for i, item in enumerate(files):
        if item.get('status') == 'skipped':
            continue
        total += item.get('total', 0)
        checkpoint = read_json(root / str(i) / 'checkpoint.json', {}) or {}
        done += len(checkpoint.get('translations', {}))
    return status, done, total


def render(root, status, saved, total, elapsed):
    state = status.get('status', 'pending')
    stage = status.get('stage') or '等待工作进程启动'
    counts = {name: sum(f.get('status') == name for f in status.get('files', []))
              for name in ('success', 'failed', 'skipped')}
    rows = [Text(f'任务：{root.name}  状态：{state}'),
            Text('当前文件：' + (status.get('current_file') or '—')),
            Text(f"成功 {counts['success']}  失败 {counts['failed']}  跳过 {counts['skipped']}"),
            Text(f'查看已用：{int(elapsed)//60:02}:{int(elapsed)%60:02}  最近状态更新：{status.get("updated", "尚未收到")}')]
    if state in {'pending', 'running'}:
        rows.append(Spinner('dots', text=Text('当前阶段：' + stage, style='cyan')))
    else:
        rows.append(Text('当前阶段：' + stage, style='cyan'))
    measured = status.get('progress')
    if measured and measured.get('total', 0) > 0:
        percent = max(0, min(100, measured.get('percent', 0)))
        rows += [Text(f"阶段进度：{measured['done']:.1f}/{measured['total']:.1f} {measured.get('unit', '')}  {percent:.1f}%"),
                 ProgressBar(total=100, completed=percent)]
    else:
        rows.append(Text('当前阶段暂无可靠百分比；等待实际检查点更新。', style='yellow'))
    rows.append(Text(f'已校验并保存译文：{saved}/{total} 条'))
    if state in {'pending', 'running'}:
        rows.append(Text('Ctrl+C 仅退出查看；后台任务继续运行。', style='dim'))
    return Panel(Group(*rows), title='字幕任务实时进度', border_style='cyan')


def worker_present(root):
    try:
        with lock(root / 'worker.lock'):
            pass
    except RuntimeError:
        return True
    if shutil.which('tmux'):
        return subprocess.run(['tmux', 'has-session', '-t', 'sf-' + root.name],
                              capture_output=True, timeout=3).returncode == 0
    return False


def follow(root, console, summary, interval=0.5, active=worker_present):
    start = time.monotonic()
    last_check = start
    warning = False
    # Explicit refresh: no console.clear(), no repeating completion summaries.
    # A non-interactive terminal gets flushed, append-only progress lines.
    tty = console.is_terminal and not console.is_dumb_terminal
    last_signature = None
    last_line = -30.0
    live = Live(console=console, auto_refresh=False, transient=True) if tty else None
    try:
        if live:
            live.start()
        while True:
            status, saved, total = snapshot(root)
            elapsed = time.monotonic() - start
            if live:
                live.update(render(root, status, saved, total, elapsed), refresh=True)
            else:
                p = status.get('progress') or {}
                signature = (status.get('status'), status.get('stage'), status.get('current_file'),
                             p.get('done'), p.get('total'), saved, total)
                if signature != last_signature or elapsed - last_line >= 30:
                    ptext = (f"{p['done']:.1f}/{p['total']:.1f} {p.get('unit', '')} {p.get('percent', 0):.1f}%"
                             if p.get('total', 0) > 0 else '暂无可靠百分比')
                    console.print(f"[{int(elapsed)}秒] {status.get('status', 'pending')} | "
                                  f"{status.get('stage', '等待启动')} | {ptext} | 译文已保存 {saved}/{total}", markup=False)
                    console.file.flush()
                    last_signature, last_line = signature, elapsed
            if status.get('status', 'pending') not in {'pending', 'running'}:
                break
            now = time.monotonic()
            if now - last_check >= 2:
                last_check = now
                if elapsed >= 8 and not active(root):
                    warning = True
                    break
            time.sleep(interval)
    except KeyboardInterrupt:
        if live:
            live.stop()
        console.print('已退出进度查看，后台任务仍继续运行。', style='yellow')
        return
    finally:
        if live:
            live.stop()
    if warning:
        console.print('未发现运行中的 worker；请检查日志，或使用 --resume 恢复。', style='yellow')
    summary(root)
