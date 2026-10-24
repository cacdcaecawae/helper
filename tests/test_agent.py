"""智能体循环、画图客户端和过程推送；假模型与假服务，不连接微信或真实接口。"""
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from plugins.agent_chat import Plugin
from wxbot_framework.agent import Agent, ImageServer, ToolError, draw_tool
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
        llm = ScriptedLLM({"tool": "missing", "args": {}}, {"tool": "broken", "args": {}}, {"reply": "画图服务没开。"})
        self.assertEqual(Agent(llm, {"broken": ("坏工具", broken)}, self.record).run({"current": "画猫"}), "画图服务没开。")
        results = [step["result"] for step in llm.calls[2][1]["steps"]]
        self.assertIn("不存在", results[0])
        self.assertEqual(results[1], "失败：服务未启动")

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

    def test_only_real_mentions_in_configured_groups_and_whitelisted_friends(self):
        mention = {"is_at_self": True, "at_members": ["wxid_bot"]}
        self.assertTrue(self.plugin._wanted(self.message("123@chatroom", **mention)))
        self.assertTrue(self.plugin._wanted(self.message("wxid_friend")))
        for message in (self.message("123@chatroom"), self.message("999@chatroom", **mention),
                        self.message("wxid_stranger"), self.message("wxid_friend", bot_generated=True)):
            with self.subTest(chat=message.chat_id, data=message.data):
                self.assertFalse(self.plugin._wanted(message))


if __name__ == "__main__":
    unittest.main()
