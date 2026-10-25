"""只读核对旧私聊文字的原消息 ID；不读微信数据库、不调用发送入口。"""
import math
import struct
import time

from .account import validate_account
from .recall import message_id


class QuoteReferenceError(ValueError):
    pass


def resolve_cached_quote(process, source, account, *, check=lambda: None, clock=time.monotonic):
    """调用者须先核对 .56 完整 DLL 指纹及账号。多条匹配不挑最近一条。"""
    chat, sender, text, stamp = (source.get(k) for k in ('chat_id', 'sender_id', 'content', 'timestamp'))
    validate_account(account); validate_account(chat); validate_account(sender)
    if (source.get('account', account) != account or sender not in (account, chat) or
        not isinstance(text, str) or not text or len(text.encode('utf-8')) >= 16384 or
        type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0):
        raise QuoteReferenceError('原消息不属于当前私聊或缺少有效时间')
    needle = struct.pack('<Q', process.base + 0x8f704b8)
    started, scanned, candidates, seen = clock(), 0, set(), set()

    def bounded():
        check()
        if clock() - started > 3 or scanned > 512 * 1024 * 1024:
            raise QuoteReferenceError('微信缓存核对超时，未发送引用；可先在微信打开该会话后重试')

    def string(address, limit):
        descriptor = process.read(address, 32)
        if len(descriptor) != 32:
            return None
        length, capacity = struct.unpack_from('<QQ', descriptor, 16)
        if not 0 < length <= limit or length > capacity or not (capacity == 15 or 16 <= capacity <= 1024*1024):
            return None
        pointer = struct.unpack_from('<Q', descriptor)[0] if capacity > 15 else address
        raw = process.read(pointer, length + 1)
        if len(raw) != length + 1 or raw[-1:] != b'\0' or b'\0' in raw[:-1]:
            return None
        try:
            return raw[:-1].decode('utf-8')
        except UnicodeError:
            return None

    bounded()
    for region, size in process.regions():
        for offset in range(0, size, 2*1024*1024 - 8):
            bounded()
            data = process.read(region + offset, min(size - offset, 2*1024*1024))
            scanned += len(data)
            position = data.find(needle)
            while position >= 0:
                address = region + offset + position
                position = data.find(needle, position + 8)
                if address in seen:
                    continue
                seen.add(address)
                if process.read(address + 8, 8) != struct.pack('<Q', 1):
                    continue
                identity, created = process.read(address + 0xb8, 8), process.read(address + 0xc8, 8)
                if len(identity) != 8 or len(created) != 8:
                    continue
                identifier, milliseconds = struct.unpack('<Q', identity)[0], struct.unpack('<Q', created)[0]
                if not identifier or abs(milliseconds / 1000 - stamp) > 10:
                    continue
                if (string(address + 0x70, 127) != chat or string(address + 0x50, 127) != sender or
                        string(address + 0xe0, 16383) != text):
                    continue
                # 前后再次核对对象身份和数字字段；不保留借用的原生指针。
                if (process.read(address, 8) != needle or process.read(address + 8, 8) != struct.pack('<Q', 1) or
                        process.read(address + 0xb8, 8) != identity or process.read(address + 0xc8, 8) != created):
                    continue
                candidates.add((str(identifier), milliseconds // 1000))
    bounded()
    if len(candidates) != 1:
        raise QuoteReferenceError('旧消息的微信原消息 ID 未唯一确认，未发送引用；可先在微信打开原消息所在会话后重试')
    identifier, created_at = candidates.pop()
    if not message_id(identifier) or created_at >= 2**32:
        raise QuoteReferenceError('原消息编号或时间无效，未发送引用')
    return {'message_id': identifier, 'created_at': created_at}
