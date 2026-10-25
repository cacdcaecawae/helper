"""原生引用的账号/会话/原消息验证；只使用模拟后端，不向微信发送。"""
import ctypes
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch

from test_web_panel import PanelFixture
from wxbot_framework.sdk import Message, ServiceFacade
from wxbot_framework.web_panel import PanelError
from wxbot_framework.members import MemberStore
from wxbot_framework.quote_references import QuoteReferenceError

SELF, PEER = 'wxid_quote_self', 'wxid_quote_peer'
ID = '3918779855605843848'


def payload():
    return {'version':1,'reply':'111哈哈哈','reference':{'message_id':ID,'sender_id':PEER,
            'sender_name':'朋友','type':1,'content':'你干啥呢'}}


class QuoteSendPanelTests(PanelFixture):
    def setUp(self):
        super().setUp()
        self.controller.start()
        self.app=self.controller.app
        self.app._connection_ready=True
        self.app.config.update(wechat_account=SELF,chat_allowlist=[])
        self.app.service=Mock()
        self.app.service.session_is_current.return_value=True
        self.app.service.send_quote.return_value=True
        self.controller.messages.append(Message('你干啥呢',PEER,PEER,session_id='s',sequence=1,
            data={'native_message_id':ID}),account=SELF,sender_name='朋友')

    def request(self, **changes):
        data={'request_id':'native_quote_request_0001','session_id':'s','chat_id':PEER,
              'content':'111哈哈哈','at_list':[],'quote_id':ID}
        return data|changes

    def test_server_resolves_original_and_never_sends_a_text_prefix(self):
        data=self.request(sender_id='wxid_spoofed',original='untrusted browser text')
        self.controller.send_text(data)
        for _ in range(100):
            if self.controller.send_status(data['request_id'])['status']!='sending':break
            time.sleep(.005)
        self.assertEqual(self.controller.send_status(data['request_id'])['status'],'sent')
        args,kwargs=self.app.service.send_quote.call_args
        self.assertEqual(args,(PEER,payload()))
        self.assertEqual(kwargs['expected_session'],'s')
        self.assertTrue(kwargs['delivery_check']())
        self.app.service.send_text.assert_not_called()
        self.controller.send_text(data)
        self.assertEqual(self.app.service.send_quote.call_count,1)

    def test_missing_cross_chat_or_numeric_ids_are_not_sent_or_downgraded(self):
        for changes in ({'quote_id':3918779855605843848},{'quote_id':'2'},
                        {'chat_id':'wxid_another_chat'},{'chat_id':'12345678@chatroom'},
                        {'at_list':[PEER]}):
            with self.subTest(changes=changes),self.assertRaises((PanelError,ValueError)):
                self.controller.send_text(self.request(**changes))
        self.app.service.send_quote.assert_not_called()
        self.app.service.send_text.assert_not_called()

    def test_recalled_or_old_session_message_fails_closed(self):
        self.app.service.session_is_current.return_value=False
        with self.assertRaises(PanelError):self.controller.send_text(self.request())
        self.app.service.session_is_current.return_value=True
        self.controller.messages.rows[0]['recalled']=True
        with self.assertRaises(PanelError):self.controller.send_text(self.request())

    def historical_source(self):
        config=self.root/'quote-members.json'
        config.write_text(json.dumps({'history_enabled':True,'database_file':'quote-data.sqlite3','groups':{}}),encoding='utf-8')
        store=MemberStore(config,SELF);self.addCleanup(store.close)
        store.set_chat(PEER,memory=True,reply=False)
        store.record(Message('你干啥呢',PEER,PEER,session_id='previous',sequence=1))
        self.app.members=store
        return store,{'quote_id':None,'quote_ref':{'id':'h1','history':True,'token':store.quote_source(PEER,1)['quote_token']}}

    def await_job(self,data):
        self.controller.send_text(data)
        for _ in range(100):
            status=self.controller.send_status(data['request_id'])
            if status['status']!='sending':return status
            time.sleep(.005)
        self.fail('mock quote job did not finish')

    def test_old_row_is_resolved_from_server_snapshot_then_id_is_saved_before_native_send(self):
        store,extra=self.historical_source()
        self.app.service.resolve_quote_reference.return_value={'message_id':ID,'created_at':1792887443}
        status=self.await_job(self.request(**extra))
        self.assertEqual(status['status'],'sent')
        source=self.app.service.resolve_quote_reference.call_args.args[0]
        self.assertEqual(source['sender_id'],PEER)
        self.assertEqual(source['content'],'你干啥呢')
        self.assertEqual(self.app.service.send_quote.call_args.args[1]['reference']['message_id'],ID)
        self.assertEqual(store.quote_source(PEER,1)['message_id'],ID)
        self.assertEqual(store.quote_source(PEER,1)['content'],'你干啥呢')
        self.app.service.send_text.assert_not_called()

    def test_ambiguous_old_source_never_sends_or_saves_a_guessed_id(self):
        store,extra=self.historical_source()
        self.app.service.resolve_quote_reference.side_effect=QuoteReferenceError('旧消息未唯一确认，未发送引用')
        status=self.await_job(self.request(**extra))
        self.assertEqual(status['status'],'failed')
        self.assertIn('未唯一确认',status['error'])
        self.assertEqual(store.quote_source(PEER,1)['message_id'],'')
        self.app.service.send_quote.assert_not_called();self.app.service.send_text.assert_not_called()

    def test_clearing_history_during_resolution_cancels_the_queued_quote(self):
        store,extra=self.historical_source()
        def resolve(*args,**kwargs):
            store.forget(PEER)
            return {'message_id':ID,'created_at':1792887443}
        self.app.service.resolve_quote_reference.side_effect=resolve
        status=self.await_job(self.request(**extra))
        self.assertEqual(status['status'],'failed')
        self.app.service.send_quote.assert_not_called();self.app.service.send_text.assert_not_called()


