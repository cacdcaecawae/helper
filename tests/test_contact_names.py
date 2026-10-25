"""账号/目录隔离、后台刷新、退出清理及原生服务名字回退。"""
from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from wxbot_framework import contact_names as names
from wxbot_framework.contact_database import ContactDatabaseError
from test_contact_database import SELF, PEER, KEY, sqlite_image, encrypt


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class ContactPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/"xwechat_files"
        self.path = self.root/(SELF+"_abcd")/"db_storage/contact/contact.db"
        self.path.parent.mkdir(parents=True)
        self.path.touch()

    def test_matches_only_exact_bound_account_and_supports_configured_levels(self):
        other = self.root/"wxid_other_abcd/db_storage/contact/contact.db"
        other.parent.mkdir(parents=True)
        other.touch()
        self.assertEqual(names.find_contact_database(SELF, roots=[self.root]), self.path.resolve())
        for path in (self.root, self.path.parents[2], self.path.parents[1], self.path.parent, self.path):
            self.assertEqual(names.find_contact_database(SELF, path), self.path.resolve())
        with self.assertRaises(ContactDatabaseError):
            names.find_contact_database(SELF, other.parents[2])

    def test_ambiguous_copies_are_not_selected_by_mtime(self):
        other = self.root/(SELF+"_1234")/"db_storage/contact/contact.db"
        other.parent.mkdir(parents=True)
        other.touch()
        with self.assertRaisesRegex(ContactDatabaseError, "歧义"):
            names.find_contact_database(SELF, roots=[self.root])
        self.assertEqual(names.find_contact_database(SELF, self.path.parents[2]), self.path.resolve())

    def test_prefix_collision_is_not_an_account_match(self):
        for account in (SELF[:-1], SELF+"_other"):
            with self.assertRaises(ContactDatabaseError):
                names.find_contact_database(account, roots=[self.root])


class ContactLoaderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/"contact.db"
        image = sqlite_image()
        try:
            self.path.write_bytes(encrypt(image))
        finally:
            names.wipe(image)
        self.process = Mock(base=0x100000)
        self.factory = Mock(side_effect=lambda: nullcontext(self.process))
        self.path_patch = patch.object(names, "find_contact_database", return_value=self.path)
        self.identity_patch = patch.object(names, "read_current_account", return_value=SELF)
        self.path_patch.start(); self.identity_patch.start()
        self.addCleanup(self.path_patch.stop); self.addCleanup(self.identity_patch.stop)

    def test_full_load_then_unchanged_fast_path_and_memory_cleanup(self):
        key = bytearray(KEY)
        with patch.object(names, "read_contact_key", return_value=key) as scan:
            stamp, result = names.load_contact_names(SELF, self.factory)
            self.assertEqual(result, {PEER: "合成群友"})
            self.assertFalse(any(key))
            unchanged, result = names.load_contact_names(SELF, self.factory, previous_stamp=stamp)
            self.assertEqual(unchanged, stamp)
            self.assertIsNone(result)
            self.assertEqual(scan.call_count, 1)

    def test_account_change_after_decryption_discards_result_and_wipes(self):
        key = bytearray(KEY)
        with patch.object(names, "read_contact_key", return_value=key), \
                patch.object(names, "read_current_account", side_effect=[SELF, "wxid_other"]):
            with self.assertRaisesRegex(ContactDatabaseError, "账号发生变化"):
                names.load_contact_names(SELF, self.factory)
        self.assertFalse(any(key))

    def test_changed_database_and_cancellation_fail_without_names(self):
        key = bytearray(KEY)
        with patch.object(names, "read_contact_key", return_value=key), \
                patch.object(names, "_stamp", side_effect=[("old",), ("new",)]), \
                self.assertRaisesRegex(ContactDatabaseError, "期间发生变化"):
            names.load_contact_names(SELF, self.factory)
        self.assertFalse(any(key))
        self.factory.reset_mock()
        with self.assertRaisesRegex(ContactDatabaseError, "取消"):
            names.load_contact_names(SELF, self.factory, cancelled=lambda: True)
        self.factory.assert_not_called()

    def test_wrong_initial_account_cannot_read_any_database(self):
        with patch.object(names, "read_current_account", return_value="wxid_other"), \
                patch.object(names, "read_snapshot") as snapshot, self.assertRaises(ContactDatabaseError):
            names.load_contact_names(SELF, self.factory)
        snapshot.assert_not_called()


