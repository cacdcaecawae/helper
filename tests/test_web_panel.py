"""本地管理台：进程所有权、配置保留、任务线程和 HTTP 隔离。"""
from concurrent.futures import Future
import http.client
import json
import logging
from pathlib import Path
import queue
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wxbot_framework.config import ConfigStore
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message
from wxbot_framework.web_panel import LogBuffer, PanelController, PanelError, PanelServer, read_json

ROOT = Path(__file__).resolve().parents[1]


class FakeLock:
    held = set()

    def __init__(self, path):
        self.path, self.acquired = str(path), False

    def acquire(self):
        if self.path in self.held:
            raise RuntimeError('已经运行')
        self.held.add(self.path)
        self.acquired = True
        return self

    def close(self):
        if self.acquired:
            self.held.discard(self.path)
            self.acquired = False

    __enter__ = acquire

    def __exit__(self, *_):
        self.close()


class FakePlugins:
    def __init__(self, store):
        self.store = store
        self.active = False
        self.thread_ids = []

    def status(self):
        enabled = self.store.load()['plugins']['echo']['enabled']
        return [{'name':'echo', 'enabled':enabled, 'active':self.active, 'description':'测试回声', 'errors':0}]

    def enable(self, name):
        self.thread_ids.append(threading.get_ident())
        self.store.set_enabled(name, True)
        self.active = True

    def disable(self, name):
        self.thread_ids.append(threading.get_ident())
        self.store.set_enabled(name, False)
        self.active = False

    def reload(self, name):
        self.thread_ids.append(threading.get_ident())

    def is_active(self, name):
        return self.active and name == 'echo'


class FakeApp:
    submit_control = BotApplication.submit_control
    _drain_controls = BotApplication._drain_controls

    def __init__(self, root, store):
        self.config = store.load()
        self.plugins = FakePlugins(store)
        self.stop_event, self.stopped_event = threading.Event(), threading.Event()
        self.controls = queue.Queue(maxsize=64)
        self._connection_ready = False
        self._last_connection_error = ''
        self.backend = SimpleNamespace(pid=1234, chats=lambda:[{'chat_id':'123@chatroom','is_group':True}])

    def run(self):
        self.thread_id = threading.get_ident()
        self._connection_ready = True
        try:
            while not self.stop_event.wait(.01):
                self._drain_controls()
        finally:
            self.stop()

    def stop(self):
        self.stop_event.set()
        self._drain_controls(stopping=True)
        self.stopped_event.set()


