"""旧消息 ID 只读匹配：模拟字节内存，无进程、数据库或发送接口。"""
import struct
import unittest
from wxbot_framework.quote_references import resolve_cached_quote,QuoteReferenceError

SELF,PEER='wxid_ref_self','wxid_ref_peer'
ID=3918779855605843848


class Memory:
    base=0x100000
    address=0x200000
    def __init__(self):self.data=bytearray(0x3000)
    def read(self,address,size):
        offset=address-self.address
        return bytes(self.data[offset:offset+size]) if 0<=offset<=len(self.data)-size else b''
    def regions(self):return [(self.address,len(self.data))]
    def message(self,offset=0x100,identifier=ID,body='你干啥呢',peer=PEER,sender=PEER,stamp=1792887443000):
        struct.pack_into('<QQ',self.data,offset,self.base+0x8f704b8,1)
        struct.pack_into('<Q',self.data,offset+0xb8,identifier)
        struct.pack_into('<Q',self.data,offset+0xc8,stamp)
        for field,value in ((0x50,sender),(0x70,peer),(0xe0,body)):
            raw=value.encode();assert len(raw)<=15
            self.data[offset+field:offset+field+len(raw)+1]=raw+b'\0'
            struct.pack_into('<QQ',self.data,offset+field+16,len(raw),15)


def source():
    return dict(account=SELF,chat_id=PEER,sender_id=PEER,content='你干啥呢',timestamp=1792887444.124)


class QuoteReferenceTests(unittest.TestCase):
    def test_one_message_in_several_cache_copies_returns_exact_uint64_string(self):
        memory=Memory();memory.message();memory.message(offset=0x800)
        self.assertEqual(resolve_cached_quote(memory,source(),SELF),{'message_id':str(ID),'created_at':1792887443})

    def test_same_words_two_messages_are_ambiguous_not_nearest_match(self):
        memory=Memory();memory.message();memory.message(offset=0x800,identifier=ID+1,stamp=1792887444000)
        with self.assertRaises(QuoteReferenceError):resolve_cached_quote(memory,source(),SELF)

    def test_different_author_chat_text_time_and_account_never_match(self):
        for changes in ({'sender':'wxid_another'},{'peer':'wxid_another'},{'body':'不同原话'},
                        {'stamp':1792887400000}):
            memory=Memory();memory.message(**changes)
            with self.subTest(changes=changes),self.assertRaises(QuoteReferenceError):
                resolve_cached_quote(memory,source(),SELF)
        memory=Memory();memory.message()
        with self.assertRaises(QuoteReferenceError):resolve_cached_quote(memory,source()|{'account':'wxid_other'},SELF)

    def test_timeout_or_account_change_does_not_return_a_partial_match(self):
        memory=Memory();memory.message();times=iter([0,0,4])
        with self.assertRaises(QuoteReferenceError):resolve_cached_quote(memory,source(),SELF,clock=lambda:next(times))
        def changed():raise QuoteReferenceError('account changed')
        with self.assertRaises(QuoteReferenceError):resolve_cached_quote(memory,source(),SELF,check=changed)


if __name__=='__main__':unittest.main()
