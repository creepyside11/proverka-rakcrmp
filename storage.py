import sqlite3
import os
import re
import time

DEFAULT_WORDS = ('дурак', 'идиот', 'дебил', 'мудак', 'тварь')
TOGGLES = ('insults', 'ads', 'luaonly', 'unknown_delete', 'scan')

class PostgresConnection:
    """Small compatibility layer for the existing moderation queries."""
    def __init__(self, url):
        import psycopg
        from psycopg.rows import dict_row
        self.conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)

    @staticmethod
    def convert(sql):
        sql = sql.replace('?', '%s')
        sql = re.sub(r'\buser\b', '"user"', sql, flags=re.I)
        if 'INSERT OR IGNORE INTO' in sql:
            sql = sql.replace('INSERT OR IGNORE INTO', 'INSERT INTO') + ' ON CONFLICT DO NOTHING'
        elif 'INSERT OR REPLACE INTO captchas' in sql:
            sql = sql.replace('INSERT OR REPLACE INTO', 'INSERT INTO')
            sql += """ ON CONFLICT (chat, "user") DO UPDATE SET token=EXCLUDED.token, correct=EXCLUDED.correct, expires=EXCLUDED.expires, attempts=0, state='preparing', message_id=NULL, question_a=EXCLUDED.question_a, widget_message_id=NULL"""
        return sql

    def execute(self, sql, params=()):
        return self.conn.execute(self.convert(sql), params)

    def executemany(self, sql, rows):
        with self.conn.cursor() as cur:
            cur.executemany(self.convert(sql), rows)

    def executescript(self, sql):
        for statement in sql.split(';'):
            statement = statement.strip()
            if statement and not statement.startswith('PRAGMA'):
                statement = re.sub(r'\bINTEGER\b', 'BIGINT', statement)
                statement = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', statement)
                self.execute(statement)

    def commit(self):
        pass

    def close(self):
        self.conn.close()

