"""引用结构及旧库增量迁移；仅使用临时 SQLite，不访问微信。"""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from wxbot_framework.members import MemberStore
from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.quotes import normalize_quote, parse_quote
from wxbot_framework.sdk import Message
from wxbot_framework.web_panel import PanelController

SELF, PEER, ROOM = 'wxid_storage_self', 'wxid_storage_peer', '12345678@chatroom'
ID = '3918779855605843848'


class QuoteStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'members.json'
        self.config.write_text(json.dumps({'history_enabled': True, 'database_file': 'data.sqlite3',
                                          'groups': {ROOM: []}}), encoding='utf-8')
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.temp.cleanup()

    def store(self):
        result = MemberStore(self.config, SELF)
        self.stores.append(result)
        return result

    def quote(self):
        return parse_quote('<msg><appmsg><type>57</type><title>111哈哈哈</title><refermsg>'
            '<type>1</type><svrid>'+ID+'</svrid><fromusr>'+PEER+'</fromusr><chatusr>'+PEER+'</chatusr>'
            '<displayname>朋友</displayname><content>你干啥呢</content>'
            '<msgsource>synthetic-secret-not-stored</msgsource></refermsg></appmsg></msg>', chat_id=ROOM)

    def test_quote_and_server_ids_survive_restart_without_storing_xml(self):
        store = self.store(); quote = self.quote()
        message = Message(quote.text, ROOM, PEER, is_group=True, session_id='first', sequence=1,
                          data={'native_message_id': '18446744073709551615', 'quote': quote.public()})
        self.assertTrue(store.record(message))
        self.assertFalse(store.record(message))
        db = store._connection()
        row = dict(db.execute('SELECT * FROM messages').fetchone())
        self.assertEqual(row['native_message_id'], '18446744073709551615')
        self.assertEqual(row['native_type'], 49)
        saved = json.loads(row['quote_json'])
        self.assertEqual(saved['reply'], '111哈哈哈')
        self.assertEqual(saved['reference']['content'], '你干啥呢')
        self.assertEqual(saved['reference']['message_id'], ID)
        self.assertNotIn('synthetic-secret', row['quote_json'])
        self.assertNotIn('<refermsg>', row['quote_json'])
        self.assertEqual(store.memory(ROOM)[1][0]['text'], quote.text)
        store.close()
        line = self.store().chat_history(ROOM)[0]
        self.assertEqual(line['quote'], saved)
        public = PanelController._history_row(ROOM, line)
        self.assertEqual(public['message_id'], '18446744073709551615')
        self.assertEqual(public['quote']['reference']['message_id'], ID)

    def test_old_schema_and_flattened_quotes_are_not_guessed_or_rewritten(self):
        with closing(sqlite3.connect(self.root / 'data.sqlite3')) as db, db:
            db.execute('''CREATE TABLE messages (id INTEGER PRIMARY KEY, account TEXT, room TEXT,
                sender_id TEXT, text TEXT, received_at TEXT, session_id TEXT, sequence INTEGER, is_self INTEGER)''')
            db.execute('INSERT INTO messages VALUES(1,?,?,?,?,?,?,?,?)',
                       (SELF,ROOM,PEER,'「引用 朋友：原话」\n旧正文','2026-10-24T10:00:00+00:00','old',1,0))
        store = self.store(); line = store.chat_history(ROOM)[0]
        self.assertNotIn('quote',line)
        self.assertNotIn('message_id',line)
        self.assertEqual(line['text'],'「引用 朋友：原话」\n旧正文')
        row = dict(store._connection().execute('SELECT * FROM messages').fetchone())
        self.assertEqual(row['quote_json'],'')
        self.assertEqual(row['native_message_id'],'')

    def test_memory_toggle_backfill_keeps_structured_quote(self):
        quote = self.quote(); buffer = MessageBuffer(clock=lambda: 1792887443)
        buffer.append(Message(quote.text,ROOM,PEER,is_group=True,data={'quote':quote.public(),
                      'native_message_id':ID}),account=SELF,sender_name='朋友')
        store = self.store()
        self.assertEqual(store.backfill(ROOM,buffer.texts(ROOM)),1)
        self.assertEqual(store.chat_history(ROOM)[0]['quote'],quote.public())
        self.assertEqual(store.chat_history(ROOM)[0]['message_id'],ID)

    def test_nontext_credentials_and_unknown_extra_fields_never_enter_saved_snapshot(self):
        value = self.quote().public()
        value['reference'].update(type=49,content='<msg><aeskey>synthetic-secret</aeskey></msg>',
                                  msgsource='synthetic-secret')
        value['raw_xml']='synthetic-secret'
        normalized = normalize_quote(value)
        self.assertEqual(normalized['reference']['content'],'[文件/分享]')
        self.assertNotIn('synthetic-secret',json.dumps(normalized))
        value['reference']['sender_id']='12345678@chatroom'
        self.assertIsNone(normalize_quote(value))

    def test_long_reference_does_not_push_the_actual_reply_out_of_model_context(self):
        quote=self.quote().public();quote['reference']['content']='长'*1000
        text='「引用 朋友：'+'长'*1000+'」\n111哈哈哈'
        store=self.store()
        store.record(Message(text,ROOM,PEER,is_group=True,session_id='long',sequence=1,data={'quote':quote}))
        context=store.memory(ROOM)[1][0]['text']
        self.assertLessEqual(len(context),500)
        self.assertIn('111哈哈哈',context)
        self.assertEqual(json.loads(store._connection().execute('SELECT quote_json FROM messages').fetchone()[0]),quote)


if __name__ == '__main__':
    unittest.main()
