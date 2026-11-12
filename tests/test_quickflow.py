import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from rich.console import Console
from srtflow import cli, quickflow, tasks
from srtflow.backends import Backend, APIError
from srtflow.storage import DEFAULTS, load_config, config_dir, write_json, read_json

SRT='1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n00:00:03,000 --> 00:00:04,000\nThanks\n\n'


class QuickFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.base=Path(self.temp.name)
        self.env=patch.dict(os.environ, {'XDG_CONFIG_HOME':str(self.base/'config'), 'XDG_STATE_HOME':str(self.base/'state')});self.env.start()
        self.cfg=copy.deepcopy(DEFAULTS)
        self.cfg['providers']={'deepl':{'endpoint':'https://api-free.deepl.com','last_test':'previous-test','verified':True}}
        self.cfg.update(batch_items=1,retries=0,request_interval=0)
        self.out=io.StringIO();self.display=patch.object(cli,'console',Console(file=self.out,width=120));self.display.start()
    def tearDown(self):
        self.display.stop();self.env.stop();self.temp.cleanup()

    def test_old_config_merges_new_defaults(self):
        write_json(config_dir()/'config.json',{'source_language':'en','transcription':{'threshold':.4},'providers':self.cfg['providers']})
        config=load_config()
        self.assertEqual(config['transcription']['threshold'],.4)
        self.assertEqual(config['transcription']['model'],'large-v3')
        self.assertEqual(config['target_language'],'zh-Hans')
        self.assertTrue(config['background'])

    def test_first_launch_enters_wizard_without_modify_question(self):
        with patch.object(cli,'config_wizard') as wizard,patch.object(cli,'choice',return_value='exit'),patch.object(cli,'yes') as yes:
            quickflow.start()
            wizard.assert_called_once();yes.assert_not_called()

    def test_existing_launch_only_modify_then_function(self):
        write_json(config_dir()/'config.json',self.cfg)
        with patch('builtins.input',side_effect=['','2']) as ask,patch.object(quickflow,'new_task') as new,patch.object(cli,'config_wizard') as wizard:
            quickflow.start()
            self.assertEqual(ask.call_count,2);wizard.assert_not_called()
            self.assertEqual(new.call_args.args[1],'transcribe')

    def test_configuration_summary_never_shows_secrets(self):
        write_json(config_dir()/'secrets.json',{'deepl':{'key':'PRIVATE'}})
        self.cfg['providers']['deepl']['endpoint']='https://account:PRIVATE@api.example.test/v1?key=PRIVATE'
        quickflow.config_summary(self.cfg)
        self.assertNotIn('PRIVATE',self.out.getvalue())
        self.assertIn('简体中文',self.out.getvalue())

    def test_known_provider_no_daily_test(self):
        with patch.object(cli,'test_provider') as test,patch.object(cli,'configure_provider') as configure:
            self.assertEqual(quickflow.ensure_engine(self.cfg),'deepl')
            test.assert_not_called();configure.assert_not_called()

    def test_video_daily_only_input_location_selection_confirmation(self):
        (self.base/'影片.2024 日本語.mp4').write_bytes(b'fixture')
        with patch.object(Path,'cwd',return_value=self.base),patch('builtins.input',side_effect=['1','a','']) as ask,patch('srtflow.transcribe.preflight',return_value='sha256:test'),patch.object(cli,'execute') as execute,patch.object(cli,'test_provider') as test:
            quickflow.new_task(self.cfg,'video_translate')
            self.assertEqual(ask.call_count,3)
            test.assert_not_called();execute.assert_called_once()
            spec=tasks.read_task(execute.call_args.args[0])
            self.assertEqual(spec['files'][0]['source_output'],str(self.base/'影片.2024 日本語.jp.srt'))
            self.assertEqual(spec['snapshot']['transcription']['threshold'],.6)
            self.assertTrue(execute.call_args.kwargs['background'])

    def test_translation_daily_only_input_location_selection_confirmation(self):
        (self.base/'English.2024.en.srt').write_text(SRT)
        with patch.object(Path,'cwd',return_value=self.base),patch('builtins.input',side_effect=['1','a','']) as ask,patch.object(cli,'execute') as execute:
            quickflow.new_task(self.cfg,'translate')
            self.assertEqual(ask.call_count,3)
            spec=tasks.read_task(execute.call_args.args[0])
            self.assertEqual(spec['files'][0]['language'],'en')
            self.assertEqual(spec['files'][0]['output'],str(self.base/'English.2024.srt'))

    def test_existing_result_default_skip_no_prompts(self):
        video=self.base/'a.mp4';video.touch();(self.base/'a.srt').write_text('existing')
        with patch('builtins.input',side_effect=AssertionError('不应逐项询问')):
            self.assertEqual(cli.video_plans([video],self.cfg,'video_translate',quick=True),[])

    def test_nonrecursive_default_never_asks_recursive(self):
        (self.base/'a.mp4').touch();(self.base/'.hidden.mp4').touch();(self.base/'._temp.mp4').touch()
        with patch.object(Path,'cwd',return_value=self.base),patch('builtins.input',side_effect=['1','a']) as ask:
            self.assertEqual(cli.choose_inputs(self.cfg,True,True),[self.base/'a.mp4']);self.assertEqual(ask.call_count,2)

    def test_configure_always_performs_real_test(self):
        with patch('builtins.input',side_effect=['1','']),patch.object(cli.getpass,'getpass',return_value='FAKE'),patch.object(Backend,'test',return_value='你好') as test:
            self.assertTrue(cli.configure_provider(self.cfg,'deepl'))
            test.assert_called_once()
        self.assertTrue(load_config()['providers']['deepl']['verified'])

    def test_failed_configuration_not_verified(self):
        with patch('builtins.input',side_effect=['1','','0']),patch.object(cli.getpass,'getpass',return_value='FAKE'),patch.object(Backend,'test',side_effect=APIError('auth','模拟失败',global_error=True)):
            self.assertFalse(cli.configure_provider(self.cfg,'deepl'))
        self.assertFalse(load_config()['providers']['deepl']['verified'])

    def test_local_optional_key_configuration(self):
        with patch('builtins.input',side_effect=['','local-model','','']),patch.object(cli.getpass,'getpass',return_value=''),patch.object(Backend,'test',return_value='你好'):
            self.assertTrue(cli.configure_provider(self.cfg,'local'))
        self.assertEqual(self.cfg['providers']['local']['endpoint'],'http://127.0.0.1:8000/v1')

    def test_local_payload_and_keyless_http(self):
        backend=Backend('local',{'endpoint':'http://127.0.0.1:8000/v1','model':'local-model','json_mode':False},None)
        data={'choices':[{'finish_reason':'stop','message':{'content':'{"translations":[{"id":5,"text":"你好"}]}'}}]}
        with patch.object(backend,'request',return_value=data) as request:
            self.assertEqual(backend.translate([{'id':5,'text':'Hello'}],'en'),['你好'])
            self.assertNotIn('response_format',request.call_args.args[1])
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value=b'{}'
            backend.request('/chat/completions',{})
            request=opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url,'http://127.0.0.1:8000/v1/chat/completions')
            self.assertIsNone(request.get_header('Authorization'))

    def make_task(self):
        source=self.base/'a.en.srt';source.write_text(SRT)
        write_json(config_dir()/'secrets.json',{'deepl':{'key':'PRIVATE'}})
        return tasks.create([{'source':str(source),'output':str(self.base/'a.srt'),'language':'en','policy':'skip'}],self.cfg,'deepl')

    def test_api_failure_reconfigure_and_resume_only_missing(self):
        root=self.make_task()
        class Failing:
            def __init__(self,*a): self.usage=[]
            def translate(self,items,*a):
                if items[0]['id']==1: raise APIError('auth','模拟失效',global_error=True)
                return ['已完成译文']
        self.assertEqual(tasks.run(root,backend_factory=Failing,sleeper=lambda _:None),1)
        status=read_json(root/'status.json')
        self.assertEqual(status['api_failure']['category'],'auth')
        tasks.reconfigure_translation(root,'local',{'endpoint':'http://localhost:8000/v1','model':'local','last_test':'now','verified':True})
        calls=[]
        class Success:
            def __init__(self,*a): self.usage=[]
            def translate(self,items,*a): calls.append(items[0]['id']);return ['新译文']
        self.assertEqual(tasks.run(root,backend_factory=Success,sleeper=lambda _:None),0)
        self.assertEqual(calls,[1])
        cp=read_json(root/'0/checkpoint.json')
        self.assertEqual(cp['origins']['0']['engine'],'deepl')
        self.assertEqual(cp['origins']['1']['engine'],'local')
        self.assertTrue((root/'config-revisions/0001.json').exists())
        self.assertNotIn('PRIVATE',(root/'task.json').read_text())
        self.assertNotIn('api_failure',read_json(root/'status.json'))

    def test_reconfigure_rejects_changed_original(self):
        root=self.make_task();status=read_json(root/'status.json');status['status']='paused';write_json(root/'status.json',status)
        (self.base/'a.en.srt').write_text(SRT+'\n')
        with self.assertRaises(RuntimeError):tasks.reconfigure_translation(root,'deepl',self.cfg['providers']['deepl'])

    def test_reconfigure_rejects_active_worker(self):
        from srtflow.storage import lock
        root=self.make_task()
        with lock(root/'worker.lock'):
            with self.assertRaises(RuntimeError):tasks.reconfigure_translation(root,'deepl',self.cfg['providers']['deepl'])

    def test_recovery_prompt_after_api_pause(self):
        root=self.make_task();status=read_json(root/'status.json');status.update(status='paused',api_failure={'category':'auth','message':'模拟失效'});write_json(root/'status.json',status)
        write_json(config_dir()/'config.json',self.cfg)
        with patch('sys.stdin.isatty',return_value=True),patch('builtins.input',side_effect=['','1','']),patch.object(cli,'configure_provider',return_value=True),patch.object(tasks,'reconfigure_translation') as reconfigure:
            self.assertTrue(quickflow.offer_recovery(root));reconfigure.assert_called_once()

    def test_detached_api_failure_never_reads_stdin(self):
        root=self.make_task();status=read_json(root/'status.json');status.update(status='paused',api_failure={'category':'auth','message':'模拟失效'});write_json(root/'status.json',status)
        with patch('sys.stdin.isatty',return_value=False),patch('builtins.input',side_effect=AssertionError('后台不读取输入')):
            self.assertFalse(quickflow.offer_recovery(root))
