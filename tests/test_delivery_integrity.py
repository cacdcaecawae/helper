"""成功发送记录、任务入队和有界历史响应；不连接微信、TTS 或模型。"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from test_review_fixes import StoreFixture, ROOT, SELF, PEER, ROOM
import test_speech as speech_tests
from test_speech import ROOM as VOICE_ROOM
from wxbot_framework.members import MemberStore, SnapshotExpired
from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.plugins import PluginManager, PluginRecord, PluginSpec
from wxbot_framework.sdk import Message, ServiceFacade
from wxbot_framework.speech import deliver_model_reply
from wxbot_framework.tts import TTSError
from wxbot_framework.web_panel import PanelController, PanelError


@unittest.skipUnless(sys.platform == 'win32', 'Windows native structures')
class OutgoingIdsTests(StoreFixture):
    def setUp(self):
        super().setUp()
        native = str(ROOT/'native')
        sys.path.insert(0, native)
        self.addCleanup(lambda:sys.path.remove(native))
        from text_service import NativeTextService
        from filehelper_service import ReceivedMediaMessageV5
        from wxbot_framework.backend import NativeTextBackend
        self.Raw = ReceivedMediaMessageV5
        self.raw = NativeTextService(pid=123, account=SELF)
        self.raw.router.set_account(SELF)
        self.raw._read_account = Mock(return_value=SELF)
        self.raw._sender_wechat_name = Mock(return_value='合成人物')
        self.raw._chat_name = Mock(return_value='Synthetic')
        self.raw._owns_bridge, self.raw._channel = True, object()
        self.raw._run = Mock(return_value={'success':True})
        self.backend = NativeTextBackend.__new__(NativeTextBackend)
        self.backend.raw, self.backend.session_id = self.raw, 'outgoing'
        self.service = ServiceFacade(text_delay={'enabled':False})
        self.service.bind(self.backend)
        self.buffer = MessageBuffer()
        self.service.message_observer = lambda msg:self.buffer.append(msg, account=SELF)
        self.service.sent_recorder = self.store.record_own
        self.sequence = 0
        self.quote = {'version':1,'reply':'回答','reference':{'message_id':'17','sender_id':PEER,
                      'sender_name':'合成人物','type':1,'content':'原话'}}

    def emit(self, local, identifier=0, *, quote=False, chat=PEER, body='重复正文'):
        self.sequence += 1
        content = body
        if quote:
            content = ('<msg><appmsg><type>57</type><title>回答</title><refermsg><type>1</type><svrid>17</svrid>'
                       '<fromusr>'+PEER+'</fromusr><displayname>另一个称呼</displayname><content>原话</content></refermsg></appmsg></msg>')
        group = chat.endswith('@chatroom')
        if group and not local:
            content = SELF+':\n'+content
        raw = self.Raw(type=49 if quote else 1, reserved=40 if quote and local else 8 if local else 0,
            sender=b'self' if local else chat.encode() if group else SELF.encode(),
            receiver=(chat if local or not group else SELF).encode(),
            text=b'' if quote else content.encode(), image_xml=content.encode() if quote else b'',
            native_message_id=identifier, sequence=self.sequence)
        data = self.raw._map_message(raw)
        if data is not None:
            msg = Message.from_native(11046, data, self.backend.session_id)
            self.buffer.append(msg, account=SELF)
            self.store.record(msg)
        return data

    def send(self, quote=False, chat=PEER):
        return self.service.send_quote(chat, self.quote) if quote else self.service.send_text(chat, '重复正文')

    def test_ids_persist_for_plain_and_quote_with_all_callback_orders(self):
        for quote in (False, True):
            for order in ('before', 'after', 'split', 'sync_first'):
                with self.subTest(quote=quote, order=order):
                    self.store.forget(PEER)
                    self.raw._own_text_echoes.clear()
                    self.raw._sent_texts.clear(); self.raw._sent_quotes.clear()
                    self.buffer.reset()
                    def run(*args, **kwargs):
                        if order in ('before', 'split'):
                            self.emit(True, quote=quote)
                        if order in ('before', 'sync_first'):
                            self.emit(False, 42, quote=quote)
                        return {'success':True}
                    self.raw._run.side_effect = run
                    self.assertTrue(self.send(quote))
                    if order == 'after': self.emit(True, quote=quote)
                    if order in ('after', 'split'): self.emit(False, 42, quote=quote)
                    if order == 'sync_first': self.emit(True, quote=quote)
                    rows = self.store.chat_history(PEER)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]['message_id'], '42')
                    self.assertEqual(len(self.buffer.rows), 1)
                    self.assertLess(self.store._connection().execute('SELECT sequence FROM messages').fetchone()[0], 0)
                    reopened = MemberStore(self.store.config_file, SELF)
                    try: self.assertEqual(reopened.chat_history(PEER)[0]['message_id'], '42')
                    finally: reopened.close()

    def test_identical_sends_and_manual_followup_keep_distinct_ids(self):
        for identifier in (42, 43):
            self.send(chat=ROOM)
            self.emit(True, chat=ROOM)
            self.emit(False, identifier, chat=ROOM)
        manual = self.emit(True, 44, chat=ROOM)
        self.assertFalse(manual.get('bot_generated', False))
        self.assertEqual([r['message_id'] for r in self.store.chat_history(ROOM)], ['42','43','44'])

    def test_clear_before_ack_does_not_resurrect_or_attach_to_later_same_text(self):
        self.send()
        self.emit(True)
        self.store.forget(PEER)
        self.emit(False, 42)
        self.assertEqual(self.store.chat_history(PEER), [])
        self.send()
        self.emit(True)
        self.emit(False, 43)
        self.assertEqual([r['message_id'] for r in self.store.chat_history(PEER)], ['43'])

    def test_failed_send_never_creates_a_success_record_from_early_ack(self):
        def failed(*args, **kwargs):
            self.emit(False, 42)
            return {'success':False}
        self.raw._run.side_effect = failed
        self.assertFalse(self.send())
        self.assertEqual(self.store.chat_history(PEER), [])

    def test_ack_for_another_session_or_chat_cannot_rewrite_current_send(self):
        self.send()
        for chat, session in ((ROOM,'outgoing'), (PEER,'old-session')):
            self.store.record(Message('重复正文', chat, SELF, is_self=True, is_group=chat==ROOM, session_id=session,
                sequence=100, data={'bot_generated':True,'outgoing_sequence':-1,'native_message_id':'99'}))
        self.assertNotIn('message_id', self.store.chat_history(PEER)[0])
        self.emit(False, 42)
        self.assertEqual(self.store.chat_history(PEER)[0]['message_id'], '42')


class VoicePersistenceTests(unittest.TestCase):
    setUp, tearDown = speech_tests.SpeechTests.setUp, speech_tests.SpeechTests.tearDown
    new_store, message, model = speech_tests.SpeechTests.new_store, speech_tests.SpeechTests.message, speech_tests.SpeechTests.model
    setup_speech = speech_tests.SpeechTests.setup_speech

    def test_all_segments_saved_once_and_not_followed_by_full_duplicate(self):
        self.setup_speech()
        self.client.max_chars_per_clip = 3
        deliver_model_reply(self.ctx, self.message('问题'), '第一句第二句第三句')
        self.assertEqual([r['text'] for r in self.store.chat_history(VOICE_ROOM)], ['第一句','第二句','第三句'])
        self.assertEqual(len(self.store.memory(VOICE_ROOM)[1]), 3)

    def test_partial_failure_preserves_confirmed_prefix_without_text_retry(self):
        for failure in (False, RuntimeError('send failed'), TTSError('send failed')):
            with self.subTest(failure=failure):
                self.setup_speech()
                self.store.forget(VOICE_ROOM)
                self.backend.sent.clear()
                self.client.max_chars_per_clip = 3
                self.backend.send_voice = Mock(side_effect=[True, failure])
                with self.assertRaises(RuntimeError):
                    deliver_model_reply(self.ctx, self.message('问题'), '第一句第二句第三句')
                self.assertEqual(self.backend.send_voice.call_count, 2)
                self.assertEqual([r['text'] for r in self.store.chat_history(VOICE_ROOM)], ['第一句'])
                self.assertEqual([r['text'] for r in self.store.memory(VOICE_ROOM)[1]], ['第一句'])
                self.assertEqual(self.backend.sent, [])  # no text fallback
                self.assertEqual(list((self.root/'tts').rglob('*.wav')), [])

    def test_clear_after_success_prevents_record_resurrection_and_further_segments(self):
        self.setup_speech()
        self.client.max_chars_per_clip = 3
        def send(*args):
            self.store.forget(VOICE_ROOM)
            return True
        self.backend.send_voice = Mock(side_effect=send)
        with self.assertRaises(SnapshotExpired):
            deliver_model_reply(self.ctx, self.message('问题'), '第一句第二句')
        self.assertEqual(self.backend.send_voice.call_count, 1)
        self.assertEqual(self.store.chat_history(VOICE_ROOM), [])

    def test_disabling_voice_after_first_send_keeps_already_spoken_text(self):
        self.setup_speech()
        self.client.max_chars_per_clip = 3
        def send(*args):
            self.active = False
            return True
        self.backend.send_voice = Mock(side_effect=send)
        with self.assertRaises(SnapshotExpired):
            deliver_model_reply(self.ctx, self.message('问题'), '第一句第二句')
        self.assertEqual([r['text'] for r in self.store.chat_history(VOICE_ROOM)], ['第一句'])


class TaskQueueTests(unittest.TestCase):
    def test_full_actual_queue_is_reported_as_busy_not_success(self):
        spec = PluginSpec('agent_chat', Path('.'), 'v1', 'Synthetic', frozenset())
        record = PluginRecord(spec, Mock(), SimpleNamespace(logger=Mock()), 'synthetic', 128)
        manager = PluginManager.__new__(PluginManager)
        manager._lock, manager.records = threading.RLock(), {'agent_chat':record}
        for _ in range(128): self.assertEqual(manager.dispatch(Message('queued', ROOM), only='agent_chat'), 1)
        app = SimpleNamespace(_connection_ready=True, service=SimpleNamespace(session_is_current=lambda s:s=='test'),
                              config={'wechat_account':SELF,'chat_allowlist':[]}, plugins=manager)
        controller = PanelController.__new__(PanelController)
        controller._call = lambda callback:callback(app)
        task = {'chat_id':ROOM,'session_id':'test','task':'交代任务'}
        with self.assertRaises(PanelError) as caught: controller.agent_task(task)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(record.queue.qsize(), 128)
        self.assertEqual(record.dropped, 1)
        record.queue.get_nowait(); record.queue.task_done()
        self.assertEqual(controller.agent_task(task), {'ok':True})
        self.assertEqual(record.queue.qsize(), 128)
        record.active = False
        self.assertFalse(record.enqueue(Message('ignored', ROOM)))
        self.assertEqual(manager.dispatch(Message('ignored',ROOM), only='agent_chat'), 0)


class BoundedHistoryTests(StoreFixture):
    def test_two_hundred_long_rows_fit_transport_and_originals_are_untouched(self):
        text = '长'*8000
        for sequence in range(1, 201):
            self.store.record(Message(text, ROOM, PEER, is_group=True, session_id='history', sequence=sequence))
        original = self.store.chat_history(ROOM)
        response = {'rows':[PanelController._history_row(ROOM, row) for row in original]}
        self.assertEqual(len(response['rows']), 200)
        self.assertLess(len(json.dumps(response,ensure_ascii=False).encode()), 4*1024*1024)
        self.assertTrue(all(row['preview_truncated'] for row in response['rows']))
        self.assertTrue(all(row['text']==text for row in self.store.chat_history(ROOM)))

    def test_quote_previews_bound_escaped_characters_without_mutating_source(self):
        source = {'id':1,'at':'2026-10-25T00:00:00+00:00','name':'合成人物','text':'\t'*16000,
                  'own':False,'message_id':'42','event_keys':['a'*64], 'quote':{'version':1,'reply':'\t'*15999+'x',
                    'reference':{'message_id':'17','sender_id':PEER,'sender_name':'名字','type':1,'content':'🙂'*1000}}}
        before = json.dumps(source)
        row = PanelController._history_row(PEER, source)
        self.assertLessEqual(len(json.dumps(row,ensure_ascii=False).encode()), 8192)
        self.assertLess(len(json.dumps({'rows':[row]*200},ensure_ascii=False).encode()), 4*1024*1024)
        self.assertEqual(json.dumps(source), before)
        self.assertEqual((row['id'],row['message_id'],row['quote']['reference']['message_id']), ('h1','42','17'))


@unittest.skipUnless(shutil.which('node'), 'Node needed for isolated frontend checks')
class TaskAndHistoryUITests(unittest.TestCase):
    def test_rejected_tasks_preserve_input_and_history_errors_are_visible_and_retryable(self):
        script = (ROOT/'webui/app.js').read_text(encoding='utf8')
        functions = script[script.index('async function submitTask()'):script.index("$('#send-form').onsubmit=")]
        functions += script[script.index('async function loadChatHistory('):script.index('function mergeChatTimeline(')]
        probe = r'''
const assert=require('node:assert/strict');
let robot='test',sendSession='s',taskSending=false,feedback='',notices=[],handler;
const chatDrafts=new Map(),chatHistory=new Map(),historyLoading=new Map(),messages=[];
let chatTimelineSignature='';const nodes=new Map();
function $(id){if(!nodes.has(id))nodes.set(id,{value:''});return nodes.get(id);}
$('#send-chat').value='wxid_peer';$('#send-content').value='keep draft';
function sendFeedback(s){feedback=s;}function renderSendState(){}function renderChatWorkspace(){}
function toast(s){notices.push(s);}async function api(...args){return handler(...args);}
(async()=>{
 handler=async()=>{throw Error('queue busy');};await submitTask();
 assert.equal($('#send-content').value,'keep draft');assert.equal(taskSending,false);assert.match(feedback,/busy/);
 handler=async()=>({ok:false});await submitTask();assert.equal($('#send-content').value,'keep draft');
 handler=async()=>({ok:true});await submitTask();assert.equal($('#send-content').value,'');assert.match(feedback,/已入队/);
 $('#send-content').value='old task';handler=async()=>{$('#send-content').value='new draft';return {ok:true};};
 await submitTask();assert.equal($('#send-content').value,'new draft');
 handler=async()=>{throw Error('history failed');};await loadChatHistory('wxid_peer');
 assert.equal(notices.length,1);assert.equal(historyLoading.size,0);assert.equal(chatHistory.size,0);
 handler=async()=>({rows:[{id:'h1'}]});await loadChatHistory('wxid_peer');
 assert.equal(chatHistory.get('test:wxid_peer').length,1);console.log('UI checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', functions+probe], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__': unittest.main()
