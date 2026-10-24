"""智能体对话：群里真实艾特机器人，或白名单好友私聊时，按需调用工具后回复。

开启后代替“艾特对话”；每一步思考和工具调用都推送到管理台聊天页。
插件选项：private_friends 私聊白名单（微信 ID 列表）；tools 可用工具；image_server 本机画图服务地址。
"""
from pathlib import Path
import tempfile
import time
import uuid

from wxbot_framework.agent import Agent, ImageServer, ToolError, draw_tool, history_tool, search_tool
from wxbot_framework.llm import LLMError
from wxbot_framework.members import SnapshotExpired
from wxbot_framework.mentions import strip_at_labels
from wxbot_framework.personality import personality_text
from wxbot_framework.speech import deliver_model_reply

TOOLS = ("draw_image", "search_history", "web_search")


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        friends, tools = ctx.config.get("private_friends", []), ctx.config.get("tools", list(TOOLS))
        if not isinstance(friends, list) or len(friends) > 1000 or not all(isinstance(f, str) and f for f in friends):
            raise ValueError("private_friends 应为微信 ID 列表")
        if not isinstance(tools, list) or not set(tools) <= set(TOOLS):
            raise ValueError("tools 只能包含 " + "、".join(TOOLS))
        self.friends, self.tools = frozenset(friends), frozenset(tools)
        self.image = ImageServer(ctx.config.get("image_server", "http://127.0.0.1:7860")) if "draw_image" in tools else None
        ctx.llm  # 启动时核对配置，不请求接口。

    def _wanted(self, message):
        if message.type != 1 or message.is_self or message.data.get("bot_generated"):
            return False
        if not message.is_group:
            return message.chat_id in self.friends
        return (message.chat_id in self.members.groups and "WXBOT_AT_CHAT_PROBE" not in message.text and
                bool(message.data.get("is_at_self")) and self.members.account in message.data.get("at_members", ()))

    def on_message(self, message, ctx):
        if not self._wanted(message):
            return
        run, started = uuid.uuid4().hex[:12], time.monotonic()

        def trace(kind, text="", status=None):
            ctx.trace(message, run, {"kind": kind, "text": text, **({"status": status} if status else {})})

        personality = ctx.personality(message)
        guard = lambda: ctx.personality_guard(personality)
        try:
            if message.is_group:
                snapshot = self.members.conversation_snapshot(message)
                guard = lambda: ctx.response_guard(snapshot, personality)
                context = {"name": snapshot.speaker_name, "current": strip_at_labels(snapshot.current),
                           "previous": [strip_at_labels(row["text"]) for row in snapshot.messages]}
            else:
                # ponytail: 私聊只带本条消息，没有历史；需要上下文时再给私聊加记录表。
                context = {"current": message.text}
            trace("start", context["current"][:300])
            reply = personality_text(Agent(ctx.llm_for(message, personality=personality),
                                           self._toolbox(message, ctx, guard), trace).run(context), personality.name)
            display = None
            if message.is_group:
                display = self.members.display_name(message.sender_id, message.chat_id) or snapshot.speaker_name
            deliver_model_reply(ctx, message, reply, display_name=display, delivery_guard=guard)
            trace("done", f"用时 {time.monotonic() - started:.1f} 秒", status="done")
        except SnapshotExpired:
            trace("error", "已取消：聊天记录或人格已变更", status="error")
        except LLMError as error:
            trace("error", str(error), status="error")
            ctx.logger.warning("智能体回复失败：%s", error)
            try:
                ctx.reply(message, "暂时无法回复，请稍后再试。", delivery_guard=guard)
            except SnapshotExpired:
                pass
        except Exception:
            trace("error", "运行出错，详情见运行日志", status="error")
            raise

    def _toolbox(self, message, ctx, guard):
        tools = {}
        codex = getattr(ctx.llm, "provider", "") == "codex"
        if "draw_image" in self.tools and (codex or self.image is not None):
            def send(data):
                with tempfile.TemporaryDirectory(prefix="agent-image-") as directory:
                    path = Path(directory) / "image.png"
                    path.write_bytes(data)
                    if not ctx.reply_image(message, path, delivery_guard=guard):
                        raise ToolError("图片发送未成功")
            tools["draw_image"] = draw_tool(ctx.llm if codex else self.image, send)
        if "search_history" in self.tools and message.is_group:
            tools["search_history"] = history_tool(self.members, message.chat_id)
        if "web_search" in self.tools and codex:
            tools["web_search"] = search_tool(ctx.llm)
        return tools
