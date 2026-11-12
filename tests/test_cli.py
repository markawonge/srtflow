import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from srtflow import cli
from srtflow.storage import load_config, load_secrets, config_dir


class CLITests(unittest.TestCase):
    def test_selection(self):
        self.assertEqual(cli.selected_indices('1,3-5',5),[0,2,3,4])
        self.assertEqual(cli.selected_indices('a',3),[0,1,2])
        for value in ['0','7','5-3']:
            with self.assertRaises(ValueError):cli.selected_indices(value,5)

    def test_free_config_and_hidden_key(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'XDG_CONFIG_HOME':temp}):
            config=load_config()
            with patch('builtins.input',side_effect=['1','']),patch.object(cli.getpass,'getpass',return_value='secret-123'),patch.object(cli.Backend, 'test', return_value='你好'):
                cli.configure_provider(config,'deepl')
            saved=load_config()
            self.assertEqual(saved['providers']['deepl']['endpoint'],'https://api-free.deepl.com')
            self.assertEqual(load_secrets()['deepl']['key'],'secret-123')
            self.assertNotIn('secret-123',(config_dir()/'config.json').read_text())

    def test_detached_command(self):
        with tempfile.TemporaryDirectory(prefix='space 日 ') as temp:
            root=Path(temp)/'20261112-task';root.mkdir()
            with patch.object(cli.shutil,'which',return_value='/usr/bin/tmux'),patch.object(cli.subprocess,'run') as runner:
                runner.return_value.returncode=1
                cli.execute(root,background=True)
                cmd=next(call.args[0] for call in runner.call_args_list if call.args[0][:3]==['tmux','new-session','-d'])
                self.assertEqual(cmd[:3],['tmux','new-session','-d'])
                self.assertIn('--worker',cmd[-1]);self.assertIn('worker.log',cmd[-1])
                self.assertTrue(any('--watch' in call.args[0] for call in runner.call_args_list))

    def test_cancel_monitor_does_not_stop_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'task';root.mkdir()
            with patch.object(cli.shutil,'which',return_value='/usr/bin/tmux'),patch.object(cli.subprocess,'run') as runner,patch.object(cli,'summary'):
                runner.side_effect=[type('Result',(),{'returncode':1})(),type('Result',(),{'returncode':0})(),KeyboardInterrupt]
                cli.execute(root,background=True)
                self.assertFalse(any('stop' in call.args[0] or 'kill-session' in call.args[0] for call in runner.call_args_list))

    def test_progress(self):
        def fake_run(root,retry,event):
            event('file',index=1,total_files=1,source='测试.jp.srt',total=3,done=1)
            event('progress',total=3,done=3,source='测试.jp.srt',stage='翻译')
            event('stage',stage='最终校验')
            event('complete',total=3,done=3,output='/目录/测试.srt')
            return 0
        with patch.object(cli,'run',side_effect=fake_run):self.assertEqual(cli.worker(Path('/tmp/mock')),0)


if __name__=='__main__':unittest.main()
