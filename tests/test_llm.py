"""大模型协议和错误处理验证；使用假响应，不消耗实际 API。"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from wxbot_framework.llm import LLMClient, LLMError
from plugins.idiom_chain.judge import IdiomJudge


class JSONClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return self.result


class LLMTests(unittest.TestCase):
    def setUp(self):
        self.options = {"api_key": "dummy-test-key", "model": "deepseek-flash"}
        self.client = LLMClient(self.options)

    def response(self, content, **extra):
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}, **extra}]}).encode())

    def test_official_json_request_and_utf8(self):
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{"idiom":"一心一意"}')) as call:
            self.assertEqual(self.client.complete_json("只返回 JSON", "中文"), {"idiom": "一心一意"})
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer dummy-test-key")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["messages"][1]["content"], "中文")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(call.call_args.kwargs["timeout"], 20)

    def test_compatible_gateway_preserves_version_path_without_vendor_parameters(self):
        self.client = LLMClient({**self.options, "base_url": "https://gateway.example.test/v1/", "model": "custom-model"})
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{"reply":"你好"}')) as call:
            self.assertEqual(self.client.complete_json("返回 JSON", "你好"), {"reply": "你好"})
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, "https://gateway.example.test/v1/chat/completions")
        self.assertEqual(set(json.loads(request.data)), {"model", "messages", "response_format", "max_tokens", "stream"})

    def test_full_endpoint_is_not_appended_twice(self):
        client = LLMClient({**self.options, "base_url": "https://gateway.example.test/v1/chat/completions/"})
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{}')) as call:
            client.complete_json("JSON", "测试")
        self.assertEqual(call.call_args.args[0].full_url, "https://gateway.example.test/v1/chat/completions")

    def test_completion_token_parameter_sends_only_selected_limit(self):
        client = LLMClient({**self.options, "token_limit_parameter": "max_completion_tokens", "max_tokens": 2048})
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{}')) as call:
            client.complete_json("JSON", "测试")
        body = json.loads(call.call_args.args[0].data)
        self.assertEqual(body["max_completion_tokens"], 2048)
        self.assertNotIn("max_tokens", body)

    def test_prompt_json_mode_omits_response_format_and_accepts_fenced_json(self):
        client = LLMClient({**self.options, "json_mode": False})
        for content in ('{"reply":"你好"}', '```json\n{"reply":"你好"}\n```', '```\n{"reply":"你好"}\n```'):
            with self.subTest(content=content), patch("wxbot_framework.llm.urlopen", return_value=self.response(content)) as call:
                self.assertEqual(client.complete_json("原功能规则", "测试"), {"reply": "你好"})
                body = json.loads(call.call_args.args[0].data)
                self.assertNotIn("response_format", body)
                self.assertIn("原功能规则", body["messages"][0]["content"])
                self.assertIn("JSON 对象", body["messages"][0]["content"])

    def test_first_object_mode_ignores_invented_continuations(self):
        content = '{"tool":"web_search","args":{}}{"reply":"invented"}'
        with patch("wxbot_framework.llm.urlopen", return_value=self.response(content)):
            self.assertEqual(self.client.complete_json("JSON", "x", first_object=True), {"tool": "web_search", "args": {}})
        with patch("wxbot_framework.llm.urlopen", return_value=self.response(content)), self.assertRaises(LLMError):
            self.client.complete_json("JSON", "x")

    def test_prompt_mode_still_requires_a_single_json_object(self):
        client = LLMClient({**self.options, "json_mode": False})
        for content in ('这是普通回答', '[]', '前言\n```json\n{}\n```', '```json\n{}\n```\n后记', '{} {}'):
            with self.subTest(content=content), patch("wxbot_framework.llm.urlopen", return_value=self.response(content)):
                with self.assertRaises(LLMError):
                    client.complete_json("JSON", "测试")

    def test_vendor_parameters_are_opt_in_and_copied(self):
        extra = {"thinking": {"type": "disabled"}, "temperature": 0.5}
        client = LLMClient({**self.options, "extra_body": extra})
        extra["thinking"]["type"] = "enabled"
        with patch("wxbot_framework.llm.urlopen", return_value=self.response('{}')) as call:
            client.complete_json("JSON", "测试")
        body = json.loads(call.call_args.args[0].data)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["temperature"], 0.5)

    def test_explicit_reasoning_options_override_official_defaults(self):
        for extra in ({"thinking":{"type":"enabled"}}, {"reasoning_effort":"low"}):
            with self.subTest(extra=extra):
                client = LLMClient({**self.options, "extra_body":extra})
                with patch("wxbot_framework.llm.urlopen", return_value=self.response('{}')) as call:
                    client.complete_json("JSON", "测试")
                body = json.loads(call.call_args.args[0].data)
                for key, value in extra.items():
                    self.assertEqual(body[key], value)
                if "thinking" not in extra:
                    self.assertNotIn("thinking", body)

    def test_legacy_reasoner_and_similar_host_do_not_receive_non_thinking_default(self):
        for change in ({"model":"deepseek-reasoner"}, {"base_url":"https://api.deepseek.com.example.test/v1"}):
            with self.subTest(change=change):
                client = LLMClient({**self.options, **change})
                with patch("wxbot_framework.llm.urlopen", return_value=self.response('{}')) as call:
                    client.complete_json("JSON", "测试")
                self.assertNotIn("thinking", json.loads(call.call_args.args[0].data))

    def test_official_empty_json_response_retries_once_in_prompt_mode(self):
        with patch("wxbot_framework.llm.urlopen", side_effect=[self.response(''),self.response('```json\n{"reply":"成功"}\n```')]) as call:
            self.assertEqual(self.client.complete_json("原功能规则 JSON", "原输入"), {"reply":"成功"})
        self.assertEqual(call.call_count, 2)
        first, second = [json.loads(args.args[0].data) for args in call.call_args_list]
        self.assertIn("response_format", first)
        self.assertNotIn("response_format", second)
        self.assertIn("原功能规则", second["messages"][0]["content"])
        self.assertEqual(first["messages"][1], second["messages"][1])
        self.assertEqual(first["thinking"], second["thinking"])
        self.assertTrue(self.client.json_mode)

    def test_gateway_empty_response_retry_keeps_configured_protocol(self):
        client = LLMClient({**self.options,"base_url":"https://gateway.example.test/v1"})
        with patch("wxbot_framework.llm.urlopen", side_effect=[self.response(None),self.response('{"reply":"成功"}')]) as call:
            self.assertEqual(client.complete_json("JSON", "测试"), {"reply":"成功"})
        requests = [json.loads(args.args[0].data) for args in call.call_args_list]
        self.assertEqual(requests[0], requests[1])
        self.assertNotIn("thinking", requests[0])

    def test_reasoning_only_responses_never_become_the_reply_and_retry_is_bounded(self):
        raw = {"choices":[{"finish_reason":"stop","message":{"content":"", "reasoning_content":"private-reasoning-text"}}]}
        with patch("wxbot_framework.llm.urlopen", side_effect=[io.BytesIO(json.dumps(raw).encode()),io.BytesIO(json.dumps(raw).encode())]) as call:
            with self.assertRaisesRegex(LLMError,"未取得最终答案") as caught:
                self.client.complete_json("JSON", "测试")
        self.assertEqual(call.call_count, 2)
        self.assertNotIn("private-reasoning-text", str(caught.exception))

    def test_refusal_filter_tool_call_and_truncation_do_not_retry(self):
        cases = [("content_filter",{}), ("stop",{"refusal":"private-provider-detail"}),
                 ("tool_calls",{}), ("stop",{"tool_calls":[{"id":"test"}]}), ("length",{})]
        for finish, fields in cases:
            raw = {"choices":[{"finish_reason":finish,"message":{"content":"",**fields}}]}
            with self.subTest(finish=finish,fields=fields), patch("wxbot_framework.llm.urlopen",return_value=io.BytesIO(json.dumps(raw).encode())) as call:
                with self.assertRaises(LLMError) as caught:
                    self.client.complete_json("JSON", "测试")
                self.assertEqual(call.call_count, 1)
                self.assertNotIn("private-provider-detail", str(caught.exception))

    def test_extension_cannot_override_protocol_or_authentication_fields(self):
        for key in ("model", "messages", "stream", "response_format", "max_tokens", "max_completion_tokens", "api_key", "Authorization"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "不能覆盖"):
                LLMClient({**self.options, "extra_body": {key: "forbidden"}})

    def test_invalid_extensions_and_compatibility_options(self):
        for change in ({"json_mode": "false"}, {"token_limit_parameter": "max_output_tokens"},
                       {"extra_body": []}, {"extra_body": None}, {"extra_body": {1: True}},
                       {"extra_body": {"temperature": float("nan")}},
                       {"extra_body": {"value": object()}}, {"extra_body": {"value": "a" * 16384}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                LLMClient({**self.options, **change})

    def test_local_http_is_allowed_only_for_loopback_hosts(self):
        for url in ("http://localhost:11434/v1", "http://127.0.0.1:1234/v1", "http://[::1]:8080/v1"):
            with self.subTest(url=url):
                self.assertEqual(LLMClient({**self.options, "base_url": url}).endpoint, url + "/chat/completions")
        for url in ("http://192.168.1.2:8000/v1", "http://localhost.example.test/v1", "https://host:bad/v1",
                    "https://host:0/v1", "https://host/v1?token=private", "https://host/v1#fragment", "https://host/\n/v1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                LLMClient({**self.options, "base_url": url})

    def test_http_errors_are_not_retried_or_returned_with_private_payloads(self):
        error = HTTPError("https://example.test", 400, "private prompt or credential", {}, None)
        with patch("wxbot_framework.llm.urlopen", side_effect=error) as call:
            with self.assertRaises(LLMError) as caught:
                self.client.complete_json("private prompt", "private content")
        self.assertEqual(call.call_count, 1)
        self.assertNotIn("private", str(caught.exception))

    def test_compatible_model_list_uses_standard_models_endpoint(self):
        client = LLMClient({**self.options, "base_url": "https://gateway.example.test/v1/chat/completions"})
        body = io.BytesIO(json.dumps({"data": [{"id": "b-model"}, {"id": "a-model"}, {"id": 3}]}).encode())
        with patch("wxbot_framework.llm.urlopen", return_value=body) as call:
            self.assertEqual(client.list_models(), ["a-model", "b-model"])
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, "https://gateway.example.test/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer dummy-test-key")
        for response in (io.BytesIO(b'{"object":"list"}'), HTTPError("u", 404, "dummy-test-key", {}, None)):
            with self.subTest(response=response), patch("wxbot_framework.llm.urlopen",
                    **({"side_effect": response} if isinstance(response, Exception) else {"return_value": response})):
                with self.assertRaises(LLMError) as caught:
                    client.list_models()
                self.assertNotIn("dummy-test-key", str(caught.exception))

    def test_config_from_utf8_bom_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "llm.json"
            path.write_text(json.dumps(self.options), encoding="utf-8-sig")
            self.assertEqual(LLMClient.from_file(path).model, "deepseek-flash")

    def test_invalid_configuration(self):
        for change in ({"api_key": ""}, {"model": 1}, {"base_url": None},
                       {"base_url": "http://example.com"}, {"base_url": "https://user:pass@example.com"},
                       {"timeout_seconds": True}, {"timeout_seconds": float("nan")},
                       {"max_tokens": 1}, {"max_tokens": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                LLMClient({**self.options, **change})

    def test_server_and_connection_errors_hide_sensitive_details(self):
        for error in (HTTPError("https://example.com", 401, "dummy-test-key", {}, None),
                      URLError("dummy-test-key"), TimeoutError("dummy-test-key")):
            with self.subTest(error=type(error).__name__), patch("wxbot_framework.llm.urlopen", side_effect=error):
                with self.assertRaises(LLMError) as caught:
                    self.client.complete_json("JSON", "测试")
                self.assertNotIn("dummy-test-key", str(caught.exception))

    def test_malformed_or_truncated_model_results(self):
        for content in ("", "not json", "[]", None):
            with self.subTest(content=content), patch("wxbot_framework.llm.urlopen", return_value=self.response(content)):
                with self.assertRaises(LLMError):
                    self.client.complete_json("JSON", "测试")
        with patch("wxbot_framework.llm.urlopen", return_value=self.response("{}", finish_reason="length")):
            with self.assertRaisesRegex(LLMError, "截断"):
                self.client.complete_json("JSON", "测试")
        with patch("wxbot_framework.llm.urlopen", return_value=io.BytesIO(b'{"choices":[null]}')):
            with self.assertRaises(LLMError):
                self.client.complete_json("JSON", "测试")


class ClaudeCodeTests(unittest.TestCase):
    """claude -p 用模拟的命令输出测试，不真正启动 Claude Code、不消耗额度。"""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.client = LLMClient({"provider": "claude_code", "claude_config_dir": self.home.name})
        which = patch("wxbot_framework.llm.shutil.which", return_value="C:/fake/claude.exe")
        which.start()
        self.addCleanup(which.stop)

    def run_with(self, result, call, *args, **kwargs):
        import subprocess
        output = subprocess.CompletedProcess([], 0, json.dumps(result).encode("utf-8"), b"")
        with patch("wxbot_framework.llm.subprocess.run", return_value=output) as run:
            value = call(*args, **kwargs)
        return value, run.call_args

    def test_runs_claude_print_isolated_with_your_own_login(self):
        value, call = self.run_with({"type": "result", "is_error": False, "result": '```json\n{"reply":"你好"}\n```'},
                                    self.client.complete_json, "只返回 JSON", "中文正文")
        self.assertEqual(value, {"reply": "你好"})
        command = call.args[0]
        self.assertEqual(command[:4], ["C:/fake/claude.exe", "-p", "--output-format", "json"])
        for flag, expected in (("--tools", ""), ("--setting-sources", "")):  # 关掉内置工具和个人设置（插件、钩子）
            self.assertEqual(command[command.index(flag) + 1], expected)
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--no-session-persistence", command)
        self.assertNotIn("--bare", command)  # --bare 不读订阅登录
        self.assertNotIn("--model", command)  # default：由 Claude Code 按订阅选
        self.assertTrue(command[command.index("--system-prompt") + 1].startswith("只返回 JSON"))
        self.assertEqual(call.kwargs["input"], "中文正文".encode("utf-8"))  # 正文走标准输入
        self.assertEqual(call.kwargs["env"]["CLAUDE_CONFIG_DIR"], self.home.name)
        self.assertIn("helper-claude-", call.kwargs["cwd"])  # 空的临时目录
        self.assertEqual(self.client.list_models(), ["default", "opus", "sonnet", "haiku"])
        sonnet = LLMClient({"provider": "claude_code", "claude_config_dir": self.home.name, "model": "sonnet"})
        _, call = self.run_with({"is_error": False, "result": '{"reply":"x"}'}, sonnet.complete_json, "s", "u")
        self.assertEqual(call.args[0][call.args[0].index("--model") + 1], "sonnet")

    def test_web_search_only_opens_the_web_search_tool(self):
        value, call = self.run_with({"is_error": False, "result": "北京今天晴"}, self.client.web_search, "北京天气")
        command = call.args[0]
        self.assertEqual(value, "北京今天晴")
        self.assertEqual((command[command.index("--tools") + 1], command[command.index("--allowedTools") + 1]),
                         ("WebSearch", "WebSearch"))

    def test_errors_become_llm_errors(self):
        with self.assertRaisesRegex(LLMError, "account is on hold"):
            self.run_with({"is_error": True, "result": "Your account is on hold"}, self.client.complete_json, "s", "u")
        import subprocess
        with patch("wxbot_framework.llm.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"not json", b"")):
            with self.assertRaisesRegex(LLMError, "没有返回有效结果"):
                self.client.complete_json("s", "u")
        with patch("wxbot_framework.llm.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 120)):
            with self.assertRaisesRegex(LLMError, "超时"):
                self.client.complete_json("s", "u")
        with patch("wxbot_framework.llm.shutil.which", return_value=None), self.assertRaisesRegex(LLMError, "没有找到 claude"):
            self.client.complete_json("s", "u")
        missing = LLMClient({"provider": "claude_code", "claude_config_dir": str(Path(self.home.name) / "missing")})
        with self.assertRaisesRegex(LLMError, "配置目录"):
            missing.complete_json("s", "u")
        with self.assertRaises(ValueError):
            LLMClient({"provider": "claude_code", "claude_config_dir": "relative/.claude"})


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        Path(self.home.name, "auth.json").write_text(json.dumps(
            {"tokens": {"access_token": "dummy-access", "account_id": "acct-test", "refresh_token": "private-refresh"}}))
        env = patch.dict("os.environ", {"CODEX_HOME": self.home.name})
        env.start()
        self.addCleanup(env.stop)
        self.client = LLMClient({"provider": "codex", "model": "gpt-test"})

    @staticmethod
    def stream(*events):
        return io.BytesIO(b"".join(b"event: x\ndata: " + json.dumps(e).encode() + b"\n\n" for e in events))

    def test_uses_codex_login_and_parses_sse(self):
        events = [{"type": "response.output_text.done", "text": '{"reply":"你好"}'}, {"type": "response.completed"}]
        with patch("wxbot_framework.llm.urlopen", return_value=self.stream(*events)) as call:
            self.assertEqual(self.client.complete_json("只返回 JSON", "中文"), {"reply": "你好"})
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, "https://chatgpt.com/backend-api/codex/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer dummy-access")
        self.assertEqual(request.get_header("Chatgpt-account-id"), "acct-test")
        body = json.loads(request.data)
        self.assertEqual(body["input"][0]["role"], "developer")
        self.assertEqual(body["input"][1]["content"][0]["text"], "中文")
        self.assertEqual(body["text"], {"format": {"type": "json_object"}})
        self.assertEqual((body["model"], body["reasoning"]), ("gpt-test", {"effort": "medium"}))
        self.assertEqual(LLMClient({"provider": "codex"}).model, "gpt-6.1-sol")
        self.assertNotIn("private-refresh", request.data.decode())

    def test_image_generation_returns_png_bytes(self):
        events = [{"type": "response.output_item.done", "item": {"type": "image_generation_call", "result": "UE5HREFUQQ=="}},
                  {"type": "response.completed"}]
        with patch("wxbot_framework.llm.urlopen", return_value=self.stream(*events)) as call:
            self.assertEqual(self.client.generate_image("橘猫"), b"PNGDATA")
        body = json.loads(call.call_args.args[0].data)
        self.assertEqual(body["tools"], [{"type": "image_generation"}])
        self.assertEqual(call.call_args.kwargs["timeout"], 120)
        with patch("wxbot_framework.llm.urlopen", return_value=self.stream({"type": "response.completed"})):
            with self.assertRaisesRegex(LLMError, "没有返回图片"):
                self.client.generate_image("橘猫")
        with self.assertRaisesRegex(LLMError, "Codex"):
            LLMClient({"api_key": "k", "model": "m"}).generate_image("橘猫")

    def test_image_edit_sends_the_original_as_input_image(self):
        events = [{"type": "response.output_item.done", "item": {"type": "image_generation_call", "result": "UE5HREFUQQ=="}},
                  {"type": "response.completed"}]
        jpeg = b"\xff\xd8\xff\xe0" + b"photo"
        with patch("wxbot_framework.llm.urlopen", return_value=self.stream(*events)) as call:
            self.assertEqual(self.client.generate_image("背景换成海边", jpeg), b"PNGDATA")
        content = json.loads(call.call_args.args[0].data)["input"][0]["content"]
        self.assertEqual(content[0], {"type": "input_text", "text": "背景换成海边"})
        self.assertEqual(content[1]["type"], "input_image")
        self.assertTrue(content[1]["image_url"].startswith("data:image/jpeg;base64,"))
        with self.assertRaisesRegex(LLMError, "格式不支持"):  # 按文件头认格式，不是图片就不上传
            self.client.generate_image("改", b"not an image")

    def test_model_list_comes_from_codex_cache_without_hidden_models(self):
        Path(self.home.name, "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-big", "visibility": "list"}, {"slug": "internal", "visibility": "hide"},
            {"slug": "gpt-small", "visibility": "list"}]}))
        with patch("wxbot_framework.llm.urlopen") as call:
            self.assertEqual(self.client.list_models(), ["gpt-big", "gpt-small"])
        call.assert_not_called()
        Path(self.home.name, "models_cache.json").unlink()
        with self.assertRaisesRegex(LLMError, "运行一次 codex"):
            self.client.list_models()

    def test_missing_login_truncation_and_auth_errors(self):
        with patch("wxbot_framework.llm.urlopen", return_value=self.stream({"type": "response.incomplete"})):
            with self.assertRaisesRegex(LLMError, "截断"):
                self.client.complete_json("JSON", "测试")
        with patch("wxbot_framework.llm.urlopen", side_effect=HTTPError("u", 401, "dummy-access", {}, None)):
            with self.assertRaisesRegex(LLMError, "codex") as caught:
                self.client.complete_json("JSON", "测试")
        self.assertNotIn("dummy-access", str(caught.exception))
        Path(self.home.name, "auth.json").unlink()
        with patch("wxbot_framework.llm.urlopen") as call, self.assertRaisesRegex(LLMError, "codex login"):
            self.client.complete_json("JSON", "测试")
        call.assert_not_called()


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.client = JSONClient({"is_idiom": True, "reason": "常见成语", "next_idiom": "发扬光大"})
        self.judge = IdiomJudge(self.client)

    def test_local_rejections_do_not_call_model(self):
        for previous, word, used in (("一心一意", "不是四字成语", ()),
                                     ("一心一意", "异想天开", ()),
                                     ("一心一意", "意气风发", ("意气风发",))):
            self.assertFalse(self.judge.evaluate(previous, word, used).accepted)
        self.assertEqual(self.client.calls, [])

    def test_success_sends_only_game_words(self):
        result = self.judge.evaluate("一心一意", "意气风发", ("一心一意",))
        self.assertTrue(result.accepted)
        self.assertEqual(result.next_idiom, "发扬光大")
        self.assertEqual(json.loads(self.client.calls[0][1]),
                         {"previous": "一心一意", "candidate": "意气风发", "used": ["一心一意", "意气风发"]})

    def test_model_can_reject_fake_four_character_word(self):
        self.client.result = {"is_idiom": False, "reason": "不是成语", "next_idiom": ""}
        result = self.judge.evaluate("一心一意", "意意意意", ("一心一意",))
        self.assertFalse(result.accepted)

    def test_invalid_model_next_word_never_advances(self):
        for word in ("假的", "一心一意", "发扬光大", "意气风发"):
            self.client.result["next_idiom"] = word
            with self.subTest(word=word), self.assertRaises(LLMError):
                self.judge.evaluate("一心一意", "意气风发", ("一心一意", "发扬光大"))

    def test_model_boolean_is_strict(self):
        self.client.result["is_idiom"] = "true"
        with self.assertRaises(LLMError):
            self.judge.evaluate("一心一意", "意气风发", ())

    def test_opening_shape(self):
        self.client.result = {"idiom": "一心一意"}
        self.assertEqual(self.judge.opening(), "一心一意")
        self.client.result = {"idiom": "假词"}
        with self.assertRaises(LLMError):
            self.judge.opening()


class RedirectTests(unittest.TestCase):
    """带密钥的请求遇到 302 不跟随，Authorization 不会被带到另一台服务器。用两个本机服务模拟，不连外网。"""

    def test_keyed_requests_do_not_follow_redirects(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.request import ProxyHandler, build_opener
        import wxbot_framework.llm as llm
        stolen = []

        def server(handle):
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.rfile.read(int(self.headers.get("Content-Length") or 0))
                    handle(self)
                do_POST = do_GET

                def log_message(self, *args):
                    pass
            instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=instance.serve_forever, daemon=True).start()
            self.addCleanup(instance.server_close)
            self.addCleanup(instance.shutdown)
            return instance

        def steal(handler):
            stolen.append(handler.headers.get("Authorization"))
            handler.send_response(200)
            handler.end_headers()
        target = server(steal)

        def redirect(handler):
            handler.send_response(302)
            handler.send_header("Location", f"http://127.0.0.1:{target.server_port}/steal")
            handler.end_headers()
        source = server(redirect)
        # 模块里的请求器装了 _NoRedirect；测试另建一个同样的请求器并绕过代理，在有系统代理的机器上也稳定。
        self.assertTrue(any(isinstance(handler, llm._NoRedirect) for handler in llm.urlopen.__self__.handlers))
        client = LLMClient({"api_key": "secret-key", "model": "m", "base_url": f"http://127.0.0.1:{source.server_port}/v1"})
        with patch("wxbot_framework.llm.urlopen", build_opener(ProxyHandler({}), llm._NoRedirect()).open):
            with self.assertRaisesRegex(LLMError, "HTTP 302"):
                client.complete_json("只回复 JSON", "你好")
            with self.assertRaisesRegex(LLMError, "HTTP 302"):
                client.list_models()
        self.assertEqual(stolen, [])
