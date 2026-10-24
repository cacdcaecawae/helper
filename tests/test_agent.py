"""智能体循环、画图客户端和过程推送；假模型与假服务，不连接微信或真实接口。"""
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from plugins.agent_chat import Plugin
from wxbot_framework.agent import MAX_OBSERVATION, Agent, ImageServer, ToolError, draw_tool, history_tool
from wxbot_framework.llm import LLMError
from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.sdk import Message


class ScriptedLLM:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def complete_json(self, system, user, *, first_object=False):
        assert first_object
        self.calls.append((system, json.loads(user)))
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
        self.plugin.members = SimpleNamespace(groups={"123@chatroom"}, account="wxid_bot")
        self.plugin.friends = frozenset({"wxid_friend"})

    def message(self, chat, text="你好", **data):
        return Message(text, chat, "wxid_x" if chat.endswith("@chatroom") else chat,
                       is_group=chat.endswith("@chatroom"), data=data)

    def test_refuses_to_load_without_saved_chat_history(self):
        ctx = SimpleNamespace(members=SimpleNamespace(history_enabled=False), config={})
        with self.assertRaisesRegex(ValueError, "history_enabled"):
            Plugin().on_load(ctx)

    def test_only_real_mentions_in_configured_groups_and_whitelisted_friends(self):
        mention = {"is_at_self": True, "at_members": ["wxid_bot"]}
        self.assertTrue(self.plugin._wanted(self.message("123@chatroom", **mention)))
        self.assertTrue(self.plugin._wanted(self.message("wxid_friend")))
        for message in (self.message("123@chatroom"), self.message("999@chatroom", **mention),
                        self.message("wxid_stranger"), self.message("wxid_friend", bot_generated=True)):
            with self.subTest(chat=message.chat_id, data=message.data):
                self.assertFalse(self.plugin._wanted(message))


class AgentPluginConversationTests(unittest.TestCase):
    """私聊多轮：第二轮能看到第一轮的问答和提问人；空行分隔的回复按条发送；记忆过长时压缩较早部分。"""

    def setUp(self):
        from wxbot_framework.personality import PersonalityState
        self.turns, self.compacted = [], []
        turns = self.turns
        self.plugin = Plugin()
        self.plugin.members = SimpleNamespace(
            agent_memory=lambda chat: ("", [turn for turn in turns if turn["chat"] == chat]),
            remember_agent_turn=lambda chat, sender, speaker, asked, answered, snapshot=None: turns.append(
                {"chat": chat, "id": len(turns) + 1, "at": "2026-10-23T06:00:00+00:00",
                 "speaker": speaker, "asked": asked, "answered": answered}),
            compact_agent_memory=lambda chat, summary, upto: self.compacted.append((chat, summary, upto)))
        self.plugin.friends, self.plugin.tools, self.plugin.image = frozenset({"wxid_friend"}), frozenset(), None
        self.sent, self.llm = [], ScriptedLLM({"reply": "推荐《星际穿越》。\n\n周末看正合适。"}, {"reply": "讲的是穿越虫洞找新家园。"})
        self.ctx = SimpleNamespace(
            llm=SimpleNamespace(provider="openai_compatible"), logger=SimpleNamespace(warning=lambda *a: None),
            personality=lambda message: PersonalityState(message.chat_id), personality_guard=lambda state: None,
            llm_for=lambda message, personality=None: self.llm, trace=lambda *a: None,
            reply=lambda message, text, delivery_guard=None: self.sent.append(text) or True)

    def test_second_turn_sees_first_turn_and_replies_are_split(self):
        data = {"sender_wechat_name": "小王"}
        self.plugin.on_message(Message("推荐个电影", "wxid_friend", "wxid_friend", data=data), self.ctx)
        self.plugin.on_message(Message("它讲什么的？", "wxid_friend", "wxid_friend", data=data), self.ctx)
        self.assertEqual(self.sent, ["推荐《星际穿越》。", "周末看正合适。", "讲的是穿越虫洞找新家园。"])
        second = self.llm.calls[1][1]
        self.assertEqual((second["scene"], second["name"], second["summary"]), ("私聊", "小王", ""))
        self.assertEqual(second["history"], [{"时间": "10-23 14:00", "提问人": "小王", "问": "推荐个电影",
                                              "你答": "推荐《星际穿越》。\n周末看正合适。"}])
        self.assertEqual(self.compacted, [])

    def test_memory_search_is_offered_only_after_compaction(self):
        self.plugin.tools = frozenset({"search_memory"})
        message = Message("你好", "wxid_friend", "wxid_friend")
        self.assertNotIn("search_memory", self.plugin._toolbox(message, self.ctx, None))
        self.assertIn("search_memory", self.plugin._toolbox(message, self.ctx, None, compacted=True))

    def test_long_memory_compacts_older_turns_and_keeps_recent_ones(self):
        from plugins.agent_chat import KEEP_CHARS, MEMORY_CHARS
        self.assertEqual(MEMORY_CHARS, 2 * KEEP_CHARS)
        # 每轮约 KEEP_CHARS/4 字，9 轮超过上限；最近 3 轮（不到 KEEP_CHARS）留原文，前 6 轮压缩。
        for index in range(9):
            self.plugin.members.remember_agent_turn("wxid_friend", "wxid_friend", "小王", f"问{index}", "长" * (KEEP_CHARS // 4))
        summarizer = SimpleNamespace(calls=[], complete_json=lambda system, user: summarizer.calls.append(json.loads(user))
                                     or {"summary": "小王问过 6 个问题"})
        self.plugin._compact("wxid_friend", SimpleNamespace(llm=summarizer))
        self.assertEqual([turn["问"] for turn in summarizer.calls[0]["turns"]], [f"问{index}" for index in range(6)])
        self.assertEqual(self.compacted, [("wxid_friend", "小王问过 6 个问题", 6)])

    def test_compaction_always_keeps_the_last_two_turns(self):
        from plugins.agent_chat import MEMORY_CHARS
        for index in range(3):  # 单轮就超过保留字数时，仍至少留最近 2 轮原文
            self.plugin.members.remember_agent_turn("wxid_friend", "wxid_friend", "小王", f"问{index}", "长" * MEMORY_CHARS)
        summarizer = SimpleNamespace(complete_json=lambda system, user: {"summary": "很长的第一轮"})
        self.plugin._compact("wxid_friend", SimpleNamespace(llm=summarizer))
        self.assertEqual(self.compacted, [("wxid_friend", "很长的第一轮", 1)])


if __name__ == "__main__":
    unittest.main()
