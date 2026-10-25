"""引用接收、ID 触发及文字记忆的离线回归，不发送真实微信消息。"""
from html import escape
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from plugins.agent_chat import Plugin as AgentPlugin
from wxbot_framework.members import MemberStore
from wxbot_framework.quotes import parse_quote, QuoteError
from wxbot_framework.sdk import Message

BOT, PEER, ROOM = 'wxid_quote_bot', 'wxid_quote_peer', '12345678@chatroom'


def quote_xml(reply='真的假的', original='明天会下雨', author=BOT, member=None, kind='1', name='小雪'):
    return ('<msg><appmsg><type>57</type><title>'+escape(reply)+'</title><refermsg>'
            '<type>'+kind+'</type><svrid>1234567890</svrid><fromusr>'+author+'</fromusr>'
            '<chatusr>'+(member if member is not None else author)+'</chatusr><displayname>'+escape(name)+
            '</displayname><content>'+escape(original)+'</content></refermsg></appmsg></msg>')


class QuoteTests(unittest.TestCase):
    def test_quote_keeps_reply_and_original_but_does_not_confuse_the_author(self):
        quote = parse_quote(quote_xml(), chat_id=ROOM)
        self.assertEqual(quote.text, '「引用 小雪：明天会下雨」\n真的假的')
        self.assertEqual((quote.author_id, quote.message_id, quote.scope_valid), (BOT,'1234567890',True))
        quote = parse_quote(quote_xml(author=ROOM, member=BOT), chat_id=ROOM)
        self.assertEqual(quote.author_id, BOT)
        self.assertTrue(quote.scope_valid)
        other = parse_quote(quote_xml(author='99999999@chatroom',member=BOT), chat_id=ROOM)
        self.assertFalse(other.scope_valid)

    def test_non_text_reference_never_exposes_embedded_xml_or_download_keys(self):
        quote = parse_quote(quote_xml(kind='49',original='<appmsg><aeskey>synthetic-secret</aeskey></appmsg>'),chat_id=ROOM)
        self.assertEqual(quote.original,'[文件/分享]')
        self.assertNotIn('synthetic-secret',quote.text)

    def test_invalid_xml_dtd_duplicate_fields_and_nul_fail_closed(self):
        for xml in ('<msg>', quote_xml()+'\0', '<!DOCTYPE x [<!ENTITY a "test">]>'+quote_xml(),
                    quote_xml().replace('<title>','<title>duplicate</title><title>')):
            with self.subTest(xml=xml[:30]), self.assertRaises(QuoteError):
                parse_quote(xml,chat_id=ROOM)
        self.assertIsNone(parse_quote('<msg><appmsg><type>6</type></appmsg></msg>',chat_id=ROOM))

    def test_reference_content_is_bounded_without_discarding_current_reply(self):
        quote = parse_quote(quote_xml(original='长'*20000),chat_id=ROOM)
        self.assertEqual(len(quote.original),1000)
        self.assertEqual(quote.reply,'真的假的')

    def test_quote_is_stored_as_text_and_included_in_context(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'members.json'
            path.write_text(json.dumps({'history_enabled':True,'database_file':'data.sqlite3','groups':{ROOM:[]}}),encoding='utf-8')
            store = MemberStore(path,BOT)
            try:
                quote = parse_quote(quote_xml(),chat_id=ROOM)
                message = Message(quote.text,ROOM,PEER,is_group=True,session_id='quote',sequence=1,
                    data={'quote_is_self':True,'quote_reply':quote.reply})
                self.assertTrue(store.record(message))
                self.assertEqual(store.conversation_snapshot(message).current,quote.text)
                self.assertEqual(store.memory(ROOM)[1][0]['text'],quote.text)
            finally:
                store.close()

    def test_only_robot_quotes_trigger_and_old_quoted_wake_words_do_not(self):
        plugin = AgentPlugin()
        plugin.members = SimpleNamespace(account=BOT,replies=lambda chat:chat==ROOM)
        plugin.wake_words = ('小雪',)
        quoted = Message('「引用 小雪：你好」\n真的假的',ROOM,PEER,is_group=True,
            data={'quote_is_self':True,'quote_reply':'真的假的'})
        self.assertEqual(plugin._trigger(quoted),'引用你的话')
        not_robot = Message(quoted.text,ROOM,PEER,is_group=True,data={'quote_is_self':False,'quote_reply':'真的假的'})
        self.assertIsNone(plugin._trigger(not_robot))
        plugin.members.replies=lambda chat:False
        self.assertIsNone(plugin._trigger(quoted))


@unittest.skipUnless(sys.platform=='win32','Windows 接收映射模拟')
class NativeQuoteTests(unittest.TestCase):
    def service(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'native'))
        from text_service import NativeTextService
        service = NativeTextService(pid=123,account=BOT)
        service.router.set_account(BOT)
        service._sender_wechat_name=Mock(return_value='群友甲')
        service._file_receiver=Mock()
        service.monitor_handler=Mock()
        return service

    def test_group_quote_does_not_go_to_file_receiver_and_is_a_normal_text_event(self):
        service=self.service()
        from filehelper_service import ReceivedMediaMessageV5
        raw=ReceivedMediaMessageV5(type=49,sequence=9,sender=ROOM.encode(),receiver=BOT.encode(),
            image_xml=(PEER+':\n'+quote_xml(author=ROOM,member=BOT)).encode())
        data=service._map_message(raw)
        service._file_receiver.submit.assert_not_called()
        service.monitor_handler.assert_not_called()
        message=Message.from_native(11046,data,'quote-test')
        self.assertEqual((message.type,message.sender_id,message.chat_id),(1,PEER,ROOM))
        self.assertTrue(message.data['quote_is_self'])
        self.assertIn('真的假的',message.text)
        self.assertNotIn('<refermsg>',message.text)

    def test_same_display_name_without_the_robot_id_is_not_a_robot_reference(self):
        service=self.service()
        from filehelper_service import ReceivedMediaMessageV5
        raw=ReceivedMediaMessageV5(type=49,sequence=10,sender=ROOM.encode(),receiver=BOT.encode(),
            image_xml=(PEER+':\n'+quote_xml(author=PEER,name='小雪')).encode())
        self.assertFalse(service._map_message(raw)['quote_is_self'])

    def test_failed_send_does_not_claim_a_later_manual_echo(self):
        service=self.service()
        service._owns_bridge=True
        service._check_account=Mock()
        service._run=Mock(return_value={'success':False})
        self.assertFalse(service.send_text(ROOM,'模拟失败'))
        self.assertFalse(service._sent_texts)
        service._run.side_effect=RuntimeError('模拟异常')
        with self.assertRaises(RuntimeError):
            service.send_text(ROOM,'模拟异常')
        self.assertFalse(service._sent_texts)


if __name__=='__main__':unittest.main()
