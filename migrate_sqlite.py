"""Import the existing moderation database into an empty PostgreSQL database."""
import argparse
import os
import sqlite3
from dotenv import load_dotenv
from storage import Store


def main():
    parser = argparse.ArgumentParser(description='Перенос настроек и капчи из SQLite в пустую PostgreSQL')
    parser.add_argument('sqlite_file', help='Путь к существующей копии moderator.sqlite3')
    args = parser.parse_args()
    load_dotenv()
    if not os.path.isfile(args.sqlite_file): raise SystemExit('SQLite-файл не найден')
    source = sqlite3.connect('file:' + os.path.abspath(args.sqlite_file) + '?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    target = Store()
    tables = ('topics','word_defaults','words','captchas')
    try:
        with target.db.conn.transaction():
            for table in tables:
                if target.db.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone():
                    raise RuntimeError('Целевая таблица не пуста: '+table)
            for table in tables:
                columns = [x[1] for x in source.execute('PRAGMA table_info('+table+')')]
                if not columns: raise RuntimeError('Нет исходной таблицы '+table)
                allowed = {'topics': {'chat','topic','insults','ads','luaonly','unknown_delete','scan','interval_sec','notice','next_at'},
                    'word_defaults': {'chat','topic'}, 'words': {'chat','topic','word'},
                    'captchas': {'chat','user','token','correct','expires','attempts','state','message_id','question_a','widget_message_id'}}
                if not set(columns) <= allowed[table]: raise RuntimeError('Неизвестные поля: '+table)
                for row in source.execute('SELECT * FROM '+table):
                    target.db.execute('INSERT INTO '+table+' ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')', tuple(row))
        print('Перенос завершён. SQLite-файл не изменён.')
    finally:
        source.close()
        target.db.close()

if __name__ == '__main__': main()
