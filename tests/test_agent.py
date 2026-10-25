"""智能体循环、画图客户端和过程推送；假模型与假服务，不连接微信或真实接口。"""
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from plugins.agent_chat import Plugin
from wxbot_framework.agent import MAX_OBSERVATION, Agent, ImageServer, ToolError, draw_tool, edit_tool, history_tool, view_tool
from wxbot_framework.llm import LLMError
from wxbot_framework.members import MemberError, SnapshotExpired
from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.sdk import Message


class ScriptedLLM:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def complete_json(self, system, user, *, first_object=False, images=()):
        assert first_object
        self.calls.append((system, json.loads(user), tuple(images)))
        return self.results.pop(0)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.trace = []
        self.record = lambda kind, text="": self.trace.append((kind, text))

    def test_tool_result_is_fed_back_and_every_step_is_traced(self):
        llm = ScriptedLLM({"thought": "要查记录", "tool": "lookup", "args": {"q": "周六"}},
                          {"thought": "查到了", "reply": "周六晚上八点。"})
        tools = {"lookup": ("查记录", lambda args: "小王：周六晚上八点 " + args["q"])}
        self.assertEqual(Agent(llm, tools, self.record).run({"current": "几点？"}), "周六晚上八点。")
        self.assertIn("- lookup 查记录", llm.calls[0][0])
        self.assertEqual(llm.calls[1][1]["steps"], [{"tool": "lookup", "args": {"q": "周六"}, "result": "小王：周六晚上八点 周六"}])
        self.assertEqual([kind for kind, _ in self.trace], ["thought", "tool", "result", "thought"])

    def test_persona_goes_into_the_system_prompt_only_when_set(self):
        llm = ScriptedLLM({"reply": "嗯"}, {"reply": "嗯"})
        Agent(llm, {}, self.record, "二十岁，爱猫").run({"current": "在吗"})
        Agent(llm, {}, self.record).run({"current": "在吗"})
        self.assertIn("你的人设", llm.calls[0][0])
        self.assertTrue(llm.calls[0][0].endswith("二十岁，爱猫"))
        self.assertNotIn("你的人设", llm.calls[1][0])

    def test_unknown_tools_and_failures_become_observations_not_crashes(self):
        def broken(args):
            raise ToolError("服务未启动")
        llm = ScriptedLLM({"tool": "missing", "args": {}}, {"tool": "broken", "args": {}}, {"tool": [], "args": {}},
                          {"reply": "画图服务没开。"})
        self.assertEqual(Agent(llm, {"broken": ("坏工具", broken)}, self.record).run({"current": "画猫"}), "画图服务没开。")
        results = [step["result"] for step in llm.calls[3][1]["steps"]]
        self.assertIn("不存在", results[0])
        self.assertEqual(results[1], "失败：服务未启动")
        self.assertIn("不存在", results[2])  # 工具名不是字符串（不可哈希）也只是一条观察，不会抛 TypeError

    def test_history_tool_treats_blank_arguments_as_missing(self):
        seen = []
        members = SimpleNamespace(search=lambda room, keyword, date=None, name=None: seen.append((keyword, date, name)) or [])
        self.assertIn("没有符合条件", history_tool(members, "123@chatroom")[1]({"keyword": "", "date": "2026-10-20", "name": ""}))
        self.assertEqual(seen, [(None, "2026-10-20", None)])

    def test_history_tool_keeps_newest_rows_and_hint_within_the_limit(self):
        rows = [{"received_at": "2026-10-20T04:00:00+00:00", "name": "群友", "text": f"第{i}条" + "长" * 190} for i in range(19)]
        rows.append({"received_at": "2026-10-20T05:00:00+00:00", "name": "小王", "text": "最后决定取消爬山"})
        members = SimpleNamespace(search=lambda room, keyword, date=None, name=None: rows)
        result = history_tool(members, "123@chatroom")[1]({"date": "2026-10-20"})
        self.assertLessEqual(len(result), MAX_OBSERVATION)  # Agent 不会再截掉尾部
        self.assertIn("最后决定取消爬山", result)
        self.assertTrue(result.startswith("（更早的") and result.endswith("缩小范围）"))

    def test_a_viewed_image_rides_along_in_every_later_step(self):
        llm = ScriptedLLM({"tool": "view_image", "args": {"id": 7}}, {"tool": "lookup", "args": {}}, {"reply": "好胖的猫"})
        tools = {"view_image": ("看图", lambda args: ("#7 是小王发的图片", b"JPEG")), "lookup": ("查", lambda args: "x")}
        self.assertEqual(Agent(llm, tools, self.record).run({"current": "[图片#7]"}), "好胖的猫")
        self.assertEqual([call[2] for call in llm.calls], [(), (b"JPEG",), (b"JPEG",)])  # 看过之后每一步都带着
        self.assertEqual(llm.calls[1][1]["steps"][0]["result"], "#7 是小王发的图片，已附在请求里（第 1 张图）")

    def test_skip_is_honoured_only_when_allowed(self):
        llm = ScriptedLLM({"skip": True}, {"skip": True}, {"reply": "在呢"})
        self.assertIsNone(Agent(llm, {}, self.record).run({"current": "好的"}, can_skip=True))
        self.assertEqual(Agent(llm, {}, self.record).run({"current": "@你 在吗"}), "在呢")  # 被艾特不能不回：提示后重答
        self.assertIn("不能不回", llm.calls[2][1]["steps"][0]["result"])
        self.assertEqual([call[1]["can_skip"] for call in llm.calls], [True, False, False])

    def test_tool_calls_are_bounded_and_must_end_with_a_valid_reply(self):
        loop = {"tool": "lookup", "args": {}}
        llm = ScriptedLLM(loop, loop, loop, {"tool": "lookup", "args": {}})
        with self.assertRaisesRegex(LLMError, "有效回复"):
            Agent(llm, {"lookup": ("", lambda args: "x")}, self.record).run({"current": "?"})
        self.assertEqual([call[1]["must_reply"] for call in llm.calls], [False, False, False, True])


