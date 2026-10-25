"""下载跨清空、历史引用跨重启的回归；临时库/伪下载器/隔离 JS，不收发真实消息。"""
from datetime import timedelta
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import test_review_fixes as fixtures
import test_web_panel as panel_tests
from wxbot_framework.backend import NativeTextBackend
from wxbot_framework.image_metadata import ImageStore
from wxbot_framework.image_receiver import ImageReceiver
from wxbot_framework.members import MemberStore
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message

ROOT = Path(__file__).resolve().parents[1]
SELF, PEER, ROOM = fixtures.SELF, fixtures.PEER, fixtures.ROOM
XML = '<msg><img aeskey="'+'a'*32+'" cdnmidimgurl="synthetic-key" /></msg>'
PNG = b'\x89PNG\r\n\x1a\n'+b'\0'*32


@unittest.skipUnless(sys.platform == 'win32', 'Native event structures only')
class PendingImageTests(fixtures.StoreFixture):
    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(ROOT/'native'))
        self.addCleanup(lambda:sys.path.remove(str(ROOT/'native')))
        from text_service import NativeTextService
        from filehelper_service import ReceivedMediaMessageV4
        self.event_type = ReceivedMediaMessageV4
        self.store.set_chat(PEER, memory=True, reply=True)
        self.store.set_chat(ROOM, memory=True, reply=True)
        self.app = BotApplication.__new__(BotApplication)
        self.app.members, self.app.config = self.store, {'chat_allowlist':['filehelper']}
        self.app.plugins = Mock()
        self.app.plugins.is_active.return_value = False
        self.app._with_group_role = lambda message:message
        self.app._observe_message = Mock()
        self.app._dispatch_recall_plugins = Mock()
        self.raw = NativeTextService(pid=123, account=SELF)
        self.raw.router.set_account(SELF)
        self.raw._sender_wechat_name = Mock(return_value='synthetic')
        backend = NativeTextBackend.__new__(NativeTextBackend)
        backend.raw, backend.session_id = self.raw, 'media-session'
        backend.set_media_chat_policy(self.app._chat_allowed)
        backend.set_media_context_provider(self.app._media_context)
        with patch.object(self.raw, 'start', return_value=True):
            backend.start(self.app._emit)
        self.entered, self.release, self.delivered = threading.Event(), threading.Event(), threading.Event()
        self.completed = []
        def download(metadata, path):
            self.entered.set()
            if not self.release.wait(3):
                raise RuntimeError('test download timed out')
            path.write_bytes(PNG)
            return True
        def deliver(data):
            try:
                self.completed.append(Message.from_native(11046, data, backend.session_id))
                self.raw._deliver_image(data)
            finally:
                self.delivered.set()
        self.receiver = ImageReceiver(ImageStore(self.store.config_file.parent/'images', SELF), download, deliver, lambda:True)
        self.raw._image_receiver = self.receiver
        self.addCleanup(self.close_receiver)

    def close_receiver(self):
        self.release.set()
        self.receiver.close(timeout=2)
        self.assertFalse(self.receiver.is_alive)

    def enqueue(self, chat=PEER, sequence=1):
        for event in (self.entered, self.release, self.delivered):event.clear()
        body = PEER+':\n'+XML if chat.endswith('@chatroom') else XML
        self.raw._map_message(self.event_type(type=3, sequence=sequence, sender=chat.encode(),
                              receiver=SELF.encode(), image_xml=body.encode()))
        self.assertTrue(self.entered.wait(2))

    def finish(self):
        self.release.set()
        self.assertTrue(self.delivered.wait(2))
        self.assertEqual(self.completed[-1].data['image_status'], 'ready')
        return self.completed[-1]

    def test_private_image_finishing_after_clear_cannot_restore_memory_or_dispatch(self):
        self.enqueue()
        self.store.forget(PEER)
        old = self.finish()
        self.assertEqual(self.store.chat_history(PEER), [])
        self.assertFalse(self.store.record(old))  # 写库层也独立拒绝旧代次，不能只依赖运行时过滤。
        self.assertFalse(self.store.recorded(old))
        self.app.plugins.dispatch.assert_not_called()
        self.enqueue(sequence=2)
        fresh = self.finish()
        self.assertTrue(self.store.recorded(fresh))
        self.assertEqual(len(self.store.chat_history(PEER)), 1)
        self.app.plugins.dispatch.assert_called_once()

    def test_group_image_with_zero_sequence_is_also_rejected_after_clear(self):
        self.enqueue(ROOM, sequence=0)
        self.store.clear(ROOM)
        old = self.finish()
        self.assertFalse(self.store.recorded(old))
        self.assertEqual(self.store.chat_history(ROOM), [])
        self.app.plugins.dispatch.assert_not_called()

    def test_success_uses_arrival_time_not_download_completion_time(self):
        arrived = self.now.isoformat()
        self.enqueue()
        context = self.app._observe_message.call_args.args[0].data['_media_context']
        self.now += timedelta(minutes=2)
        fresh = self.finish()
        self.assertEqual(fresh.received_at, context['received_at'])
        self.assertEqual(self.store.chat_history(PEER)[0]['at'], arrived)
        self.app.plugins.dispatch.assert_called_once()

    def test_clear_between_record_and_dispatch_drops_the_completed_image(self):
        record = self.store.record
        def record_then_clear(message):
            result = record(message)
            self.store.forget(message.chat_id)
            return result
        self.enqueue()
        with patch.object(self.store, 'record', side_effect=record_then_clear):
            self.finish()
        self.assertEqual(self.store.chat_history(PEER), [])
        self.app.plugins.dispatch.assert_not_called()

    def test_clearing_other_chat_does_not_cancel_this_image(self):
        self.enqueue()
        self.store.forget(ROOM)
        self.finish()
        self.assertEqual(len(self.store.chat_history(PEER)), 1)
        self.app.plugins.dispatch.assert_called_once()

    def test_daily_clear_invalidates_pre_midnight_download_but_not_new_admission(self):
        self.store.daily_clear = True
        self.enqueue(ROOM)
        self.now += timedelta(days=1)
        self.store.rollover()
        self.finish()
        self.assertEqual(self.store.chat_history(ROOM), [])
        self.app.plugins.dispatch.assert_not_called()
        self.enqueue(ROOM, sequence=2)
        self.finish()
        self.assertEqual(len(self.store.chat_history(ROOM)), 1)

    def test_new_store_or_wrong_chat_cannot_accept_old_media_context(self):
        context = self.store.media_context(PEER)
        old = Message('', PEER, PEER, type=3, session_id='s', sequence=1,
                      data={'_media_context':context, 'image_path':'synthetic.png'})
        self.assertTrue(self.store.media_is_current(old))
        other = Message('', ROOM, PEER, type=3, data={'_media_context':context})
        self.assertFalse(self.store.media_is_current(other))
        reopened = MemberStore(self.store.config_file, SELF, now=lambda:self.now)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.media_is_current(old))
        self.assertFalse(reopened.record(old))


