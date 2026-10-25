"""本次连接内的消息身份；不把正文相同当作同一条消息。"""
from collections import deque
import hashlib
import json
import time


def quote_token(namespace, generation, *identity):
    """浏览器引用快照身份：清空/重启后失效，补写服务器 ID 不影响它。"""
    value = json.dumps([namespace, generation, *identity], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def event_key(account, chat, session, sequence):
    """供历史库与实时窗口对齐；0 是旧版无身份的发送通知。"""
    if not account or not session or not sequence:
        return ''
    value = json.dumps([account, chat, session, sequence], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class OwnTextEchoes:
    """合并普通文字/已解析引用的本机监听与同步回传，不合并同通道重复发送。

    本机事件没有服务器 ID，只能在短窗口内按账号、会话、正文/引用身份、相反来源
    一对一关联；两个已知且不同的服务器 ID 永远保留。未匹配的消息照常投递。
    由单个接收线程调用，pending 中保留首条 data 以继承稍后设置的机器人标记。
    """
    def __init__(self, *, clock=time.monotonic, window=10, capacity=256):
        self.clock, self.window = clock, window
        self.pending = deque(maxlen=capacity)
        self.ids = deque(maxlen=capacity)

    def clear(self):
        self.pending.clear()
        self.ids.clear()

    def accept(self, data, *, local):
        now = self.clock()
        while self.pending and now - self.pending[0][0] >= self.window:
            self.pending.popleft()
        quote = data.get('quote')
        if isinstance(quote, dict):
            reference_id = quote['reference']['message_id']
            # 引用只认原消息 ID + 回复正文，绝不按昵称或展示文本猜测身份。
            payload = ('quote', reference_id, quote['reply'].strip()) if reference_id else None
        elif data.get('native_type') == 49:
            payload = None
        else:
            payload = ('text', data['msg'])
        scope = (data.get('self_wxid'), data['chat_id'], data['sender_id'], payload)
        identifier = data.get('native_message_id') or ''
        if identifier == '0':
            identifier = ''
        identity = (*scope[:3], identifier)
        if identifier and identity in self.ids:
            # 同 ID 的另一通道也消耗对应的待配对事件，不能留给之后的相同文字。
            match = next((item for item in self.pending if item[1] != local and item[2] == scope
                          and item[3].get('native_message_id') == identifier), None)
            if match is not None:
                self.pending.remove(match)
            return None
        match = next((item for item in self.pending if payload is not None and item[1] != local and item[2] == scope
                      and (not identifier or item[3].get('native_message_id') in ('', '0', None, identifier))), None)
        if identifier:
            self.ids.append(identity)
        if match is None:
            if payload is not None:
                self.pending.append((now, local, scope, data))
            return data
        self.pending.remove(match)
        first = match[3]
        if not identifier or first.get('native_message_id') not in ('', '0', None):
            return None
        # 不再次派发消息，只补上首条记录的真实服务器 ID（撤回/引用会用到）。
        data.update(sequence=first['sequence'], echo_update=True, dispatch_to_plugins=False)
        # 两通道的引用昵称/原文展示可能不同。保留首次快照，才能准确补写同一条库记录的 ID。
        for field in ('msg', 'content', 'quote', 'quote_reply', 'quote_is_self', 'quoted_sender_id', 'quoted_message_id'):
            if field in first:
                data[field] = first[field]
        for flag in ('bot_generated', 'own_text_recorded', 'local_send', 'outgoing_sequence'):
            if flag in first:
                data[flag] = first[flag]
        return data