class ImageServerTests(unittest.TestCase):
    @staticmethod
    def body(value):
        return io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())

    def test_draw_disables_adult_lora_polls_and_downloads(self):
        responses = [self.body({"ok": True, "prompt_id": "job-1"}), self.body({"done": False}),
                     self.body({"done": True, "images": [{"url": "/api/image?filename=a.png"}]}), self.body(b"PNGDATA")]
        with patch("wxbot_framework.agent.urlopen", side_effect=responses) as call, patch("wxbot_framework.agent.time.sleep"):
            self.assertEqual(ImageServer().generate_image("一只橘猫"), b"PNGDATA")
        submitted = json.loads(call.call_args_list[0].args[0].data)
        self.assertEqual((submitted["prompt"], submitted["use_lora"], submitted["random_seed"]), ("一只橘猫", False, False))
        self.assertEqual(call.call_args_list[3].args[0].full_url, "http://127.0.0.1:7860/api/image?filename=a.png")

    def test_remote_hosts_and_foreign_image_urls_are_refused(self):
        with self.assertRaises(ValueError):
            ImageServer("http://192.168.1.5:7860")
        responses = [self.body({"ok": True, "prompt_id": "job-1"}), self.body({"done": True, "images": [{"url": "http://evil.test/x"}]})]
        with patch("wxbot_framework.agent.urlopen", side_effect=responses), patch("wxbot_framework.agent.time.sleep"):
            with self.assertRaises(ToolError):
                ImageServer().generate_image("猫")

    def test_draw_tool_sends_the_image(self):
        sent = []
        server = SimpleNamespace(generate_image=lambda prompt: b"IMG:" + prompt.encode())
        description, run = draw_tool(server, sent.append)
        self.assertEqual(run({"prompt": " 猫 "}), "图片已生成并发送到当前会话")
        self.assertEqual(sent, ["IMG:猫".encode()])


    def test_view_and_edit_tools_take_the_image_by_number(self):
        from PIL import Image
        original = io.BytesIO()
        Image.new("RGB", (3000, 1500), "red").save(original, "PNG")

        def load(number):
            if number != 12:
                raise ToolError(f"找不到图片 #{number}")
            return original.getvalue(), "10-24 21:03 小王"
        text, data = view_tool(load)[1]({"id": "#12"})  # 模型可能填 "#12"
        self.assertEqual(text, "#12 是 10-24 21:03 小王 发的图片")
        with Image.open(io.BytesIO(data)) as shrunk:
            self.assertEqual((shrunk.format, shrunk.size), ("JPEG", (1024, 512)))  # 缩到长边 1024 再给模型看
        sent, seen = [], []
        generator = SimpleNamespace(generate_image=lambda prompt, image: seen.append((prompt, image)) or b"EDITED")
        run = edit_tool(generator, load, sent.append)[1]
        self.assertEqual(run({"id": 12, "prompt": " 背景换成海边 "}), "修改后的图片已生成并发送到当前会话")
        self.assertEqual((seen, sent), ([("背景换成海边", original.getvalue())], [b"EDITED"]))  # 改图用原图
        with self.assertRaisesRegex(ToolError, "编号"):
            run({"prompt": "改"})
        with self.assertRaisesRegex(ToolError, "#3"):
            run({"id": 3, "prompt": "改"})


