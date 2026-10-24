"""通过配置接入 OpenAI 兼容接口或本机 Codex 登录，不记录密钥或聊天正文。"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
from ipaddress import ip_address
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    # 请求都带密钥；urllib 跟随重定向时会把 Authorization 带到新地址（可能是别的域名或明文 HTTP），一律不跟随。
    def redirect_request(self, *args, **kwargs):
        return None


urlopen = build_opener(_NoRedirect()).open


class LLMError(RuntimeError):
    pass


CODEX_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
CODEX_DEFAULT_MODEL = "gpt-6.1-sol"
CODEX_DEFAULT_REASONING = {"effort": "medium"}


def _codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


class LLMClient:
    def __init__(self, options):
        if not isinstance(options, dict):
            raise ValueError("大模型配置必须是 JSON 对象")
        self.provider = options.get("provider", "openai_compatible")
        if self.provider not in ("openai_compatible", "codex"):
            raise ValueError("provider 应为 openai_compatible 或 codex")
        base_url = options.get("base_url", "https://api.deepseek.com")
        if not isinstance(base_url, str):
            raise ValueError("大模型 base_url 必须是接口地址字符串")
        self.base_url = base_url.rstrip("/")
        try:
            parsed = urlsplit(self.base_url)
            local = parsed.hostname == "localhost"
            if not local and parsed.hostname:
                try:
                    local = ip_address(parsed.hostname).is_loopback
                except ValueError:
                    pass
            valid = (parsed.scheme == "https" or (parsed.scheme == "http" and local))
            valid = valid and parsed.hostname and parsed.port != 0
            valid = valid and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
        except ValueError:
            valid = False
        if not valid or any(ch.isspace() or ord(ch) < 32 for ch in base_url):
            raise ValueError("大模型 base_url 应为不含认证信息的 HTTPS 地址；本机回环地址可使用 HTTP")
        self.endpoint = (self.base_url if self.base_url.endswith("/chat/completions")
                         else self.base_url + "/chat/completions")
        self._deepseek_official = (parsed.scheme == "https" and parsed.hostname == "api.deepseek.com"
                                  and parsed.port in (None, 443))
        self.model = options.get("model", CODEX_DEFAULT_MODEL if self.provider == "codex" else "deepseek-flash")
        self._api_key = options.get("api_key", "")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("大模型 model 不能为空")
        if self.provider != "codex" and (not isinstance(self._api_key, str) or not self._api_key.strip()):
            raise ValueError("请在大模型配置文件中填写 api_key")
        self.timeout = options.get("timeout_seconds", 20)
        self.max_tokens = options.get("max_tokens", 512)
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 1 <= self.timeout <= 60:
            raise ValueError("timeout_seconds 应为 1–60 秒")
        if type(self.max_tokens) is not int or not 128 <= self.max_tokens <= 4096:
            raise ValueError("max_tokens 应为 128–4096 的整数")
        self.json_mode = options.get("json_mode", True)
        if type(self.json_mode) is not bool:
            raise ValueError("json_mode 应为布尔值")
        self.token_limit_parameter = options.get("token_limit_parameter", "max_tokens")
        if self.token_limit_parameter not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("token_limit_parameter 应为 max_tokens 或 max_completion_tokens")
        extra = options.get("extra_body", {})
        if not isinstance(extra, dict) or not all(isinstance(key, str) for key in extra):
            raise ValueError("extra_body 应为 JSON 对象")
        reserved = {"model", "messages", "stream", "response_format", "max_tokens", "max_completion_tokens",
                    "api_key", "authorization", "Authorization"}
        if self.provider == "codex":
            reserved |= {"input", "instructions", "store", "text", "max_output_tokens"}
        if reserved.intersection(extra):
            raise ValueError("extra_body 不能覆盖模型、消息、输出格式、令牌上限或认证字段")
        try:
            encoded = json.dumps(extra, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > 16384:
                raise ValueError
            # 隔离调用方后续修改，扩展参数只在显式配置时发送。
            self.extra_body = json.loads(encoded)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("extra_body 必须是有效 JSON，且不能超过 16 KiB") from None

    @classmethod
    def from_file(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8-sig")))

    def complete_json(self, system, user, *, first_object=False):
        json_instruction = "\n\n请只返回一个有效的 JSON 对象，不要使用 Markdown 代码围栏或附加说明。"
        if not self.json_mode:
            system += json_instruction
        payload = {"model": self.model, "messages": [{"role": "system", "content": system},
                                                       {"role": "user", "content": user}],
                   self.token_limit_parameter: self.max_tokens, "stream": False, **self.extra_body}
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        # 保留官方 DeepSeek 旧版的非思考行为；兼容网关不携带专属参数。
        # 用户显式指定的思考设置始终优先，旧的 reasoner 模型也不强制关闭。
        if (self._deepseek_official and self.model in ("deepseek-flash", "deepseek-v4-pro", "deepseek-chat")
                and not {"thinking", "reasoning_effort"}.intersection(self.extra_body)):
            payload["thinking"] = {"type": "disabled"}
        allow_fenced = not self.json_mode
        try:
            if self.provider == "codex":
                return self._parse_json(self._codex_text(system, user), allow_fenced, first_object)
            for attempt in range(2):
                request = Request(self.endpoint,
                                  data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                  headers={"Authorization": "Bearer " + self._api_key,
                                           "Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise LLMError("大模型响应过大")
                result = json.loads(raw)
                choice = result["choices"][0]
                finish = choice.get("finish_reason")
                message = choice["message"]
                if finish == "length":
                    raise LLMError("大模型响应被截断，请增加 max_tokens")
                if finish == "content_filter" or message.get("refusal"):
                    raise LLMError("大模型接口未提供可用正文，请检查服务商的内容限制")
                if finish in ("tool_calls", "function_call") or message.get("tool_calls") or message.get("function_call"):
                    raise LLMError("大模型返回了工具调用，当前功能需要直接返回 JSON 正文")
                content = message["content"]
                if content is None or (isinstance(content, str) and not content.strip()):
                    if attempt == 0 and finish in (None, "stop"):
                        if self._deepseek_official and self.json_mode:
                            # 官方 JSON 模式存在偶发空正文：仅此情形退回提示词 JSON，仍严格解析。
                            payload.pop("response_format", None)
                            payload["messages"][0]["content"] += json_instruction
                            allow_fenced = True
                        logging.getLogger("LLM").warning("大模型返回空正文，重试一次")
                        continue
                    suffix = "（只收到思考内容，未取得最终答案）" if message.get("reasoning_content") else ""
                    raise LLMError("大模型返回了空结果" + suffix)
                if not isinstance(content, str):
                    raise LLMError("大模型正文格式不正确")
                return self._parse_json(content, allow_fenced, first_object)
        except HTTPError as error:
            # 不输出响应正文或请求头，避免服务器错误回显密钥。
            raise LLMError(f"大模型接口 HTTP {error.code}；请核对密钥、余额和模型配置") from None
        except (URLError, TimeoutError, OSError):
            raise LLMError("大模型连接失败或超时，请稍后再试") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("大模型响应格式不正确，请重试") from None

    @staticmethod
    def _parse_json(content, allow_fenced, first_object=False):
        content = content.strip()
        if allow_fenced and content.startswith("```") and content.endswith("```"):
            opening, separator, body = content.partition("\n")
            if separator and opening.strip().lower() in ("```", "```json"):
                content = body[:-3].strip()
        # first_object：模型有时在工具调用后自行续写编造的结果，只取第一个对象。
        data = json.JSONDecoder().raw_decode(content)[0] if first_object else json.loads(content)
        if not isinstance(data, dict):
            raise LLMError("大模型结果应为 JSON 对象")
        return data

    def list_models(self):
        """可选模型 ID：Codex 读本机 models_cache.json（Codex 自己维护），兼容接口请求标准 /models。"""
        if self.provider == "codex":
            try:
                models = json.loads((_codex_home() / "models_cache.json").read_text(encoding="utf-8-sig"))["models"]
                result = [item["slug"] for item in models if item.get("visibility") == "list"
                          and isinstance(item.get("slug"), str)]
                if not result:
                    raise ValueError
                return result
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                raise LLMError("未找到 Codex 模型列表，请先在本机运行一次 codex") from None
        request = Request(self.endpoint[:-len("/chat/completions")] + "/models",
                          headers={"Authorization": "Bearer " + self._api_key})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError
            result = sorted({item["id"] for item in json.loads(raw)["data"] if isinstance(item.get("id"), str)})
            if not result:
                raise ValueError
            return result
        except HTTPError as error:
            raise LLMError(f"获取模型列表失败（HTTP {error.code}），请核对接口地址和密钥") from None
        except (URLError, TimeoutError, OSError):
            raise LLMError("获取模型列表失败：连接失败或超时") from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise LLMError("该接口没有提供标准模型列表，请手动填写模型名称") from None

    def web_search(self, query):
        """用 Codex 后端自带的联网搜索回答问题，返回带来源的简短摘要；仅 Codex 接入方式可用。"""
        if self.provider != "codex":
            raise LLMError("联网搜索需要使用本机 Codex 登录")
        return self._codex_stream({"tools": [{"type": "web_search"}], "max_output_tokens": 2048, "input": [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text":
                "联网搜索后用不超过 300 字的中文回答，列出关键事实和来源网站名；搜不到就直说。"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": query}]}]})

    def generate_image(self, prompt):
        """用 Codex 后端自带的生图工具画一张图，返回 PNG 字节；仅 Codex 接入方式可用。"""
        if self.provider != "codex":
            raise LLMError("Codex 生图需要使用本机 Codex 登录")
        return self._codex_stream({"tools": [{"type": "image_generation"}], "tool_choice": {"type": "image_generation"},
                                   "input": [{"type": "message", "role": "user", "content": [
                                       {"type": "input_text", "text": prompt}]}]}, image=True)

    def _codex_text(self, system, user):
        payload = {"max_output_tokens": self.max_tokens,
                   "input": [{"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}
                             for role, text in (("developer", system), ("user", user))], **self.extra_body}
        if self.json_mode:
            payload["text"] = {"format": {"type": "json_object"}}
        return self._codex_stream(payload)

    def _codex_stream(self, payload, *, image=False):
        # ponytail: 每次调用只读 Codex 的 token、不自行刷新（刷新会轮换 refresh_token，与 Codex 抢登录）；
        # 长期不用 Codex 导致过期时，运行一次 codex 即可续期。
        try:
            tokens = json.loads((_codex_home() / "auth.json").read_text(encoding="utf-8-sig"))["tokens"]
            token, account = tokens["access_token"], tokens["account_id"]
            if not (isinstance(token, str) and token and isinstance(account, str) and account):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise LLMError("未找到 Codex 的 ChatGPT 登录信息，请先在本机运行 codex login") from None
        payload = {"model": self.model, "store": False, "stream": True, "reasoning": CODEX_DEFAULT_REASONING, **payload}
        request = Request(CODEX_ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Authorization": "Bearer " + token, "chatgpt-account-id": account,
                                   "Content-Type": "application/json", "Accept": "text/event-stream",
                                   "originator": "codex_cli_rs"}, method="POST")
        parts, size, picture = [], 0, None
        try:
            with urlopen(request, timeout=max(self.timeout, 120) if image else self.timeout) as response:
                for line in response:
                    size += len(line)
                    if size > (64 if image else 4) * 1024 * 1024:
                        raise LLMError("大模型响应过大")
                    if not line.startswith(b"data:"):
                        continue
                    event = json.loads(line[5:])
                    kind = event.get("type")
                    if kind == "response.output_text.done":
                        parts.append(event["text"])
                    elif image and kind == "response.output_item.done" and event["item"].get("type") == "image_generation_call":
                        picture = base64.b64decode(event["item"]["result"], validate=True)
                    elif kind == "response.completed" and image:
                        if not picture:
                            raise LLMError("Codex 没有返回图片，可能触发了内容限制")
                        return picture
                    elif kind == "response.completed":
                        if not "".join(parts).strip():
                            raise LLMError("大模型返回了空结果")
                        return "".join(parts)
                    elif kind == "response.incomplete":
                        raise LLMError("大模型响应被截断，请增加 max_tokens")
                    elif kind in ("response.failed", "error"):
                        raise LLMError("Codex 接口返回错误，请核对模型名称和额度")
        except HTTPError as error:
            # 不输出响应正文或请求头，避免服务器错误回显 token。
            hint = "；请在本机运行一次 codex 刷新登录" if error.code == 401 else "；请核对模型名称和 Codex 额度"
            raise LLMError(f"Codex 接口 HTTP {error.code}{hint}") from None
        except (URLError, TimeoutError, OSError):
            raise LLMError("大模型连接失败或超时，请稍后再试") from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise LLMError("大模型响应格式不正确，请重试") from None
        raise LLMError("Codex 响应提前结束，请稍后再试")
