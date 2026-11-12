import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from srtflow.backends import APIError, Backend, validate
from srtflow.storage import DEFAULTS, config_dir, read_json, write_json
from srtflow.subtitles import parse, render, output_name, discover, infer_language
from srtflow.tasks import create, run, read_task

SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello.\n\n2\n00:00:03,000 --> 00:00:04,000\nThank you.\n\n3\n00:00:05,000 --> 00:00:06,000\nGoodbye.\n\n"


class MockBackend:
    calls = []
    mode = "ok"
    def __init__(self, *args): self.usage = []
    def translate(self, items, language, context):
        ids = [i["id"] for i in items]
        self.calls.append(ids)
        self.usage.append({"engine": "mock", "billed_characters": len(items)})
        if self.mode == "interrupt" and 1 in ids:
            raise KeyboardInterrupt
        if self.mode == "auth":
            raise APIError("auth", "测试鉴权失败", global_error=True)
        if self.mode == "rate":
            raise APIError("rate", "测试限流", True)
        if self.mode == "bad_batch" and len(ids) > 1:
            raise APIError("format", "测试缺条", True)
        if self.mode == "single_fail" and 1 in ids:
            raise APIError("format", "测试空译文", True)
        return ["译文"+str(i) for i in ids]


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.base/"config"), "XDG_STATE_HOME": str(self.base/"state")})
        self.env.start()
        self.cfg = dict(DEFAULTS) | {"providers": {"deepl": {"endpoint": "https://api-free.deepl.com", "plan": "free"}}, "batch_items": 1, "retries": 0, "request_interval": 0}
        write_json(config_dir()/"secrets.json", {"deepl": {"key": "TEST-SECRET"}})
        MockBackend.calls = []
        MockBackend.mode = "ok"

    def tearDown(self):
        self.env.stop(); self.temp.cleanup()

    def task(self, filename="电影.2024.jp.srt", policy="skip"):
        source = self.base/filename
        source.write_text(SRT, encoding="utf-8")
        output = self.base/output_name(source)
        return create([{"source": str(source), "output": str(output), "language": "ja", "policy": policy}], self.cfg, "deepl"), source, output

    def go(self, root, **kw): return run(root, backend_factory=MockBackend, sleeper=lambda n: None, **kw)
    def state(self, root): return read_json(root/"status.json")

    def test_roundtrip_en_ja_time_and_number(self):
        cues = parse(SRT.replace("1\n", "001\n", 1))
        text = render(cues, ["こんにちは", "谢谢", "再见"])
        self.assertEqual([(x.number, x.timing) for x in parse(text)], [(x.number, x.timing) for x in cues])

    def test_invalid_srt(self):
        for text in ["", SRT.replace("00:00:02,000", "00:00:00,000"), SRT.replace("2\n00:", "1\n00:")]:
            with self.assertRaises(ValueError): parse(text)

    def test_names(self):
        self.assertEqual(output_name("Movie.2024.jp.srt"), "Movie.2024.srt")
        self.assertEqual(output_name("日本語 空格.en.srt"), "日本語 空格.srt")
        self.assertEqual(infer_language("test.jp.srt"), "ja")
        with self.assertRaises(ValueError): output_name("unknown.srt")

    def test_scan_hidden_recursive_dedup(self):
        d = self.base/"input";d.mkdir();(d/"sub").mkdir();(d/".hidden").mkdir()
        for name in ["a.en.srt", "._a.srt", "b.tmp.srt", "~temp.srt"]:(d/name).touch()
        (d/"sub"/"日本.jp.srt").touch();(d/".hidden"/"secret.srt").touch()
        self.assertEqual(len(discover([d,d/"a.en.srt"], False)),1)
        self.assertEqual(len(discover([d,d/"sub"], True)),2)

    def test_validate_ids(self):
        for rows in [[{"id": 1,"text": "a"},{"id": 1,"text": "b"}], [{"id": 1,"text": ""}], [{"id": 2,"text": "a"}]]:
            with self.assertRaises(APIError): validate(rows,[1])

    def test_deepl_free_payload(self):
        backend = Backend("deepl", self.cfg["providers"]["deepl"], {"key":"TEST"})
        with patch.object(backend,"request",return_value={"translations":[{"text":"你好","billed_characters":5}]}) as request:
            self.assertEqual(backend.translate([{"id":7,"text":"Hello"}],"ja","context"),["你好"])
            body=request.call_args.args[1]
            self.assertEqual(body["source_lang"],"JA");self.assertEqual(body["target_lang"],"ZH-HANS")
            self.assertEqual(body["context"],"context")

    def test_baidu_signature_and_single_cue(self):
        backend=Backend("baidu",{"endpoint":"https://fanyi-api.baidu.com"},{"key":"SECRET","appid":"ID"})
        with patch.object(backend,"request",return_value={"trans_result":[{"dst":"早上好"}]}) as req:
            self.assertEqual(backend.translate([{"id":1,"text":"おはよう"}],"ja"),["早上好"])
            body=req.call_args.args[1];self.assertEqual(body["from"],"jp");self.assertIn("sign",body)

    def test_deepseek_truncation_and_order(self):
        backend=Backend("deepseek",{"endpoint":"https://api.deepseek.com","model":"configured-model"},{"key":"TEST"})
        data={"usage":{"total_tokens":10},"choices":[{"finish_reason":"stop","message":{"content":json.dumps({"translations":[{"id":2,"text":"乙"},{"id":1,"text":"甲"}]})}}]}
        with patch.object(backend,"request",return_value=data):
            self.assertEqual(backend.translate([{"id":1,"text":"a"},{"id":2,"text":"b"}],"en"),["甲","乙"])
        data["choices"][0]["finish_reason"]="length"
        with patch.object(backend,"request",return_value=data):
            with self.assertRaises(APIError):backend.translate([{"id":1,"text":"a"}],"en")

    def test_success_and_idempotence(self):
        root,source,out=self.task();self.assertEqual(self.go(root),0)
        self.assertTrue(out.exists());self.assertEqual(len(MockBackend.calls),3)
        self.assertEqual(self.go(root),0);self.assertEqual(len(MockBackend.calls),3)
        self.assertNotIn("TEST-SECRET",(root/"task.json").read_text())

    def test_interrupt_resume_only_missing(self):
        root,source,out=self.task();MockBackend.mode="interrupt"
        self.assertEqual(self.go(root),1);self.assertFalse(out.exists())
        cp=read_json(root/"0/checkpoint.json");self.assertEqual(set(cp["translations"]),{"0"})
        MockBackend.mode="ok";MockBackend.calls=[]
        self.assertEqual(self.go(root),0);self.assertEqual(MockBackend.calls,[[1],[2]])

    def test_fail_retry_only_failed(self):
        root,source,out=self.task();MockBackend.mode="single_fail"
        self.assertEqual(self.go(root),1);self.assertFalse(out.exists())
        MockBackend.mode="ok";MockBackend.calls=[]
        self.assertEqual(self.go(root,retry_only=True),0);self.assertEqual(MockBackend.calls,[[1]])

    def test_shrink_bad_batch(self):
        self.cfg["batch_items"]=20;root,_,out=self.task();MockBackend.mode="bad_batch"
        self.assertEqual(self.go(root),0);self.assertTrue(out.exists());self.assertIn([0,1,2],MockBackend.calls)
        cp=read_json(root/"0/checkpoint.json");self.assertEqual(sum(x["billed_characters"] for x in cp["usage"]),sum(map(len,MockBackend.calls)))

    def test_global_failure_pauses(self):
        root,_,out=self.task();MockBackend.mode="auth"
        self.assertEqual(self.go(root),1);self.assertEqual(self.state(root)["status"],"paused");self.assertEqual(len(MockBackend.calls),1)

    def test_rate_exhausted_pauses(self):
        root,_,out=self.task();MockBackend.mode="rate"
        self.assertEqual(self.go(root),1);self.assertEqual(self.state(root)["status"],"paused");self.assertEqual(len(MockBackend.calls),1)

    def test_source_change_refused(self):
        root,source,out=self.task();source.write_text(SRT+"\n")
        self.assertEqual(self.go(root),1);self.assertFalse(out.exists());self.assertEqual(MockBackend.calls,[])

    def test_snapshot_change_refused(self):
        root,_,_=self.task();data=read_json(root/"task.json");data["snapshot"]["timeout"]=999;write_json(root/"task.json",data)
        with self.assertRaises(RuntimeError):read_task(root)

    def test_default_config_change_does_not_change_task(self):
        root,_,out=self.task();write_json(config_dir()/"config.json",{"default_engine":"baidu"})
        self.assertEqual(self.go(root),0);self.assertEqual(read_task(root)["snapshot"]["engine"],"deepl")

    def test_new_output_conflict_not_overwritten(self):
        root,_,out=self.task();out.write_text("OTHER")
        self.assertEqual(self.go(root),1);self.assertEqual(out.read_text(),"OTHER")

    def test_backup_existing_output(self):
        source=self.base/"Movie.en.srt";source.write_text(SRT);out=self.base/"Movie.srt";out.write_text("OLD")
        root=create([{"source":str(source),"output":str(out),"language":"en","policy":"backup"}],self.cfg,"deepl")
        self.assertEqual(self.go(root),0);self.assertEqual(out.with_name(out.name+".bak-"+root.name).read_text(),"OLD")

    def test_task_and_output_locks(self):
        from srtflow.storage import lock,sha,private_dir,state_dir
        root,_,out=self.task()
        with lock(root/"worker.lock"):
            with self.assertRaises(RuntimeError):self.go(root)
        path=private_dir(state_dir()/"output-locks")/(sha(str(out))+".lock")
        with lock(path):self.assertEqual(self.go(root),1)
        self.assertFalse(out.exists())

    def test_permissions(self):
        root,_,_=self.task()
        self.assertEqual((config_dir()/"secrets.json").stat().st_mode & 0o777,0o600)
        self.assertEqual(root.stat().st_mode & 0o777,0o700)


if __name__ == "__main__":unittest.main()
