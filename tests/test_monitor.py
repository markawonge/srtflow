import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from rich.console import Console
from srtflow.monitor import follow


class MonitorTests(unittest.TestCase):
    def scenario(self, tty):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            producer = '''import json, os, sys, time
from pathlib import Path
p=Path(sys.argv[1])
for stage, done, state in [('VAD扫描',None,'running'),('转写',50,'running'),('翻译',75,'running'),('中文/原文发布完成',100,'success')]:
 data={'status':state,'stage':stage,'updated':stage,'files':[]}
 if done is not None: data['progress']={'done':done,'total':100,'percent':done,'unit':'条'}
 t=p/'temp.json';t.write_text(json.dumps(data));os.replace(t,p/'status.json')
 time.sleep(.2)
'''
            process = subprocess.Popen([sys.executable, '-c', producer, str(root)])
            output = io.StringIO()
            console = Console(file=output, force_terminal=tty, width=100)
            summary = Mock()
            try:
                follow(root, console, summary, interval=.02, active=lambda _: True)
                summary.assert_called_once_with(root)
                rendered = output.getvalue()
                self.assertIn('VAD扫描', rendered)
                self.assertIn('50.0%', rendered)
                self.assertIn('75.0%', rendered)
                self.assertIn('100.0%', rendered)
                self.assertEqual(process.poll(), None, 'monitor must finish before producer exits')
            finally:
                process.wait(timeout=3)

    def test_separate_process_progress_nonterminal(self):
        self.scenario(False)

    def test_separate_process_progress_terminal(self):
        with patch.dict(os.environ, {"TERM": "xterm-256color"}):
            self.scenario(True)

    def test_interrupt_is_only_detach(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'status.json').write_text(json.dumps({'status':'running'}))
            out = io.StringIO()
            summary = Mock()
            from unittest.mock import patch
            with patch('srtflow.monitor.time.sleep', side_effect=KeyboardInterrupt):
                follow(root, Console(file=out), summary)
            self.assertIn('后台任务仍继续运行', out.getvalue())
            summary.assert_not_called()