class ContactCacheTests(unittest.TestCase):
    def test_startup_is_nonblocking_account_isolated_and_close_discards_late_result(self):
        entered, release = threading.Event(), threading.Event()
        def loader(**_):
            entered.set()
            release.wait(3)
            return ("stamp",), {PEER: "合成名字"}
        cache = names.ContactNames(SELF, loader)
        self.addCleanup(cache.close)
        self.assertTrue(entered.wait(2))
        self.assertIsNone(cache.get(PEER, account=SELF))
        release.set()
        self.assertTrue(wait_for(lambda: cache.status == "ready"))
        self.assertEqual(cache.get(PEER, account=SELF), "合成名字")
        self.assertIsNone(cache.get(PEER, account="wxid_other"))
        cache.close()
        self.assertEqual(cache.status, "stopped")
        self.assertIsNone(cache.get(PEER, account=SELF))

    def test_refresh_failure_is_redacted_and_clears_cache_then_retries(self):
        recovered = threading.Event()
        calls = []
        def loader(**options):
            calls.append(options["previous_stamp"])
            if len(calls) == 1:
                return ("first",), {PEER: "旧名字"}
            if len(calls) == 2:
                raise RuntimeError("sensitive-synthetic-error-marker")
            recovered.set()
            return ("next",), {PEER: "新名字"}
        with self.assertLogs("ContactNames", "WARNING") as captured:
            cache = names.ContactNames(SELF, loader, refresh_seconds=.02, retry_seconds=.1)
            try:
                self.assertTrue(wait_for(lambda: cache.status == "unavailable"))
                self.assertIsNone(cache.get(PEER, account=SELF))
                self.assertTrue(recovered.wait(2))
                self.assertTrue(wait_for(lambda: cache.get(PEER, account=SELF) == "新名字"))
            finally:
                cache.close()
        self.assertEqual(calls[:3], [None, ("first",), None])
        self.assertNotIn("sensitive-synthetic-error-marker", "".join(captured.output))

    def test_unchanged_refresh_retains_names_but_expired_cache_not_served(self):
        calls = []
        def loader(**options):
            calls.append(options)
            return ("same",), {PEER: "名字"} if len(calls) == 1 else None
        cache = names.ContactNames(SELF, loader, refresh_seconds=.02)
        try:
            self.assertTrue(wait_for(lambda: len(calls) >= 2))
            self.assertEqual(cache.get(PEER, account=SELF), "名字")
            with patch.object(names.time, "monotonic", return_value=cache._updated+121):
                self.assertIsNone(cache.get(PEER, account=SELF))
        finally:
            cache.close()

    def test_close_while_loading_cannot_republish(self):
        entered = threading.Event()
        def loader(**options):
            entered.set()
            while not options["cancelled"]():
                time.sleep(.005)
            return ("late",), {PEER: "不应发布"}
        cache = names.ContactNames(SELF, loader)
        self.assertTrue(entered.wait(2))
        cache.close()
        self.assertEqual(cache.status, "stopped")
        self.assertFalse(cache._thread.is_alive())
        self.assertIsNone(cache.get(PEER, account=SELF))


@unittest.skipUnless(sys.platform == "win32", "Windows 原生服务模拟")
class NativeContactNameTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"native"))
        from text_service import NativeTextService
        self.service = NativeTextService(pid=123, account=SELF)
        self.service.router.set_account(SELF)
        self.service._channel = Mock(core_base=0x100000)
        self.service._contact_names = Mock()
        self.service._contact_names.get.return_value = "库里的微信名"

    def test_sender_prefers_native_name_and_falls_back_only_for_same_account(self):
        with patch("text_service.read_contact_name", return_value="缓存名字"):
            self.assertEqual(self.service._sender_wechat_name(PEER), "缓存名字")
        self.service._contact_names.get.assert_not_called()
        with patch("text_service.read_contact_name", return_value=None), \
                patch("text_service.read_current_account", return_value=SELF):
            self.assertEqual(self.service._sender_wechat_name(PEER), "库里的微信名")
        with patch("text_service.read_contact_name", return_value=None), \
                patch("text_service.read_current_account", return_value="wxid_other"):
            self.assertIsNone(self.service._sender_wechat_name(PEER))

    def test_group_enrichment_preserves_group_nickname_role_and_id(self):
        self.service._owns_bridge = True
        rows = ({"wxid": PEER, "nickname": "群昵称", "group_nickname": "群昵称", "wechat_name": "", "role": "admin"},
                {"wxid": SELF, "nickname": "", "group_nickname": "", "wechat_name": "", "role": "owner"})
        with patch.object(self.service, "_check_account"), \
                patch("wxbot_framework.group_members.read_group_members", return_value=rows):
            actual = self.service.group_members("12345678@chatroom")
        self.assertEqual(actual[0]["nickname"], "群昵称")
        self.assertEqual(actual[0]["wechat_name"], "库里的微信名")
        self.assertEqual(actual[0]["role"], "admin")
        self.assertEqual(actual[1]["nickname"], "库里的微信名")
        self.assertEqual(actual[1]["group_nickname"], "")
        self.assertEqual(actual[1]["wxid"], SELF)
        self.assertEqual(rows[0]["wechat_name"], "")  # 不修改原始缓存快照。

    def test_login_switch_closes_and_clears_name_cache(self):
        cache = self.service._contact_names
        with patch.object(self.service, "_read_account", return_value="wxid_other"):
            with self.assertRaises(RuntimeError):
                self.service._check_account(force=True)
        cache.close.assert_called_once()
        self.assertIsNone(self.service._contact_names)


if __name__ == "__main__":
    unittest.main()
