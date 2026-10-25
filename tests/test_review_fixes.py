"""审查回归：合成事件、临时数据库与隔离 JS；不连接微信或调用模型。"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from wxbot_framework.members import MemberStore
from wxbot_framework.message_identity import OwnTextEchoes, event_key
from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.sdk import Message, ServiceFacade
from wxbot_framework.web_panel import PanelController

ROOT = Path(__file__).resolve().parents[1]
SELF, PEER, ROOM = 'wxid_review_robot', 'wxid_review_peer', '12345678@chatroom'


class StoreFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='helper-review-')
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / 'members.json'
        path.write_text(json.dumps({'database_file':'history.sqlite3', 'history_enabled':True,
                                   'groups':{ROOM:[]}}), encoding='utf-8')
        self.now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
        self.store = MemberStore(path, SELF, now=lambda:self.now)
        self.addCleanup(self.store.close)
        self.store.set_chat(PEER, memory=True, reply=False)


class HistoryRegressionTests(StoreFixture):
    def test_history_keeps_subsecond_neighbors_and_optionally_the_exact_boundary(self):
        boundary = self.now + timedelta(seconds=1)
        for sequence, delta in enumerate((0, .999, 1, 1.001), 1):
            self.now = boundary + timedelta(seconds=delta-1)
            self.store.record(Message('same words', ROOM, PEER, is_group=True, session_id='one', sequence=sequence))
        rows = self.store.chat_history(ROOM, before=boundary.timestamp())
        self.assertEqual([row['id'] for row in rows], [1, 2])
        inclusive = self.store.chat_history(ROOM, before=boundary.timestamp(), include_boundary=True)
        self.assertEqual([row['id'] for row in inclusive], [1, 2, 3])
        self.assertEqual(len({row['event_keys'][0] for row in inclusive}), 3)
        self.assertEqual(PanelController._history_row(ROOM, inclusive[0])['event_keys'], inclusive[0]['event_keys'])

    def test_sent_notifications_and_history_share_identity_but_repeated_words_do_not(self):
        buffer = MessageBuffer()
        service = ServiceFacade()
        service.message_observer = lambda msg:buffer.append(msg, account=SELF)
        service.sent_recorder = self.store.record_own
        for _ in range(2):
            service._sent(True, SimpleNamespace(session_id='one'), ROOM, 1, text='same words')
        history = self.store.chat_history(ROOM)
        self.assertEqual(len(history), 2)
        self.assertEqual([row['event_keys'] for row in history], [row['event_keys'] for row in buffer.rows])
        self.assertNotEqual(history[0]['event_keys'], history[1]['event_keys'])
        self.assertNotEqual(event_key(SELF, ROOM, 'one', 1), event_key(SELF, ROOM, 'two', 1))

    def test_memory_range_index_is_created_for_existing_databases_without_changing_records(self):
        self.store.record_own(ROOM, 'preserved', 'old')
        before = self.store.chat_history(ROOM)
        db = self.store._connection()
        db.execute('DROP INDEX messages_by_chat_id')
        db.commit()
        config = self.store.config_file
        self.store.close()
        reopened = MemberStore(config, SELF)
        self.addCleanup(reopened.close)
        # 浏览器引用令牌随进程/存储实例失效；持久原文和身份字段仍须完全不变。
        persisted = lambda rows: [{k:v for k,v in row.items() if k != 'quote_token'} for row in rows]
        self.assertEqual(persisted(reopened.chat_history(ROOM)), persisted(before))
        db = reopened._connection()
        self.assertEqual([r['name'] for r in db.execute('PRAGMA index_info(messages_by_chat_id)')],
                         ['account', 'room', 'id'])
        for query in ('SELECT id,text FROM messages WHERE account=? AND room=? AND id>? ORDER BY id',
                      'SELECT SUM(length(text)) FROM messages WHERE account=? AND room=? AND id>?'):
            plan = str([tuple(row) for row in db.execute('EXPLAIN QUERY PLAN '+query, (SELF, ROOM, 0))])
            self.assertIn('messages_by_chat_id', plan)
            self.assertIn('id>?', plan)
            self.assertNotIn('TEMP B-TREE', plan)


@unittest.skipUnless(sys.platform == 'win32', 'Windows native event structure')
class EchoRegressionTests(StoreFixture):
    def setUp(self):
        super().setUp()
        native_path = str(ROOT/'native')
        sys.path.insert(0, native_path)
        self.addCleanup(lambda:sys.path.remove(native_path))
        from text_service import NativeTextService
        from filehelper_service import ReceivedMediaMessageV5
        self.Raw = ReceivedMediaMessageV5
        self.service = NativeTextService(pid=123, account=SELF)
        self.service.router.set_account(SELF)
        self.service._read_account = Mock(return_value=SELF)
        self.service._sender_wechat_name = Mock(return_value='Synthetic')
        self.service._chat_name = Mock(return_value='Synthetic')
        self.buffer = MessageBuffer()
        self.sequence = 0

    def event(self, local, identifier=0, text='same words', chat=ROOM):
        self.sequence += 1
        group = chat.endswith('@chatroom')
        body = text if local or not group else SELF+':\n'+text
        raw = self.Raw(type=1, reserved=8 if local else 0, sequence=self.sequence,
            sender=(b'self' if local else chat.encode() if group else SELF.encode()),
            receiver=(chat if local or not group else SELF).encode(), text=body.encode(), native_message_id=identifier)
        data = self.service._map_message(raw)
        if data is not None:
            message = Message.from_native(11046, data, 'one')
            self.buffer.append(message, account=SELF)
            self.store.record(message)
        return data

    def test_zero_id_local_then_sync_updates_one_record_and_one_context_line(self):
        first = self.event(True)
        original = dict(self.buffer.rows[0])
        second = self.event(False, 42)
        self.assertTrue(second['echo_update'])
        self.assertFalse(second['dispatch_to_plugins'])
        self.assertEqual(first['sequence'], second['sequence'])
        self.assertEqual(len(self.store.memory(ROOM)[1]), 1)
        self.assertEqual([row['message_id'] for row in self.store.chat_history(ROOM)], ['42'])
        self.assertEqual(len(self.buffer.rows), 1)
        row = self.buffer.rows[0]
        self.assertEqual((row['id'], row['timestamp']), (original['id'], original['timestamp']))
        self.assertEqual(row['message_id'], '42')
        self.assertGreater(row['revision'], original['revision'])
        self.assertIsNone(self.event(False, 42))

    def test_sync_before_local_and_private_messages_are_paired(self):
        self.event(False, 42, chat=PEER)
        self.assertIsNone(self.event(True, chat=PEER))
        self.assertEqual(len(self.store.memory(PEER)[1]), 1)
        self.assertEqual(len(self.buffer.rows), 1)

    def test_repeated_identical_words_are_paired_one_to_one_not_collapsed(self):
        for local, identifier in ((True,0),(True,0),(False,42),(False,43),(True,0),(False,44)):
            self.event(local, identifier)
        self.assertEqual([row['message_id'] for row in self.store.chat_history(ROOM)], ['42','43','44'])
        self.assertEqual(len(self.store.memory(ROOM)[1]), 3)
        self.assertEqual(len(self.buffer.rows), 3)

    def test_two_sync_events_before_two_local_events_preserve_both_sends(self):
        for local, identifier in ((False,42),(False,43),(True,0),(True,0)):
            self.event(local, identifier)
        self.assertEqual([row['message_id'] for row in self.store.chat_history(ROOM)], ['42','43'])
        self.assertEqual(len(self.buffer.rows), 2)

    def test_known_different_ids_and_different_chats_never_merge(self):
        self.event(True, 41)
        self.event(False, 42)
        self.event(True, chat=PEER)
        self.event(False, 43)
        self.assertEqual(len(self.store.memory(ROOM)[1]), 3)
        self.assertEqual(len(self.store.memory(PEER)[1]), 1)

    def test_expired_match_does_not_drop_a_later_message(self):
        clock = [0]
        self.service._own_text_echoes = OwnTextEchoes(clock=lambda:clock[0])
        self.event(True)
        clock[0] = 11
        self.event(False, 42)
        self.assertEqual(len(self.buffer.rows), 2)

    def test_clear_during_pending_echo_never_resurrects_message(self):
        self.event(True)
        self.store.forget(ROOM)
        self.buffer.reset()
        self.event(False, 42)
        self.assertEqual(self.store.chat_history(ROOM), [])
        self.assertEqual(list(self.buffer.rows), [])

    def test_program_send_three_notifications_still_produce_one_record_per_send(self):
        import time
        facade = ServiceFacade()
        facade.message_observer = lambda message:self.buffer.append(message, account=SELF)
        facade.sent_recorder = self.store.record_own
        for identifier in (42, 43):
            self.service._sent_texts.append((ROOM, 'same words', time.monotonic()))
            facade._sent(True, SimpleNamespace(session_id='one'), ROOM, 1, text='same words')
            self.assertTrue(self.event(True)['bot_generated'])
            self.assertTrue(self.event(False, identifier)['bot_generated'])
        self.assertEqual(len(self.store.memory(ROOM)[1]), 2)
        self.assertEqual(len(self.buffer.rows), 2)
        history = self.store.chat_history(ROOM)
        for old, live in zip(history, self.buffer.rows):
            self.assertIn(old['event_keys'][0], live['event_keys'])
        self.assertEqual([row['message_id'] for row in self.buffer.rows], ['42','43'])

    def test_program_notification_after_native_echoes_keeps_history_identity(self):
        import time
        self.service._sent_texts.append((ROOM, 'same words', time.monotonic()))
        self.event(True)
        self.event(False, 42)
        facade = ServiceFacade()
        facade.message_observer = lambda message:self.buffer.append(message, account=SELF)
        facade.sent_recorder = self.store.record_own
        facade._sent(True, SimpleNamespace(session_id='one'), ROOM, 1, text='same words')
        history = self.store.chat_history(ROOM)
        self.assertEqual(len(history), 1)
        self.assertEqual(len(self.buffer.rows), 1)
        self.assertIn(history[0]['event_keys'][0], self.buffer.rows[0]['event_keys'])
        self.assertEqual(self.buffer.rows[0]['message_id'], '42')


NODE = shutil.which('node')


@unittest.skipUnless(NODE, 'Node required for isolated frontend checks')
class ComposerRegressionTests(unittest.TestCase):
    def test_connection_members_history_and_stale_responses(self):
        script = (ROOT/'webui/app.js').read_text(encoding='utf-8')
        selected = script[script.index('async function selectSendChat()'):script.index("$('#refresh-send-chats').onclick=")]
        merge = script[script.index('function mergeChatTimeline('):script.index('const accountKey =')]
        state = script[script.index('function renderSendState()'):script.index('function renderSendMembers()')]
        probe = r'''
const assert=require('node:assert/strict');
let room='12345678@chatroom',robot='one',sendGeneration=0,sendSession='',sendLoading=false,sendMembers=[],
    mentionSelection=new Set(),serviceAvailable=true,state={connected:true},busy=false,pendingSend=null,
    taskSending=false,taskMode=false,
    chatQuotes=new Map(),feedback='',calls=[],respond;
const nodes=new Map();
function $(name){if(!nodes.has(name))nodes.set(name,{value:'',disabled:false,textContent:'',
  classList:{toggle(){}},querySelectorAll:()=>[]});return nodes.get(name);}
$('#send-chat').value=room;$('#send-content').value='plain text';
function renderQuotePreview(){}function renderChatWorkspace(){}function loadChatHistory(){}
function renderSendMembers(){renderSendState();}function sendFeedback(text){feedback=text;}
async function api(path,body,requested){calls.push(path);return respond(path,requested);}
function defer(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
(async()=>{
  // 成员接口错误：普通发送仍启用，艾特名单为空。
  respond=async path=>{if(path==='send-connection')return {session_id:'s1'};throw Error('cache not ready');};
  await selectSendChat();assert.equal(sendSession,'s1');assert.equal($('#send-text').disabled,false);
  assert.equal(sendMembers.length,0);assert.match(feedback,/普通文字仍可发送/);
  assert.deepEqual(calls,['send-connection','group-members?room='+encodeURIComponent(room)]);
  // 不必等待缓慢的群成员请求完成。
  const slow=defer(),started=defer();
  respond=async path=>{if(path==='send-connection')return {session_id:'s1'};started.resolve();return slow.promise;};
  const pending=selectSendChat();await started.promise;assert.equal($('#send-text').disabled,false);
  // 切换会话后，旧的名单响应不能污染私聊或发送会话。
  $('#send-chat').value='wxid_peer';respond=async()=>({session_id:'s2'});await selectSendChat();
  slow.resolve({session_id:'s1',members:[{wxid:'old'}]});await pending;
  assert.equal(sendSession,'s2');assert.equal(sendMembers.length,0);
  // 连接请求失败仍阻止发送，不拿旧 session 冒险。
  respond=async()=>{throw Error('offline');};await selectSendChat();
  assert.equal(sendSession,'');assert.equal($('#send-text').disabled,true);
  // 机器人切换后的旧连接响应也必须丢弃。
  const stale=defer();respond=()=>stale.promise;const previous=selectSendChat();robot='two';
  stale.resolve({session_id:'old-account'});await previous;assert.equal(sendSession,'');
  // 恢复群成员；艾特名单独立返回且必须来自同一连接。
  $('#send-chat').value=room;
  respond=async path=>path==='send-connection'?{session_id:'s3'}:{session_id:'s3',members:[{wxid:'current'}]};
  await selectSendChat();assert.equal(sendMembers[0].wxid,'current');assert.equal($('#send-text').disabled,false);
  respond=async path=>path==='send-connection'?{session_id:'s3'}:{session_id:'new-session',members:[{wxid:'wrong'}]};
  await selectSendChat();assert.equal(sendSession,'');assert.equal(sendMembers.length,0);assert.equal($('#send-text').disabled,true);
  // 同一句话两条不同消息都保留，只有同身份历史副本被过滤。
  const h=(id,more={})=>({id,chat_id:room,content:'same',...more});
  const history=[h('h1',{message_id:'41'}),h('h2',{message_id:'42'}),h('h3',{event_keys:['first']}),h('h4')];
  const live=[h(1,{message_id:'42',recalled:true}),h(2,{event_keys:['first','send']}),
              h(3,{message_id:'41',forwarded:true}),h(4,{message_id:'41',kind:'agent'})];
  assert.deepEqual(mergeChatTimeline(history,live).map(r=>r.id),['h1','h4',1,2,3,4]);
  assert.equal(mergeChatTimeline([],live).length,4);
  console.log('composer and history regression checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([NODE, '-e', merge+state+selected+probe], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('regression checks passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
