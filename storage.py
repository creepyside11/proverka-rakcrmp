import sqlite3
import time

DEFAULT_WORDS = ('дурак', 'идиот', 'дебил', 'мудак', 'тварь')
TOGGLES = ('insults', 'ads', 'luaonly', 'unknown_delete', 'scan')

class Store:
    def __init__(self, path='moderator.sqlite3'):
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
        return [r[0] for r in self.db.execute('SELECT word FROM words WHERE chat=? AND topic=? ORDER BY word', (chat,topic))]

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
