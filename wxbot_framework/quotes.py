"""微信 appmsg/type=57 引用回复转为可记录的文字；不发送原生引用卡片。"""
from dataclasses import dataclass
import json
import re
import xml.etree.ElementTree as ET

from .account import validate_account
from .recall import message_id as valid_message_id

MAX_XML = 256 * 1024 - 1


def quote_capture_metadata(value):
    """采样诊断只保留偏移、长度及布尔值；不向网页暴露原始对象或 XML。"""
    if isinstance(value, (bytes, str)):
        if len(value) > 4095:
            return None
        try:
            value = json.loads(value)
        except (ValueError, UnicodeError):
            return None
    if not isinstance(value, dict):
        return None
    def number(item, limit):
        return type(item) is int and 0 <= item <= limit
    if (not number(value.get('vtable_rva'), 0xc34f000) or
            type(value.get('known_layout')) is not bool or
            type(value.get('ambiguous')) is not bool or
            not number(value.get('xml_offset'), 0x738)):
        return None
    fields = value.get('fields')
    if not isinstance(fields, list) or len(fields) > 26:
        return None
    if any(not isinstance(item, dict) or not number(item.get('offset'), 0x738) or
           not number(item.get('length'), MAX_XML) for item in fields):
        return None
    result = {key: value[key] for key in ('vtable_rva', 'known_layout', 'ambiguous', 'xml_offset')} | {
        'fields': [{key: item[key] for key in ('offset', 'length')} for item in fields]}
    if number(value.get('reply_length'), MAX_XML):
        result['reply_length'] = value['reply_length']
    if type(value.get('reference_present')) is bool:
        result['reference_present'] = value['reference_present']
    return result


class QuoteError(ValueError):
    pass


@dataclass(frozen=True)
class QuoteReply:
    reply: str
    author: str
    author_id: str
    original: str
    message_id: str
    scope_valid: bool
    original_type: int = 1

    @property
    def text(self):
        return f"「引用 {self.author}：{self.original}」\n{self.reply}"

    def public(self):
        return normalize_quote({'version': 1, 'reply': self.reply, 'reference': {
            'message_id': self.message_id, 'sender_id': self.author_id,
            'sender_name': self.author, 'type': self.original_type, 'content': self.original}})


def normalize_quote(value):
    """有界引用快照。只含显示/记忆字段，不保存 XML、msgsource 或媒体凭据。"""
    if isinstance(value, str):
        if len(value) > 100000:
            return None
        try:
            value = json.loads(value)
        except (ValueError, UnicodeError):
            return None
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        return None
    ref = value.get('reference')
    if not isinstance(ref, dict):
        return None
    reply, name, content, sender = value.get('reply'), ref.get('sender_name'), ref.get('content'), ref.get('sender_id')
    for item, limit in ((reply, 16000), (name, 64), (content, 1000), (sender, 127)):
        if not isinstance(item, str) or len(item) > limit or any(ord(ch) < 32 and ch not in '\r\n\t' for ch in item):
            return None
    if not reply.strip() or not name or '\n' in name or '\r' in name or type(ref.get('type')) is not int:
        return None
    if sender:
        try:
            validate_account(sender)
        except ValueError:
            return None
    kind = ref['type']
    if not 0 <= kind < 2**31:
        return None
    if kind != 1:
        content = {3: '[图片]', 34: '[语音]', 43: '[视频]', 47: '[表情]', 49: '[文件/分享]'}.get(kind, '[非文字消息]')
    return {'version': 1, 'reply': reply, 'reference': {
        'message_id': valid_message_id(ref.get('message_id')), 'sender_id': sender,
        'sender_name': name, 'type': kind, 'content': content}}


