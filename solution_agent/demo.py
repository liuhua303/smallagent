"""Scripted offline fixture; deliberately fictional, never a quality evaluation."""

import json


QUESTION = "希望为客服建设企业知识库，必须本地部署，回答需附原文出处。"
CONTENT = "示例资料：演示知识台（虚构产品）支持本地部署，回答可以附上文档原文出处。本文仅用于离线流程演示，不代表任何真实产品。"


class DemoClient:
    model = "offline-scripted-fixture"
    thinking = True
    reasoning_effort = "scripted-not-a-model"

    def __init__(self):
        self.metrics = {"model_calls": 0, "http_attempts": 0, "usage": {}}
        self.returned_models = {self.model}
        self.research_step = 0

    def chat(self, messages, tools=None, json_output=False, stop=None):
        self.metrics["model_calls"] += 1
        if tools:
            self.research_step += 1
            if self.research_step > 2:
                return {"role": "assistant", "content": "示例已取证。", "tool_calls": [
                    {"id": "demo_finish", "type": "function", "function": {"name": "finish",
                     "arguments": json.dumps({"summary": "示例资料已取得，真实环境适配仍未知。"}, ensure_ascii=False)}}]}
            name = "search" if self.research_step == 1 else "fetch"
            args = {"query": "客服知识库 本地部署 原文引用"} if name == "search" else {"url": "https://example.com/fictional-demo"}
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": "demo_" + str(self.research_step), "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}
        if stop:
            self.research_step += 1
            step = self.research_step
            actions = ["Search[客服知识库 本地部署 原文引用]",
                       "Fetch[https://example.com/fictional-demo]",
                       "Finish[虚构示例资料已取得，真实方案和环境适配仍未知。]"]
            return {"role": "assistant", "reasoning_content": "离线模拟，不是实际模型推理。",
                    "content": "Thought {0}: 本步验证脚本流程。\nAction {0}: {1}".format(step, actions[min(step, 3) - 1])}
        if "hard_constraints" in messages[0]["content"] and "search_query" in messages[0]["content"]:
            result = {"goal": "为客服寻找可复用知识库方案", "hard_constraints": ["本地部署", "回答附原文出处"],
                      "preferences": [], "unknowns": ["权限隔离与文档类型未知"], "questions": [],
                      "search_query": "客服知识库 本地部署 原文引用"}
        else:
            result = {"candidates": [{"name": "演示知识台（虚构）", "facts": [
                {"statement": "演示资料声称支持本地部署并提供原文出处。", "source_id": "S001", "quote": "支持本地部署，回答可以附上文档原文出处"}],
                "constraint_checks": [{"constraint": c, "status": "met", "fact_indexes": [0]}
                                      for c in ("本地部署", "回答附原文出处")],
                "assessment": "仅用于展示研究和溯源流程，不能据此选择真实方案。",
                "reuse_plan": "真实的部署与集成方式尚未验证。"}],
                "unknowns": ["这是脚本模拟，没有检索真实产品，没有验证模型能力。"],
                "poc_steps": ["配置真实 API 后，用实际问答样本检验引用正确性、权限隔离与部署依赖。"]}
        return {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)}

    def search(self, query):
        return {"results": [{"title": "虚构演示资料", "url": "https://example.com/fictional-demo",
                             "summary": "离线模拟搜索结果"}]}

    def fetch(self, url):
        return {"title": "虚构演示资料", "content": CONTENT}
