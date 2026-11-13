import base64
import gzip
import os
import tempfile
import time
import unittest
from moderation import check_text, is_lua
from scanner import inspect
from storage import Store

class ScannerTests(unittest.TestCase):
    def test_clear_is_not_bytecode(self):
        self.assertEqual(inspect(b'print("hello")', 'test.lua').status, 'clear')
        self.assertEqual(inspect(b'\x1bLua\x00print', 'test.luac').status, 'unknown')
        self.assertEqual(inspect(b'print("hello")', 'test.luac').status, 'unknown')
    def test_commands_and_encoding(self):
        self.assertEqual(inspect(b'os.execute("id")', 'a.lua').status, 'suspicious')
        self.assertEqual(inspect(br'\111\115\046\101\120\101\099\117\116\101("id")', 'a.lua').status, 'suspicious')
        payload = base64.b64encode(b'os.execute("very dangerous command foo bar")')
        self.assertEqual(inspect(b'local x="'+payload+b'"', 'a.lua').status, 'suspicious')
        self.assertEqual(inspect(gzip.compress(b'os.execute("id")'), 'a.lua').status, 'suspicious')
    def test_unknown(self):
        for data in (b'', b'loadstring(x)()', b'\xff\x00'):
            self.assertEqual(inspect(data,'a.lua').status, 'unknown')
        self.assertEqual(inspect(b'x' * (5*1024*1024+1), 'x.lua').status, 'unknown')

class FilterTests(unittest.TestCase):
    def test_filenames(self):
        for name in ['a.lua','a.LUAC']: self.assertTrue(is_lua(name))
        for name in ['x.lua.exe',None, 'x.zip']: self.assertFalse(is_lua(name))
    def test_text(self):
        cfg={'ads':1,'insults':1}
        self.assertIsNotNone(check_text('Привет, дУрАк!',[],cfg,['дурак']))
        self.assertIsNone(check_text('дурачок',[],cfg,['дурак']))
        self.assertIsNotNone(check_text('https://example.com',[],cfg,[]))
        self.assertIsNone(check_text('hello',[],cfg,[]))

class StorageTests(unittest.TestCase):
    def test_isolation_persistence_and_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp,'data.sqlite3')
            s = Store(path)
            s.toggle(-1,4,'ads',True)
            s.word(-1,4,'дурак',False)
            s.schedule(-1,4,1,'проверяйте файлы')
            s.db.close()
            s = Store(path)
            self.assertEqual(s.get(-1,4)['ads'],1)
            self.assertEqual(s.get(-1,5)['ads'],0)
            self.assertNotIn('дурак',s.words(-1,4))
            self.assertIn('дурак',s.words(-1,5))
            self.assertEqual(s.get(-1,4)['notice'],'проверяйте файлы')
            self.assertEqual(s.due(),[])
            s.db.execute('UPDATE topics SET next_at=?', (time.time()-1,)); s.db.commit()
            self.assertEqual(len(s.due()),1)
            s.advance(-1,4)
            self.assertEqual(s.due(),[])
            s.schedule(-1,4,0,'')
            self.assertEqual(s.get(-1,4)['interval_sec'],0)
            with self.assertRaises(ValueError): s.toggle(-1,4,'unknown',True)
            s.db.close()

if __name__ == '__main__': unittest.main()
