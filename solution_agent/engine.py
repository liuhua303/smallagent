"""ReAct-style tool loop and evidence-first synthesis (adaptation, not replication)."""

import ipaddress
import json
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit

from .client import AgentError
from .prompts import BRIEF, RESEARCH, REACT, REPORT, TOOLS, PROMPT_VERSION


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def require_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("需要非空字符串")
    return value.strip()


def text_list(value):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError("需要字符串列表")
    return value


def validate_brief(value):
    require_text(value["goal"])
    for key in ("hard_constraints", "preferences", "unknowns", "questions"):
        text_list(value[key])
    if len(value["questions"]) > 3:
        raise ValueError("最多三个问题")
    query = require_text(value["search_query"])
    if len(query) > 128:
        raise ValueError("检索词超长")


def validate_report(value):
    if not isinstance(value["candidates"], list) or len(value["candidates"]) > 3:
        raise ValueError("最多三个候选")
    text_list(value["unknowns"])
    text_list(value["poc_steps"])
    for candidate in value["candidates"]:
        for key in ("name", "assessment", "reuse_plan"):
            require_text(candidate[key])
        if not isinstance(candidate["facts"], list) or not isinstance(candidate["constraint_checks"], list):
            raise ValueError("facts 和 constraint_checks 必须是列表")
        for fact in candidate["facts"]:
            for key in ("statement", "source_id", "quote"):
                require_text(fact[key])
        for check in candidate["constraint_checks"]:
            require_text(check["constraint"])
            if check["status"] not in ("met", "unmet", "unknown"):
                raise ValueError("未知的约束状态")
            indexes = check["fact_indexes"]
            if not isinstance(indexes, list) or any(type(i) is not int or i < 0 for i in indexes):
                raise ValueError("证据索引必须为非负整数列表")


