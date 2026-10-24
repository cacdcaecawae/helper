"""成员绑定、持久记录和插件共用接口验证，不连接微信。"""
import json
import logging
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from plugins.member_identity import Plugin
from wxbot_framework.members import MemberError, MemberStore
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade
from wxbot_framework.runtime import BotApplication
from wxbot_framework.config import ConfigStore

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".runtime/tests"
ROOM = "12345678@chatroom"
ROBOT, SMALL, JING = "wxid_robot", "wxid_small", "wxid_jing"


class MemberTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.options = {"database_file": "data.sqlite3", "history_enabled": True,
                        "groups": {ROOM: [
                            {"key": "xiaomei", "name": "成员甲", "display_name": "成员甲昵称", "aliases": ["小号"], "wxid": SMALL},
                            {"key": "jing", "name": "成员乙", "display_name": "成员乙群名", "aliases": ["成员乙别名"], "wxid": None},
                            {"key": "yuting", "name": "成员丁", "display_name": "DemoMemberC", "wxid": None}]}}
        self.config_path = self.root / "members.json"
        self.write_options()
        self.stores = []
        self.store = self.create_store()

    def write_options(self):
        self.config_path.write_text(json.dumps(self.options, ensure_ascii=False), encoding="utf-8")

    def create_store(self, account=ROBOT):
        store = MemberStore(self.config_path, account)
        self.stores.append(store)
        return store

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.temporary.cleanup()

    def message(self, text="中文消息 🌸", sender=JING, sequence=1, **kwargs):
        return Message(text, ROOM, sender_id=sender, is_group=True, sequence=sequence, session_id="first", **kwargs)

    def test_memory_is_the_whole_chat_and_survives_restart_compaction_and_clear(self):
        self.store.record(self.message("周末去哪玩", sender=SMALL, sequence=1))
        self.store.record(self.message("去爬山吧", sequence=2))
        self.store.record(self.message("/总结群聊", sender=SMALL, sequence=3))  # 命令不算聊天
        self.store.record(self.message("推荐《白日梦想家》", sender=ROBOT, sequence=4, is_self=True, data={"bot_generated": True}))
        current = self.message("@小雪 你觉得呢", sender=SMALL, sequence=5)
        self.store.record(current)
        reopened = self.create_store()  # 模拟重启：新的连接读同一个库
        summary, lines = reopened.memory(ROOM, exclude=current)
        self.assertEqual((summary, [(line["name"], line["text"]) for line in lines]),
                         ("", [("成员甲昵称", "周末去哪玩"), ("群友", "去爬山吧"), ("你", "推荐《白日梦想家》")]))
        before = reopened.memory_size(ROOM)
        self.assertTrue(reopened.compact_memory(ROOM, "商量周末", lines[1]["id"]))
        summary, rest = reopened.memory(ROOM)
        self.assertEqual((summary, [line["text"] for line in rest]), ("商量周末", ["推荐《白日梦想家》", "@小雪 你觉得呢"]))
        self.assertLess(reopened.memory_size(ROOM), before)
        # 压缩掉的原文还能查到；发言人填“你”查本账号说过的话
        self.assertEqual([row["text"] for row in reopened.search(ROOM, "爬山")], ["去爬山吧"])
        self.assertEqual([row["text"] for row in reopened.search(ROOM, name="你")], ["推荐《白日梦想家》"])
        reopened.clear(ROOM)
        self.assertEqual(reopened.memory(ROOM), ("", []))
        self.assertFalse(reopened.compact_memory(ROOM, "清空后不能写回", rest[-1]["id"]))
        self.assertEqual(reopened.memory(ROOM), ("", []))

    def test_chat_switches_persist_and_control_recording(self):
        friend = "wxid_friend"
        private = lambda text, sequence, chat=friend, **kwargs: Message(text, chat, chat, session_id="first",
                                                                         sequence=sequence, **kwargs)
        self.assertFalse(self.store.record(private("默认只接收，不记录", 1)))
        self.assertEqual(self.store.chat_setting(friend), {"memory": False, "reply": False})
        # 开自动回复会一并开长期记忆；重启后仍在
        self.assertEqual(self.store.set_chat(friend, memory=False, reply=True), {"memory": True, "reply": True})
        self.assertEqual(self.create_store().chat_setting(friend), {"memory": True, "reply": True})
        # 配置里的群默认记录；聊天页关掉后以开关为准
        self.assertTrue(self.store.remembers(ROOM))
        self.store.set_chat(ROOM, memory=False, reply=False)
        reopened = self.create_store()
        reopened.initialize()
        self.assertFalse(reopened.remembers(ROOM))
        self.assertFalse(reopened.record(self.message("关了记忆就不记", sequence=9)))
        new_group = "87654321@chatroom"  # 不在配置里的群，打开开关后当场就能艾特取到当前消息
        self.store.set_chat(new_group, memory=True, reply=True)
        message = Message("@小雪 你好", new_group, sender_id=SMALL, is_group=True, sequence=10, session_id="first")
        self.assertTrue(self.store.record(message))
        self.assertEqual(self.store.conversation_snapshot(message).current, "@小雪 你好")
        for value in (None, "yes"):
            with self.subTest(value=value), self.assertRaises(MemberError):
                self.store.set_chat(friend, memory=value, reply=False)

    def test_backfill_adds_only_lines_after_the_last_record(self):
        friend = "wxid_friend"
        self.store.set_chat(friend, memory=True, reply=False)
        self.store.record(Message("已经记过的", friend, friend, session_id="first", sequence=1))
        last = datetime.fromisoformat(self.store.memory(friend)[1][-1]["at"]).timestamp()
        rows = [{"timestamp": last - 60, "content": "已经记过的", "direction": "incoming", "sender_id": friend, "sender_name": "小王"},
                {"timestamp": last + 60, "content": "周六有空吗", "direction": "incoming", "sender_id": friend, "sender_name": "小王"},
                {"timestamp": last + 120, "content": "有空", "direction": "outgoing", "sender_id": ROBOT, "sender_name": "我"}]
        self.assertEqual(self.store.backfill(friend, rows), 2)
        self.assertEqual([(line["name"], line["text"]) for line in self.store.memory(friend)[1]],
                         [("小王", "已经记过的"), ("小王", "周六有空吗"), ("你", "有空")])
        self.assertEqual(self.store.backfill(friend, rows), 0)  # 再补一次不会重复
        self.assertEqual(self.store.backfill("wxid_off", rows), 0)  # 没开长期记忆的会话不补

    def test_private_chat_and_spoken_replies_are_recorded(self):
        friend = "wxid_friend"
        private = lambda text, sequence, chat=friend, **kwargs: Message(text, chat, chat, session_id="first",
                                                                         sequence=sequence, **kwargs)
        self.store.set_chat(friend, memory=True, reply=False)
        self.assertTrue(self.store.record(private("推荐本书", 2, data={"sender_wechat_name": "小王"})))
        self.assertTrue(self.store.record_spoken(private("推荐本书", 2), "推荐《白夜行》"))  # 语音回复没有文字回传，补记
        self.assertEqual([(line["name"], line["text"]) for line in self.store.memory(friend)[1]],
                         [("小王", "推荐本书"), ("你", "推荐《白夜行》")])
        self.assertFalse(self.store.record_spoken(private("x", 3, chat="wxid_other"), "不在白名单不记"))

    def test_search_combines_day_speaker_and_keyword(self):
        clock = [datetime(2026, 10, 19, 4, 0, tzinfo=timezone.utc)]  # 北京 10-19 12:00
        store = MemberStore(self.config_path, ROBOT, now=lambda: clock[0])
        self.stores.append(store)
        store.record(self.message("周日去爬山", sender=SMALL, sequence=1))
        clock[0] += timedelta(days=1)
        store.record(self.message("今天下雨不去了", sender=SMALL, sequence=2))
        store.record(self.message("那改周六", sequence=3))
        texts = lambda **criteria: [row["text"] for row in store.search(ROOM, **criteria)]
        self.assertEqual(texts(date="2026-10-19"), ["周日去爬山"])
        self.assertEqual(texts(date="2026-10-20"), ["今天下雨不去了", "那改周六"])
        self.assertEqual(texts(name="成员甲"), ["周日去爬山", "今天下雨不去了"])
        self.assertEqual(texts(name="成员甲", date="2026-10-20", keyword="下雨"), ["今天下雨不去了"])
        for criteria in ({}, {"date": "10-20"}, {"name": "成员乙"}, {"name": "不存在的人"}):
            with self.subTest(criteria=criteria), self.assertRaises(MemberError):
                store.search(ROOM, **criteria)

    def test_search_finds_speaker_by_group_nickname(self):
        self.store.remember_group_nickname("wxid_wang", ROOM, "群里小王")
        self.store.record(self.message("周末加班", sender="wxid_wang", sequence=1))
        self.assertEqual([row["text"] for row in self.store.search(ROOM, name="群里小王")], ["周末加班"])
        self.store.remember_group_nickname("wxid_other", ROOM, "群里小王")
        with self.assertRaisesRegex(MemberError, "对应多人"):
            self.store.search(ROOM, name="群里小王")

    def test_keyword_search_returns_chat_in_order_including_own_lines(self):
        self.store.record(self.message("周六晚上开黑", sequence=1))
        self.store.record(self.message("周六见", sender=SMALL, sequence=2))
        self.store.record(self.message("周六我也来", sender=ROBOT, sequence=3, is_self=True, data={"bot_generated": True}))
        rows = self.store.search(ROOM, "周六")
        self.assertEqual([(row["name"], row["text"]) for row in rows],
                         [("群友", "周六晚上开黑"), ("成员甲昵称", "周六见"), ("你", "周六我也来")])
        for room, keyword in (("999@chatroom", "周六"), (ROOM, " "), (ROOM, "x" * 101)):
            with self.subTest(room=room, keyword=keyword), self.assertRaises(MemberError):
                self.store.search(room, keyword)

    def test_configuration_check_does_not_create_database(self):
        self.assertFalse(self.store.path.exists())
        self.store.initialize()
        self.assertTrue(self.store.path.exists())

    def test_aliases_are_resolved_case_insensitively_and_small_is_bound(self):
        self.assertEqual(self.store.resolve("小号").wxid, SMALL)
        self.assertEqual(self.store.resolve("成员乙群名", ROOM).name, "成员乙")
        self.assertEqual(self.store.resolve("demomemberc", ROOM).name, "成员丁")
        self.assertIsNone(self.store.resolve("成员乙").wxid)

    def test_unknown_names_are_not_guessed(self):
        with self.assertRaises(MemberError):
            self.store.resolve("未登记的昵称")
        with self.assertRaises(MemberError):
            self.store.bind("未登记的昵称", ROOM, JING)

    def test_binding_survives_restart_and_null_profile_does_not_clear_it(self):
        self.store.bind("成员乙群名", ROOM, JING)
        self.store.close()
        reopened = self.create_store()
        self.assertEqual(reopened.resolve("成员乙", ROOM).wxid, JING)

    def test_conflicting_binding_does_not_replace_identity(self):
        self.store.bind("成员乙", ROOM, JING)
        self.assertEqual(self.store.bind("成员乙", ROOM, JING).wxid, JING)
        with self.assertRaises(MemberError):
            self.store.bind("成员乙", ROOM, "wxid_other")
        with self.assertRaises(MemberError):
            self.store.bind("成员丁", ROOM, JING)
        self.assertEqual(self.store.resolve("成员乙", ROOM).wxid, JING)

    def test_history_before_binding_becomes_queryable_by_alias(self):
        self.store.record(self.message())
        with self.assertRaises(MemberError):
            self.store.history("成员乙")
        self.store.bind("成员乙", ROOM, JING)
        rows = self.store.history("成员乙别名")
        self.assertEqual([row["text"] for row in rows], ["中文消息 🌸"])
        self.assertEqual(rows[0]["sender_id"], JING)
        self.assertEqual(datetime.fromisoformat(rows[0]["received_at"]).utcoffset(), timedelta(0))

    def test_history_is_isolated_by_robot_account_and_author(self):
        self.store.bind("成员乙", ROOM, JING)
        self.store.record(self.message("成员乙的话"))
        self.store.record(self.message("成员甲的话", sender=SMALL, sequence=2))
        second = self.create_store("wxid_second_robot")
        second.bind("成员乙", ROOM, JING)
        self.assertEqual(second.history("成员乙"), [])
        self.assertEqual([row["text"] for row in self.store.history("成员乙")], ["成员乙的话"])

    def test_private_other_group_and_wrong_account_are_not_archived(self):
        self.assertFalse(self.store.record(Message("私聊", JING, sender_id=JING, session_id="first")))
        self.assertFalse(self.store.record(Message("另一群", "99999@chatroom", sender_id=JING, is_group=True, session_id="first")))
        self.assertFalse(self.store.record(self.message(data={"self_wxid": "wxid_other_robot"})))

    def test_sequence_deduplication_and_new_session(self):
        self.assertTrue(self.store.record(self.message()))
        self.assertFalse(self.store.record(self.message()))
        self.assertTrue(self.store.record(Message("重连消息", ROOM, JING, is_group=True, sequence=1, session_id="second")))
        self.store.bind("成员乙", ROOM, JING)
        self.assertEqual(len(self.store.history("成员乙")), 2)

    def test_limit_and_time_filter_return_chronological_records(self):
        # 固定时钟：Windows 上 now() 精度不足，since 可能和第一句拿到同一个时间戳
        clock = [datetime(2026, 10, 19, 4, 0, tzinfo=timezone.utc)]
        self.store = MemberStore(self.config_path, ROBOT, now=lambda: clock[0])
        self.stores.append(self.store)
        self.store.bind("成员乙", ROOM, JING)
        self.store.record(self.message("第一句", sequence=1))
        clock[0] += timedelta(seconds=1)
        since = clock[0]
        self.store.record(self.message("第二句", sequence=2))
        self.assertEqual([r["text"] for r in self.store.history("成员乙", limit=1)], ["第二句"])
        self.assertEqual([r["text"] for r in self.store.history("成员乙", since=since)], ["第二句"])
        self.assertEqual([r["text"] for r in self.store.history("成员乙")], ["第一句", "第二句"])
        for limit in (True, 0, 1001):
            with self.assertRaises(MemberError):
                self.store.history("成员乙", limit=limit)
        with self.assertRaises(MemberError):
            self.store.history("成员乙", since=datetime.now())

    def test_disabled_history_keeps_member_directory(self):
        self.options["history_enabled"] = False
        self.write_options()
        disabled = self.create_store()
        self.assertEqual(disabled.resolve("成员甲").wxid, SMALL)
        self.assertFalse(disabled.record(self.message()))

    def test_same_alias_in_two_groups_requires_group(self):
        second_room = "99999@chatroom"
        self.options["groups"][second_room] = self.options["groups"][ROOM]
        self.write_options()
        multi = self.create_store()
        with self.assertRaises(MemberError):
            multi.resolve("成员乙")
        self.assertEqual(multi.resolve("成员乙", second_room).room, second_room)

    def test_configuration_rejects_duplicate_casefolded_aliases(self):
        self.options["groups"][ROOM][1]["aliases"] = ["demomemberc"]
        self.write_options()
        with self.assertRaises(MemberError):
            self.create_store()

    def test_recording_and_lookup_from_different_threads(self):
        self.store.bind("成员乙", ROOM, JING)
        errors = []
        def write(index):
            try:
                self.store.record(self.message(str(index), sequence=index))
                self.store.resolve("成员乙")
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=write, args=(index,)) for index in range(1, 13)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.history("成员乙")), 12)

    def test_identity_plugin_binds_actual_message_author(self):
        ctx = Mock(members=self.store, logger=logging.getLogger("MembersTest"))
        plugin = Plugin()
        plugin.on_load(ctx)
        plugin.on_message(self.message("/绑定 成员乙"), ctx)
        self.assertEqual(self.store.resolve("成员乙").wxid, JING)
        ctx.reply.assert_called_once_with(unittest.mock.ANY, "已绑定：成员乙。")
        plugin.on_message(self.message("/绑定 成员丁"), ctx)
        self.assertIsNone(self.store.resolve("成员丁").wxid)

    def test_context_auto_mention_uses_group_name_instead_of_personal_alias(self):
        self.store.bind("成员乙", ROOM, JING)
        backend = Mock(session_id="first")
        service = ServiceFacade(text_delay={"enabled": False})
        service.bind(backend)
        try:
            ctx = PluginContext("example", {}, service, [], member_store=self.store)
            self.assertEqual(ctx.members.resolve("成员乙").wxid, JING)
            ctx.reply_at(self.message(), "回复")
            backend.send_at_text.assert_called_once_with(ROOM, "@成员乙群名\u2005\n回复", (JING,))
        finally:
            service.close()

    def test_runtime_archives_before_plugin_dispatch(self):
        (self.root / "plugins").mkdir()
        path = self.root / "bot_config.json"
        path.write_text(json.dumps({"backend": "native_text", "wechat_account": ROBOT,
                                    "members_config_file": self.config_path.name}), encoding="utf-8")
        app = BotApplication(self.root, ConfigStore(path))
        try:
            app.members.bind("成员乙", ROOM, JING)
            def verify(message):
                self.assertEqual(app.members.history("成员乙")[0]["text"], message.text)
            with patch.object(app.plugins, "dispatch", side_effect=verify):
                app._emit(self.message())
        finally:
            app.stop()

    def test_free_registration_accepts_new_name_and_links_existing_history(self):
        self.store.record(self.message("绑定前说过的话"))
        member = self.store.register("测试成员", ROOM, JING)
        self.assertEqual(member.wxid, JING)
        self.assertEqual(self.store.history("测试成员")[0]["text"], "绑定前说过的话")

    def test_same_account_can_rename_without_losing_aliases_or_history(self):
        self.store.register("测试成员", ROOM, JING)
        self.store.record(self.message("原有记录"))
        self.store.register("成员丙", ROOM, JING)
        self.assertEqual(self.store.resolve("测试成员").name, "成员丙")
        self.assertEqual(self.store.resolve("成员丙").wxid, JING)
        self.assertEqual(self.store.resolve("成员丙").display_name, "成员丙")
        self.assertEqual(self.store.history("成员丙")[0]["text"], "原有记录")
        self.assertEqual(len([m for m in self.store.list_members() if m.wxid == JING]), 1)

    def test_another_account_cannot_take_existing_nickname(self):
        self.store.register("测试成员", ROOM, JING)
        with self.assertRaises(MemberError):
            self.store.register("测试成员", ROOM, SMALL)
        self.assertEqual(self.store.resolve("测试成员").wxid, JING)

    def test_registered_name_survives_config_seeding_on_restart(self):
        self.store.register("成员乙", ROOM, JING)
        self.store.register("测试成员", ROOM, JING)
        self.store.close()
        reopened = self.create_store()
        self.assertEqual(reopened.resolve("成员乙").name, "测试成员")
        self.assertEqual(reopened.resolve("测试成员").wxid, JING)

    def test_free_registration_still_limits_groups_and_name_shape(self):
        with self.assertRaises(MemberError):
            self.store.register("测试成员", "99999@chatroom", JING)
        for name in ("", "x" * 65, "喵\n喵", "喵\0喵"):
            with self.subTest(name=name), self.assertRaises(MemberError):
                self.store.register(name, ROOM, JING)

    def test_group_binding_overrides_legacy_display_and_survives_restart(self):
        self.store.register("成员甲", ROOM, SMALL)
        self.assertEqual(self.store.display_name(SMALL, ROOM), "成员甲")
        self.store.close()
        self.store = self.create_store()
        self.assertEqual(self.store.display_name(SMALL, ROOM), "成员甲")
        self.assertTrue(self.store.resolve("成员甲", ROOM).self_named)

    def test_wechat_default_and_bindings_are_separate_in_each_group(self):
        other_room = "87654321@chatroom"
        self.options["groups"][other_room] = []
        self.write_options()
        store = self.create_store()
        store.register("成员甲", ROOM, SMALL)
        store.remember_wechat_name(SMALL, "DemoUser")
        message = Message("新群的原话", other_room, SMALL, is_group=True, session_id="new", sequence=1)
        store.record(message)
        self.assertEqual(store.display_name(SMALL, other_room), "DemoUser")
        self.assertEqual(store.list_members(other_room), [])
        self.assertEqual(store.today_snapshot("DemoUser", other_room).member.wxid, SMALL)
        backend = Mock(session_id="new")
        service = ServiceFacade(text_delay={"enabled": False})
        service.bind(backend)
        try:
            ctx = PluginContext("test", {}, service, [], member_store=store)
            ctx.reply_at(message, "默认回复")
            self.assertEqual(backend.send_at_text.call_args.args[1], "@DemoUser\u2005\n默认回复")
            store.register("新群名字", other_room, SMALL)
            ctx.reply_at(message, "已绑定", display_name="旧的硬编码名字")
            self.assertEqual(backend.send_at_text.call_args.args[1], "@新群名字\u2005\n已绑定")
            self.assertEqual(store.display_name(SMALL, ROOM), "成员甲")
            self.assertEqual(store.today_snapshot("新群名字", other_room).messages[0]["text"], "新群的原话")
        finally:
            service.close()

    def test_native_wechat_name_updates_default_without_overwriting_group_binding(self):
        other_room = "87654321@chatroom"
        self.options["groups"][other_room] = []
        self.write_options()
        store = self.create_store()
        store.register("成员甲", ROOM, SMALL)
        for sequence, name in ((1, "DemoUser"), (2, "新的微信名")):
            msg = Message("原文", other_room, SMALL, is_group=True, session_id="new", sequence=sequence,
                          data={"sender_wechat_name": name, "self_wxid": ROBOT})
            store.record(msg)
            self.assertEqual(store.display_name(SMALL, other_room), name)
        self.assertEqual(store.display_name(SMALL, ROOM), "成员甲")
        self.assertEqual(store.today_group_snapshot(other_room).messages[-1]["speaker_name"], "新的微信名")
        self.assertEqual(store.conversation_snapshot(msg).speaker_name, "新的微信名")
        store.clear(other_room)
        self.assertEqual(store.today_snapshot("新的微信名", other_room).messages, ())

    def test_duplicate_default_wechat_names_do_not_mix_member_identities(self):
        self.options["groups"][ROOM] = []
        self.write_options()
        store = self.create_store()
        for index, wxid in enumerate((SMALL, JING), 1):
            store.record(self.message("不同成员", sender=wxid, sequence=index,
                                      data={"sender_wechat_name": "同名"}))
        with self.assertRaisesRegex(MemberError, "多个成员"):
            store.today_snapshot("同名", ROOM)
        store.register("指定成员", ROOM, SMALL)
        self.assertEqual(store.today_snapshot("指定成员", ROOM).member.wxid, SMALL)

    def test_plugin_free_registration_and_usage(self):
        ctx = Mock(members=self.store, logger=logging.getLogger("MembersTest"))
        plugin = Plugin()
        plugin.on_load(ctx)
        plugin.on_message(self.message("/绑定 测试成员"), ctx)
        self.assertEqual(self.store.resolve("测试成员").wxid, JING)
        plugin.on_message(self.message("/绑定"), ctx)
        self.assertEqual(ctx.reply.call_args.args[1], "用法：/绑定 昵称")
        plugin.on_message(self.message(" /绑定\t成员丙  "), ctx)
        self.assertEqual(self.store.resolve("成员丙").wxid, JING)