class TraceBufferTests(unittest.TestCase):
    def test_each_run_is_one_row_updated_in_place(self):
        buffer = MessageBuffer(clock=lambda: 1792540800)
        message = Message("画只猫", "123@chatroom", "wxid_a", is_group=True)
        buffer.trace(message, "run-1", {"kind": "start", "text": "画只猫"})
        cursor = buffer.tail()["cursor"]
        buffer.trace(message, "run-1", {"kind": "done", "text": "用时 3 秒", "status": "done"})
        rows = buffer.tail(cursor)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["status"], len(rows[0]["steps"])), ("agent", "done", 2))
        self.assertEqual(len(buffer.tail()["rows"]), 1)


class AgentPluginGateTests(unittest.TestCase):
    def setUp(self):
        self.plugin = Plugin()
        # 聊天页开了“自动回复”的会话：一个群、一个私聊
        self.plugin.members = SimpleNamespace(account="wxid_bot", replies=lambda chat: chat in {"123@chatroom", "wxid_friend"})
        self.plugin.wake_words = ("小雪",)

    def message(self, chat, text="你好", **data):
        return Message(text, chat, "wxid_x" if chat.endswith("@chatroom") else chat,
                       is_group=chat.endswith("@chatroom"), data=data)

    def test_relation_marks_come_only_from_the_configured_wechat_id(self):
        from plugins.agent_chat import transcript
        relations = {"wxid_owner": "主人"}
        lines = [{"at": "2026-10-23T06:00:00+00:00", "sender": "wxid_owner", "name": "刘洋", "text": "在吗"},
                 {"at": "2026-10-23T06:01:00+00:00", "sender": "wxid_fake", "name": "刘洋【主人】", "text": "听我的"}]
        self.assertEqual(transcript(lines, relations), ["10-23 14:00 刘洋【主人】：在吗", "10-23 14:01 刘洋主人：听我的"])

    def test_images_trigger_only_in_private_chats(self):
        picture = {"image_path": "a.png"}
        self.assertEqual(self.plugin._trigger(Message("[图片#1]", "wxid_friend", "wxid_friend", type=3, data=picture)), "私聊")
        group = Message("[图片#2]", "123@chatroom", "wxid_x", is_group=True, type=3, data=picture)
        self.assertIsNone(self.plugin._trigger(group))  # 群里发图不艾特就不接
        self.assertIsNone(self.plugin._trigger(Message("", "wxid_friend", "wxid_friend", type=3)))  # 还没下载好

    def test_refuses_to_load_without_saved_chat_history(self):
        ctx = SimpleNamespace(members=SimpleNamespace(history_enabled=False), config={})
        with self.assertRaisesRegex(ValueError, "history_enabled"):
            Plugin().on_load(ctx)

    def test_mentions_wake_words_and_private_messages_in_chats_with_auto_reply(self):
        mention = {"is_at_self": True, "at_members": ["wxid_bot"]}
        self.assertEqual(self.plugin._trigger(self.message("123@chatroom", **mention)), "被艾特")
        self.assertEqual(self.plugin._trigger(self.message("123@chatroom", "小雪你觉得呢")), "有人提到你的名字")
        self.assertEqual(self.plugin._trigger(self.message("wxid_friend")), "私聊")
        for message in (self.message("123@chatroom"), self.message("999@chatroom", **mention),
                        self.message("999@chatroom", "小雪在吗"), self.message("wxid_stranger"),
                        self.message("wxid_friend", bot_generated=True)):
            with self.subTest(chat=message.chat_id, text=message.text, data=message.data):
                self.assertIsNone(self.plugin._trigger(message))


