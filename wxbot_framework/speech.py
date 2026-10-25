"""共享语音回复层：音色按群保存、切换后取消旧音频、失败回退文字。"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time

from .members import SnapshotExpired
from .summary import split_text
from .tts import FishTTS, TTSError, VOICE_ID

REQUIREMENTS = Path(__file__).resolve().parents[1] / 'requirements-voice.txt'


def ensure_encoder(logger):
    """本机 Qwen3 和 Fish 都返回 WAV，发微信语音要先编码成 SILK。缺编码器时按 requirements-voice.txt 锁定的版本
    自动装进当前 Python，装不上抛 ValueError 并给出手动命令，不等到发语音时才出错。"""
    def available():
        importlib.invalidate_caches()
        try:
            importlib.import_module('pysilk')
            return True
        except (ImportError, OSError):
            return False
    if available():
        return
    package = re.search(r'^silk-python==\S+$', REQUIREMENTS.read_text(encoding='utf-8'), re.M).group(0)
    logger.info('语音缺少编码器，正在自动安装 %s', package)
    try:
        result = subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', package],
                                capture_output=True, timeout=300, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            logger.warning('自动安装失败：%s', result.stdout.decode('utf-8', 'replace')[-500:])
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not available():
        raise ValueError(f'自动安装语音编码器失败，请手动运行：{sys.executable} -m pip install {package}')


def speech_chunks(text, limit):
    text = text.strip()
    while text:
        if len(text) <= limit:
            yield text
            return
        cut = max((text.rfind(mark, 0, limit) + 1 for mark in '。！？!?；;\n'), default=0)
        if cut < limit // 2:
            cut = limit
        part, text = text[:cut].strip(), text[cut:].strip()
        if part:
            yield part


class SpeechController:
    def __init__(self, config_path, directory, members, active):
        self.path, self.directory = Path(config_path), Path(directory)
        self.members, self._active = members, active
        self._lock = threading.RLock()
        self._client = None
        self._catalog = ()
        self._catalog_time = 0
        self._public_pages = {}
        self._public_voices = {}
        self._generation = 0
        self._closed = False

    @property
    def enabled(self):
        return not self._closed and self._active() is True

    def reload(self):
        if self.members is None:
            raise TTSError("语音回复需要配置成员目录，以保存各群音色")
        client = FishTTS.from_file(self.path)
        with self._lock:
            self._client, self._catalog, self._catalog_time = client, (), 0
            self._public_pages.clear()
            self._public_voices.clear()
            self._generation += 1
            self._closed = False

    def catalog(self, *, refresh=False):
        with self._lock:
            if self._closed or self._client is None:
                raise TTSError("语音回复服务未启用")
            client, generation = self._client, self._generation
            if not refresh and self._catalog and time.monotonic() - self._catalog_time < 600:
                return self._catalog
        catalog = client.voices()
        with self._lock:
            if generation != self._generation or self._closed:
                raise SnapshotExpired("语音配置已更新")
            self._catalog, self._catalog_time = catalog, time.monotonic()
            return catalog

    def public_catalog(self, *, page=1, keyword='', page_size=10, all_languages=False):
        key = (page, keyword, page_size, all_languages)
        with self._lock:
            if self._closed or self._client is None:
                raise TTSError('语音回复服务未启用')
            cached = self._public_pages.get(key)
            if cached and time.monotonic() - cached[0] < 600:
                return cached[1]
            client, generation = self._client, self._generation
        result = client.public_voices(page=page, keyword=keyword, page_size=page_size, all_languages=all_languages)
        with self._lock:
            if self._closed or generation != self._generation:
                raise SnapshotExpired('语音配置已更新')
            now = time.monotonic()
            self._public_pages[key] = (now, result)
            for voice in result.voices:
                self._public_voices[voice.id] = (now, voice)
            while len(self._public_pages) > 64:
                self._public_pages.pop(next(iter(self._public_pages)))
            while len(self._public_voices) > 2000:
                self._public_voices.pop(next(iter(self._public_voices)))
        return result

    def _public_voice(self, identifier):
        with self._lock:
            cached = self._public_voices.get(identifier)
            if cached and time.monotonic() - cached[0] < 600:
                return cached[1]
            if self._closed or self._client is None:
                raise TTSError('语音回复服务未启用')
            client, generation = self._client, self._generation
        voice = client.public_voice(identifier)
        with self._lock:
            if self._closed or generation != self._generation:
                raise SnapshotExpired('语音配置已更新')
            self._public_voices[identifier] = (time.monotonic(), voice)
        return voice

    def resolve(self, value):
        # 个人音色优先，保留已有配置；公开重名音色必须用 ID 指定。
        if not isinstance(value, str) or not value or len(value) > 128:
            raise TTSError('请发送 /查看音色，选择音色名称或 ID')
        matches = [voice for voice in self.catalog() if voice.id == value or voice.name.casefold() == value.casefold()]
        if not matches and VOICE_ID.fullmatch(value):
            return self._public_voice(value)
        if not matches:
            candidates = {}
            for page in range(1, 11):
                result = self.public_catalog(page=page, keyword=value, page_size=100, all_languages=True)
                for voice in result.voices:
                    if voice.name.casefold() == value.casefold():
                        candidates[voice.id] = voice
                if len(candidates) > 1 or not result.has_more:
                    break
            else:
                raise TTSError('同名搜索范围过大，请使用 /音色 ID 选择')
            matches = list(candidates.values())
        if len(matches) > 1:
            identifiers = '\n'.join(voice.id for voice in matches[:3])
            raise TTSError('存在同名音色，请用 /音色 ID 选择：\n' + identifiers)
        if not matches:
            raise TTSError('音色未找到，请发送 /查看音色 关键词 搜索；切换时使用完整名称或 ID')
        return matches[0]

    def mode(self, room):
        """智能体这次的语音情况：always 开了语音回复且本会话没 /音色 关闭，每句都念；optional 默认发文字，
        模型可以自己选语音（对方要求时）；空串表示发不了语音（配置读不了、缺编码器，或既没选音色也没默认音色）。"""
        if self.members is None:
            return ""
        state = self.members.voice_selection(room)
        if self.enabled and state.enabled:
            return "always"
        if importlib.util.find_spec("pysilk") is None:  # 微信语音要先编码成 SILK
            return ""
        with self._lock:
            if self._closed:
                return ""
            loaded = self._client is not None
        if not loaded:  # 语音回复插件没开过：配置还没加载，这里临时加载
            try:
                self.reload()
            except (TTSError, ValueError):
                return ""
        with self._lock:
            default = self._client.default_voice if self._client is not None else ""
        return "optional" if state.voice_id or default else ""

    def selected(self, room, *, forced=False):
        """forced：模型主动选了语音，本会话 /音色 关闭了也照用（选过的音色或默认音色）。"""
        state = self.members.voice_selection(room)
        if not state.enabled and not forced:
            return state, None
        with self._lock:
            client = self._client
        if client is None:
            raise TTSError("语音配置未加载")
        return state, self.resolve(state.voice_id or client.default_voice)

    def select(self, room, name):
        if name in ('关闭', '文字'):
            return self.members.set_voice(room, '', '', enabled=False)
        voice = self.resolve(name)
        return self.members.set_voice(room, voice.id, voice.name)

    def close(self):
        with self._lock:
            self._closed = True
            self._generation += 1
            self._catalog = ()
            self._public_pages.clear()
            self._public_voices.clear()

    def reply(self, ctx, message, text, *, delivery_guard=None, forced=False):
        self.members.rollover()
        memory_epoch = self.members.epoch(message.chat_id)
        state, voice = self.selected(message.chat_id, forced=forced)
        if voice is None:
            return False
        with self._lock:
            client, generation = self._client, self._generation

        @contextmanager
        def guard():
            if not ctx._service.session_is_current(message.session_id):
                raise SnapshotExpired("微信连接已结束")
            if not (forced or self.enabled):
                raise SnapshotExpired("语音回复已停用")
            with delivery_guard() if delivery_guard else nullcontext():
                with self.members.voice_delivery_guard(state), self._lock:
                    if self.members.epoch(message.chat_id) != memory_epoch:
                        raise SnapshotExpired("记忆已清空")
                    if self._closed or generation != self._generation:
                        raise SnapshotExpired("语音回复配置已变更")
                    yield

        # 先合成完整回复的所有片段；失败时还没发送，可安全回退整段文字。
        account = hashlib.sha256(self.members.account.encode('ascii')).hexdigest()[:16]
        directory = self.directory / account
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='tts-', dir=directory) as temporary:
            owned = Path(temporary).resolve()
            if not owned.is_relative_to(directory.resolve()):
                raise TTSError("语音临时目录超出预期范围")
            paths = []
            for index, part in enumerate(speech_chunks(text, client.max_chars_per_clip)):
                with guard():
                    pass
                data = client.synthesize(part, voice.id)
                with guard():
                    pass
                path = owned / f'{index}.wav'
                path.write_bytes(data)
                paths.append((path, part))
            with guard():
                pass
            for path, part in paths:
                try:
                    sent = ctx.reply_voice(message, path, delivery_guard=guard)
                except TTSError as error:
                    # 已进入发送阶段，不能再由上层按“合成失败”回退整段文字。
                    raise RuntimeError("语音发送失败，未重发文字") from error
                if not sent:
                    # 微信是否已送达不确定，不在此处重复发文字或重试。
                    raise RuntimeError("原生语音发送未确认成功")
                # 每段确认发送后立即保存该段正文；后段失败不影响已确认的前段。
                # 只检查记忆代次：已说出口的内容不因随后断线/切音色而丢失，但清空后不能复活。
                self.members.record_own(message.chat_id, part, message.session_id, expected_epoch=memory_epoch)
        return True


def deliver_model_reply(ctx, message, text, *, display_name=None, delivery_guard=None, voice=False):
    """开了语音回复就念出来；voice=True 是模型主动选了语音，没开语音回复也念。合成失败（还没发出）退回文字。
    text 为空时只艾特 display_name 那个人、不说话（后台任务点名时）。"""
    if not text:
        if display_name is None or not ctx.reply_at(message, "", display_name=display_name, delivery_guard=delivery_guard):
            raise RuntimeError("原生回复发送未成功")
        return True
    speech = getattr(ctx, '_speech', None)
    if speech is not None and (voice or speech.enabled is True):
        try:
            if speech.reply(ctx, message, text, delivery_guard=delivery_guard, forced=voice):
                return True
        except TTSError as error:
            ctx.logger.warning("语音合成失败，回退文字：%s", error)
    budget = 1023 if display_name is None else 1023 - len(('@' + display_name + '\u2005\n').encode('utf-8'))
    for part in split_text(text, byte_limit=budget):
        sent = (ctx.reply(message, part, delivery_guard=delivery_guard) if display_name is None else
                ctx.reply_at(message, part, display_name=display_name, delivery_guard=delivery_guard))
        if not sent:
            raise RuntimeError("原生回复发送未成功")
    return True
