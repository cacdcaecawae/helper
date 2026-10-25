"""长文字和引用双通道回传回归；只操作合成队列及临时数据库。"""
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from xml.sax.saxutils import escape

from test_review_fixes import StoreFixture, SELF, PEER, ROOM
from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.sdk import Message, ServiceFacade


@unittest.skipUnless(sys.platform == 'win32', 'Windows native event structures')
class ReceiveConsistencyTests(StoreFixture):
    def setUp(self):
        super().setUp()
        native = str(Path(__file__).resolve().parents[1]/'native')
        sys.path.insert(0, native)
        self.addCleanup(lambda:sys.path.remove(native))
        from filehelper_service import ReceivedMediaMessageV5
        from text_service import NativeTextService
        self.Raw = ReceivedMediaMessageV5
        self.service = NativeTextService(pid=123, account=SELF)
        self.service.router.set_account(SELF)
        self.service._read_account = Mock(return_value=SELF)
        self.service._sender_wechat_name = Mock(return_value='本机称呼')
        self.service._chat_name = Mock(return_value='Synthetic')
        self.buffer = MessageBuffer()
        self.sequence = 0

    def deliver(self, raw):
        self.sequence += 1
        raw.sequence = self.sequence
        data = self.service._map_message(raw)
        if data is not None:
            message = Message.from_native(11046, data, 'test')
            self.buffer.append(message, account=SELF)
            self.store.record(message)
        return data

    def reset(self):
        for chat in (PEER, ROOM):
            self.store.forget(chat)
        self.buffer.reset()
        self.service._own_text_echoes.clear()
        self.service._sent_quotes.clear()

    def payload(self, reference='42', reply='真的假的', name='本机称呼'):
        return {'version':1, 'reply':reply, 'reference':{'message_id':reference,
                'sender_id':PEER, 'sender_name':name, 'content':'原文', 'type':1}}

    def quote(self, local, identifier=0, *, reference='42', reply='真的假的', chat=PEER,
              name='服务器旧称呼', snapshot=True):
        if local and snapshot and not chat.endswith('@chatroom'):
            body = json.dumps(self.payload(reference, reply), ensure_ascii=False)
            flags = 104
        else:
            body = ('<msg><appmsg><type>57</type><title>'+escape(reply)+'</title><refermsg><type>1</type>'
                    '<svrid>'+reference+'</svrid><fromusr>'+PEER+'</fromusr><chatusr>'+PEER+'</chatusr>'
                    '<displayname>'+name+'</displayname><content>原文</content></refermsg></appmsg></msg>')
            flags = 40 if local else 0
        group = chat.endswith('@chatroom')
        if group and not local:
            body = SELF+':\n'+body
        return self.deliver(self.Raw(type=49, reserved=flags, native_message_id=identifier,
            sender=(b'self' if local else chat.encode() if group else SELF.encode()),
            receiver=(chat if local or not group else SELF).encode(), image_xml=body.encode()))

    def test_received_long_private_and_group_text_is_saved_whole_and_dispatched(self):
        for chat in (PEER, ROOM):
            for body in ('汉'*342, '🙂汉字\n'*3000, 'x'*65536):
                with self.subTest(chat=chat, size=len(body)):
                    group = chat == ROOM
                    native_body = PEER+':\n'+body if group else body
                    data = self.deliver(self.Raw(type=1, reserved=16, sender=chat.encode(), receiver=SELF.encode(),
                        image_xml=native_body.encode(), native_message_id=self.sequence+100))
                    self.assertEqual(data['msg'], body)
                    self.assertEqual(data['sender_id'], PEER)
                    self.assertTrue(data['dispatch_to_plugins'])
                    self.assertFalse(data.get('local_send', False))
                    self.assertEqual(self.store.chat_history(chat)[-1]['text'], body)
                    self.assertTrue(self.store.memory(chat)[1][-1]['text'])

    def test_incoming_large_text_keeps_mentions_and_small_text_path_is_unchanged(self):
        body = '@机器人 '+ '汉'*400
        data = self.deliver(self.Raw(type=1, reserved=16, sender=ROOM.encode(), receiver=SELF.encode(),
            image_xml=(PEER+':\n'+body).encode(), mention_source=('<msgsource><atuserlist>'+SELF+'</atuserlist></msgsource>').encode()))
        self.assertTrue(data['is_at_self'])
        self.assertEqual(data['at_members'], (SELF,))
        data = self.deliver(self.Raw(type=1, sender=PEER.encode(), receiver=SELF.encode(), text=b'short'))
        self.assertEqual(data['msg'], 'short')

    def test_long_local_and_sync_self_echo_use_the_same_body_for_deduplication(self):
        body = '🙂汉字'*1000
        self.deliver(self.Raw(type=1, reserved=24, sender=b'self', receiver=ROOM.encode(), image_xml=body.encode()))
        update = self.deliver(self.Raw(type=1, reserved=16, sender=ROOM.encode(), receiver=SELF.encode(),
            image_xml=(SELF+':\n'+body).encode(), native_message_id=900))
        self.assertTrue(update['echo_update'])
        self.assertEqual(len(self.store.chat_history(ROOM)), 1)
        self.assertEqual(self.store.chat_history(ROOM)[0]['message_id'], '900')

    def test_manual_quotes_merge_in_both_orders_and_private_and_group_chats(self):
        for chat in (PEER, ROOM):
            for sync_first in (False, True):
                with self.subTest(chat=chat, sync_first=sync_first):
                    self.reset()
                    first = self.quote(not sync_first, 900 if sync_first else 0, chat=chat, name='先到称呼')
                    second = self.quote(sync_first, 0 if sync_first else 900, chat=chat, name='后到称呼')
                    if sync_first:
                        self.assertIsNone(second)
                    else:
                        self.assertTrue(second['echo_update'])
                        self.assertFalse(second['dispatch_to_plugins'])
                    rows = self.store.chat_history(chat)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(len(self.store.memory(chat)[1]), 1)
                    self.assertEqual(rows[0]['text'], first['msg'])
                    self.assertEqual(rows[0]['message_id'], '900')
                    self.assertEqual(len(self.buffer.rows), 1)
                    self.assertEqual(self.buffer.rows[0]['message_id'], '900')
                    self.assertNotIn('raw_content', first)

    def test_repeated_same_quote_is_paired_by_occurrence_not_collapsed(self):
        for sync_first in (False, True):
            self.reset()
            events = [(True,0),(True,0),(False,900),(False,901)]
            if sync_first:
                events = events[2:]+events[:2]
            for local, identifier in events:
                self.quote(local, identifier)
            self.assertEqual([row['message_id'] for row in self.store.chat_history(PEER)], ['900','901'])
            self.assertEqual(len(self.store.memory(PEER)[1]), 2)
            self.assertEqual(len(self.buffer.rows), 2)

    def test_equal_reply_to_different_references_is_not_merged(self):
        self.quote(True, reference='42')
        other = self.quote(False, 901, reference='43')
        self.assertFalse(other.get('echo_update', False))
        self.quote(False, 900, reference='42')
        self.quote(True, reference='43')
        rows = self.store.chat_history(PEER)
        self.assertEqual([r['quote']['reference']['message_id'] for r in rows], ['42','43'])
        self.assertEqual([r['message_id'] for r in rows], ['900','901'])

    def test_plain_literal_quote_text_and_actual_quote_do_not_collide(self):
        data = self.quote(True)
        self.deliver(self.Raw(type=1, sender=SELF.encode(), receiver=PEER.encode(), text=data['msg'].encode(), native_message_id=901))
        self.quote(False, 900)
        self.assertEqual(len(self.store.chat_history(PEER)), 2)
        self.assertEqual(len(self.buffer.rows), 2)

    def test_missing_reference_id_does_not_guess_a_match(self):
        self.quote(True, reference='', snapshot=False)
        self.quote(False, 900, reference='')
        self.assertEqual(len(self.store.chat_history(PEER)), 2)

    def test_known_distinct_outgoing_ids_are_kept_even_for_the_same_quote(self):
        self.quote(True, 900)
        self.quote(False, 901)
        self.assertEqual(len(self.store.chat_history(PEER)), 2)

    def test_clearing_pending_quote_does_not_restore_it_from_sync(self):
        self.quote(True)
        self.store.forget(PEER)
        self.buffer.reset()
        self.quote(False, 900)
        self.assertEqual(self.store.chat_history(PEER), [])
        self.assertEqual(list(self.buffer.rows), [])

    def test_program_quote_three_events_are_one_record_and_keep_bot_classification(self):
        for sync_first in (False, True):
            for notification_first in (False, True):
                for reply in ('真的假的', ' 真的假的 '):
                    with self.subTest(sync_first=sync_first, notification_first=notification_first, reply=reply):
                        self.reset()
                        facade = ServiceFacade()
                        facade.message_observer = lambda msg:self.buffer.append(msg, account=SELF)
                        facade.sent_recorder = self.store.record_own
                        payload = self.payload(reply=reply)
                        def notification():
                            facade._sent(True, SimpleNamespace(session_id='test'), PEER, 1,
                                text='「引用 本机称呼：原文」\n'+reply, quote=payload)
                        self.service._sent_quotes.append((PEER, reply, '42', time.monotonic()))
                        if notification_first:
                            notification()
                        first = self.quote(not sync_first, 900 if sync_first else 0, reply=reply)
                        second = self.quote(sync_first, 0 if sync_first else 900, reply=reply)
                        self.assertTrue(first['bot_generated'])
                        if second is not None:
                            self.assertTrue(second['bot_generated'])
                            self.assertFalse(second['dispatch_to_plugins'])
                        if not notification_first:
                            notification()
                        self.assertEqual(len(self.store.chat_history(PEER)), 1)
                        self.assertEqual(len(self.store.memory(PEER)[1]), 1)
                        self.assertEqual(len(self.buffer.rows), 1)
                        self.assertEqual(self.buffer.rows[0]['message_id'], '900')
                        self.assertFalse(self.service._sent_quotes)


if __name__ == '__main__':
    unittest.main()
