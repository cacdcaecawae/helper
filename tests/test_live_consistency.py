"""清空后的引用、实时媒体开关和响应预算；仅临时库/模拟后端，无真实收发。"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import Mock

import test_review_fixes as fixtures
import test_native_quote_send as quote_tests
import test_web_panel as panel_tests
from wxbot_framework.backend import NativeTextBackend
from wxbot_framework.members import MemberStore
from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.message_projection import MAX_ROW_BYTES
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message
from wxbot_framework.web_panel import PanelController, PanelError

SELF, PEER, ROOM = fixtures.SELF, fixtures.PEER, fixtures.ROOM
ROOT = Path(__file__).resolve().parents[1]


class HistoryIdentityTests(fixtures.StoreFixture):
    def record(self, sequence=1):
        self.store.record(Message('same text', PEER, PEER, session_id='same', sequence=sequence))

    def test_clear_reused_row_even_with_same_text_author_and_timestamp_is_not_same_source(self):
        self.record()
        old = self.store.quote_source(PEER, 1)
        self.store.forget(PEER)
        self.record(2)
        new = self.store.quote_source(PEER, 1)
        for key in ('id', 'content', 'sender_id', 'timestamp'):
            self.assertEqual(old[key], new[key])
        self.assertNotEqual(old['quote_token'], new['quote_token'])
        self.assertFalse(self.store.attach_quote_id(PEER, 1, old, '42'))
        self.assertEqual(self.store.quote_source(PEER, 1)['message_id'], '')

    def test_id_fill_and_name_change_keep_token_but_reopen_invalidates_browser_snapshot(self):
        self.record()
        old = self.store.quote_source(PEER, 1)
        self.assertTrue(self.store.attach_quote_id(PEER, 1, old, '42'))
        self.assertEqual(self.store.chat_history(PEER)[0]['quote_token'], old['quote_token'])
        self.store.remember_wechat_name(PEER, 'new name')
        self.assertEqual(self.store.quote_source(PEER, 1)['quote_token'], old['quote_token'])
        self.store.close()
        reopened = MemberStore(self.store.config_file, SELF, now=lambda:self.now)
        self.addCleanup(reopened.close)
        new = reopened.quote_source(PEER, 1)
        self.assertEqual(new['message_id'], '42')
        self.assertNotEqual(new['quote_token'], old['quote_token'])

    def test_preview_token_uses_full_source_and_clear_does_not_affect_other_chat(self):
        self.store.record(Message('原文' * 8000, PEER, PEER, session_id='s', sequence=1))
        line = self.store.chat_history(PEER)[0]
        view = PanelController._history_row(PEER, line)
        self.assertTrue(view['preview_truncated'])
        self.assertEqual(view['quote_token'], self.store.quote_source(PEER, 1)['quote_token'])
        self.assertEqual(len(self.store.quote_source(PEER, 1)['content']), len(line['text']))
        self.store.forget(ROOM)
        self.assertEqual(view['quote_token'], self.store.quote_source(PEER, 1)['quote_token'])


class PanelIdentityTests(panel_tests.PanelFixture):
    def setUp(self):
        super().setUp()
        self.controller.start()
        self.app = self.controller.app
        self.app._connection_ready = True
        self.app.config.update(wechat_account=quote_tests.SELF, chat_allowlist=[])
        self.app.service = Mock()
        self.app.service.session_is_current.return_value = True
        self.app.service.send_quote.return_value = True

    historical_source = quote_tests.QuoteSendPanelTests.historical_source
    request = quote_tests.QuoteSendPanelTests.request
    await_job = quote_tests.QuoteSendPanelTests.await_job

    def reuse_history(self, store):
        store.forget(quote_tests.PEER)
        store.record(Message('new unrelated message', quote_tests.PEER, quote_tests.PEER,
                             session_id='new', sequence=2, data={'native_message_id':'99'}))
        self.assertEqual(store.chat_history(quote_tests.PEER)[0]['id'], 1)

    def test_stale_history_selected_before_clear_rejected_before_queueing(self):
        store, extra = self.historical_source()
        self.reuse_history(store)
        with self.assertRaises(PanelError):
            self.controller.send_text(self.request(**extra))
        self.app.service.resolve_quote_reference.assert_not_called()
        self.app.service.send_quote.assert_not_called()
        self.assertEqual(self.controller.send_jobs, {})

    def test_missing_or_wrong_token_fails_closed(self):
        store, extra = self.historical_source()
        for token in (None, '', 'a'*64, 1):
            ref = dict(extra['quote_ref'])
            if token is None:
                del ref['token']
            else:
                ref['token'] = token
            with self.subTest(token=token), self.assertRaises(PanelError):
                self.controller.send_text(self.request(quote_id=None, quote_ref=ref))
        self.app.service.send_quote.assert_not_called()

    def test_clear_and_reuse_during_id_lookup_cannot_attach_id_or_send(self):
        store, extra = self.historical_source()
        def resolve(*args, **kwargs):
            self.reuse_history(store)
            return {'message_id':quote_tests.ID, 'created_at':1792887443}
        self.app.service.resolve_quote_reference.side_effect = resolve
        self.assertEqual(self.await_job(self.request(**extra))['status'], 'failed')
        self.assertEqual(store.quote_source(quote_tests.PEER, 1)['message_id'], '99')
        self.app.service.send_quote.assert_not_called()

    def test_clear_after_queueing_is_rechecked_by_send_guard(self):
        store, extra = self.historical_source()
        self.app.service.resolve_quote_reference.return_value = {'message_id':quote_tests.ID, 'created_at':1792887443}
        def send(*args, **kwargs):
            self.assertTrue(kwargs['delivery_check']())
            self.reuse_history(store)
            self.assertFalse(kwargs['delivery_check']())
            return False
        self.app.service.send_quote.side_effect = send
        self.assertEqual(self.await_job(self.request(**extra))['status'], 'failed')

    def test_live_reset_reusing_row_id_is_not_accepted(self):
        buffer = self.controller.messages
        event = Message('same', quote_tests.PEER, quote_tests.PEER, session_id='s', sequence=1,
                        data={'native_message_id':quote_tests.ID})
        buffer.append(event, account=quote_tests.SELF)
        old = buffer.tail()['rows'][0]
        buffer.reset()
        buffer.append(event, account=quote_tests.SELF)
        new = buffer.tail()['rows'][0]
        self.assertEqual(old['id'], new['id'])
        self.assertNotEqual(old['quote_token'], new['quote_token'])
        with self.assertRaises(PanelError):
            self.controller.send_text(self.request(quote_id=None,
                quote_ref={'id':old['id'], 'history':False, 'token':old['quote_token']}))
        self.app.service.send_quote.assert_not_called()
        status = self.await_job(self.request(quote_id=None,
            quote_ref={'id':new['id'], 'history':False, 'token':new['quote_token']}))
        self.assertEqual(status['status'], 'sent')


@unittest.skipUnless(sys.platform == 'win32', 'Native event structures only')
class LiveMediaPolicyTests(fixtures.StoreFixture):
    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(ROOT/'native'))
        self.addCleanup(lambda:sys.path.remove(str(ROOT/'native')))
        from text_service import NativeTextService
        from filehelper_service import ReceivedMediaMessageV4
        self.raw_event = ReceivedMediaMessageV4
        self.raw = NativeTextService(pid=123, account=SELF, image_allowlist=('filehelper',))
        self.raw.router.set_account(SELF)
        self.raw._sender_wechat_name = Mock(return_value='test')
        self.raw._image_receiver, self.raw._file_receiver, self.raw._forwarded_receiver = Mock(), Mock(), Mock()
        self.raw.monitor_handler = Mock()
        self.app = BotApplication.__new__(BotApplication)
        self.app.config = {'chat_allowlist':['filehelper']}
        self.app.members = self.store
        backend = NativeTextBackend.__new__(NativeTextBackend)
        backend.raw = self.raw
        backend.set_media_chat_policy(self.app._chat_allowed)
        self.store.set_chat(PEER, memory=False, reply=False)

    def emit(self, chat, kind=3, xml=None):
        xml = xml or '<msg><img aeskey="'+'a'*32+'" cdnmidimgurl="test-key" /></msg>'
        if chat.endswith('@chatroom'):
            xml = PEER + ':\n' + xml
        return self.raw._map_message(self.raw_event(type=kind, sequence=42, sender=chat.encode(),
            receiver=SELF.encode(), image_xml=xml.encode()))

    def test_private_and_group_switches_apply_immediately_without_reconnect(self):
        for chat in (PEER, ROOM):
            with self.subTest(chat=chat):
                self.store.set_chat(chat, memory=False, reply=False)
                self.raw._image_receiver.reset_mock()
                self.emit(chat)
                self.raw._image_receiver.submit.assert_not_called()
                self.store.set_chat(chat, memory=True, reply=True)
                self.emit(chat)
                self.raw._image_receiver.submit.assert_called_once()
                data = self.raw._image_receiver.submit.call_args.args[0]
                self.assertEqual(data['chat_id'], chat)
                self.store.set_chat(chat, memory=False, reply=False)
                self.emit(chat)
                self.raw._image_receiver.submit.assert_called_once()

    def test_memory_only_and_current_allowlist_not_startup_snapshot_control_media(self):
        self.store.set_chat(PEER, memory=True, reply=False)
        self.emit(PEER)
        self.raw._image_receiver.submit.assert_called_once()
        self.store.set_chat(PEER, memory=False, reply=False)
        self.app.config = {'chat_allowlist':[PEER]}
        self.emit(PEER)
        self.assertEqual(self.raw._image_receiver.submit.call_count, 2)
        self.app.config = {'chat_allowlist':[]}
        self.emit(PEER)
        self.assertEqual(self.raw._image_receiver.submit.call_count, 3)

    def test_files_and_forwarded_follow_same_policy(self):
        import test_file_integration as files
        import test_forwarded as forwarded
        for kind, xml, receiver in ((49, files.XML, self.raw._file_receiver),
                                    (49, forwarded.forward_xml(), self.raw._forwarded_receiver)):
            self.store.set_chat(PEER, memory=False, reply=False)
            self.emit(PEER, kind, xml)
            receiver.submit.assert_not_called()
            self.store.set_chat(PEER, memory=True, reply=True)
            self.emit(PEER, kind, xml)
            receiver.submit.assert_called_once()
        self.assertEqual(self.raw.monitor_handler.call_args.args[0]['forwarded_status'], 'pending')

    def test_policy_failure_skips_download_but_keeps_monitor_event(self):
        self.raw.media_chat_allowed = Mock(side_effect=RuntimeError('synthetic'))
        self.emit(PEER)
        self.raw._image_receiver.submit.assert_not_called()
        self.raw.monitor_handler.assert_called_once()

    def test_connect_installs_policy_before_backend_start(self):
        app = self.app
        seen = []
        class Backend:
            pid, session_id = 123, 'test'
            def set_media_chat_policy(self, policy): self.policy = policy
            def start(self, emit): seen.append(self.policy(PEER))
        backend = Backend()
        app.backend_factory = lambda:backend
        app.service = Mock()
        app._connect()
        self.assertEqual(seen, [False])
        self.assertTrue(app._connection_ready)
        app.members.set_chat(PEER, memory=True, reply=True)
        self.assertTrue(backend.policy(PEER))


class LiveBudgetTests(unittest.TestCase):
    def quote_message(self, sequence, text='汉'*8000):
        return Message(text, PEER, PEER, session_id='s', sequence=sequence, data={
            'quote':{'version':1, 'reply':text, 'reference':{'message_id':str(sequence),
                'sender_id':SELF, 'sender_name':'me', 'type':1, 'content':'旧'*1000}}})

    def assert_budget(self, batch):
        self.assertLess(len(json.dumps(batch, ensure_ascii=False).encode()), 4*1024*1024)
        for row in batch['rows']:
            self.assertLessEqual(len(json.dumps(row, ensure_ascii=False).encode()), MAX_ROW_BYTES)

    def test_200_long_quotes_fit_without_changing_internal_quote_or_cursor(self):
        buffer = MessageBuffer()
        for sequence in range(1, 201):
            buffer.append(self.quote_message(sequence), account=SELF)
        original = deepcopy(list(buffer.rows))
        batch = buffer.tail()
        self.assert_budget(batch)
        self.assertEqual(len(batch['rows']), 200)
        self.assertEqual(batch['cursor'], 200)
        self.assertTrue(all(row['preview_truncated'] for row in batch['rows']))
        self.assertEqual(list(buffer.rows), original)
        self.assertEqual(len(buffer.texts(PEER)[0]['quote']['reply']), 8000)
        batch['rows'][0]['quote']['reply'] = 'mutated browser copy'
        self.assertEqual(buffer.rows[0]['quote']['reply'], '汉'*8000)

    def test_incremental_batches_do_not_skip_messages(self):
        buffer = MessageBuffer()
        for sequence in range(1, 452):
            buffer.append(self.quote_message(sequence), account=SELF)
        cursor, ids = 1, []
        while cursor < 451:
            batch = buffer.tail(cursor)
            self.assert_budget(batch)
            self.assertGreater(batch['cursor'], cursor)
            cursor = batch['cursor']
            ids.extend(row['id'] for row in batch['rows'])
        self.assertEqual(ids, list(range(2, 452)))

    def test_utf8_and_json_escaping_count_toward_budget(self):
        for text in ('😀'*8000, '\t\n"\\'*2000):
            buffer = MessageBuffer()
            buffer.append(self.quote_message(1, text), account=SELF)
            self.assert_budget(buffer.tail())
            self.assertEqual(buffer.rows[0]['quote']['reply'], text)

    def test_tool_trace_previews_fit_and_do_not_shorten_in_memory_steps(self):
        buffer = MessageBuffer()
        event = Message('', PEER)
        for run in range(200):
            for _ in range(40):
                buffer.trace(event, str(run), {'kind':'result', 'text':'汉'*2000, 'status':'running'}, account=SELF)
        batch = buffer.tail()
        self.assert_budget(batch)
        self.assertEqual(len(batch['rows']), 200)
        self.assertTrue(all(row['preview_truncated'] for row in batch['rows']))
        self.assertEqual(len(buffer.rows[0]['steps'][0]['text']), 2000)

    def test_live_token_does_not_change_when_message_id_is_attached(self):
        buffer = MessageBuffer(clock=lambda:123)
        buffer.append(Message('text', PEER, PEER, session_id='s', sequence=1), account=SELF)
        old = buffer.tail()['rows'][0]
        source = buffer.quote_row(PEER, old['id'], account=SELF)
        self.assertEqual(source['quote_token'], old['quote_token'])
        self.assertTrue(buffer.attach_quote_id(PEER, old['id'], source, '42', account=SELF))
        self.assertEqual(buffer.tail()['rows'][0]['quote_token'], old['quote_token'])
        buffer.reset()
        buffer.append(Message('text', PEER, PEER, session_id='s', sequence=1), account=SELF)
        self.assertFalse(buffer.attach_quote_id(PEER, old['id'], source, '42', account=SELF))


@unittest.skipUnless(shutil.which('node'), 'Node required for isolated UI')
class ClearQuoteUITests(unittest.TestCase):
    def test_clear_cancels_quote_and_confirmation_but_preserves_draft(self):
        script = (ROOT/'webui/app.js').read_text(encoding='utf-8')
        start = script.index("$('#chat-forget').onclick =")
        handler = script[start:script.index("$('#llm-form').elements.base_url", start)]
        probe = r'''
const assert=require('assert');let robot='r',chatConfirm={robot:'r',chat:'wxid_peer'};
const chatHistory=new Map(),historyLoading=new Map(),chatQuotes=new Map([['r:wxid_peer',{native:true}]]);
let switchHint={},chatListMarkup='',chatTimelineSignature='',closed=false;
const nodes={'#send-chat':{value:'wxid_peer'},'#chat-heading':{textContent:'peer'},
  '#send-content':{value:'keep typed reply'},'#chat-send-dialog':{close(){closed=true;}},'#chat-forget':{}};
function $(id){return nodes[id];}function confirm(){return true;}function renderChatWorkspace(){}
async function api(){return {cleared:1};}
'''
        tail = r'''
(async()=>{await $('#chat-forget').onclick();assert.equal(chatQuotes.size,0);assert.equal(chatConfirm,null);
assert.equal(closed,true);assert.equal($('#send-content').value,'keep typed reply');})()
.catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', probe+handler+tail], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__': unittest.main()