class HistoryGenerationTests(panel_tests.PanelFixture):
    def test_bot_restart_changes_history_generation_without_erasing_live_window(self):
        self.controller.start()
        self.controller.messages.append(Message('keep visible', PEER, PEER), account=SELF)
        first = self.controller.message_tail()
        self.wait_stopped()
        self.assertEqual(self.controller.message_tail()['history_generation'], first['history_generation'])
        self.controller.start()
        second = self.controller.message_tail()
        self.assertEqual(second['stream_id'], first['stream_id'])
        self.assertEqual(second['rows'], first['rows'])
        self.assertNotEqual(second['history_generation'], first['history_generation'])


@unittest.skipUnless(shutil.which('node'), 'Node required for isolated UI')
class HistoryRestartUITests(unittest.TestCase):
    def test_restart_reloads_history_and_ignores_old_inflight_results_without_erasing_draft(self):
        script = (ROOT/'webui/app.js').read_text(encoding='utf-8')
        functions = script[script.index('async function loadChatHistory('):script.index('function mergeChatTimeline(')]
        functions += script[script.index('async function pollMessages('):script.index('function renderLogs(')]
        probe = r'''
const assert=require('assert');let robot='r',messageGeneration=0,messageCursor=0,messageStream='stream',messageError='';
let messages=[],state={connected:true},serviceAvailable=true,sendGeneration=0,sendSession='old',sendLoading=false;
let chatLoadKey='r',chatTimelineSignature='',closed=0,chatConfirm={robot:'r',chat:'peer'};
const chatHistory=new Map(),historyLoading=new Map(),chatQuotes=new Map([['r:peer',{native:true}]]);
const chatHistoryScopes=new Map([['r',JSON.stringify(['stream','old'])]]);
const nodes={'#send-chat':{value:'peer'},'#send-content':{value:'keep draft'},'#chat-send-dialog':{close(){closed++;}}};
function $(id){return nodes[id];}function renderSendState(){}function renderChatWorkspace(){}function renderMessages(){}
function toast(text){throw Error(text);}let resolveOld,historyCalls=0,generation='new';
async function api(path){
  if(path.startsWith('messages?'))return {rows:[],cursor:0,stream_id:'stream',history_generation:generation,reset:false};
  historyCalls++;if(historyCalls===1)return new Promise(resolve=>resolveOld=resolve);
  return {rows:[{id:'h1',quote_token:'new-token'}]};
}
(async()=>{
  chatHistory.set('other:peer',[{id:'unrelated'}]);chatQuotes.set('other:peer',{native:true});
  const pending=loadChatHistory('peer');await pollMessages('r',0);
  resolveOld({rows:[{id:'h1',quote_token:'expired-token'}]});await pending;
  assert.equal(chatHistory.get('r:peer')[0].quote_token,'new-token');assert.equal(historyCalls,2);
  assert.equal(chatQuotes.has('r:peer'),false);assert.equal(chatConfirm,null);assert.equal(closed,1);
  assert.equal($('#send-content').value,'keep draft');assert.equal(sendSession,'');assert.equal(sendGeneration,1);
  assert.equal(chatHistory.get('other:peer')[0].id,'unrelated');assert.equal(chatQuotes.has('other:peer'),true);
  await pollMessages('r',0);assert.equal(historyCalls,2); // 没重启时不反复失效缓存。
  messageStream='';generation='third'; // 切去别的机器人期间重启，再切回来也能发现换代。
  chatHistory.set('r:peer',[{id:'h1',quote_token:'stale-again'}]);await pollMessages('r',0);
  assert.equal(historyCalls,3);assert.equal(chatHistory.get('r:peer')[0].quote_token,'new-token');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', functions+probe], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__': unittest.main()
