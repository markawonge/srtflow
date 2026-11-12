import io
import json
import unittest
import urllib.error
from unittest.mock import patch
from srtflow.backends import Backend, APIError


class NetworkTests(unittest.TestCase):
    def backend(self):return Backend("deepl",{"endpoint":"https://api-free.deepl.com"},{"key":"PRIVATE"})

    def test_http_errors(self):
        for code,category,global_error in [(403,"auth",True),(456,"quota",True),(429,"rate",False),(503,"server",False),(400,"configuration",True)]:
            with self.subTest(code=code), patch('urllib.request.build_opener') as opener:
                opener.return_value.open.side_effect=urllib.error.HTTPError('https://api-free.deepl.com/v2/translate',code,'private body',{},None)
                with self.assertRaises(APIError) as ctx:self.backend().request('/v2/translate',{})
                self.assertEqual(ctx.exception.category,category)
                self.assertEqual(ctx.exception.global_error,global_error)
                self.assertNotIn('private body',str(ctx.exception))

    def test_network(self):
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=urllib.error.URLError('PRIVATE')
            with self.assertRaises(APIError) as ctx:self.backend().request('/v2/translate',{})
            self.assertEqual(ctx.exception.category,'network');self.assertNotIn('PRIVATE',str(ctx.exception))

    def test_request_auth_and_json(self):
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value=b'{"translations":[]}'
            result=self.backend().request('/v2/translate',{'text':['日本語']})
            request=opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url,'https://api-free.deepl.com/v2/translate')
            self.assertEqual(request.get_header('Authorization'),'DeepL-Auth-Key PRIVATE')
            self.assertEqual(json.loads(request.data)['text'],['日本語'])

    def test_invalid_json(self):
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value=b'html error'
            with self.assertRaises(APIError) as ctx:self.backend().request('/v2/translate',{})
            self.assertEqual(ctx.exception.category,'format')


if __name__=='__main__':unittest.main()
