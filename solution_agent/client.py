"""Small DeepSeek HTTP adapter; Python 3.8+, standard library only."""

import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import load_settings


class AgentError(Exception):
    pass


class DeepSeekClient:
    def __init__(self, api_key=None, model=None, timeout=120, thinking=True, reasoning_effort="high"):
        settings = load_settings()
        self.api_key = api_key or settings["DEEPSEEK_API_KEY"]
        self.model = model or settings["DEEPSEEK_MODEL"]
        if not self.api_key.strip():
            raise AgentError("未配置 DEEPSEEK_API_KEY。请在本地 .env 或环境变量中设置。")
        self.timeout = timeout
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.metrics = {"http_attempts": 0, "model_calls": 0, "usage": {}}
        self.returned_models = set()

    def post(self, path, payload):
        request = Request(
            "https://api.deepseek.com" + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"},
            method="POST",
        )
        # One bounded retry for explicit retryable responses. No automatic replay
        # on ambiguous network timeouts: a model request may already be billed.
        for attempt in range(2):
            self.metrics["http_attempts"] += 1
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if not isinstance(result, dict):
                    raise AgentError("接口未返回 JSON 对象。")
                return result
            except HTTPError as error:
                if attempt == 0 and (error.code == 429 or 500 <= error.code < 600):
                    time.sleep(1)
                    continue
                labels = {401: "Key 无效", 402: "余额不足", 403: "访问被拒绝",
                          404: "接口或网页不可用", 429: "调用频率受限"}
                # Never print response bodies or request headers; they may contain secrets.
                raise AgentError("DeepSeek HTTP {}：{}".format(
                    error.code, labels.get(error.code, "请求失败，请检查接口配置"))) from None
            except (URLError, socket.timeout, TimeoutError):
                raise AgentError("网络连接失败或超时；未自动重放请求，请查看本次运行记录。") from None
            except (ValueError, UnicodeError):
                raise AgentError("接口返回无法解析的 JSON。") from None

    def chat(self, messages, tools=None, json_output=False, stop=None):
        payload = {"model": self.model, "messages": messages,
                   "thinking": {"type": "enabled" if self.thinking else "disabled"},
                   "max_tokens": 16384 if self.thinking else 4500}
        if self.thinking:
            payload["reasoning_effort"] = self.reasoning_effort
        else:
            payload["temperature"] = 0
        if tools:
            payload["tools"] = tools
        if json_output:
            payload["response_format"] = {"type": "json_object"}
        if stop:
            payload["stop"] = stop
        self.metrics["model_calls"] += 1
        result = self.post("/chat/completions", payload)
        for key, value in result.get("usage", {}).items():
            if isinstance(value, int) and not isinstance(value, bool):
                self.metrics["usage"][key] = self.metrics["usage"].get(key, 0) + value
        if result.get("model"):
            self.returned_models.add(result["model"])
        try:
            choice = result["choices"][0]
            if choice.get("finish_reason") not in ("stop", "tool_calls"):
                raise AgentError("模型输出未完整结束（{}）。".format(choice.get("finish_reason")))
            message = choice["message"]
            if not isinstance(message, dict):
                raise AgentError("模型消息结构异常。")
            if message.get("refusal"):
                raise AgentError("模型拒绝了本次请求。")
            return message
        except (KeyError, IndexError, TypeError):
            raise AgentError("模型响应结构异常。") from None

    def search(self, query):
        return self.post("/v1/tools/search", {"query": query, "count": 5,
                                               "need_content": False})

    def fetch(self, url):
        return self.post("/v1/tools/fetch", {"url": url})
