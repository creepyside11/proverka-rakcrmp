import os
import unittest
from unittest.mock import patch
from storage import Store

@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'TEST_DATABASE_URL is not configured')
class PostgresIntegration(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {'DATABASE_URL':os.environ['TEST_DATABASE_URL']}):
            self.store=Store()
        self.store.init_shop()
        for table in ('orders','products','captchas','words','word_defaults','topics'):
            self.store.db.execute('DELETE FROM '+table)

    def tearDown(self): self.store.db.close()

    def test_settings_captcha_and_store(self):
        s=self.store; chat=-1001234567890
        s.toggle(chat,5,'ads',True)
        self.assertEqual(s.get(chat,5)['ads'],1)
        self.assertIn('дурак',s.words(chat,5))
        s.word(chat,5,'дурак',False)
        self.assertNotIn('дурак',s.words(chat,5))
        s.create_captcha(chat,123,'abc',7,55.5,3)
        self.assertEqual(s.captcha(chat,123)['token'],'abc')
        s.create_captcha(chat,123,'xyz',8,56.5,4)
        self.assertEqual(s.captcha(chat,123)['token'],'xyz')
        s.product_add({'id':'p1','category':'scripts','name':'A','description':'B','price_kopecks':100,
                       'file_id':'file','filename':'a.lua','instruction':'read'})
        s.order_add({'id':'o1','buyer':123,'product_id':'p1','name':'A','price_kopecks':100,'file_id':'file',
            'filename':'a.lua','instruction':'read','provider':'sbp','provider_id':'t1',
            'payment_url':'https://example.com','state':'pending','created_at':1})
        s.mark_paid('o1'); s.mark_delivered('o1')
        self.assertEqual(s.order('o1')['state'],'delivered')

    def test_migration_of_existing_sqlite(self):
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite3'
            old = Store(str(path))
            old.toggle(-1009876543210, 7, 'ads', True)
            old.word(-1009876543210, 7, 'тестовоеслово', True)
            old.create_captcha(-1009876543210,123,'token',4,2000000000,1)
            old.db.close()
            env = {**os.environ, 'DATABASE_URL':os.environ['TEST_DATABASE_URL']}
            subprocess.run([sys.executable,'migrate_sqlite.py',str(path)],env=env,check=True,capture_output=True)
            self.assertEqual(self.store.get(-1009876543210,7)['ads'],1)
            self.assertIn('тестовоеслово',self.store.words(-1009876543210,7))
            self.assertEqual(self.store.captcha(-1009876543210,123)['token'],'token')
            second = subprocess.run([sys.executable,'migrate_sqlite.py',str(path)],env=env,capture_output=True)
            self.assertNotEqual(second.returncode, 0)