class PanelFixture(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.runtime/tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for path in ROOT.glob('*.example.json'):
            shutil.copy2(path, self.root/path.name)
        (self.root/'plugins/echo').mkdir(parents=True)
        (self.root/'plugins/echo/__init__.py').write_text('',encoding='utf-8')
        (self.root/'plugins/echo/plugin.json').write_text(json.dumps({'description':'回声','default_enabled':True}),encoding='utf-8')
        self.accounts = [{'pid':1234,'wxid':'wxid_test_robot','name':'测试账号'}]
        self.controller = PanelController(self.root, app_factory=FakeApp, scan=lambda root:self.accounts, lock_factory=FakeLock)
        self.addCleanup(self.controller.close)

    def wait_stopped(self):
        self.controller.stop()
        self.controller.worker.join(2)
        self.assertFalse(self.controller.worker.is_alive())


class ControllerTests(PanelFixture):
    def test_start_plugin_operation_uses_runtime_thread_then_stop_releases_lock(self):
        self.controller.start()
        with self.assertRaises(PanelError):
            self.controller.start()
        app = self.controller.app
        self.controller.plugin_action({'name':'echo','action':'enable'})
        self.assertEqual(app.plugins.thread_ids, [app.thread_id])
        self.assertNotEqual(app.thread_id, threading.get_ident())
        self.assertEqual(self.controller.chats()[0]['chat_id'], '123@chatroom')
        with self.assertRaises(PanelError):
            self.controller.save_configuration({'groups':[]})
        self.wait_stopped()
        self.assertEqual(self.controller.snapshot()['state'], 'stopped')
        self.controller.start()
        self.wait_stopped()

    def test_chat_switches_come_with_chats_and_save_while_running(self):
        class Members:
            settings, backfilled = {}, []

            def chat_setting(self, chat):
                return dict(self.settings.get(chat, {'memory': False, 'reply': False}))

            def remembers(self, chat):
                return self.chat_setting(chat)['memory']

            def set_chat(self, chat, *, memory, reply):
                self.settings[chat] = {'memory': memory or reply, 'reply': reply}
                return self.chat_setting(chat)

            def backfill(self, chat, rows):
                self.backfilled.append([row['content'] for row in rows])
                return len(rows)

            def remembered_chats(self):
                return [chat for chat, setting in self.settings.items() if setting['memory']]

            def chat_history(self, chat, **options):
                return []
        self.controller.start()
        self.controller.app.members = members = Members()
        self.controller.messages.append(Message('周六有空吗', '123@chatroom', 'wxid_a', is_group=True), account='wxid_test_robot')
        self.assertEqual(self.controller.chats(), [{'chat_id':'123@chatroom','is_group':True,'memory':False,'reply':False}])
        # 运行中直接改，不用停机；刚打开长期记忆时把窗口里的消息补进去；智能体没开时提醒一句
        self.assertEqual(self.controller.set_chat_settings({'chat_id':'123@chatroom','memory':False,'reply':True}),
                         {'memory':True, 'reply':True,
                          'notice':'已把窗口里的 1 条消息补进长期记忆；还需在插件管理中开启“智能体”才会自动回复'})
        self.assertEqual(members.backfilled, [['周六有空吗']])
        self.controller.set_chat_settings({'chat_id':'123@chatroom','memory':True,'reply':False})
        self.assertEqual(len(members.backfilled), 1)  # 本来就开着，不再补
        self.assertEqual({key: self.controller.chats()[0][key] for key in ('memory', 'reply')}, {'memory': True, 'reply': False})
        with self.assertRaises(ValueError):
            self.controller.set_chat_settings({'chat_id':'../x', 'memory':True, 'reply':False})
        members.forget = lambda chat: 3
        self.assertEqual(self.controller.forget_chat({'chat_id':'123@chatroom'}), {'cleared': 3})
        self.wait_stopped()

    def test_remembered_chats_are_listed_and_history_loads_after_restart(self):
        class Members:
            def remembered_chats(self):
                return ['wxid_old_friend']

            def chat_setting(self, chat):
                return {'memory': chat == 'wxid_old_friend', 'reply': False}

            def chat_history(self, chat, *, before=None, limit=200):
                if chat != 'wxid_old_friend':
                    return []
                lines = [{'id': 7, 'at': '2026-10-21T00:00:00+00:00', 'text': '周六见', 'own': False, 'name': '老王'},
                         {'id': 8, 'at': '2026-10-21T00:01:00+00:00', 'text': '好', 'own': True, 'name': '我'}]
                return lines[-limit:]
        self.controller.start()
        app = self.controller.app
        native = [{'chat_id':'123@chatroom','is_group':True}]
        app.backend = SimpleNamespace(pid=1234, chats=lambda: list(native),
                                      remember_chat=lambda chat: native.append({'chat_id': chat, 'is_group': False, 'name': '老王'}))
        app.members = Members()
        rows = {row['chat_id']: row for row in self.controller.chats()}
        # 本次还没来消息、但开了长期记忆的会话也列出来，名字照常由原生层读，预览是库里最后一条
        self.assertEqual((rows['wxid_old_friend']['name'], rows['wxid_old_friend']['preview']['content']), ('老王', '好'))
        self.assertNotIn('preview', rows['123@chatroom'])
        history = self.controller.chat_history('wxid_old_friend')['rows']
        self.assertEqual([(row['sender_name'], row['content'], row['direction'], row['date'], row['time']) for row in history],
                         [('老王', '周六见', 'incoming', '2026-10-21', '08:00:00'), ('我', '好', 'outgoing', '2026-10-21', '08:01:00')])
        with self.assertRaises(ValueError):
            self.controller.chat_history('../x')
        self.wait_stopped()

    def test_multi_account_requires_explicit_binding_and_stale_selection_rejected(self):
        self.accounts.append({'pid':4321,'wxid':'wxid_other_robot','name':'另一个账号'})
        with self.assertRaises(PanelError):
            self.controller.start()
        self.assertIsNone(self.controller.store.load()['wechat_account'])
        with self.assertRaises(PanelError):
            self.controller.bind_account({'wxid':'wxid_test_robot','pid':5678})
        self.controller.bind_account({'wxid':'wxid_other_robot','pid':4321})
        self.assertEqual(self.controller.store.load()['wechat_account'], 'wxid_other_robot')

    def test_other_console_instance_blocks_config_and_plugin_edits(self):
        with FakeLock(self.root/'.runtime/bot.lock'):
            for operation in (lambda:self.controller.start(),
                lambda:self.controller.save_configuration({'groups':[]}),
                lambda:self.controller.bind_account({'wxid':'wxid_test_robot','pid':1234}),
                lambda:self.controller.plugin_action({'name':'echo','action':'disable'})):
                with self.subTest(operation=operation), self.assertRaises(PanelError):
                    operation()

    def test_key_is_not_returned_blank_retains_and_explicit_clear_works(self):
        path = self.root/'llm_config.json'
        data = read_json(path)
        data.update(api_key='private-example-credential', extra_setting='keep me')
        path.write_text(json.dumps(data),encoding='utf-8')
        response = self.controller.configuration()
        self.assertNotIn('private-example-credential', json.dumps(response))
        self.assertTrue(response['llm']['api_key_configured'])
        self.controller.save_configuration({'llm':{'api_key':'','model':'new-model'}})
        self.assertEqual(read_json(path)['api_key'], 'private-example-credential')
        self.assertEqual(read_json(path)['extra_setting'], 'keep me')
        self.controller.save_configuration({'llm':{'clear_api_key':True}})
        self.assertEqual(read_json(path)['api_key'], '')
        self.assertGreater(len(list((self.root/'.runtime/web/backups').glob('*'))),0)

    def test_group_edit_keeps_profiles_and_adds_nonempty_allowlist(self):
        path = self.root/'members_config.json'
        data = read_json(path)
        data['groups'] = {'123@chatroom':[{'key':'test_person','name':'测试玩家'}]}
        path.write_text(json.dumps(data),encoding='utf-8')
        self.controller.save_configuration({'groups':['123@chatroom','456@chatroom'], 'chat_allowlist':['filehelper']})
        self.assertEqual(read_json(path)['groups']['123@chatroom'], data['groups']['123@chatroom'])
        self.assertEqual(self.controller.store.load()['chat_allowlist'], ['filehelper','123@chatroom','456@chatroom'])

    def test_model_list_uses_page_choice_and_saved_key_without_returning_it(self):
        path = self.root/'llm_config.json'
        data = read_json(path); data['api_key'] = 'private-model-key'
        path.write_text(json.dumps(data), encoding='utf-8')
        seen = []
        def models(client):
            seen.append((client.provider, client.endpoint, client._api_key))
            return ['model-a']
        with patch('wxbot_framework.llm.LLMClient.list_models', models):
            result = self.controller.llm_models({'provider':'openai_compatible', 'base_url':data['base_url']})
            # 地址改了又没填密钥：不能把已保存的密钥发到新地址
            with self.assertRaisesRegex(PanelError, '接口地址已更改'):
                self.controller.llm_models({'provider':'openai_compatible', 'base_url':'https://gateway.example.test/v1'})
            self.controller.llm_models({'base_url':'https://gateway.example.test/v1', 'api_key':' typed-key '})
        self.assertEqual(result, {'models':['model-a'], 'default':None})
        self.assertEqual([key for _, _, key in seen], ['private-model-key', 'typed-key'])
        self.assertEqual(seen[1][1], 'https://gateway.example.test/v1/chat/completions')
        self.assertEqual(read_json(path)['api_key'], 'private-model-key')

    def test_claude_code_needs_no_key_and_keeps_its_config_dir(self):
        result = self.controller.llm_models({'provider':'claude_code', 'base_url':'https://changed.example.test/v1'})
        self.assertEqual(result, {'models':['default', 'opus', 'sonnet', 'haiku'], 'default':'default'})
        directory = str(self.root)
        self.controller.save_configuration({'llm':{'provider':'claude_code', 'model':'sonnet', 'claude_config_dir':directory}})
        saved = self.controller.configuration()['llm']
        self.assertEqual((saved['provider'], saved['model'], saved['claude_config_dir']), ('claude_code', 'sonnet', directory))

    def test_compatible_llm_options_round_trip_without_changing_saved_key(self):
        path = self.root/'llm_config.json'
        original = read_json(path)
        original['api_key'] = 'private-model-key'
        path.write_text(json.dumps(original), encoding='utf-8')
        settings = {'base_url':'https://gateway.example.test/v1', 'model':'custom-model',
                    'json_mode':False, 'token_limit_parameter':'max_completion_tokens',
                    'extra_body':{'reasoning_effort':'low'}}
        # 换地址不带新密钥：拒绝保存，旧密钥不能跟到新地址；文件不变
        with self.assertRaisesRegex(PanelError, '接口地址已更改'):
            self.controller.save_configuration({'llm':settings})
        self.assertEqual(read_json(path), original)
        self.controller.save_configuration({'llm':dict(settings, api_key='gateway-key')})
        saved = read_json(path)
        for key, value in settings.items():
            self.assertEqual(saved[key], value)
            self.assertEqual(self.controller.configuration()['llm'][key], value)
        self.assertEqual(saved['api_key'], 'gateway-key')
        self.assertNotIn('gateway-key', json.dumps(self.controller.configuration()))
        # 不换地址时留空仍沿用已保存的密钥；换地址并勾选清除则保存为空密钥
        self.controller.save_configuration({'llm':{'base_url':settings['base_url'], 'model':'other-model'}})
        self.assertEqual(read_json(path)['api_key'], 'gateway-key')
        self.controller.save_configuration({'llm':{'base_url':'https://local.example.test/v1', 'clear_api_key':True}})
        self.assertEqual(read_json(path)['api_key'], '')
        self.controller.save_configuration({'llm':{'extra_body':{}}})
        self.assertEqual(read_json(path)['extra_body'], {})

    def test_legacy_llm_config_has_compatible_defaults_and_invalid_edits_are_atomic(self):
        path = self.root/'llm_config.json'
        value = read_json(path)
        for key in ('json_mode', 'token_limit_parameter', 'extra_body'):
            value.pop(key, None)
        path.write_text(json.dumps(value), encoding='utf-8')
        settings = self.controller.configuration()['llm']
        self.assertTrue(settings['json_mode'])
        self.assertEqual(settings['token_limit_parameter'], 'max_tokens')
        self.assertEqual(settings['extra_body'], {})
        original = path.read_bytes()
        for edit in ({'json_mode':'false'}, {'token_limit_parameter':'invalid'}, {'extra_body':[]},
                     {'extra_body':{'stream':True}}):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                self.controller.save_configuration({'llm':edit})
            self.assertEqual(path.read_bytes(), original)

    def test_invalid_model_or_group_does_not_partially_save(self):
        original = {p:p.read_bytes() for p in self.root.glob('*_config.json')}
        for data in ({'llm':{'model':'changed'},'groups':['not-a-room']},
                     {'tts':{'max_chars_per_clip':500}}, {'llm':{'timeout_seconds':0}},
                     {'groups':'123@chatroom'}, {'daily_clear':1}):
            with self.subTest(data=data), self.assertRaises((PanelError, ValueError, RuntimeError)):
                self.controller.save_configuration(data)
            for path, raw in original.items():
                self.assertEqual(path.read_bytes(), raw)

    def test_write_failure_rolls_back_already_written_configs(self):
        from wxbot_framework.web_panel import atomic_json
        old = (self.root/'llm_config.json').read_bytes()
        def fail_on_main(path, data):
            if path.name == 'bot_config.json':
                raise OSError('模拟磁盘写入失败')
            atomic_json(path, data)
        with patch('wxbot_framework.web_panel.atomic_json',side_effect=fail_on_main):
            with self.assertRaises(OSError):
                self.controller.save_configuration({'llm':{'model':'new-model'}})
        self.assertEqual((self.root/'llm_config.json').read_bytes(), old)

    def test_cancelled_task_does_not_execute_and_shutdown_finishes_pending_tasks(self):
        app = FakeApp(self.root,self.controller.store)
        calls = []
        cancelled=app.submit_control(lambda:calls.append('wrong'))
        cancelled.cancel()
        app._drain_controls()
        self.assertEqual(calls,[])
        waiting=app.submit_control(lambda:None)
        app.stop()
        with self.assertRaises(RuntimeError):
            waiting.result()

    def test_offline_plugin_settings_persist_without_changing_other_plugins(self):
        before = self.controller.store.load()['plugins']['picture_idiom']
        self.controller.plugin_action({'name':'echo','action':'configure','options':{'enabled':False,'prefix':'ping '}})
        config = self.controller.store.load()
        self.assertEqual(config['plugins']['echo']['prefix'],'ping ')
        self.assertEqual(config['plugins']['picture_idiom'],before)
        with self.assertRaises(PanelError):
            self.controller.plugin_action({'name':'echo','action':'configure','options':{'enabled':'yes'}})


class HTTPTests(PanelFixture):
    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT/'webui', self.root/'webui')
        self.server = PanelServer(self.controller, 0)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self,path='/',data=None,*,token=True,origin=None,host=None,raw=None,content_type='application/json'):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        headers={}
        if token:headers['X-Helper-Token']=self.server.token
        if origin:headers['Origin']=origin
        if host:headers['Host']=host
        if data is not None or raw is not None:headers['Content-Type']=content_type
        body=json.dumps(data).encode() if data is not None else raw
        conn.request('POST' if body is not None else 'GET',path,body=body,headers=headers)
        response=conn.getresponse()
        result=response.status,dict(response.getheaders()),response.read()
        conn.close()
        return result

    def test_page_and_assets_are_local_and_token_is_not_in_url(self):
        status,headers,body=self.request(token=False)
        self.assertEqual(status,200)
        self.assertIn(self.server.token.encode(),body)
        self.assertEqual(headers['X-Frame-Options'],'DENY')
        self.assertEqual(headers['Cache-Control'],'no-store')
        for path in ('/app.js','/style.css'):
            self.assertEqual(self.request(path,token=False)[0],200)

    def test_api_rejects_missing_token_cross_origin_and_rebound_host(self):
        self.assertEqual(self.request('/api/config',token=False)[0],403)
        self.assertEqual(self.request('/api/config',origin='https://attacker.invalid')[0],403)
        self.assertEqual(self.request('/',host=f'attacker.invalid:{self.port}')[0],403)
        self.assertEqual(self.request('/api/start',{},origin='null')[0],403)

    def test_http_start_stop_and_secret_free_config(self):
        self.assertEqual(self.request('/api/config')[0],200)
        self.assertNotIn(b'"api_key":',self.request('/api/config')[2])
        self.assertEqual(self.request('/api/start',{})[0],200)
        self.assertEqual(self.request('/api/config',{'groups':[]})[0],409)
        self.assertEqual(self.request('/api/stop',{})[0],200)

    def test_member_and_send_routes_validate_token_and_preserve_connection(self):
        from wxbot_framework.sdk import ServiceFacade
        self.assertEqual(self.request('/api/send-connection')[0],409)
        self.controller.start()
        calls=[]
        backend=SimpleNamespace(session_id='http-test-session',pid=1234,stop=lambda: {},
            group_members=lambda room: ({'wxid':'wxid_test_member','nickname':'测试群友','role':'admin'},),
            send_at_text=lambda *args: calls.append(args) or True)
        service=ServiceFacade(text_delay={'enabled':False});service.bind(backend)
        self.addCleanup(service.close)
        self.controller.app.backend=backend;self.controller.app.service=service
        data=json.loads(self.request('/api/group-members?room=123%40chatroom')[2])
        self.assertEqual(data['members'][0]['role'],'admin')
        self.assertEqual(data['session_id'],'http-test-session')
        body={'request_id':'http-synthetic-request-001','session_id':data['session_id'],
              'chat_id':'123@chatroom','content':'你好','at_list':['wxid_test_member']}
        self.assertEqual(self.request('/api/send-text',body,token=False)[0],403)
        self.assertFalse(calls)
        self.assertEqual(self.request('/api/send-text',body)[0],200)
        for _ in range(100):
            result=json.loads(self.request('/api/send-status?id='+body['request_id'])[2])
            if result['status']!='sending':break
            time.sleep(.005)
        self.assertEqual(result['status'],'sent')
        self.assertEqual(calls,[('123@chatroom','@测试群友\u2005\n你好',('wxid_test_member',))])

    def test_invalid_json_oversized_body_and_unknown_routes(self):
        self.assertEqual(self.request('/api/config',raw=b'{broken')[0],400)
        self.assertEqual(self.request('/api/config',raw=b'x'*65537)[0],413)
        self.assertEqual(self.request('/api/config',raw=b'{}',content_type='text/plain')[0],400)
        self.assertEqual(self.request('/../bot_config.json')[0],404)
        self.assertEqual(self.request('/api/unknown',{})[0],404)


class LogTests(unittest.TestCase):
    def test_log_ring_buffer_hides_common_keys_and_obeys_cursor(self):
        buffer=LogBuffer()
        buffer.handle(logging.LogRecord('Test',logging.INFO,'',0,'hello',(),None))
        buffer.handle(logging.LogRecord('Test',logging.WARNING,'',0,'key sk-'+'a'*24,(),None))
        rows=buffer.tail(after=1)
        self.assertEqual(len(rows['rows']),1)
        self.assertNotIn('a'*24,rows['rows'][0]['message'])
        self.assertEqual(rows['cursor'],2)


if __name__ == '__main__':
    unittest.main()