class Store:
    def __init__(self, path=None):
        if path is None:
            url = os.getenv('DATABASE_URL', '').strip()
            if not url.startswith(('postgres://', 'postgresql://')):
                raise RuntimeError('Укажите PostgreSQL DATABASE_URL')
            self.db = PostgresConnection(url)
        else:
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS topics (
          chat INTEGER NOT NULL, topic INTEGER NOT NULL,
          insults INTEGER NOT NULL DEFAULT 0, ads INTEGER NOT NULL DEFAULT 0,
          luaonly INTEGER NOT NULL DEFAULT 0, unknown_delete INTEGER NOT NULL DEFAULT 1,
          scan INTEGER NOT NULL DEFAULT 1, interval_sec INTEGER NOT NULL DEFAULT 0,
          notice TEXT NOT NULL DEFAULT '', next_at REAL NOT NULL DEFAULT 0,
          PRIMARY KEY(chat, topic));
        CREATE TABLE IF NOT EXISTS words (chat INTEGER NOT NULL, topic INTEGER NOT NULL,
          word TEXT NOT NULL, PRIMARY KEY(chat,topic,word));
        CREATE TABLE IF NOT EXISTS word_defaults (chat INTEGER NOT NULL, topic INTEGER NOT NULL,
          PRIMARY KEY(chat,topic));
        CREATE TABLE IF NOT EXISTS captchas (
          chat INTEGER NOT NULL, user INTEGER NOT NULL, token TEXT UNIQUE NOT NULL,
          correct INTEGER NOT NULL, expires REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL DEFAULT 'pending', message_id INTEGER, question_a INTEGER, widget_message_id INTEGER,
          PRIMARY KEY(chat,user));
        CREATE TABLE IF NOT EXISTS bot_users (
          user_id INTEGER PRIMARY KEY, first_seen REAL NOT NULL, subscribed INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS required_channels (
          chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, username TEXT);
        CREATE TABLE IF NOT EXISTS captcha_results (
          token TEXT PRIMARY KEY, result TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS web_challenges (
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, question TEXT NOT NULL,
          options TEXT NOT NULL, correct INTEGER NOT NULL, expires REAL NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, demo INTEGER NOT NULL DEFAULT 0);
        ''')
        self.db.commit()

    def ensure(self, chat, topic):
        self.db.execute('INSERT OR IGNORE INTO topics(chat,topic) VALUES(?,?)', (chat,topic))
        if self.db.execute('INSERT OR IGNORE INTO word_defaults VALUES(?,?)', (chat,topic)).rowcount:
            self.db.executemany('INSERT OR IGNORE INTO words VALUES(?,?,?)', [(chat,topic,w) for w in DEFAULT_WORDS])
        self.db.commit()

    def get(self, chat, topic):
        self.ensure(chat,topic)
        return dict(self.db.execute('SELECT * FROM topics WHERE chat=? AND topic=?', (chat,topic)).fetchone())

    def toggle(self, chat, topic, key, value):
        if key not in TOGGLES: raise ValueError('unknown setting')
        self.ensure(chat,topic)
        self.db.execute(f'UPDATE topics SET {key}=? WHERE chat=? AND topic=?', (int(value),chat,topic))
        self.db.commit()

    def words(self, chat, topic):
        self.ensure(chat, topic)
        return [r['word'] for r in self.db.execute('SELECT word FROM words WHERE chat=? AND topic=? ORDER BY word', (chat,topic))]

    def word(self, chat, topic, word, add):
        self.ensure(chat,topic)
        if add: self.db.execute('INSERT OR IGNORE INTO words VALUES(?,?,?)', (chat,topic,word))
        else: self.db.execute('DELETE FROM words WHERE chat=? AND topic=? AND word=?', (chat,topic,word))
        self.db.commit()

    def schedule(self, chat, topic, minutes, notice):
        self.ensure(chat,topic)
        self.db.execute('UPDATE topics SET interval_sec=?,notice=?,next_at=? WHERE chat=? AND topic=?', (minutes*60,notice,time.time()+minutes*60 if minutes else 0,chat,topic))
        self.db.commit()

    def due(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM topics WHERE interval_sec>0 AND next_at<=?', (time.time(),))]

    def advance(self, chat, topic):
        self.db.execute('UPDATE topics SET next_at=?+interval_sec WHERE chat=? AND topic=?', (time.time(),chat,topic))
        self.db.commit()

    def create_captcha(self, chat, user, token, correct, expires, question_a):
        self.db.execute('''INSERT OR REPLACE INTO captchas
          (chat,user,token,correct,expires,attempts,state,message_id,question_a)
          VALUES(?,?,?,?,?,0,'preparing',NULL,?)''', (chat,user,token,correct,expires,question_a))
        self.db.commit()

    def captcha(self, chat, user):
        row = self.db.execute('SELECT * FROM captchas WHERE chat=? AND user=?', (chat,user)).fetchone()
        return dict(row) if row else None

    def captcha_token(self, token):
        row = self.db.execute('SELECT * FROM captchas WHERE token=?', (token,)).fetchone()
        return dict(row) if row else None

    def captcha_update(self, chat, user, **values):
        allowed = {'state', 'attempts', 'message_id', 'widget_message_id'}
        if not values or not set(values) <= allowed: raise ValueError('invalid fields')
        fields = ','.join(k + '=?' for k in values)
        self.db.execute(f'UPDATE captchas SET {fields} WHERE chat=? AND user=?', (*values.values(),chat,user))
        self.db.commit()

    def captcha_remove(self, chat, user):
        self.db.execute('DELETE FROM captchas WHERE chat=? AND user=?', (chat,user))
        self.db.commit()

    def captcha_waiting(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM captchas')]

    def register_user(self, user_id):
        self.db.execute('INSERT OR IGNORE INTO bot_users(user_id,first_seen,subscribed) VALUES(?,?,1)', (user_id,time.time()))
        self.db.commit()

    def opt_out(self, user_id):
        self.db.execute('UPDATE bot_users SET subscribed=0 WHERE user_id=?', (user_id,))
        self.db.commit()

    def opt_in(self, user_id):
        self.register_user(user_id)
        self.db.execute('UPDATE bot_users SET subscribed=1 WHERE user_id=?', (user_id,))
        self.db.commit()

    def broadcast_users(self):
        return [r['user_id'] for r in self.db.execute('SELECT user_id FROM bot_users WHERE subscribed=1 ORDER BY user_id')]

    def add_channel(self, chat_id, title, url, username):
        self.db.execute('DELETE FROM required_channels WHERE chat_id=?', (chat_id,))
        self.db.execute('INSERT INTO required_channels(chat_id,title,url,username) VALUES(?,?,?,?)', (chat_id,title,url,username))
        self.db.commit()

    def channels(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM required_channels ORDER BY title')]

    def delete_channel(self, chat_id):
        result = self.db.execute('DELETE FROM required_channels WHERE chat_id=?', (chat_id,))
        self.db.commit()
        return result.rowcount

    def record_captcha(self, token, result):
        if result not in ('passed','failed'): raise ValueError('invalid result')
        self.db.execute('INSERT OR IGNORE INTO captcha_results(token,result,created_at) VALUES(?,?,?)', (token,result,time.time()))
        self.db.commit()

    def create_web_challenge(self, token, user_id, question, options, correct, expires, demo=0):
        self.db.execute('DELETE FROM web_challenges WHERE expires<?',(time.time()-3600,))
        self.db.execute('INSERT INTO web_challenges(token,user_id,question,options,correct,expires,demo) VALUES(?,?,?,?,?,?,?)',
                        (token,user_id,question,options,correct,expires,int(demo)))
        self.db.commit()

    def web_challenge(self, token):
        row=self.db.execute('SELECT * FROM web_challenges WHERE token=?',(token,)).fetchone()
        return dict(row) if row else None

    def delete_web_challenge(self, token):
        self.db.execute('DELETE FROM web_challenges WHERE token=?',(token,))
        self.db.commit()

    def statistics(self):
        def count(sql):
            return self.db.execute(sql).fetchone()['n']
        return {'users':count('SELECT COUNT(*) AS n FROM bot_users'),
                'broadcast':count('SELECT COUNT(*) AS n FROM bot_users WHERE subscribed=1'),
                'passed':count("SELECT COUNT(*) AS n FROM captcha_results WHERE result='passed'"),
                'failed':count("SELECT COUNT(*) AS n FROM captcha_results WHERE result='failed'"),
                'pending':count("SELECT COUNT(*) AS n FROM captchas WHERE state='pending'")}

    def init_shop(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS products (
          id TEXT PRIMARY KEY, category TEXT NOT NULL, name TEXT NOT NULL,
          description TEXT NOT NULL, price_kopecks BIGINT NOT NULL,
          file_id TEXT NOT NULL, filename TEXT NOT NULL, instruction TEXT NOT NULL,
          active BIGINT NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS orders (
          id TEXT PRIMARY KEY, buyer BIGINT NOT NULL, product_id TEXT NOT NULL,
          name TEXT NOT NULL, price_kopecks BIGINT NOT NULL, file_id TEXT NOT NULL,
          filename TEXT NOT NULL, instruction TEXT NOT NULL, provider TEXT NOT NULL,
          provider_id TEXT NOT NULL, payment_url TEXT NOT NULL, state TEXT NOT NULL,
          created_at DOUBLE PRECISION NOT NULL, delivered_at DOUBLE PRECISION,
          UNIQUE(provider, provider_id));
        CREATE TABLE IF NOT EXISTS donations (
          id TEXT PRIMARY KEY, buyer BIGINT NOT NULL, price_kopecks BIGINT NOT NULL,
          provider TEXT NOT NULL, provider_id TEXT NOT NULL, payment_url TEXT NOT NULL,
          state TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL,
          UNIQUE(provider, provider_id));
        ''')
        self.db.commit()

    def product_add(self, product):
        self.db.execute('''INSERT INTO products(id,category,name,description,price_kopecks,file_id,filename,instruction)
          VALUES(?,?,?,?,?,?,?,?)''', tuple(product[k] for k in ('id','category','name','description','price_kopecks','file_id','filename','instruction')))
        self.db.commit()

    def product(self, product_id):
        row = self.db.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
        return dict(row) if row else None

    def products(self, category=None, all_items=False):
        sql = 'SELECT * FROM products WHERE 1=1'
        params = []
        if category:
            sql += ' AND category=?'; params.append(category)
        if not all_items: sql += ' AND active=1'
        return [dict(x) for x in self.db.execute(sql + ' ORDER BY name', params)]

    def product_set_active(self, product_id, active):
        result = self.db.execute('UPDATE products SET active=? WHERE id=?', (int(active), product_id))
        self.db.commit()
        return result.rowcount > 0

    def order_add(self, order):
        keys = ('id','buyer','product_id','name','price_kopecks','file_id','filename','instruction','provider','provider_id','payment_url','state','created_at')
        self.db.execute('INSERT INTO orders ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')', tuple(order[k] for k in keys))
        self.db.commit()

    def order(self, order_id):
        row = self.db.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
        return dict(row) if row else None

    def pending_orders(self):
        return [dict(x) for x in self.db.execute("SELECT * FROM orders WHERE state IN ('pending','paid') ORDER BY created_at")]

    def mark_paid(self, order_id):
        self.db.execute("UPDATE orders SET state='paid' WHERE id=? AND state='pending'", (order_id,))
        self.db.commit()

    def mark_delivered(self, order_id):
        self.db.execute("UPDATE orders SET state='delivered',delivered_at=? WHERE id=? AND state='paid'", (time.time(),order_id))
        self.db.commit()

    def donation_add(self, donation):
        keys = ('id','buyer','price_kopecks','provider','provider_id','payment_url','state','created_at')
        self.db.execute('INSERT INTO donations ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')', tuple(donation[k] for k in keys))
        self.db.commit()

    def donation(self, donation_id):
        row = self.db.execute('SELECT * FROM donations WHERE id=?', (donation_id,)).fetchone()
        return dict(row) if row else None

    def pending_donations(self):
        return [dict(x) for x in self.db.execute("SELECT * FROM donations WHERE state IN ('pending','paid') ORDER BY created_at")]

    def mark_donation_paid(self, donation_id):
        self.db.execute("UPDATE donations SET state='paid' WHERE id=? AND state='pending'", (donation_id,))
        self.db.commit()

    def mark_donation_notified(self, donation_id):
        self.db.execute("UPDATE donations SET state='notified' WHERE id=? AND state='paid'", (donation_id,))
        self.db.commit()