def parse_local_quote_snapshot(content, *, chat_id, self_id, resolve_name):
    """.56 本机私聊文字引用快照；群聊和不相符的作者绝不套用此布局。"""
    if chat_id.endswith('@chatroom') or not isinstance(content, str) or len(content) > 100000:
        return None
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get('reference'), dict):
        return None
    ref = value['reference']
    if ref.get('sender_id') not in (self_id, chat_id) or ref.get('type') != 1 or not isinstance(ref.get('content'), str):
        return None
    ref['content'] = ref['content'][:1000]
    ref['sender_name'] = resolve_name(ref['sender_id']) or '好友'
    result = normalize_quote(value)
    if result is None or not result['reference']['message_id']:
        return None
    ref = result['reference']
    return QuoteReply(result['reply'], ref['sender_name'], ref['sender_id'], ref['content'],
                      ref['message_id'], True, 1)


def quote_context_text(value, limit):
    """上下文截断先让出引用原文的空间，不能把当前回复挤没。"""
    quote = normalize_quote(value)
    if quote is None:
        return None
    ref, reply = quote['reference'], quote['reply']
    prefix, suffix = f"「引用 {ref['sender_name']}：", f"」\n{reply}"
    full = prefix + ref['content'] + suffix
    if len(full) <= limit:
        return full
    available = limit - len(prefix) - len(suffix)
    if available <= 1:
        return reply[:limit]
    return prefix + ref['content'][:available-1] + '…' + suffix


def parse_quote(content, *, chat_id):
    """其他 appmsg 返回 None。引用身份只认 ID，不通过显示名判断机器人。"""
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        size = len(content.encode('utf-8'))
    except UnicodeError:
        raise QuoteError("引用编码无效") from None
    if (len(content) > MAX_XML or size > MAX_XML or '\0' in content or
            re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', content, re.I)):
        raise QuoteError("引用 XML 无效或超过上限")
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        raise QuoteError("引用 XML 无法解析") from None
    if root.tag != 'msg':
        return None
    apps = root.findall('appmsg')
    if len(apps) != 1:
        return None
    app = apps[0]
    if app.findtext('type') != '57':
        return None
    pending, count = [(root, 1)], 0
    while pending:
        node, depth = pending.pop(); count += 1
        if depth > 16 or count > 512:
            raise QuoteError("引用 XML 结构过于复杂")
        pending.extend((child, depth+1) for child in node)

    def text(parent, field, limit, *, multiline=False):
        elements = parent.findall(field)
        if len(elements) > 1 or (elements and len(elements[0])):
            raise QuoteError("引用字段结构不合法")
        value = (elements[0].text or '') if elements else ''
        if len(value) > limit or any(ord(ch) < 32 and (not multiline or ch not in '\r\n\t') for ch in value):
            raise QuoteError("引用字段过长或无效")
        return value.strip()

    refs = app.findall('refermsg')
    if len(refs) != 1:
        raise QuoteError("引用消息缺少唯一 refermsg")
    ref = refs[0]
    reply = text(app, 'title', 16000, multiline=True)
    if not reply:
        raise QuoteError("引用回复正文为空")
    source = text(ref, 'fromusr', 127)
    member = text(ref, 'chatusr', 127)
    scope_valid = True
    if source.endswith('@chatroom'):
        author_id, scope_valid = member, source == chat_id
    else:
        author_id = source
        scope_valid = not member or member in (source, chat_id)
    try:
        validate_account(author_id)
    except ValueError:
        author_id, scope_valid = '', False
    author = text(ref, 'displayname', 64) or '群友'
    kind = text(ref, 'type', 10)
    if kind == '1':
        original = text(ref, 'content', MAX_XML, multiline=True)[:1000] or '[空文字]'
    else:
        # 非文字引用里的 content 可能包含下载凭证/XML，绝不带进模型或日志。
        original = {'3':'[图片]', '34':'[语音]', '43':'[视频]', '47':'[表情]', '49':'[文件/分享]'}.get(kind, '[非文字消息]')
    message_id = text(ref, 'svrid', 24)
    if message_id and (not message_id.isascii() or not message_id.isdecimal()):
        message_id = ''
    original_type = int(kind) if kind.isascii() and kind.isdecimal() and len(kind) <= 9 else 0
    return QuoteReply(reply, author, author_id, original, valid_message_id(message_id), scope_valid, original_type)