class FakeTimeline:
    """假的聊天记录：像原生层一样，收到的消息和机器人发出的文字都会记进去。"""

    def __init__(self):
        self.lines, self.compacted, self.summary, self.upto, self.images = [], [], "", 0, {}
        self.account, self.reply_chats, self.wipes = "wxid_bot", {"wxid_friend"}, 0

    def image(self, chat, number):
        if (chat, number) not in self.images:
            raise MemberError(f"这个会话里没有图片 #{number}")
        return self.images[(chat, number)]

    def stored_text(self, message):
        return next((line["text"] for line in self.lines if line["text"] == message.text), None)

    def remembers(self, chat):
        return chat in ("123@chatroom", "wxid_friend")

    def wechat_name(self, wxid):
        return "小雪三号" if wxid == self.account else None

    def speaker_name(self, wxid, chat):
        return "小王" if wxid == "wxid_friend" else "群友·0000"

    def replies(self, chat):
        return chat in self.reply_chats

    def epoch(self, chat):
        return self.wipes

    def recorded(self, message):
        return any(line["text"] == message.text for line in self.lines)

    def has_newer(self, message):
        return bool(self.lines) and self.lines[-1]["text"] != message.text

    def reply_guard(self, chat, epoch, inner=None):
        from contextlib import contextmanager

        @contextmanager
        def guard():
            if chat not in self.reply_chats:
                raise SnapshotExpired("自动回复已关闭")
            if epoch != self.wipes:
                raise SnapshotExpired("记忆已清空")
            yield
        return guard()

    def forget(self, chat):
        self.lines, self.summary, self.upto, self.wipes = [], "", 0, self.wipes + 1

    def add(self, name, text):
        self.lines.append({"id": len(self.lines) + 1, "at": "2026-10-23T06:00:00+00:00", "name": name, "text": text})

    def memory(self, chat, exclude=None):
        return self.summary, [line for line in self.lines if line["id"] > self.upto
                              and (exclude is None or line["text"] != exclude.text)]

    def memory_size(self, chat):
        return len(self.summary) + sum(len(line["text"]) for line in self.lines if line["id"] > self.upto)

    def compact_memory(self, chat, summary, upto, epoch):
        self.compacted.append((summary, upto))
        self.summary, self.upto = summary, upto
        return True


