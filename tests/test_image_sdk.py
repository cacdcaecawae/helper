"""图片消息、纯 Python 路由与发送保护；不连接微信或网络。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from wxbot_framework.images import MAX_IMAGE_BYTES, validate_image_path
from wxbot_framework.sdk import CapabilityError, LegacyService, Message, PluginContext, ServiceFacade
from wxbot_framework.text import TextRouter

SELF = "wxid_image_robot"
FRIEND = "wxid_image_friend"
ROOM = "1234567890@chatroom"
IMAGE_XML = '<msg><img md5="example" /></msg>'
PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\0" * 8
SCRATCH = Path(__file__).resolve().parent.parent / ".runtime/tests"


def make_temporary(test, prefix):
    SCRATCH.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix=prefix, dir=SCRATCH)
    root = Path(temporary.name).resolve()

    def cleanup():
        test.assertTrue(root.is_relative_to(SCRATCH.resolve()))
        temporary.cleanup()

    test.addCleanup(cleanup)
    return root


class ImageBackend:
    def __init__(self, session="original"):
        self.session_id = session
        self.sent = []
        self.stopped = False

    def send_image(self, chat_id, path):
        self.sent.append((chat_id, path))
        return True

    def stop(self):
        self.stopped = True


class WaitingSendLock:
    """明确观察发送已经排队，避免靠 sleep 猜测重连发生的时序。"""
    def __init__(self):
        self.lock = threading.Lock()
        self.waiting = threading.Event()

    def __enter__(self):
        self.waiting.set()
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


class ImageMessageTests(unittest.TestCase):
    def test_native_image_keeps_type_raw_content_and_immutable_path_without_text(self):
        source = {"wx_type": 3, "msg": IMAGE_XML, "image_path": "C:/images/example.png",
                  "chat_id": FRIEND, "sender_id": FRIEND}
        message = Message.from_native(11046, source, "session")
        source["image_path"] = "changed.png"
        source["msg"] = "changed"
        self.assertEqual(message.type, 3)
        self.assertEqual(message.text, "")
        self.assertEqual(message.data["raw_content"], IMAGE_XML)
        self.assertEqual(message.image_path, "C:/images/example.png")
        with self.assertRaises(FrozenInstanceError):
            message.image_path = "cannot-change.png"
        with self.assertRaises(TypeError):
            message.data["image_path"] = "cannot-change.png"

    def test_legacy_text_and_alternate_type_field_remain_compatible(self):
        text = Message.from_native(11046, {"content": "普通文本", "from_wxid": FRIEND}, "session")
        self.assertEqual((text.type, text.text, text.image_path), (1, "普通文本", ""))
        image = Message.from_native(11046, {"type": 3, "content": IMAGE_XML}, "session")
        self.assertEqual((image.type, image.text, image.data["raw_content"]), (3, "", IMAGE_XML))
        preferred = Message.from_native(11046, {"wx_type": 1, "type": 3, "msg": "文本"}, "session")
        self.assertEqual((preferred.type, preferred.text), (1, "文本"))

    def test_explicit_unknown_group_author_is_not_replaced_by_room_id(self):
        data = {"wx_type": 3, "room_wxid": ROOM, "sender_id": "", "native_sender": ROOM,
                "msg": IMAGE_XML, "raw_content": "original raw metadata"}
        message = Message.from_native(11046, data, "session")
        self.assertEqual((message.chat_id, message.sender_id, message.is_group), (ROOM, "", True))
        self.assertEqual(message.data["raw_content"], "original raw metadata")


class ImageRoutingTests(unittest.TestCase):
    def setUp(self):
        self.router = TextRouter()
        self.router.set_account(SELF)

    def test_friend_and_filehelper_images_keep_conversation_and_type(self):
        for sender, receiver, chat in ((FRIEND, SELF, FRIEND), (SELF, "filehelper", "filehelper")):
            with self.subTest(chat=chat):
                data = self.router.convert(sender, receiver, IMAGE_XML, msg_type=3, sequence=7)
                message = Message.from_native(11046, data, "session")
                self.assertEqual((message.chat_id, message.sender_id, message.type), (chat, sender, 3))
                self.assertEqual((message.sequence, message.text), (7, ""))

    def test_group_image_extracts_only_an_explicit_person_prefix(self):
        data = self.router.convert(ROOM, SELF, FRIEND + ":\n" + IMAGE_XML, msg_type=3)
        self.assertEqual((data["chat_id"], data["sender_id"], data["raw_content"]),
                         (ROOM, FRIEND, IMAGE_XML))
        self.assertTrue(data["dispatch_to_plugins"])
        for content in (IMAGE_XML, "非法作者:\n" + IMAGE_XML,
                        ROOM + ":\n" + IMAGE_XML, "filehelper:\n" + IMAGE_XML,
                        "weixin:\n" + IMAGE_XML, "gh_public:\n" + IMAGE_XML):
            with self.subTest(content=content):
                data = self.router.convert(ROOM, SELF, content, msg_type=3)
                self.assertEqual((data["chat_id"], data["sender_id"], data["raw_content"]),
                                 (ROOM, "", content))
                self.assertEqual(Message.from_native(11046, data, "session").sender_id, "")
        self.assertEqual(self.router.unparsed_groups, 0)

    def test_image_body_cannot_enable_text_only_self_commands(self):
        for sender, receiver, body in ((SELF, ROOM, "/help"),
                                       (ROOM, SELF, SELF + ":\n成语接龙")):
            self.assertIsNone(self.router.convert(sender, receiver, body, msg_type=3))
        self.assertEqual(self.router.ignored_self, 2)
        router = TextRouter(include_self=True)
        router.set_account(SELF)
        data = router.convert(ROOM, SELF, SELF + ":\n/help", msg_type=3)
        self.assertTrue(data["is_self"])
        self.assertFalse(data["dispatch_to_plugins"])
        self.assertEqual(data["wx_type"], 3)

    def test_text_group_prefix_and_self_command_rules_are_unchanged(self):
        self.assertIsNone(self.router.convert(ROOM, SELF, "无成员前缀"))
        self.assertEqual(self.router.unparsed_groups, 1)
        data = self.router.convert(ROOM, SELF, SELF + ":\n/help")
        self.assertEqual((data["sender_id"], data["msg"], data["wx_type"]), (SELF, "/help", 1))
        self.assertTrue(data["dispatch_to_plugins"])

    def test_wrong_connection_and_unidentified_private_images_are_rejected(self):
        self.assertIsNone(TextRouter().convert(FRIEND, SELF, IMAGE_XML, msg_type=3))
        self.assertIsNone(self.router.convert(FRIEND, "wxid_other_account", IMAGE_XML, msg_type=3))
        with self.assertRaises(RuntimeError):
            self.router.convert(ROOM, "wxid_other_account", IMAGE_XML, msg_type=3)
        self.assertEqual(self.router.self_wxid, SELF)


class ImageFileTests(unittest.TestCase):
    def setUp(self):
        self.root = make_temporary(self, "image-files-")

    def test_supported_magic_is_accepted_even_without_image_extension(self):
        signatures = (b"\xff\xd8\xff\xe0", PNG_HEADER, b"GIF87a", b"GIF89a",
                      b"RIFF\x10\0\0\0WEBP", b"BM")
        for index, signature in enumerate(signatures):
            with self.subTest(index=index):
                path = self.root / f"image-{index}.dat"
                path.write_bytes(signature + b"\0" * 32)
                self.assertEqual(validate_image_path(path), path.resolve())

    def test_invalid_paths_and_disguised_non_images_are_rejected(self):
        disguised, empty, wave = self.root / "text.png", self.root / "empty.png", self.root / "wave.webp"
        disguised.write_text("not an image", encoding="utf-8")
        empty.touch()
        wave.write_bytes(b"RIFF\x10\0\0\0WAVE" + b"\0" * 16)
        for path in (None, 3, b"image.png", "", " ", "bad\0.png", self.root,
                     self.root / "missing.png", disguised, empty, wave):
            with self.subTest(path=repr(path)), self.assertRaises(ValueError):
                validate_image_path(path)

    def test_size_limit_is_enforced_at_twenty_mib(self):
        path = self.root / "large.png"
        with path.open("wb") as output:
            output.write(PNG_HEADER)
            output.truncate(MAX_IMAGE_BYTES)
        self.assertEqual(validate_image_path(path), path.resolve())
        with path.open("ab") as output:
            output.write(b"\0")
        with self.assertRaises(ValueError):
            validate_image_path(path)


class ImageSendTests(unittest.TestCase):
    def setUp(self):
        self.root = make_temporary(self, "image-send-")
        self.image = self.root / "示例图片.png"
        self.image.write_bytes(PNG_HEADER)
        self.backend = ImageBackend()
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)
        self.ctx = PluginContext("images", {}, self.service, {"image.send"})

    def test_plugin_and_legacy_apis_send_absolute_path_without_text_pacing(self):
        self.service.text_delay = Mock()
        self.ctx.send_image(FRIEND, self.image)
        self.ctx.reply_image(Message("", ROOM, session_id="original"), self.image)
        LegacyService(self.service, "original").send_image("filehelper", str(self.image))
        self.assertEqual(self.backend.sent, [(FRIEND, str(self.image.resolve())),
                                            (ROOM, str(self.image.resolve())),
                                            ("filehelper", str(self.image.resolve()))])
        self.service.text_delay.seconds_for.assert_not_called()

    def test_stale_reply_and_legacy_session_cannot_send_to_replacement_backend(self):
        old_message = Message("", FRIEND, session_id="original")
        legacy = LegacyService(self.service, "original")
        self.service.close()
        replacement = ImageBackend("replacement")
        self.service.bind(replacement)
        for send in (lambda: self.ctx.reply_image(old_message, self.image),
                     lambda: legacy.send_image(FRIEND, self.image)):
            with self.assertRaises(RuntimeError):
                send()
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(replacement.sent, [])

    def test_missing_backend_capability_is_reported_without_send(self):
        self.backend.send_image = None
        with self.assertRaises(CapabilityError):
            self.ctx.send_image(FRIEND, self.image)
        self.assertEqual(self.backend.sent, [])

    def test_invalid_target_or_file_never_reaches_backend(self):
        for chat, path in (("非法微信目标", self.image), (FRIEND, self.image.parent),
                           (FRIEND, self.image.with_name("missing.png"))):
            with self.subTest(chat=chat, path=str(path)), self.assertRaises(ValueError):
                self.service.send_image(chat, path)
        self.assertEqual(self.backend.sent, [])

    def test_cancelled_or_disconnected_service_rejects_new_images(self):
        self.service.cancel_pending()
        with self.assertRaises(RuntimeError):
            self.service.send_image(FRIEND, self.image)
        self.assertEqual(self.backend.sent, [])
        self.service.close()
        with self.assertRaises(RuntimeError):
            self.service.send_image(FRIEND, self.image)

    def test_delivery_guard_runs_before_native_send_and_can_cancel_it(self):
        entered = []

        @contextmanager
        def guard():
            entered.append(True)
            raise RuntimeError("当前图片回复已过期")
            yield

        with self.assertRaises(RuntimeError):
            self.ctx.reply_image(Message("", FRIEND, session_id="original"), self.image,
                                 delivery_guard=guard)
        self.assertEqual(entered, [True])
        self.assertEqual(self.backend.sent, [])

    def test_queued_image_is_cancelled_when_connection_closes(self):
        self._check_queued_image(reconnect=False)

    def test_queued_image_cannot_move_to_new_connection_without_session_argument(self):
        self._check_queued_image(reconnect=True)

    def test_file_removed_while_queued_does_not_reach_backend(self):
        gate = WaitingSendLock()
        self.service._send_lock = gate
        gate.lock.acquire()
        errors = []

        def send():
            try:
                self.service.send_image(FRIEND, self.image)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        try:
            self.assertTrue(gate.waiting.wait(2))
            self.image.unlink()
        finally:
            gate.lock.release()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertEqual(self.backend.sent, [])

    def _check_queued_image(self, *, reconnect):
        gate = WaitingSendLock()
        self.service._send_lock = gate
        gate.lock.acquire()
        errors = []

        def send():
            try:
                self.service.send_image(FRIEND, self.image)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        replacement = ImageBackend("replacement")
        try:
            self.assertTrue(gate.waiting.wait(2))
            self.assertTrue(self.service.connected)
            self.service.close()
            if reconnect:
                self.service.bind(replacement)
        finally:
            gate.lock.release()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(replacement.sent, [])
        if reconnect:
            self.service.send_image(FRIEND, self.image)
            self.assertEqual(replacement.sent, [(FRIEND, str(self.image.resolve()))])


if __name__ == "__main__":
    unittest.main()
