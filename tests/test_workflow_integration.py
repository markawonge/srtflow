"""Exercise the real CLI and HTTP adapter with a small loopback test service."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from srtflow.storage import DEFAULTS
import copy


class WorkflowIntegrationTests(unittest.TestCase):
    def test_cli_config_to_file_to_local_api_to_export(self):
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path,body))
                items=json.loads(body['messages'][1]['content'])['translate_only']
                payload={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'translations':[{'id':i['id'],'text':'模拟中文译文'} for i in items]})}}]}
                content=json.dumps(payload).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='srtflow 日本語 ') as temp:
                base=Path(temp);config=base/'config/srtflow';config.mkdir(parents=True)
                cfg=copy.deepcopy(DEFAULTS)
                cfg.update(default_engine='local',background=False,request_interval=0)
                cfg['providers']={'local':{'endpoint':f'http://127.0.0.1:{server.server_port}/v1','model':'mock-local','verified':True,'last_test':'mock-verified'}}
                (config/'config.json').write_text(json.dumps(cfg))
                source=base/'影片.2024 日本語.en.srt'
                source.write_text('001\n00:00:01,000 --> 00:00:02,000\nHello\n\n002\n00:00:03,000 --> 00:00:04,000\nThanks\n\n')
                inputs='\n3\n3\n'+str(source)+'\n\na\n\n'
                env=dict(os.environ,XDG_CONFIG_HOME=str(base/'config'),XDG_STATE_HOME=str(base/'state'))
                result=subprocess.run([sys.executable,'-m','srtflow.cli'],input=inputs,text=True,capture_output=True,env=env,timeout=15)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                output=base/'影片.2024 日本語.srt'
                self.assertTrue(output.exists(),result.stdout)
                self.assertIn('001\n00:00:01,000 --> 00:00:02,000\n模拟中文译文',output.read_text())
                self.assertEqual(len(requests),1,'daily invocation must not add an API test')
                self.assertEqual(requests[0][0],'/v1/chat/completions')
                self.assertNotIn('执行方式\n',result.stdout)
                self.assertNotIn('VAD阈值：',result.stdout)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)