class AgentPluginConversationTests(unittest.TestCase):
    """记忆就是完整聊天记录：第二轮能看到第一轮双方说的话；回复按条发送；记忆过长时压缩较早部分。"""

    def setUp(self):
        from wxbot_framework.personality import PersonalityState
        self.timeline = FakeTimeline()
        self.plugin = Plugin()
        self.plugin.members = self.timeline
        wait = patch("plugins.agent_chat.PRIVATE_WAIT", 0)  # 测试里私聊不真等
        wait.start()
        self.addCleanup(wait.stop)
        self.plugin.tools, self.plugin.image, self.plugin.per_minute, self.plugin.recent = frozenset(), None, 5, {}
        self.plugin.wake_words, self.plugin.images = ("小雪",), {}
        self.traces = []
        self.sent, self.llm = [], ScriptedLLM({"reply": "推荐《星际穿越》。\n\n周末看正合适。"}, {"reply": "讲的是穿越虫洞找新家园。"})
        self.summarizer = SimpleNamespace(calls=[], complete_json=lambda system, user: self.summarizer.calls.append(
            json.loads(user)) or {"summary": "较早的聊天"}, provider="openai_compatible")

        def reply(message, text, delivery_guard=None):
            with delivery_guard():  # 和真实发送一样，发送前过一遍保护
                self.sent.append(text)
                self.timeline.add("你", text)  # 发出的文字进聊天记录
            return True
        self.ctx = SimpleNamespace(
            llm=self.summarizer, logger=SimpleNamespace(warning=lambda *a: None),
            personality=lambda message: PersonalityState(message.chat_id), personality_guard=lambda state: None,
            llm_for=lambda message, personality=None: self.llm, reply=reply,
            trace=lambda message, run, step: self.traces.append(step["text"]))

    def say(self, text, chat="wxid_friend", sender="wxid_friend", **kwargs):
        self.timeline.add("小王", text)  # 运行时先记录收到的消息，再分发给插件
        self.plugin.on_message(Message(text, chat, sender, data={"sender_wechat_name": "小王"}, **kwargs), self.ctx)

    def test_second_turn_sees_both_sides_of_the_first_and_replies_are_split(self):
        self.say("推荐个电影")
        self.say("它讲什么的？")
        self.assertEqual(self.sent, ["推荐《星际穿越》。", "周末看正合适。", "讲的是穿越虫洞找新家园。"])
        second = self.llm.calls[1][1]
        self.assertEqual((second["scene"], second["name"], second["summary"], second["current"]), ("私聊", "小王", "", "它讲什么的？"))
        self.assertEqual(second["chat"], ["10-23 14:00 小王：推荐个电影", "10-23 14:00 你：推荐《星际穿越》。",
                                          "10-23 14:00 你：周末看正合适。"])
        self.assertEqual(self.timeline.compacted, [])

    def test_private_chat_may_choose_not_to_reply(self):
        self.llm = ScriptedLLM({"thought": "话说完了", "skip": True})
        self.say("好的晚安")
        self.assertTrue(self.llm.calls[0][1]["can_skip"])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.traces[-1], "决定不回")

    def test_switching_auto_reply_off_during_generation_cancels_sending(self):
        def answer(system, user, *, first_object=False):
            self.timeline.reply_chats.discard("wxid_friend")  # 模型还在生成时，页面上关掉了自动回复
            return {"reply": "好的"}
        self.llm = SimpleNamespace(complete_json=answer)
        self.say("在吗")
        self.assertEqual(self.sent, [])
        self.assertIn("自动回复已关闭", self.traces[-1])

    def test_clearing_memory_during_generation_cancels_sending_and_nothing_is_written_back(self):
        self.say("我喜欢科幻电影")
        self.sent.clear()

        def answer(system, user, *, first_object=False):
            self.timeline.forget("wxid_friend")  # 模型已读到“喜欢科幻电影”，这时页面上清空了记忆
            return {"reply": "你喜欢科幻电影"}
        self.llm = SimpleNamespace(complete_json=answer)
        self.say("我喜欢什么")
        self.assertEqual((self.sent, self.timeline.lines), ([], []))  # 没发出去，也没写回记忆

    def test_a_message_queued_before_a_clear_is_not_answered(self):
        self.timeline.add("小王", "我喜欢科幻电影")
        self.timeline.forget("wxid_friend")  # 这条还在排队时，页面上清空了记忆
        self.plugin.on_message(Message("我喜欢科幻电影", "wxid_friend", "wxid_friend"), self.ctx)
        self.assertEqual((self.sent, self.llm.calls, self.timeline.lines), ([], [], []))

    def test_a_burst_of_private_messages_gets_one_reply_to_the_latest(self):
        self.timeline.add("小王", "在吗")
        self.timeline.add("小王", "周六有空吗")  # 处理“在吗”时，下一条已经到了
        self.plugin.on_message(Message("在吗", "wxid_friend", "wxid_friend"), self.ctx)
        self.assertEqual((self.sent, self.llm.calls), ([], []))
        self.plugin.on_message(Message("周六有空吗", "wxid_friend", "wxid_friend"), self.ctx)
        self.assertEqual(self.llm.calls[0][1]["chat"], ["10-23 14:00 小王：在吗"])  # 前一条在上下文里
        self.assertEqual(self.llm.calls[0][1]["current"], "周六有空吗")

    def test_private_reply_waits_five_seconds_from_receipt_counting_time_already_queued(self):
        import time
        with patch("plugins.agent_chat.PRIVATE_WAIT", 5), patch("plugins.agent_chat.time.sleep") as sleep:
            for queued in (2, 10):  # 刚收到 2 秒：再等约 3 秒；在队列里已排了 10 秒：不再等
                self.timeline.add("小王", f"排队{queued}秒")
                self.plugin.on_message(Message(f"排队{queued}秒", "wxid_friend", "wxid_friend",
                                               received_at=time.monotonic() - queued), self.ctx)
            self.say("群里的话", chat="123@chatroom", sender="wxid_x", is_group=True)  # 群聊不等
        waits = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(len(waits), 2)
        self.assertAlmostEqual(waits[0], 3, delta=0.5)
        self.assertEqual(waits[1], 0)

    def test_context_tells_who_you_are_and_why_you_speak(self):
        self.say("在吗")
        context = self.llm.calls[0][1]
        self.assertEqual((context["me"], context["trigger"]), ("小雪三号（大家也叫你：小雪）", "私聊"))
        self.assertRegex(context["now"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} 周[一二三四五六日]$")

    def test_a_private_image_gets_a_reply_and_shows_as_its_number(self):
        self.timeline.add("小王", "[图片#1]")  # 运行时先把下载好的图记进聊天记录
        self.plugin.on_message(Message("[图片#1]", "wxid_friend", "wxid_friend", type=3, data={"image_path": "a.png"}), self.ctx)
        self.assertEqual(self.llm.calls[0][1]["current"], "[图片#1]")
        self.assertEqual(self.sent[0], "推荐《星际穿越》。")

    def test_images_are_looked_up_by_number_and_edit_needs_codex(self):
        import tempfile
        from pathlib import Path
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "photo.png"
            Image.new("RGB", (8, 8), "blue").save(photo)
            self.timeline.images[("wxid_friend", 12)] = (photo, "10-24 21:03 小王")
            self.plugin.tools = frozenset({"view_image", "edit_image"})
            message = Message("背景换成海边", "wxid_friend", "wxid_friend")
            tools = self.plugin._toolbox(message, self.ctx, None)
            self.assertEqual(tools["view_image"][1]({"id": 12})[0], "#12 是 10-24 21:03 小王 发的图片")
            with self.assertRaisesRegex(ToolError, "找不到图片 #5"):
                tools["view_image"][1]({"id": 5})
            photo.unlink()  # 原图文件被删了：找不到，不抛文件系统异常
            with self.assertRaisesRegex(ToolError, "找不到图片 #12"):
                tools["view_image"][1]({"id": 12})
        self.assertNotIn("edit_image", tools)  # 本机画图不能改图
        self.ctx.llm = SimpleNamespace(provider="openai_compatible", image_provider="codex")  # 聊天用别家，画图用 Codex
        self.assertIn("edit_image", self.plugin._toolbox(message, self.ctx, None))

    def test_rate_limit_skips_replies_beyond_the_per_minute_cap(self):
        self.plugin.per_minute = 2
        self.llm = ScriptedLLM({"reply": "一"}, {"reply": "二"})
        for text in ("1", "2", "3"):
            self.say(text)
        self.assertEqual(self.sent, ["一", "二"])  # 第三条没调用模型
        self.assertIn("防刷屏", self.traces[-1])

    def test_search_is_offered_in_private_chats_too(self):
        self.plugin.tools = frozenset({"search_history"})
        self.assertIn("search_history", self.plugin._toolbox(Message("你好", "wxid_friend", "wxid_friend"), self.ctx, None))

    def test_long_memory_compacts_older_lines_and_keeps_recent_ones(self):
        from plugins.agent_chat import KEEP_CHARS, MEMORY_CHARS
        self.assertEqual(MEMORY_CHARS, 2 * KEEP_CHARS)
        # 每条 KEEP_CHARS/4 字，9 条超过上限；最近 4 条正好 KEEP_CHARS 留原文，前 5 条压缩。
        for index in range(9):
            self.timeline.add("小王", str(index) + "长" * (KEEP_CHARS // 4 - 1))
        self.plugin._compact("wxid_friend", self.ctx)
        self.assertEqual([line.split("：")[1][0] for line in self.summarizer.calls[0]["chat"]], list("01234"))
        self.assertEqual(self.timeline.compacted, [("较早的聊天", 5)])

    def test_compaction_always_keeps_the_last_two_lines(self):
        from plugins.agent_chat import MEMORY_CHARS
        for index in range(3):  # 单条就超过保留字数时，仍至少留最近 2 条原文
            self.timeline.add("小王", "长" * MEMORY_CHARS)
        self.plugin._compact("wxid_friend", self.ctx)
        self.assertEqual(self.timeline.compacted, [("较早的聊天", 1)])

    def test_a_large_backlog_is_compacted_in_batches_that_fit_the_window(self):
        from plugins.agent_chat import MEMORY_CHARS
        for index in range(30):  # 插件关着期间攒下 7.5 倍上限
            self.timeline.add("小王", "长" * (MEMORY_CHARS // 4))
        self.plugin._compact("wxid_friend", self.ctx)
        self.assertGreater(len(self.summarizer.calls), 1)
        self.assertTrue(all(sum(len(line) for line in call["chat"]) <= MEMORY_CHARS + 100 for call in self.summarizer.calls))
        self.assertLessEqual(self.timeline.memory_size("wxid_friend"), MEMORY_CHARS)

    def test_reply_context_stays_within_the_limit_when_compaction_fails(self):
        from plugins.agent_chat import MEMORY_CHARS

        def broken(system, user):
            raise LLMError("压缩失败")
        self.ctx.llm = SimpleNamespace(complete_json=broken, provider="openai_compatible")
        for index in range(6):
            self.timeline.add("小王", "长" * (MEMORY_CHARS // 4))
        self.say("还记得吗")
        self.assertLessEqual(sum(len(line) for line in self.llm.calls[0][1]["chat"]), MEMORY_CHARS + 100)

    def test_plain_group_chatter_also_triggers_compaction(self):
        from plugins.agent_chat import MEMORY_CHARS
        for index in range(3):
            self.timeline.add("群友", "长" * MEMORY_CHARS)
        self.say("没艾特机器人的闲聊", chat="123@chatroom", sender="wxid_x", is_group=True)
        self.assertEqual(self.sent, [])  # 不回复，但记忆照样整理
        self.assertTrue(self.timeline.compacted)

if __name__ == "__main__":
    unittest.main()
