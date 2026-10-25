"""好友、群聊和文件传输助手的原生文本、图片、文件及语音服务。"""
from __future__ import annotations

import logging
import time
import uuid
import ctypes as c
from collections import deque
from pathlib import Path

from filehelper_service import NativeFileHelperService, ReceivedMediaMessageV5
from wxbot_framework.mentions import parse_at_members
from run_selftest import TEXT_DLL, K, check, modules, wechat_candidates, run
from wxbot_framework.text import TextRouter, validate_target, validate_mentions
from wxbot_framework.account import read_current_account, read_current_profile, select_account_process, validate_account
from wxbot_framework.contacts import read_contact_name
from wxbot_framework.message_identity import OwnTextEchoes

logger = logging.getLogger("NativeText")


def run_text(mode, **options):
    return run(mode, bridge_path=TEXT_DLL, **options)


def _inspect_identity(pid, reader):
    core = modules(pid).get("weixin.dll")
    if not core:
        return None
    process = check(K.OpenProcess(0x410, False, pid), "只读核对微信账号")
    try:
        def read(address, size):
            buffer = c.create_string_buffer(size)
            count = c.c_size_t()
            check(K.ReadProcessMemory(process, address, buffer, size, c.byref(count)), "只读核对账号字段")
            return buffer.raw[:count.value]
        return reader(read, core[0])
    finally:
        K.CloseHandle(process)


def inspect_account(pid):
    """只读核对候选进程的当前账号，不向候选进程注入 DLL。"""
    return _inspect_identity(pid, read_current_account)


def inspect_profile(pid):
    """首次绑定只读取 wxid 和微信名，名称暂不可读时仍可使用 wxid。"""
    def read_profile(read, base):
        profile = read_current_profile(read, base)
        if profile:
            return profile
        account = read_current_account(read, base)
        return {"wxid": account, "name": ""} if account else None
    return _inspect_identity(pid, read_profile)


def find_account_process(account=None):
    candidates = []
    for pid in wechat_candidates():
        try:
            candidates.append((pid, inspect_account(pid)))
        except OSError:
            # 枚举后退出的进程不参与选择。
            pass
    return select_account_process(candidates, account)