def public_url(value):
    value = require_text(value)
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (len(value) > 8192 or parsed.scheme not in ("http", "https") or not host
            or parsed.username is not None or parsed.password is not None
            or any(ch.isspace() or ch in '<>"\\' for ch in value)
            or host == "localhost" or host.endswith((".localhost", ".local"))):
        raise ValueError("只允许不含凭据的公开 HTTP/HTTPS URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("不允许内网主机名")
    else:
        if not address.is_global:
            raise ValueError("不允许本地或私有地址")
    return value


def compact(value):
    return json.dumps(value, ensure_ascii=False)


def parse_react(content, step):
    """Parse exactly one numbered Thought/Action; observations come from code."""
    content = require_text(content)
    if re.search(r"(?m)^\s*Observation\s+\d+\s*:", content):
        raise ValueError("Observation 只能由程序执行真实工具后提供")
    pattern = r"Thought\s+" + str(step) + r":\s*(.+?)\nAction\s+" + str(step) + r":\s*(Search|Fetch|Lookup|Finish)\[(.*)\]"
    match = re.fullmatch(pattern, content, flags=re.DOTALL)
    if not match or len(re.findall(r"(?m)^Action\s+\d+:", content)) != 1:
        raise ValueError("请仅返回 Thought {0}: ... 和 Action {0}: Search/Fetch/Lookup/Finish[...]".format(step))
    thought, name, value = match.groups()
    return require_text(thought), name, require_text(value)


def evidence_gate(report, sources, constraints):
    """Check provenance only. This is NOT a semantic entailment/factuality score."""
    rejected, proposed, accepted = [], 0, 0
    source_map = {s["id"]: s for s in sources}
    clean_candidates = []
    for candidate in report["candidates"]:
        valid = {}
        facts = []
        for index, fact in enumerate(candidate["facts"]):
            proposed += 1
            source = source_map.get(fact["source_id"])
            normalize = lambda s: re.sub(r"\s+", "", s)
            if source and normalize(fact["quote"]) in normalize(source["content"]):
                item = dict(fact, original_index=index)
                valid[index] = item
                facts.append(item)
                accepted += 1
            else:
                rejected.append({"candidate": candidate["name"], "fact": fact,
                                 "reason": "来源不存在或引文不在已保存正文中"})
        checks = []
        for constraint in constraints:
            matches = [c for c in candidate["constraint_checks"] if c["constraint"] == constraint]
            check = dict(matches[0]) if len(matches) == 1 else {
                "constraint": constraint, "status": "unknown", "fact_indexes": []}
            if (check["status"] != "unknown" and
                    (not check["fact_indexes"] or any(i not in valid for i in check["fact_indexes"]))):
                check.update(status="unknown", fact_indexes=[])
            checks.append(check)
        if not facts:
            eligibility = "证据不足，不能形成推荐"
        elif any(c["status"] == "unmet" for c in checks):
            eligibility = "排除：存在不符合的硬性约束（模型判断，需复核）"
        elif any(c["status"] == "unknown" for c in checks):
            eligibility = "条件候选：硬性约束尚未全部确认"
        else:
            eligibility = "可进入 PoC：约束符合度仍需人工复核"
        clean = {k: candidate[k] for k in ("name", "assessment", "reuse_plan")}
        clean.update(facts=facts, constraint_checks=checks, eligibility=eligibility)
        # Do not surface uncited assessments when all supporting facts were rejected.
        if not facts:
            clean["assessment"] = "没有通过原文溯源检查的事实，暂不提供适配判断。"
            clean["reuse_plan"] = "先补充可靠来源，再确定复用方式。"
        clean_candidates.append(clean)
    return {"candidates": clean_candidates, "unknowns": report["unknowns"],
            "poc_steps": report["poc_steps"], "rejected_facts": rejected,
            "provenance": {"proposed": proposed, "traceable": accepted,
                           "ratio": accepted / proposed if proposed else None,
                           "meaning": "引文原文存在率；不代表语义支持率或答案正确率"}}


class ResearchAgent:
    def __init__(self, client, max_tools=8, log=None, progress=None,
                 research_style="react", max_steps=7, max_stalls=0):
        self.client = client
        self.max_tools = max_tools
        self.log = log or (lambda event: None)
        self.progress = progress or (lambda message: None)
        self.sources = []
        self.seen = set()
        self.tool_count = 0
        self.stop_reason = "not_started"
        if research_style not in ("react", "tools") or max_steps < 1 or max_stalls < 0:
            raise AgentError("研究策略或停止参数无效。")
        self.research_style = research_style
        self.max_steps = max_steps
        self.max_stalls = max_stalls
        self.research_steps = 0
        self.finish_summary = None
        self.trajectory = []
        self.lookup_positions = {}

    def event(self, kind, **data):
        self.log(dict(time=now(), kind=kind, **data))

    def json_call(self, instruction, data, validator, phase):
        messages = [{"role": "system", "content": instruction},
                    {"role": "user", "content": compact(data)}]
        for attempt in range(2):
            message = self.client.chat(messages, json_output=True)
            self.event("model_response", phase=phase, attempt=attempt + 1, message=message)
            try:
                result = json.loads(message.get("content") or "")
                if not isinstance(result, dict):
                    raise ValueError("需要 JSON 对象")
                validator(result)
                return result
            except (ValueError, KeyError, TypeError):
                messages.append({"role": "assistant", "content": message.get("content") or ""})
                messages.append({"role": "user", "content": "JSON 内容或字段不符合指定结构，请重新生成完整对象。"})
        raise AgentError("模型连续两次未返回符合结构的 {}。".format(phase))

    def make_brief(self, requirement):
        self.progress("整理业务目标与未知条件…")
        return self.json_call(BRIEF, {"requirement": requirement}, validate_brief, "brief")

    def execute(self, name, arguments):
        self.tool_count += 1  # Invalid/duplicate proposals also consume the loop budget.
        self.progress("研究动作 {}/{}：{}".format(self.tool_count, self.max_tools, name))
        try:
            if name not in ("search", "fetch", "lookup") or not isinstance(arguments, dict):
                raise ValueError("未知工具或参数类型")
            key = {"search": "query", "fetch": "url", "lookup": "keyword"}[name]
            if set(arguments) != {key}:
                raise ValueError("工具参数字段不正确")
            value = require_text(arguments[key])
            if name == "search" and len(value) > 128:
                raise ValueError("检索词须为1至128字符")
            if name == "fetch":
                value = public_url(value)
            signature = (name, value)
            if name != "lookup" and signature in self.seen:
                raise ValueError("该动作已执行，请针对新的资料缺口换一个动作")
            self.seen.add(signature)
            if name == "search":
                raw = self.client.search(value)
                if not isinstance(raw.get("results"), list):
                    raise AgentError("搜索响应结构异常")
                self.event("tool_raw", tool=name, arguments=arguments, response=raw)
                items = [{k: item.get(k) for k in ("title", "url", "summary", "date")}
                         for item in raw["results"][:5] if isinstance(item, dict)]
                result = {"results": items, "notice": "摘要仅为检索线索，不可用于最终事实引用"}
            elif name == "fetch":
                raw = self.client.fetch(value)
                content = require_text(raw.get("content"))
                source = {"id": "S{:03d}".format(len(self.sources) + 1), "url": value,
                          "title": raw.get("title") or value, "retrieved_at": now(),
                          "content": content}
                self.sources.append(source)
                self.event("source_saved", source=source)
                result = dict(source, content=content[:12000], truncated=len(content) > 12000)
            else:
                if not self.sources:
                    raise ValueError("需要先 Fetch 一个页面，再 Lookup")
                source = self.sources[-1]
                position_key = (source["id"], value.lower())
                start = self.lookup_positions.get(position_key, 0)
                position = source["content"].lower().find(value.lower(), start)
                if position < 0:
                    result = {"source_id": source["id"], "matches": [], "notice": "当前页面没有更多匹配"}
                else:
                    self.lookup_positions[position_key] = position + len(value)
                    excerpt = source["content"][max(0, position - 300):position + len(value) + 900]
                    source.setdefault("lookup_excerpts", []).append(excerpt)
                    result = {"source_id": source["id"], "excerpt": excerpt, "match_offset": position}
            self.event("tool_result", tool=name, arguments=arguments, result=result)
            return result
        except (AgentError, ValueError, TypeError) as error:
            result = {"error": str(error)}
            self.event("tool_result", tool=name, arguments=arguments, result=result)
            return result

    def record_step(self, step, thought, action, observation):
        item = {"step": step, "thought": thought, "action": action, "observation": observation}
        self.trajectory.append(item)
        self.event("trajectory_step", **item)

    def submit_finish(self, summary):
        self.finish_summary = require_text(summary)
        self.stop_reason = "explicit_finish" if self.sources else "explicit_finish_without_sources"

    def research_react(self, brief):
        messages = [{"role": "system", "content": REACT},
                    {"role": "user", "content": "Question: " + compact(brief)}]
        failures = 0
        for step in range(1, self.max_steps + 1):
            if self.tool_count >= self.max_tools:
                self.stop_reason = "tool_budget_exhausted"
                return
            self.research_steps = step
            messages.append({"role": "user", "content": "仅生成第 {} 步的 Thought 和 Action。".format(step)})
            message = self.client.chat(messages, stop=["\nObservation {}:".format(step)])
            self.event("model_response", phase="research_react", step=step, message=message)
            # No native tools in this path: only the explicit text is replayed.
            messages.append({"role": "assistant", "content": message.get("content") or ""})
            try:
                thought, action, value = parse_react(message.get("content"), step)
                if action == "Finish":
                    self.submit_finish(value)
                    self.record_step(step, thought, "Finish[{}]".format(value),
                                     {"submitted": True, "quality_verified": False})
                    return
                key = {"Search": "query", "Fetch": "url", "Lookup": "keyword"}[action]
                result = self.execute(action.lower(), {key: value})
                action_text = "{}[{}]".format(action, value)
            except ValueError as error:
                thought, action_text = "格式不合规，未执行动作", "Invalid"
                result = {"error": str(error)}
            self.record_step(step, thought, action_text, result)
            messages.append({"role": "user", "content": "Observation {}: {}".format(step, compact(result))})
            stalled = "error" in result or result.get("results") == [] or result.get("matches") == []
            failures = failures + 1 if stalled else 0
            if self.max_stalls and failures >= self.max_stalls:
                self.stop_reason = "repeated_no_progress"
                return
        self.stop_reason = "step_budget_exhausted"

    def research(self, brief, mode):
        if mode == "single":
            result = self.execute("search", {"query": brief["search_query"]})
            # Fixed retrieval plan; there is no model-driven follow-up in this baseline.
            for item in result.get("results", [])[:3]:
                if self.tool_count >= self.max_tools:
                    break
                self.execute("fetch", {"url": item.get("url")})
            self.stop_reason = "fixed_retrieval_complete"
            return
        if self.research_style == "react":
            return self.research_react(brief)
        messages = [{"role": "system", "content": RESEARCH},
                    {"role": "user", "content": compact(brief)}]
        failures = 0
        for step in range(1, self.max_steps + 1):
            if self.tool_count >= self.max_tools:
                self.stop_reason = "tool_budget_exhausted"
                return
            self.research_steps = step
            message = self.client.chat(messages, tools=TOOLS)
            self.event("model_response", phase="research", message=message)
            calls = message.get("tool_calls") or []
            if not calls:
                self.stop_reason = "missing_finish"
                return
            if (not isinstance(calls, list) or any(not isinstance(c, dict)
                    or not isinstance(c.get("id"), str) or not c["id"]
                    or not isinstance(c.get("function"), dict) for c in calls)
                    or len({c["id"] for c in calls}) != len(calls)):
                raise AgentError("模型工具调用协议不完整。")
            messages.append({"role": "assistant", "content": message.get("content"),
                             "tool_calls": calls, **({"reasoning_content": message["reasoning_content"]}
                              if "reasoning_content" in message else {})})
            for call in calls:
                function = call["function"]
                if function.get("name") == "finish":
                    try:
                        args = json.loads(function.get("arguments", ""))
                        if len(calls) != 1 or not isinstance(args, dict) or set(args) != {"summary"}:
                            raise ValueError("finish 必须单独调用，且只传入 summary")
                        self.submit_finish(args["summary"])
                        self.record_step(step, message.get("content") or "", "Finish[{}]".format(args["summary"]),
                                         {"submitted": True, "quality_verified": False})
                        return
                    except (ValueError, TypeError, KeyError) as error:
                        result = {"error": str(error)}
                elif self.tool_count >= self.max_tools:
                    result = {"error": "工具预算已耗尽，本动作未执行"}
                else:
                    try:
                        args = json.loads(call["function"].get("arguments", ""))
                    except (ValueError, TypeError):
                        args = None
                    result = self.execute(call["function"].get("name"), args)
                self.record_step(step, message.get("content") or "", compact(function), result)
                failures = failures + 1 if "error" in result or result.get("results") == [] else 0
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": compact(result)})
            if self.max_stalls and failures >= self.max_stalls:
                self.stop_reason = "repeated_no_progress"
                return
        self.stop_reason = "step_budget_exhausted"

    def run(self, requirement, mode="agent", clarify=None):
        if mode not in ("agent", "single", "direct"):
            raise AgentError("未知运行模式。")
        begin = time.monotonic()
        started = now()
        brief = self.make_brief(requirement)
        if clarify and brief["questions"]:
            answer = clarify(brief["questions"])
            if answer.strip():
                requirement += "\n用户补充：" + answer
                brief = self.make_brief(requirement)
        self.event("brief", requirement=requirement, brief=brief)
        state = {"started_at": started, "requirement": requirement, "brief": brief,
                 "mode": mode, "prompt_version": PROMPT_VERSION,
                 "requested_model": self.client.model, "max_tools": self.max_tools,
                 "thinking": getattr(self.client, "thinking", None),
                 "reasoning_effort": getattr(self.client, "reasoning_effort", None),
                 "research_style": self.research_style, "max_steps": self.max_steps,
                 "max_stalls": self.max_stalls}
        if mode == "direct":
            message = self.client.chat([
                {"role": "system", "content": "你帮助技术负责人比较可复用AI方案。依据需求给出最多三个候选、取舍、未知项和PoC计划。不编造已完成的检索或测试。"},
                {"role": "user", "content": compact(brief)}])
            self.event("model_response", phase="direct_baseline", message=message)
            state["direct_answer"] = require_text(message.get("content"))
            self.stop_reason = "direct_baseline_complete"
        else:
            self.research(brief, mode)
            self.progress("根据已获取原文生成对比报告…")
            if self.sources:
                data = {"brief": brief, "stop_reason": self.stop_reason,
                        "submission": self.finish_summary,
                        "sources": [dict(s, content=s["content"][:12000]) for s in self.sources]}
                raw_report = self.json_call(REPORT, data, validate_report, "report")
            else:
                raw_report = {"candidates": [], "unknowns": ["未获取可引用正文，不能形成有证据的方案推荐。"],
                              "poc_steps": ["先检查搜索与页面读取是否成功，补充可靠资料后重新研究。"]}
            state["report"] = evidence_gate(raw_report, self.sources, brief["hard_constraints"])
            if self.stop_reason not in ("explicit_finish", "explicit_finish_without_sources", "fixed_retrieval_complete"):
                state["report"]["unknowns"].append("调研被截断或没有收到显式提交，可能遗漏候选方案和关键条件。")
        state.update(sources=self.sources, stop_reason=self.stop_reason,
                     tool_calls=self.tool_count, finished_at=now(), metrics=self.client.metrics,
                     elapsed_seconds=round(time.monotonic() - begin, 3),
                     research_steps=self.research_steps, finish_summary=self.finish_summary,
                     trajectory=self.trajectory,
                     research_completion_verified=False,
                     returned_models=sorted(self.client.returned_models))
        return state