class QuoteFacadeTests(unittest.TestCase):
    def service(self):
        service=ServiceFacade(text_delay={'enabled':False})
        backend=Mock(session_id='s');backend.send_quote.return_value=True
        service.bind(backend)
        service.message_observer=Mock();service.sent_recorder=Mock()
        return service,backend

    def test_success_projects_readable_text_and_saves_structure_once(self):
        service,backend=self.service()
        self.assertTrue(service.send_quote(PEER,payload(),expected_session='s'))
        message=service.message_observer.call_args.args[0]
        self.assertEqual(message.text,'「引用 朋友：你干啥呢」\n111哈哈哈')
        self.assertEqual(message.data['quote'],payload())
        service.sent_recorder.assert_called_once_with(PEER,message.text,'s',quote=payload(),sequence=message.sequence)
        self.assertLess(message.sequence,0)
        backend.send_text.assert_not_called()

    def test_failure_and_changed_source_do_not_record_a_success_or_send_text(self):
        service,backend=self.service()
        with self.assertRaises(RuntimeError):
            service.send_quote(PEER,payload(),expected_session='old')
        with self.assertRaises(RuntimeError):
            service.send_quote(PEER,payload(),expected_session='s',delivery_check=lambda:False)
        backend.send_quote.assert_not_called()
        backend.send_quote.return_value=False
        self.assertFalse(service.send_quote(PEER,payload(),expected_session='s'))
        service.sent_recorder.assert_not_called();service.message_observer.assert_not_called()


@unittest.skipUnless(sys.platform=='win32','Windows 原生 ABI')
class QuoteNativeValidationTests(unittest.TestCase):
    def test_old_or_incomplete_probe_reports_cannot_claim_quote_success(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'native'))
        from run_selftest import quote_report_succeeded
        report = dict(diagnostic_version=2, operation='quote_probe', message_type=49, app_type=57,
            completion_success=True, built=True, source_found=True, source_complete=True,
            send_called=False, sent=False, stage=97, status=0, exception=0, cleanup_exception=0,
            native_status=0, native_status_high=0, target=PEER)
        self.assertTrue(quote_report_succeeded(report,target=PEER,probe=True))
        for key,value in [('diagnostic_version',1),('source_complete',False),('send_called',True),
                          ('stage',91),('exception',0xc0000005),('cleanup_exception',1),
                          ('native_status_high',1),('target',SELF)]:
            with self.subTest(key=key):
                self.assertFalse(quote_report_succeeded(report|{key:value},target=PEER,probe=True))
        sent = report|dict(operation='quote_send',stage=99,send_called=True,sent=True)
        self.assertTrue(quote_report_succeeded(sent,target=PEER,probe=False))
        self.assertFalse(quote_report_succeeded(report,target=PEER,probe=False))
        self.assertFalse(quote_report_succeeded(sent,target=PEER,probe=True))

    def test_native_preparation_failure_is_explained_and_does_not_leave_an_echo_marker(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'native'))
        from text_service import NativeTextService
        service=NativeTextService(pid=123,account=SELF)
        service.router.set_account(SELF)
        service._owns_bridge=True; service._channel=Mock(); service._check_account=Mock()
        reports = [
            (dict(stage=93,status=1168,send_called=False),'没有查到'),
            (dict(stage=95,status=13,send_called=False),'数据不完整'),
            (dict(stage=92,status=31,send_called=False),'尚未调用发送'),
            (dict(stage=98,status=31,send_called=True),'不要连续重试'),
        ]
        with patch('run_selftest.K.OpenProcess',side_effect=AssertionError('No process access')):
            for report,expected in reports:
                service._run=Mock(return_value={'success':False,'report':dict(diagnostic_version=2)|report})
                with self.subTest(report=report),self.assertRaisesRegex(QuoteReferenceError,expected):
                    service.send_quote(PEER,payload())
                self.assertFalse(service._sent_quotes)

    def test_quote_config_and_unsupported_operations_fail_before_accessing_wechat(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'native'))
        from run_selftest import run,TEXT_DLL,QuoteConfig
        self.assertEqual(ctypes.sizeof(QuoteConfig),18344)
        for changes in ({'quote':None},{'target':'12345678@chatroom'},{'account':'12345678@chatroom'},
                        {'quote_probe':1},{'quote_created_at':-1},{'image_path':'synthetic.png'}):
            args={'bridge_path':TEXT_DLL,'target':PEER,'text':'111哈哈哈','quote':payload(),
                  'quote_probe':True,'account':SELF}|changes
            with self.subTest(changes=changes), patch('run_selftest.K.OpenProcess',side_effect=AssertionError('No process access')):
                with self.assertRaises(ValueError):run('--quote',**args)
        with patch('run_selftest.K.OpenProcess',side_effect=RuntimeError('validated; process access mocked')):
            with self.assertRaisesRegex(RuntimeError,'process access mocked'):
                run('--quote',pid=123,bridge_path=TEXT_DLL,target=PEER,text='111哈哈哈',quote=payload(),
                    quote_probe=True,account=SELF,keep_loaded=True)


if __name__=='__main__':unittest.main()