class NativeTextService(NativeFileHelperService):
    _dll = TEXT_DLL
    _poll_export = b"WxPollMediaMessageV5"
    _message_type = ReceivedMediaMessageV5
    _run = staticmethod(run_text)

    def __init__(self, *, account=None, media_directory=None, image_allowlist=(),
                 wechat_data_directory=None, emoticon_directory=None, voice_directory=None,
                 file_directory=None, **options):
        super().__init__(**options)
        self.preferred_account = validate_account(account) if account is not None else None
        self.router = TextRouter(include_self=True)
        self.chats = {}
        self._next_account_check = 0
        self._sent_texts = deque(maxlen=64)
        self._own_text_echoes = OwnTextEchoes()
        self._sent_quotes = deque(maxlen=64)
        self._outgoing_sequence = 0
        self.mention_messages = 0
        self.media_directory = Path(media_directory or Path(__file__).resolve().parent.parent / ".runtime/images")
        self.image_allowlist = frozenset(image_allowlist)
        self.media_chat_allowed = None  # 运行时注入，与文字共用实时白名单/会话开关
        self.media_context_provider = None
        self._image_receiver = None
        self._file_receiver = None
        self._forwarded_receiver = None
        self.file_directory = Path(file_directory or
                                   Path(__file__).resolve().parent.parent / ".runtime/files")
        self.wechat_data_directory = Path(wechat_data_directory) if wechat_data_directory is not None else None
        self.emoticon_directory = Path(emoticon_directory or
                                       Path(__file__).resolve().parent.parent / ".runtime/emoticons")
        self.voice_directory = Path(voice_directory or
                                    Path(__file__).resolve().parent.parent / ".runtime/voices")
        self.last_send_result = None
        self._friend_requests = None
        self._friend_session_id = ""
        self._contact_names = None

    @property
    def self_wxid(self):
        return self.router.self_wxid

    def _media_allowed(self, chat):
        if self.media_chat_allowed is None:
            return not self.image_allowlist or chat in self.image_allowlist
        try:
            return bool(self.media_chat_allowed(chat))
        except Exception:
            logger.warning('媒体会话配置暂不可读，跳过下载')
            return False

    def start(self):
        self.router = TextRouter(include_self=True)
        self.chats = {}
        self._sent_texts.clear()
        self._own_text_echoes.clear()
        self._sent_quotes.clear()
        if self.pid is None:
            self.pid = find_account_process(self.preferred_account)
        elif self.preferred_account and inspect_account(self.pid) != self.preferred_account:
            # 手动指定 PID 时也先只读核对，避免向其他账号加载 DLL。
            raise RuntimeError("目标进程登录的不是绑定的机器人账号，等待正确账号登录")
        result = super().start()
        logger.info("当前登录微信ID=%s；好友和群聊文本、图片、文件已就绪", self.self_wxid)
        return result

    def _read_account(self):
        return read_current_account(self._channel.read_memory, self._channel.core_base)

    def _prepare_channel(self):
        account = self._read_account()
        if not account:
            raise RuntimeError("微信尚未完成登录，自动等待当前账号就绪")
        if self.preferred_account and account != self.preferred_account:
            raise RuntimeError("目标进程登录的不是绑定的机器人账号，等待正确账号登录")
        self.router.set_account(account)
        self._start_contact_names(account)
        from wxbot_framework.friend_requests import PendingFriendRequests
        self._friend_session_id = uuid.uuid4().hex
        self._friend_requests = PendingFriendRequests(account, self._friend_session_id)
        self._next_account_check = time.monotonic() + 1
        from wxbot_framework.image_metadata import ImageStore
        from wxbot_framework.image_receiver import ImageReceiver
        self._image_receiver = ImageReceiver(ImageStore(self.media_directory, account),
            self._download_image, self._deliver_image,
            lambda: not self._stop.is_set() and self._owns_bridge and self.self_wxid == account)
        from wxbot_framework.file_metadata import FileStore
        from wxbot_framework.file_receiver import FileReceiver
        self._file_receiver = FileReceiver(FileStore(self.file_directory, account),
            self._download_file, self._deliver_file,
            lambda: not self._stop.is_set() and self._owns_bridge and self.self_wxid == account)
        from wxbot_framework.forwarded_receiver import ForwardedReceiver
        self._forwarded_receiver = ForwardedReceiver(ImageStore(self.media_directory, account),
            self._download_image, self._deliver_forwarded,
            lambda: not self._stop.is_set() and self._owns_bridge and self.self_wxid == account,
            queue_size=8)

    def _deliver_forwarded(self, data):
        if self._stop.is_set():
            return
        for handler in list(self.msg_handlers):
            try:
                handler(self.pid, 11046, data, self)
            except Exception:
                logger.warning('合并转发消息处理器暂不可用')

    def _download_file(self, metadata, destination):
        from wxbot_framework.files import MAX_FILE_BYTES
        # attachid 是微信应用附件标识，不能未经验证当作 CDN 地址。
        if (not metadata.cdnattachurl or not metadata.aeskey or type(metadata.size) is not int or
                not 1 <= metadata.size <= MAX_FILE_BYTES):
            return False
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                return False
            self._check_account(force=True)
            result = self._run("--download-file", pid=self.pid, keep_loaded=True,
                preserve_report=self.preserve_reports, account=self.self_wxid,
                download_file={"destination": str(destination), "filekey": metadata.cdnattachurl,
                               "aeskey": metadata.aeskey, "expected_size": metadata.size})
            self._check_account(force=True)
            return result.get("success") is True

    def _deliver_file(self, data):
        if self._stop.is_set():
            return
        # 群消息日志仍只显示群 ID；不输出附件 XML、CDN 地址或密钥。
        if data.get("room_wxid"):
            logger.info("群文件接收 | 群ID=%s", data["chat_id"])
        for handler in list(self.msg_handlers):
            try:
                handler(self.pid, 11046, data, self)
            except Exception:
                logger.exception("文件消息处理器异常")

    def _download_image(self, metadata, destination):
        filekey = metadata.cdnmidimgurl or metadata.filekey or metadata.cdnbigimgurl or metadata.cdnthumburl
        aeskey = metadata.aeskey if (metadata.cdnmidimgurl or metadata.filekey or metadata.cdnbigimgurl) else (metadata.cdnthumbaeskey or metadata.aeskey)
        image_type = (2 if metadata.cdnmidimgurl or metadata.filekey else
                      1 if metadata.cdnbigimgurl else 3)
        if not filekey or not aeskey:
            return False
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                return False
            self._check_account(force=True)
            result = self._run("--download-image", pid=self.pid, keep_loaded=True,
                preserve_report=self.preserve_reports,
                download_image={"destination": str(destination), "filekey": filekey,
                                "aeskey": aeskey, "image_type": image_type})
            self._check_account(force=True)
            return result.get("success") is True

    def _deliver_image(self, data):
        if self._stop.is_set():
            return
        logger.info("图片接收 | 会话ID=%s | 状态=%s | 文件=%s", data["chat_id"],
                    data.get("image_status"), data.get("image_path", ""))
        for handler in list(self.msg_handlers):
            try:
                handler(self.pid, 11046, data, self)
            except Exception:
                logger.exception("图片消息处理器异常")

    def stop(self):
        self._stop.set()
        if self._contact_names is not None:
            self._contact_names.close()
            self._contact_names = None
        if self._friend_requests is not None:
            self._friend_requests.close()
            self._friend_requests = None
        if self._forwarded_receiver is not None:
            self._forwarded_receiver.close(timeout=30)
            if self._forwarded_receiver.is_alive:
                raise RuntimeError('合并转发图片下载尚未结束，保留桥接等待清理重试')
            self._forwarded_receiver = None
        if self._file_receiver is not None:
            self._file_receiver.close(timeout=30)
            if self._file_receiver.is_alive:
                raise RuntimeError("文件原生下载尚未结束，保留桥接等待清理重试")
            self._file_receiver = None
        if self._image_receiver is not None:
            self._image_receiver.close(timeout=30)
            if self._image_receiver.is_alive:
                raise RuntimeError("图片原生下载尚未结束，保留桥接等待清理重试")
            self._image_receiver = None
        return super().stop()

    def _check_account(self, *, force=False):
        if not force and time.monotonic() < self._next_account_check:
            return
        account = self._read_account()
        if not account or account != self.self_wxid:
            if self._contact_names is not None:
                self._contact_names.close()
                self._contact_names = None
            error = RuntimeError("微信已退出登录或当前账号发生变化，重新连接并自动识别")
            self.last_error = error
            self._stop.set()
            raise error
        self._next_account_check = time.monotonic() + 1

    def _before_poll(self):
        self._check_account()

    def group_members(self, room):
        from wxbot_framework.group_management import validate_group_id
        from wxbot_framework.group_members import read_group_members
        room = validate_group_id(room)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set() or self._channel is None:
                raise RuntimeError("群成员查询需要已连接的微信账号")
            self._check_account(force=True)
            rows = read_group_members(self._channel.read_memory, self._channel.core_base, room,
                                      expected_account=self.self_wxid)
            if rows is not None and self._contact_names is not None:
                enriched = []
                for row in rows:
                    row = dict(row)
                    if not row.get("wechat_name"):
                        name = self._contact_names.get(row["wxid"], account=self.self_wxid)
                        if name:
                            row["wechat_name"] = name
                            row["nickname"] = row.get("group_nickname") or name
                    enriched.append(row)
                rows = tuple(enriched)
            self._check_account(force=True)
            return rows

    def _start_contact_names(self, account):
        """已有账号/PID 选择负责绑定；可选只读补全失败不影响原生连接。"""
        from wxbot_framework.contact_names import ContactNames, load_contact_names
        from wxbot_framework.contact_keys import ContactProcess
        if self._contact_names is not None:
            self._contact_names.close()
        pid = self.pid  # 不在后台随可变服务字段切换账号或进程。
        directory = self.wechat_data_directory

        def process_factory():
            core = modules(pid).get("weixin.dll")
            if not core:
                raise RuntimeError("联系人进程已退出")
            return ContactProcess(pid, core[0], core[1])

        def loader(**options):
            return load_contact_names(account, process_factory, directory, **options)

        try:
            self._contact_names = ContactNames(account, loader)
        except Exception:
            self._contact_names = None
            logger.warning("联系人名字补全未启动，保留原有缓存")

    def _sender_wechat_name(self, wxid):
        if self._channel is None:
            return None
        name = read_contact_name(self._channel.read_memory, self._channel.core_base, wxid,
                                 expected_account=self.self_wxid)
        if name is None and wxid == self.self_wxid:
            profile = read_current_profile(self._channel.read_memory, self._channel.core_base)
            name = profile["name"] if profile and profile["wxid"] == wxid else None
        if (not name and self._contact_names is not None and
                read_current_account(self._channel.read_memory, self._channel.core_base) == self.self_wxid):
            name = self._contact_names.get(wxid, account=self.self_wxid)
        return name

    def _chat_name(self, chat):
        """私聊取好友微信名、群聊取群名；读不到（未命名群、公众号、缓存不符）时返回 None。"""
        if self._channel is None:
            return None
        try:
            if chat.endswith("@chatroom"):
                return read_contact_name(self._channel.read_memory, self._channel.core_base, chat,
                                         expected_account=self.self_wxid, group=True)
            return self._sender_wechat_name(chat)
        except ValueError:
            return None

    def remember_chat(self, chat):
        """登记一个本次还没来消息的会话（如开了长期记忆的），顺带读名字。"""
        self._remember_chat(chat, chat.endswith("@chatroom"))

    def _remember_chat(self, chat, group):
        entry = self.chats.setdefault(chat, {"chat_id": chat, "is_group": group})
        if not entry.get("name"):
            name = self._chat_name(chat)
            if name:
                entry["name"] = name

    def _map_message(self, message):
        if message.type == 37:
            return self._map_friend_request(message)
        if message.type in (10000, 10002):
            return self._map_group_system(message)
        if message.type not in (1, 3, 34, 43, 47, 49, 62):
            return None
        previous = self.self_wxid
        previous_unparsed = self.router.unparsed_groups
        local_send = bool(getattr(message, 'reserved', 0) & 8)
        local_quote = local_send and message.type == 49 and bool(getattr(message, 'reserved', 0) & 32)
        if local_send:
            if message.type != 1 and not local_quote:
                return None
            self._check_account(force=True)
        large_text = message.type == 1 and bool(getattr(message, 'reserved', 0) & 16)
        body = (getattr(message, "image_xml", b"") if message.type != 1 or large_text else message.text).decode("utf-8", errors="replace")
        sender = self.self_wxid if local_send else message.sender.decode("utf-8")
        data = self.router.convert(sender, message.receiver.decode("utf-8"),
                                   body, sequence=message.sequence, msg_type=message.type,
                                   thread_id=message.thread_id)
        if self.self_wxid and not previous:
            logger.info("当前账号已识别；好友和群聊文本已就绪")
        if data:
            data['native_message_id'] = str(getattr(message, 'native_message_id', 0) or '')
            if data['is_self'] and message.type == 1:
                data = self._own_text_echoes.accept(data, local=local_send)
                if data is None:
                    return None
                if data.get('echo_update'):
                    return data
            if local_send:
                data['local_send'] = True
            if local_quote:
                from wxbot_framework.quotes import quote_capture_metadata
                metadata = quote_capture_metadata(getattr(message, 'mention_source', b''))
                if metadata is not None:
                    data['quote_capture'] = metadata
                # 原生发送监听只观察，绝不因引用自己而触发再次回复（含文件助手）。
                data['dispatch_to_plugins'] = False
            name = self._sender_wechat_name(data["sender_id"]) if data["sender_id"] else None
            if name:
                data["sender_wechat_name"] = name
            source = '' if local_quote else getattr(message, "mention_source", b"").decode("utf-8", errors="replace")
            data["at_members"] = parse_at_members(source)
            data["is_at_self"] = bool(data["room_wxid"] and self.self_wxid in data["at_members"])
            if data["at_members"]:
                self.mention_messages += 1
            sent = message.type == 1 and data["is_self"] and next((entry for entry in tuple(self._sent_texts) if entry[0] == data["chat_id"] and
                                             entry[1] == data["msg"] and time.monotonic() - entry[2] < 120), None)
            if sent:
                try:  # 一次发送只认领一条回传：之后手动发同样的话是你自己说的，照常显示、记录
                    self._sent_texts.remove(sent)
                except ValueError:
                    pass
                self.router.ignored_self += 1
                data["bot_generated"] = True
                data["own_text_recorded"] = message.type == 1
                data["dispatch_to_plugins"] = False
                if len(sent) > 4 and type(sent[4]) is int and sent[4] < 0:
                    data['outgoing_sequence'] = sent[4]
            elif data["is_at_self"]:
                data["dispatch_to_plugins"] = True
            self._remember_chat(data["chat_id"], bool(data["room_wxid"]))
            if message.type == 49:
                from wxbot_framework.quotes import parse_quote, parse_local_quote_snapshot, QuoteError
                try:
                    if local_quote and getattr(message, 'reserved', 0) & 64:
                        quote = parse_local_quote_snapshot(body, chat_id=data['chat_id'], self_id=self.self_wxid,
                                                           resolve_name=self._sender_wechat_name)
                    else:
                        quote = parse_quote(data.get('raw_content', data['msg']), chat_id=data['chat_id'])
                except QuoteError:
                    quote = None
                if quote is not None:
                    data.update(msg=quote.text, content=quote.text, wx_type=1, native_type=49, app_type=57,
                        quote_is_self=quote.scope_valid and quote.author_id == self.self_wxid,
                        quote_reply=quote.reply, quoted_sender_id=quote.author_id, quoted_message_id=quote.message_id)
                    data['quote'] = quote.public()
                    data.pop('raw_content', None)
                    if data['is_self']:
                        # XML 与本机 JSON 先统一成结构化引用；昵称差异不影响消息身份。
                        data = self._own_text_echoes.accept(data, local=local_send)
                        if data is None or data.get('echo_update'):
                            return data
                        marker = next((item for item in tuple(self._sent_quotes) if
                            (item[0], item[1].strip(), item[2]) == (data['chat_id'], quote.reply.strip(), quote.message_id)
                            and time.monotonic() - item[3] < 120), None)
                        if marker is not None:
                            try:
                                self._sent_quotes.remove(marker)
                            except ValueError:
                                pass
                            data.update(bot_generated=True, own_text_recorded=True, dispatch_to_plugins=False)
                            if len(marker) > 4 and type(marker[4]) is int and marker[4] < 0:
                                data['outgoing_sequence'] = marker[4]
                    return data
                if local_quote:
                    # 布局未确认或 XML 不完整：只显示诊断，不误当文件下载，不写入上下文。
                    notice = '[本机引用：结构待确认]'
                    reply = message.text.decode('utf-8', errors='replace')
                    if reply:
                        notice += '\n' + reply
                    data.update(msg=notice, content=notice,
                                app_type=57, monitor_only=True, dispatch_to_plugins=False)
                    data.pop('raw_content', None)
                    return data
            if message.type in (34, 43, 47, 62):
                data['monitor_only'] = True
                return data
            if message.type in (3, 49) and self.media_context_provider is not None:
                try:
                    data['_media_context'] = self.media_context_provider(data['chat_id'])
                except Exception:
                    logger.warning('媒体接收代次暂不可读，跳过本次下载')
                    return None
            media_allowed = self._media_allowed(data['chat_id']) if message.type in (3, 49) else False
            forwarded = None
            if message.type == 49:
                from wxbot_framework.forwarded import ForwardedError, parse_forwarded
                try:
                    forwarded = parse_forwarded(body)
                except ForwardedError as error:
                    if error.forwarded or '<recorditem' in body:
                        data.update(app_type=19, forwarded_status='failed')
                if forwarded is not None:
                    data.update(forwarded.public())
                    data['forwarded_status'] = 'pending'
                    if not media_allowed:
                        data['forwarded_status'] = 'metadata'
                        for item in data['forwarded_records']:
                            if item.get('image_status') == 'pending':
                                item['image_status'] = 'skipped'
                if getattr(message, 'reserved', 0) & 1:
                    data['metadata_unavailable'] = True
            if message.type in (3, 49):
                monitor = getattr(self, 'monitor_handler', None)
                if monitor is not None:
                    monitor(dict(data, monitor_only=True))
            if message.type == 3 and self._image_receiver is not None:
                if media_allowed:
                    self._image_receiver.submit(data)
                return None
            if message.type == 49:
                if forwarded is not None:
                    if self._forwarded_receiver is not None and media_allowed:
                        self._forwarded_receiver.submit(data, forwarded)
                    return None
                if data.get('app_type') == 19 or data.get('metadata_unavailable'):
                    return None
                if self._file_receiver is not None and media_allowed:
                    self._file_receiver.submit(data)
                return None
        elif self.router.unparsed_groups > previous_unparsed:
            # 第一条失败时提示；后续通过 status 查看累计数。
            if self.router.unparsed_groups == 1:
                logger.warning("群文本作者格式未匹配，已跳过；需要用测试群消息核对实际布局")
        return data

    def _map_group_system(self, message):
        # 系统事件不作为聊天正文，不进入模型或普通文本插件。
        sender = message.sender.decode("utf-8")
        receiver = message.receiver.decode("utf-8")
        if getattr(message, 'reserved', 0) & 2:
            # 独立系统解析入口发生在已核对的当前微信进程；这里没有 AddMsg 收发双方。
            from wxbot_framework.recall import parse_recall
            recall = parse_recall(message.image_xml.decode('utf-8', errors='replace'))
            if recall is None or sender != 'weixin' or receiver != 'weixin' or not self.self_wxid:
                return None
            chat = recall['chat_id']
            return {'msg':'', 'content':'', 'wx_type':10002, 'chat_id':chat,
                    'room_wxid':chat if chat.endswith('@chatroom') else '', 'sender_id':'',
                    'native_receiver':self.self_wxid, 'self_wxid':self.self_wxid,
                    'is_self':False, 'sequence':message.sequence, 'recall':recall, 'monitor_only':True}
        if receiver != self.self_wxid and sender != self.self_wxid:
            return None
        chat = receiver if sender == self.self_wxid else sender
        validate_target(chat)
        content = message.image_xml.decode("utf-8", errors="replace")
        from wxbot_framework.recall import parse_recall
        recall = parse_recall(content, chat_id=chat)
        group = chat.endswith('@chatroom')
        if not group and recall is None:
            return None
        self._remember_chat(chat, group)
        data = {"msg": "", "content": "", "system_content": content, "wx_type": message.type,
                "chat_id": chat, "room_wxid": chat if group else "", "sender_id": "",
                "native_sender": sender, "native_receiver": receiver,
                "self_wxid": self.self_wxid, "is_self": False, "sequence": message.sequence,
                "native_message_id": str(getattr(message, 'native_message_id', 0) or '')}
        if recall is not None:
            data.update(recall=recall, monitor_only=True)
        return data

    def _map_friend_request(self, message):
        from wxbot_framework.friend_requests import FriendRequestError, parse_friend_request
        if self._stop.is_set() or self._friend_requests is None:
            return None
        try:
            receiver = message.receiver.decode("utf-8")
            sender = message.sender.decode("utf-8")
            if receiver != self.self_wxid or sender == self.self_wxid:
                return None
            request = parse_friend_request(message.image_xml.decode("utf-8"))
            # type37 也包含验证结果等通知；仅处理明确的入站申请。
            if request.opcode != 2:
                return None
            public = self._friend_requests.add(request)
        except (UnicodeError, FriendRequestError):
            logger.warning("好友申请元数据无效或不是当前支持的格式，已跳过")
            return None
        return {"msg": "", "content": "", "wx_type": 37, "chat_id": "fmessage",
                "from_wxid": request.sender_id, "sender_id": request.sender_id,
                "native_receiver": receiver, "self_wxid": self.self_wxid,
                "room_wxid": "", "is_self": False, "sequence": message.sequence,
                "friend_request": public, "dispatch_to_plugins": public["status"] == "pending"}

    def friend_requests(self):
        with self._calls:
            if not self._owns_bridge or self._stop.is_set() or self._friend_requests is None:
                raise RuntimeError("好友申请接收服务没有运行")
            self._check_account(force=True)
            return self._friend_requests.list()

    def accept_friend(self, request_id):
        with self._calls:
            if not self._owns_bridge or self._stop.is_set() or self._friend_requests is None:
                raise RuntimeError("好友申请接收服务没有运行")
            self._check_account(force=True)
            catalog = self._friend_requests
            owner = {"account": self.self_wxid, "session_id": self._friend_session_id}
            record = catalog.resolve(request_id, **owner)
            if record.status == "accepted":
                return True
            if record.status != "pending" or record.request.opcode != 2:
                raise ValueError("好友申请已处理或不可通过；请让对方重新发送申请")
            try:
                self.last_result = self._run("--accept-friend", pid=self.pid, keep_loaded=True,
                    preserve_report=self.preserve_reports, account=self.self_wxid,
                    friend_request=record.request)
                self._check_account(force=True)
            except Exception:
                catalog.mark_failed(request_id, **owner)
                raise RuntimeError("通过好友申请失败，请检查连接及申请状态") from None
            accepted = self.last_result.get("success") is True
            if accepted:
                catalog.mark_accepted(request_id, **owner)
            else:
                catalog.mark_failed(request_id, **owner)
            return accepted

    def invite_group_members(self, room, members):
        from wxbot_framework.group_management import validate_group_id, validate_group_members
        room = validate_group_id(room)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生群管理服务没有运行")
            self._check_account(force=True)
            members = validate_group_members(members, self_wxid=self.self_wxid)
            self.last_result = self._run("--manage-group", pid=self.pid, keep_loaded=True,
                preserve_report=self.preserve_reports, account=self.self_wxid,
                target=room, group_operation="invite", group_members=members)
            self._check_account(force=True)
            return self.last_result.get("success") is True

    def create_group(self, members):
        from wxbot_framework.group_management import validate_group_id, validate_group_members
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生群管理服务没有运行")
            self._check_account(force=True)
            members = validate_group_members(members, creating=True, self_wxid=self.self_wxid)
            self.last_result = self._run("--manage-group", pid=self.pid, keep_loaded=True,
                preserve_report=self.preserve_reports, account=self.self_wxid,
                group_operation="create", group_members=members)
            self._check_account(force=True)
            if self.last_result.get("success") is not True:
                raise RuntimeError("微信未确认创建群聊，请检查返回状态")
            room = validate_group_id(self.last_result.get("report", {}).get("room_id"))
            self._remember_chat(room, True)
            return room

    def send_text(self, to_wxid, content):
        return self._send_text(to_wxid, content)

    def send_at_text(self, room_wxid, content, at_list):
        members = validate_mentions(room_wxid, at_list)
        return self._send_text(room_wxid, content, mentions=members)

    def send_image(self, to_wxid, path):
        from wxbot_framework.images import validate_image_path
        validate_target(to_wxid)
        path = validate_image_path(path)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生图片服务没有运行")
            self._check_account(force=True)
            self.last_result = self._run("--dispatch", pid=self.pid, image_path=path,
                                        target=to_wxid, keep_loaded=True,
                                        preserve_report=self.preserve_reports)
            return self.last_result["success"]

    def send_emoticon(self, to_wxid, path):
        from wxbot_framework.emoticons import validate_emoticon_path
        validate_target(to_wxid)
        path = validate_emoticon_path(path)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生表情服务没有运行")
            self._check_account(force=True)
            self.last_result = self._run("--dispatch", pid=self.pid, emoticon_path=path,
                                        account=self.self_wxid, target=to_wxid, keep_loaded=True,
                                        preserve_report=self.preserve_reports)
            self._check_account(force=True)
            return self.last_result["success"]

    def send_voice(self, to_wxid, path):
        from wxbot_framework.voices import prepare_voice
        validate_target(to_wxid)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生语音服务没有运行")
            self._check_account(force=True)
            account = validate_account(self.self_wxid)
            voice_root = self.voice_directory.resolve()
            account_directory = (voice_root / account).resolve()
            if account_directory.parent != voice_root:
                raise ValueError("语音账号目录必须位于当前项目的语音目录内")
            # 转码和快照归当前账号所有；原生调用结束前保持快照文件有效。
            with prepare_voice(path, account_directory) as voice:
                if not self._owns_bridge or self._stop.is_set():
                    raise RuntimeError("微信连接已结束，取消待发送语音")
                self._check_account(force=True)
                if self.self_wxid != account:
                    raise RuntimeError("微信账号已发生变化，取消待发送语音")
                self.last_result = self.last_send_result = self._run(
                    "--dispatch", pid=self.pid, keep_loaded=True,
                    preserve_report=self.preserve_reports, target=to_wxid,
                    voice_path=str(voice.path), voice_duration_ms=voice.duration_ms,
                    account=account)
                self._check_account(force=True)
                return self.last_send_result.get("success", False)

    def send_file(self, to_wxid, path):
        from wxbot_framework.files import prepare_file
        validate_target(to_wxid)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生文件服务没有运行")
            self._check_account(force=True)
            account = validate_account(self.self_wxid)
            file_root = self.file_directory.resolve()
            account_directory = (file_root / account).resolve()
            if account_directory.parent != file_root:
                raise ValueError("文件账号目录必须位于当前项目的文件目录内")
            with prepare_file(path, account_directory) as file:
                if not self._owns_bridge or self._stop.is_set():
                    raise RuntimeError("微信连接已结束，取消待发送文件")
                self._check_account(force=True)
                if self.self_wxid != account:
                    raise RuntimeError("微信账号已发生变化，取消待发送文件")
                self.last_result = self.last_send_result = self._run(
                    "--dispatch", pid=self.pid, keep_loaded=True,
                    preserve_report=self.preserve_reports, target=to_wxid,
                    file_path=str(file.path), account=account)
                self._check_account(force=True)
                return self.last_send_result.get("success", False)

    def export_emoticons(self, *, convert=True):
        from wxbot_framework.emoticon_account import read_emoticon_cache_key
        from wxbot_framework.emoticons import export_local_emoticons
        if type(convert) is not bool:
            raise ValueError("convert 必须是布尔值")
        if self.wechat_data_directory is None:
            raise ValueError("请先在本地配置 wechat_data_directory，指向当前账号的数据目录")
        with self._calls:
            if not self._owns_bridge or self._stop.is_set() or self._channel is None:
                raise RuntimeError("原生表情服务没有运行")
            self._check_account(force=True)
            account = self.self_wxid
            key = read_emoticon_cache_key(self._channel.read_memory, self._channel.core_base,
                                          expected_account=account)

            def is_current():
                return (self._owns_bridge and not self._stop.is_set() and
                        self.self_wxid == account and self._read_account() == account)

            try:
                result = export_local_emoticons(self.wechat_data_directory, self.emoticon_directory,
                                               key, expected_account=account, convert=convert,
                                               is_current=is_current)
                self._check_account(force=True)
                return result
            finally:
                # 不持久化密钥；引用只活到这次显式导出结束。
                del key

    def resolve_quote_reference(self, source):
        import hashlib
        from wxbot_framework.contact_keys import ContactProcess
        from wxbot_framework.account import read_current_account
        from wxbot_framework.native_profiles import profile_for_fingerprint
        from wxbot_framework.quote_references import resolve_cached_quote, QuoteReferenceError
        with self._calls:
            if not self._owns_bridge or self._stop.is_set() or self._channel is None:
                raise QuoteReferenceError('请先连接机器人，再核对原消息')
            self._check_account(force=True)
            account = self.self_wxid
            core = modules(self.pid).get('weixin.dll')
            if core is None:
                raise QuoteReferenceError('微信核心模块已变化')
            path, digest = Path(core[1]), hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(2*1024*1024), b''):
                    if self._stop.is_set():
                        raise QuoteReferenceError('连接已停止，取消核对')
                    digest.update(block)
            profile = profile_for_fingerprint(path.stat().st_size, digest.hexdigest())
            if profile is None or profile.version != '4.1.15.56':
                raise QuoteReferenceError('此微信构建尚未验证旧消息原生引用')
            with ContactProcess(self.pid, core[0], path) as process:
                def check():
                    if self._stop.is_set() or read_current_account(process.read, core[0]) != account:
                        raise QuoteReferenceError('微信账号或连接已变化，取消核对')
                result = resolve_cached_quote(process, dict(source), account, check=check)
            self._check_account(force=True)
            return result

    def send_quote(self, to_wxid, quote, *, created_at=0, probe=False):
        from wxbot_framework.quotes import normalize_quote
        from wxbot_framework.quote_references import QuoteReferenceError
        validate_target(to_wxid)
        quote = normalize_quote(quote)
        if quote is None:
            raise ValueError('引用消息格式无效')
        if type(probe) is not bool:
            raise ValueError('引用检查标志必须是布尔值')
        with self._calls:
            if not self._owns_bridge or self._stop.is_set() or self._channel is None:
                raise RuntimeError('原生引用服务没有运行')
            self._check_account(force=True)
            marker = (to_wxid, quote['reply'], quote['reference']['message_id'], time.monotonic(), self._outgoing_sequence)
            if not probe:
                self._sent_quotes.append(marker)
            try:
                self.last_result = self.last_send_result = self._run('--quote', pid=self.pid, target=to_wxid,
                    text=quote['reply'], quote=quote, quote_probe=probe, quote_created_at=created_at,
                    account=self.self_wxid, keep_loaded=True, preserve_report=self.preserve_reports)
                success = self.last_result.get('success') is True
            except BaseException:
                if not probe and marker in self._sent_quotes:
                    self._sent_quotes.remove(marker)
                raise
            if not success and not probe and marker in self._sent_quotes:
                self._sent_quotes.remove(marker)
            self._check_account(force=True)
            if not success:
                report = self.last_result.get('report', {})
                if isinstance(report, dict) and report.get('diagnostic_version') == 2:
                    stage = report.get('stage')
                    stage = stage if type(stage) is int and 90 <= stage <= 99 else 0
                    if report.get('send_called') is False:
                        if report.get('status') == 1168:  # ERROR_NOT_FOUND
                            raise QuoteReferenceError('微信没有查到这条原消息，未发送引用；请确认原消息仍存在')
                        if report.get('stage') == 95 and report.get('status') == 13:
                            raise QuoteReferenceError('原消息数据不完整或会话/作者不符，已取消引用发送')
                        raise QuoteReferenceError(f'原生引用准备失败（阶段 {stage}），尚未调用发送；需要检查原生日志')
                    raise QuoteReferenceError(f'原生引用发送状态未确认（阶段 {stage}）；请先核对微信是否收到，不要连续重试')
            return success

    def _send_text(self, to_wxid, content, *, mentions=None):
        validate_target(to_wxid)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生文本服务没有运行")
            self._check_account(force=True)
            pending = (to_wxid, content, time.monotonic(), uuid.uuid4().hex, self._outgoing_sequence)
            self._sent_texts.append(pending)
            try:
                self.last_result = self._run("--dispatch", pid=self.pid, text=content, target=to_wxid,
                                            keep_loaded=True, preserve_report=self.preserve_reports, mentions=mentions)
            except BaseException:
                try:
                    self._sent_texts.remove(pending)
                except ValueError:
                    pass
                raise
            if self.last_result.get("success") is not True:
                try:
                    self._sent_texts.remove(pending)
                except ValueError:
                    pass
            if content.startswith("WXBOT_"):
                logger.info("测试文本发送完成：chat_id=%s success=%s native_thread=%s",
                            to_wxid, self.last_result["success"],
                            self.last_result.get("report", {}).get("thread_id"))
            return self.last_result["success"]
