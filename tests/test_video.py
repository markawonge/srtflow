import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from srtflow import tasks,transcribe,asr_worker
from srtflow.storage import DEFAULTS,write_json,read_json,file_sha,sha

SRT="1\n00:00:02,500 --> 00:00:04,000\nHello.\n\n"


class FakeBackend:
    count=0
    fail=False
    def __init__(self,*args):self.usage=[]
    def translate(self,items,*args):
        self.count+=1
        if self.fail:
            from srtflow.backends import APIError
            raise APIError('auth','测试密钥失效',global_error=True)
        return ['你好。' for i in items]


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'XDG_CONFIG_HOME':str(self.base/'config'),'XDG_STATE_HOME':str(self.base/'state')});self.env.start()
        import copy
        self.config=copy.deepcopy(DEFAULTS)
        self.config['providers']={'deepl':{'endpoint':'https://api-free.deepl.com'}}
        self.config['transcription']['image_id']='sha256:test'
        self.config['transcription']['cache_dir']=str(self.base/'cache')
        self.config['retries']=0;self.config['request_interval']=0
        self.config['task_mode']='video_translate'
        self.asr_calls=0
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def make(self,language='ja',reuse=False):
        video=self.base/'影片.2024 空格.mp4';video.write_bytes(b'video fixture')
        source=self.base/(video.stem+'.jp.srt');output=self.base/(video.stem+'.srt')
        if reuse:source.write_text(SRT)
        candidates=[source] if language!='auto' else [source,self.base/(video.stem+'.en.srt')]
        plan={'kind':'video','source':str(video),'output':str(output),'output_dir':str(self.base),
              'language':language,'policy':'skip','source_output':str(source) if language!='auto' else str(self.base/(video.stem+'.auto.srt')),
              'source_expected':{str(p):file_sha(p) if p.exists() else None for p in candidates},
              'source_existing_sha':file_sha(source) if reuse else None,'source_policy':'reuse' if reuse else 'skip','reuse_source':reuse}
        root=tasks.create([plan],self.config,None if self.config['task_mode']=='transcribe' else 'deepl')
        return root,video,source,output
    def fake_ensure(self,root,index,item,cfg,emit,save):
        if not item.get('source_sha'):
            self.asr_calls+=1
            content=SRT.encode();(root/str(index)/'source.srt').write_bytes(content)
            item['language']='ja';item['source_sha']=sha(content)
            item['source_output']=str(Path(item['output_dir'])/(Path(item['source']).stem+'.jp.srt'))
            Path(item['source_output']).write_bytes(content);item['source_published_sha']=sha(content);item['total']=1
            save()
        return tasks.files_compatible(root,index,item)
    def run_task(self,root):
        with patch.object(transcribe,'ensure_source',side_effect=self.fake_ensure):
            return tasks.run(root,backend_factory=FakeBackend,sleeper=lambda n:None)

    def test_video_translate_names(self):
        root,video,source,output=self.make()
        self.assertEqual(self.run_task(root),0);self.assertTrue(source.exists());self.assertTrue(output.exists())
        self.assertEqual(self.asr_calls,1);self.assertEqual(self.run_task(root),0);self.assertEqual(self.asr_calls,1)

    def test_transcribe_only_no_backend(self):
        self.config['task_mode']='transcribe';root,video,source,output=self.make()
        with patch.object(transcribe,'ensure_source',side_effect=self.fake_ensure):
            def forbidden(*a):raise AssertionError('不应调用API')
            self.assertEqual(tasks.run(root,backend_factory=forbidden),0)
        self.assertTrue(source.exists());self.assertFalse(output.exists())

    def test_translation_retry_keeps_asr(self):
        root,video,source,output=self.make()
        FakeBackend.fail=True
        try:self.assertEqual(self.run_task(root),1)
        finally:FakeBackend.fail=False
        self.assertTrue(source.exists());self.assertFalse(output.exists())
        self.assertEqual(self.run_task(root),0);self.assertEqual(self.asr_calls,1)

    def test_changed_video_refused(self):
        root,video,source,output=self.make();self.assertEqual(self.run_task(root),0)
        video.write_bytes(b'changed');self.assertEqual(self.run_task(root),1)

    def test_changed_published_source_refused(self):
        root,video,source,output=self.make();self.assertEqual(self.run_task(root),0)
        source.write_text(SRT.replace('Hello','Changed'));self.assertEqual(self.run_task(root),1)

    def test_reuse_source_without_docker(self):
        root,video,source,output=self.make(reuse=True)
        item=read_json(root/'status.json')['files'][0]
        with patch.object(transcribe,'preflight',side_effect=AssertionError('不应调用Docker')):
            cues=transcribe.ensure_source(root,0,item,tasks.read_task(root)['snapshot'],lambda *a,**kw:None,lambda:None)
        self.assertEqual(len(cues),1);self.assertEqual(item['language'],'ja')

    def test_asr_worker_region_checkpoint(self):
        import sys
        calls=[]
        class Audio(list):
            def __getitem__(self,i):return Audio(super().__getitem__(i)) if isinstance(i,slice) else super().__getitem__(i)
        class Model:
            def __init__(self,*a,**kw):pass
            def transcribe(self,audio,**kw):
                calls.append(kw)
                return iter([types.SimpleNamespace(start=.5,end=2.,text='こんにちは')]),types.SimpleNamespace(language='ja')
        fw=types.ModuleType('faster_whisper');fw.WhisperModel=Model
        au=types.ModuleType('faster_whisper.audio');au.decode_audio=lambda *a,**kw:Audio([0]*160000)
        va=types.ModuleType('faster_whisper.vad');va.VadOptions=lambda **kw:kw
        va.get_speech_timestamps=lambda *a,**kw:[{'start':32000,'end':64000},{'start':96000,'end':128000}]
        folder=self.base/'job';folder.mkdir();write_json(folder/'asr-settings.json',{'config':self.config['transcription'],'language':'ja','input_sha':'test'})
        with patch.dict(sys.modules,{'faster_whisper':fw,'faster_whisper.audio':au,'faster_whisper.vad':va}),patch.object(sys,'argv',['worker','--input','fake.mp4','--job',str(folder)]):
            asr_worker.main();self.assertEqual(len(calls),2)
            text=(folder/'source.srt').read_text();self.assertIn('00:00:02,500 --> 00:00:04,000',text);self.assertIn('00:00:06,500 --> 00:00:08,000',text)
            asr_worker.main();self.assertEqual(len(calls),2)
        self.assertEqual(read_json(folder/'asr-checkpoint.json')['next_region'],2)

    def test_auto_naming(self):
        root,video,source,output=self.make(language='auto')
        self.assertEqual(self.run_task(root),0)
        state=read_json(root/'status.json')['files'][0]
        self.assertEqual(state['language'],'ja');self.assertTrue(state['source_output'].endswith('.jp.srt'))

    def test_docker_adapter_readonly_and_cache(self):
        root,video,source,output=self.make()
        item=read_json(root/'status.json')['files'][0];folder=root/'0'
        original_stat=Path.stat;original_glob=Path.glob
        def stat(path,*a,**kw):
            return types.SimpleNamespace(st_gid=990) if str(path)=='/dev/kfd' else original_stat(path,*a,**kw)
        def glob(path,pattern):
            return iter([]) if str(path)=='/dev/dri' else original_glob(path,pattern)
        command=[]
        class Process:
            def __init__(self,cmd,**kw):
                command.extend(cmd)
                (folder/'source.srt').write_text(SRT)
                write_json(folder/'asr-result.json',{'language':'ja','count':1,'duration':10})
                self.stdout=['SRTFLOW_EVENT '+json.dumps({'kind':'asr_progress','done':2,'total':2,'full_duration':10,'regions':1})+'\n']
            def wait(self,*a,**kw):return 0
        events=[]
        with patch.object(transcribe,'preflight',return_value='sha256:test'),patch.object(Path,'stat',stat),patch.object(Path,'glob',glob),patch.object(transcribe.subprocess,'Popen',Process):
            cues=transcribe.ensure_source(root,0,item,tasks.read_task(root)['snapshot'],lambda k,**v:events.append(k),lambda:None)
        self.assertEqual(len(cues),1)
        self.assertIn(f'{video.parent}:/input:ro',command)
        self.assertIn(f"{self.config['transcription']['cache_dir']}:/cache",command)
        self.assertIn('asr_progress',events);self.assertTrue(source.exists())

    def test_container_partial_resume_only_unfinished_region(self):
        import sys
        count=[0];stop=[True]
        class Audio(list):
            def __getitem__(self,i):return Audio(super().__getitem__(i)) if isinstance(i,slice) else super().__getitem__(i)
        class Model:
            def __init__(self,*a,**kw):pass
            def transcribe(self,audio,**kw):
                count[0]+=1
                if stop[0] and count[0]==2:raise KeyboardInterrupt
                return iter([types.SimpleNamespace(start=.5,end=2.,text='Hello')]),types.SimpleNamespace(language='en')
        fw=types.ModuleType('faster_whisper');fw.WhisperModel=Model
        au=types.ModuleType('faster_whisper.audio');au.decode_audio=lambda *a,**kw:Audio([0]*160000)
        va=types.ModuleType('faster_whisper.vad');va.VadOptions=lambda **kw:kw
        va.get_speech_timestamps=lambda *a,**kw:[{'start':32000,'end':64000},{'start':96000,'end':128000}]
        folder=self.base/'partial';folder.mkdir();write_json(folder/'asr-settings.json',{'config':self.config['transcription'],'language':'en','input_sha':'test'})
        with patch.dict(sys.modules,{'faster_whisper':fw,'faster_whisper.audio':au,'faster_whisper.vad':va}),patch.object(sys,'argv',['worker','--input','fake.mp4','--job',str(folder)]):
            with self.assertRaises(KeyboardInterrupt):asr_worker.main()
            self.assertEqual(read_json(folder/'asr-checkpoint.json')['next_region'],1)
            self.assertFalse((folder/'source.srt').exists())
            stop[0]=False;before=count[0];asr_worker.main()
            self.assertEqual(count[0]-before,1)

    def test_video_scan_names_and_hidden(self):
        from srtflow.subtitles import discover,VIDEO_EXTENSIONS
        folder=self.base/'scan';folder.mkdir()
        for name in ['._test.mp4','test.part.mp4','电影.2024.mkv','日本語 clip.mp4']:(folder/name).touch()
        self.assertEqual(len(discover([folder],extensions=VIDEO_EXTENSIONS)),2)


if __name__=='__main__':unittest.main()
