"""本机手动文字使用统一收消息管道；只用模拟队列，不发送微信消息。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

SELF, ROOM = 'wxid_local_self', '12345678@chatroom'


@unittest.skipUnless(sys.platform=='win32','Windows 原生消息结构')
class ManualTextTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'native'))
        from text_service import NativeTextService
        from filehelper_service import ReceivedMediaMessageV5
        self.Raw=ReceivedMediaMessageV5
        self.service=NativeTextService(pid=123,account=SELF)
        self.service.router.set_account(SELF)
        self.service._read_account=Mock(return_value=SELF)
        self.service._sender_wechat_name=Mock(return_value='自己')

    def local(self,text='我自己手动发的',identifier=42,large=False):
        return self.Raw(type=1,reserved=24 if large else 8,sequence=identifier,sender=b'self',receiver=ROOM.encode(),
            text=b'' if large else text.encode(),image_xml=text.encode() if large else b'',native_message_id=identifier)

    def test_local_send_has_the_real_self_identity_and_does_not_trigger_an_ordinary_reply(self):
        data=self.service._map_message(self.local())
        self.assertEqual((data['chat_id'],data['sender_id'],data['msg']),(ROOM,SELF,'我自己手动发的'))
        self.assertTrue(data['is_self'])
        self.assertTrue(data['local_send'])
        self.assertFalse(data['dispatch_to_plugins'])
        self.assertFalse(data.get('bot_generated',False))

    def test_local_and_sync_with_same_id_are_not_emitted_twice_but_repeated_words_are_kept(self):
        self.assertIsNotNone(self.service._map_message(self.local()))
        sync=self.Raw(type=1,sequence=88,sender=ROOM.encode(),receiver=SELF.encode(),
            text=(SELF+':\n我自己手动发的').encode(),native_message_id=42)
        self.assertIsNone(self.service._map_message(sync))
        self.assertIsNotNone(self.service._map_message(self.local(identifier=43)))
        sync.native_message_id=44
        self.assertIsNotNone(self.service._map_message(sync))

    def test_long_manual_text_uses_v5_large_buffer_without_truncating_a_utf8_character(self):
        body='长文字🌸'*1000
        self.assertEqual(self.service._map_message(self.local(body,large=True))['msg'],body)

    def test_account_switch_rejects_a_pending_local_send_instead_of_using_the_old_account(self):
        self.service._read_account.return_value='wxid_another_account'
        with self.assertRaises(RuntimeError):
            self.service._map_message(self.local())
        self.assertTrue(self.service._stop.is_set())

    def test_program_send_marker_still_distinguishes_a_bot_reply_from_a_manual_message(self):
        import time
        self.service._sent_texts.append((ROOM,'我自己手动发的',time.monotonic()))
        data=self.service._map_message(self.local())
        self.assertTrue(data['bot_generated'])
        self.assertTrue(data['own_text_recorded'])
        data=self.service._map_message(self.local(identifier=43))
        self.assertFalse(data.get('bot_generated',False))

    def quote(self, xml=True):
        import json
        layout = dict(vtable_rva=0x9026cd8, known_layout=True, ambiguous=False,
                      xml_offset=0x38 if xml else 0, fields=[dict(offset=0x38, length=123)])
        body = ('<msg><appmsg><type>57</type><title>111</title><refermsg><type>1</type>'
                '<svrid>1234567890123456789</svrid><fromusr>wxid_peer</fromusr>'
                '<displayname>朋友</displayname><content>你干啥呢</content></refermsg></appmsg></msg>')
        return self.Raw(type=49,reserved=40,sequence=100,sender=b'self',receiver=ROOM.encode(),
                        image_xml=body.encode() if xml else b'',mention_source=json.dumps(layout).encode())

    def test_local_native_quote_is_observed_as_text_without_triggering_or_downloading(self):
        self.service._file_receiver=Mock()
        data=self.service._map_message(self.quote())
        self.assertEqual(data['msg'],'「引用 朋友：你干啥呢」\n111')
        self.assertEqual(data['wx_type'],1)
        self.assertEqual(data['sender_id'],SELF)
        self.assertEqual(data['quoted_message_id'],'1234567890123456789')
        self.assertEqual(data['quote_capture']['xml_offset'],0x38)
        self.assertFalse(data['dispatch_to_plugins'])
        self.service._file_receiver.submit.assert_not_called()
        from wxbot_framework.message_monitor import MessageBuffer
        from wxbot_framework.sdk import Message
        buffer=MessageBuffer()
        buffer.append(Message.from_native(11046,data,'test'))
        row=buffer.tail()['rows'][0]
        self.assertEqual(row['direction'],'outgoing')
        self.assertEqual(row['quote_capture']['xml_offset'],0x38)
        self.assertNotIn('<appmsg>',str(row))

    def test_missing_quote_xml_is_monitor_only_and_never_downloaded_or_saved_as_raw_xml(self):
        self.service._file_receiver=Mock()
        raw=self.quote(xml=False)
        for body in (b'',b'<msg>broken',b'<msg><appmsg><type>6</type></appmsg></msg>'):
            raw.image_xml=body
            data=self.service._map_message(raw)
            self.assertTrue(data['monitor_only'])
            self.assertFalse(data['dispatch_to_plugins'])
            self.assertNotIn('raw_content',data)
            self.assertEqual(data['msg'],'[本机引用：结构待确认]')
        self.service._file_receiver.submit.assert_not_called()

    def test_capture_diagnostics_discard_extra_strings_and_invalid_shapes(self):
        from wxbot_framework.quotes import quote_capture_metadata
        metadata=dict(vtable_rva=123,known_layout=False,ambiguous=False,xml_offset=0,
                      fields=[dict(offset=0x38,length=20,secret='not exposed')],secret='not exposed')
        result=quote_capture_metadata(metadata)
        self.assertNotIn('not exposed',str(result))
        metadata['fields']=[dict(offset='secret',length=20)]
        self.assertIsNone(quote_capture_metadata(metadata))
        self.assertIsNone(quote_capture_metadata(b'\xff'))
        self.assertIsNone(quote_capture_metadata(b'x'*4096))

    def test_private_native_quote_snapshot_preserves_structure_and_rejects_other_authors(self):
        import json
        from wxbot_framework.quotes import parse_local_quote_snapshot
        peer = 'wxid_private_peer'
        value = {'version':1,'reply':'111哈哈哈','reference':{'message_id':'3918779855605843848',
                 'sender_id':peer,'sender_name':'ignored','type':1,'content':'你干啥呢'}}
        raw = self.quote(xml=False)
        raw.receiver=peer.encode(); raw.reserved=104; raw.image_xml=json.dumps(value).encode()
        self.service._sender_wechat_name=Mock(return_value='朋友')
        data=self.service._map_message(raw)
        self.assertEqual(data['msg'],'「引用 朋友：你干啥呢」\n111哈哈哈')
        self.assertEqual(data['quote']['reply'],'111哈哈哈')
        self.assertFalse(data.get('monitor_only',False))
        self.assertFalse(data['dispatch_to_plugins'])
        value['reference']['sender_id']='wxid_another_chat'
        self.assertIsNone(parse_local_quote_snapshot(json.dumps(value),chat_id=peer,self_id=SELF,resolve_name=lambda _: ''))
        self.assertIsNone(parse_local_quote_snapshot(json.dumps(value),chat_id=ROOM,self_id=SELF,resolve_name=lambda _: ''))
