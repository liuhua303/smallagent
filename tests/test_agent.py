import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from solution_agent.client import AgentError, DeepSeekClient
from solution_agent.config import load_settings
from solution_agent.demo import CONTENT, DemoClient, QUESTION
from solution_agent.engine import ResearchAgent as ActualResearchAgent, evidence_gate, public_url, validate_report, parse_react
from solution_agent.evaluate import summarize
from solution_agent.reporting import render, review_template, save_run


ROOT = Path(__file__).resolve().parents[1]


class ResearchAgent(ActualResearchAgent):
    """Keep the existing native-tool regression suite independent of CLI defaults."""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("research_style", "tools")
        super().__init__(*args, **kwargs)


def call(identifier, name, arguments):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class ScriptedClient(DemoClient):
    def __init__(self, responses):
        super().__init__()
        self.responses = iter(responses)
        self.inputs = []

    def chat(self, messages, tools=None, json_output=False):
        if tools:
            self.inputs.append(copy.deepcopy(messages))
            self.metrics["model_calls"] += 1
            return next(self.responses)
        return super().chat(messages, tools, json_output)


class LoopTests(unittest.TestCase):
    def test_demo_uses_real_loop_and_report_gate(self):
        events = []
        result = ResearchAgent(DemoClient(), log=events.append).run(QUESTION)
        self.assertEqual(result["tool_calls"], 2)
        self.assertEqual(result["stop_reason"], "explicit_finish")
        self.assertEqual(result["sources"][0]["content"], CONTENT)
        self.assertEqual(result["report"]["provenance"]["traceable"], 1)
        self.assertTrue(any(e["kind"] == "source_saved" for e in events))

    def test_tool_result_is_returned_with_original_call_id(self):
        client = ScriptedClient([
            {"role": "assistant", "tool_calls": [call("original_id", "search", {"query": "test"})]},
            {"role": "assistant", "content": "done"}])
        ResearchAgent(client).run(QUESTION)
        feedback = client.inputs[1][-1]
        self.assertEqual(feedback["role"], "tool")
        self.assertEqual(feedback["tool_call_id"], "original_id")
        self.assertIn("results", json.loads(feedback["content"]))

    def test_multi_call_cannot_exceed_budget(self):
        client = ScriptedClient([{"role": "assistant", "tool_calls": [
            call("1", "search", {"query": "a"}), call("2", "fetch", {"url": "https://example.com/a"}),
            call("3", "fetch", {"url": "https://example.com/b"})]}])
        agent = ResearchAgent(client, max_tools=2)
        result = agent.run(QUESTION)
        self.assertEqual(result["tool_calls"], 2)
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["stop_reason"], "tool_budget_exhausted")

    def test_duplicate_actions_do_not_reach_remote_api(self):
        client = ScriptedClient([
            {"role": "assistant", "tool_calls": [call(str(i), "search", {"query": "same"})]}
            for i in range(3)])
        with patch.object(client, "search", wraps=client.search) as search:
            result = ResearchAgent(client, max_stalls=2).run(QUESTION)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(result["stop_reason"], "repeated_no_progress")

    def test_tool_failure_becomes_observation_without_source(self):
        client = ScriptedClient([
            {"role": "assistant", "tool_calls": [call("1", "fetch", {"url": "https://example.com"})]},
            {"role": "assistant", "content": "done"}])
        with patch.object(client, "fetch", side_effect=AgentError("HTTP 404")):
            result = ResearchAgent(client).run(QUESTION)
        self.assertIn("error", json.loads(client.inputs[1][-1]["content"]))
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["report"]["candidates"], [])

    def test_unknown_tool_and_private_url_never_execute(self):
        client = DemoClient()
        agent = ResearchAgent(client)
        with patch.object(client, "fetch") as fetch:
            self.assertIn("error", agent.execute("fetch", {"url": "http://127.0.0.1/private"}))
            self.assertIn("error", agent.execute("run_shell", {"command": "anything"}))
        fetch.assert_not_called()

    def test_invalid_tool_arguments_are_recoverable(self):
        client = ScriptedClient([
            {"role": "assistant", "tool_calls": [dict(call("1", "search", {}), function={
                "name": "search", "arguments": "not json"})]},
            {"role": "assistant", "content": "done"}])
        result = ResearchAgent(client).run(QUESTION)
        self.assertEqual(result["tool_calls"], 1)
        self.assertIn("error", json.loads(client.inputs[1][-1]["content"]))

    def test_premature_model_finish_does_not_invent_sources(self):
        client = ScriptedClient([{"role": "assistant", "content": "已查到多个方案"}])
        result = ResearchAgent(client).run(QUESTION)
        self.assertEqual(result["stop_reason"], "missing_finish")
        self.assertEqual(result["report"]["candidates"], [])

    def test_single_mode_has_no_model_directed_research(self):
        client = DemoClient()
        result = ResearchAgent(client).run(QUESTION, "single")
        self.assertEqual(client.research_step, 0)
        self.assertEqual(result["stop_reason"], "fixed_retrieval_complete")
        self.assertEqual(result["tool_calls"], 2)

    def test_empty_search_has_no_report_model_call(self):
        client = DemoClient()
        with patch.object(client, "search", return_value={"results": []}):
            result = ResearchAgent(client).run(QUESTION, "single")
        self.assertEqual(client.metrics["model_calls"], 1)
        self.assertEqual(result["report"]["candidates"], [])

    def test_blank_json_repaired_only_once(self):
        client = DemoClient()
        with patch.object(client, "chat", return_value={"content": "   "}) as chat:
            with self.assertRaises(AgentError):
                ResearchAgent(client).make_brief(QUESTION)
        self.assertEqual(chat.call_count, 2)

    def test_invalid_tool_id_has_a_clear_failure(self):
        client = ScriptedClient([{"role": "assistant", "tool_calls": [call([], "search", {"query": "x"})]}])
        with self.assertRaises(AgentError):
            ResearchAgent(client).run(QUESTION)

    def test_clarification_preserves_user_answer(self):
        client = DemoClient()
        agent = ResearchAgent(client)
        original = agent.make_brief(QUESTION)
        original["questions"] = ["允许云模型吗？"]
        updated = copy.deepcopy(original)
        updated["questions"] = []
        with patch.object(agent, "make_brief", side_effect=[original, updated]):
            state = agent.run(QUESTION, "single", clarify=lambda questions: "不允许")
        self.assertIn("用户补充：不允许", state["requirement"])


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        state = ResearchAgent(DemoClient()).run(QUESTION)
        self.sources = state["sources"]
        self.report = state["report"]

    def gate(self, constraints=("本地部署", "回答附原文出处")):
        return evidence_gate(self.report, self.sources, constraints)

    def test_invented_source_rejected_and_constraint_downgraded(self):
        self.report["candidates"][0]["facts"][0]["source_id"] = "S999"
        result = self.gate()
        self.assertEqual(result["provenance"]["traceable"], 0)
        self.assertEqual(len(result["rejected_facts"]), 1)
        self.assertTrue(all(c["status"] == "unknown" for c in result["candidates"][0]["constraint_checks"]))
        self.assertIn("证据不足", result["candidates"][0]["eligibility"])

    def test_invented_quote_rejected(self):
        self.report["candidates"][0]["facts"][0]["quote"] = "系统保证百分百正确"
        self.assertEqual(self.gate()["provenance"]["traceable"], 0)

    def test_missing_constraint_becomes_unknown(self):
        result = self.gate(("必须接入未知SSO",))
        self.assertEqual(result["candidates"][0]["constraint_checks"][0]["status"], "unknown")
        self.assertIn("条件候选", result["candidates"][0]["eligibility"])

    def test_unmet_constraint_excludes_candidate(self):
        self.report["candidates"][0]["constraint_checks"][0]["status"] = "unmet"
        self.assertIn("排除", self.gate()["candidates"][0]["eligibility"])

    def test_invalid_fact_reference_is_not_met(self):
        self.report["candidates"][0]["constraint_checks"][0]["fact_indexes"] = [99]
        result = self.gate()
        self.assertEqual(result["candidates"][0]["constraint_checks"][0]["status"], "unknown")

    def test_quote_match_does_not_claim_semantic_support(self):
        self.report["candidates"][0]["facts"][0]["statement"] = "这个结论根本没有被原文支持"
        result = self.gate()
        self.assertEqual(result["provenance"]["traceable"], 1)
        self.assertIn("不代表", result["provenance"]["meaning"])
        state = {"mode": "agent", "requirement": QUESTION, "report": result, "sources": self.sources}
        self.assertIsNone(review_template(state)["claims"][0]["human_supported"])

    def test_schema_rejects_boolean_fact_index(self):
        self.report["candidates"][0]["constraint_checks"][0]["fact_indexes"] = [True]
        with self.assertRaises(ValueError):
            validate_report(self.report)

    def test_whitespace_only_quote_rejected_by_schema(self):
        self.report["candidates"][0]["facts"][0]["quote"] = "  "
        with self.assertRaises(ValueError):
            validate_report(self.report)

    def test_private_urls_credentials_and_invalid_schemes(self):
        for url in ("http://localhost/x", "http://10.0.0.1/", "http://[::1]/", "file:///secret",
                    "https://user:pass@example.com/x", "http://intranet/", "http://a.local",
                    "https://example.com/a>\n![x](evil)"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_url(url)
        self.assertEqual(public_url("https://example.com/a"), "https://example.com/a")


class OutputTests(unittest.TestCase):
    def test_run_artifacts_and_unreviewed_metrics(self):
        state = ResearchAgent(DemoClient()).run(QUESTION)
        state["demo"] = True
        with tempfile.TemporaryDirectory() as temp:
            save_run(temp, state)
            report = (Path(temp) / "report.md").read_text(encoding="utf-8")
            self.assertIn("离线模拟", report)
            self.assertIn("https://example.com/fictional-demo", report)
            review = json.loads((Path(temp) / "review.json").read_text(encoding="utf-8"))
            result = summarize(review)
            self.assertIsNone(result["human_support_rate_on_reviewed"])
            self.assertEqual(result["claims_reviewed"], 0)
            review["claims"][0]["human_supported"] = False
            self.assertEqual(summarize(review)["human_support_rate_on_reviewed"], 0)

    def test_generated_markdown_cannot_embed_remote_images(self):
        state = ResearchAgent(DemoClient()).run(QUESTION)
        state["report"]["candidates"][0]["assessment"] = "![image](https://example.com/tracker)"
        self.assertNotIn("![image](", render(state))

    def test_review_rejects_text_booleans(self):
        review = review_template(ResearchAgent(DemoClient()).run(QUESTION))
        review["claims"][0]["human_supported"] = "true"
        with self.assertRaises(ValueError):
            summarize(review)

    def test_cli_demo_without_key(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, "-m", "solution_agent", "--demo", "--output-dir", temp],
                                    cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
            runs = list(Path(temp).iterdir())
            self.assertEqual(len(runs), 1)
            events = [json.loads(x) for x in (runs[0] / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(x["kind"] == "tool_result" for x in events))

    def test_empty_question_is_rejected(self):
        with patch("solution_agent.__main__.DeepSeekClient", return_value=DemoClient()), \
             patch("sys.argv", ["agent", "--question", ""]):
            from solution_agent.__main__ import main
            self.assertEqual(main(), 2)

    def test_cli_saves_failure_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("solution_agent.__main__.DeepSeekClient", return_value=DemoClient()), \
                 patch("solution_agent.__main__.ResearchAgent.run", side_effect=AgentError("simulated failure")), \
                 patch("sys.argv", ["agent", "--question", "test", "--output-dir", temp]):
                from solution_agent.__main__ import main
                self.assertEqual(main(), 2)
            files = list(Path(temp).glob("*/failure.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text(encoding="utf-8"))["status"], "failed")


class TransportTests(unittest.TestCase):
    def test_missing_key_fails_without_network(self):
        with patch("solution_agent.client.load_settings", return_value={
                "DEEPSEEK_API_KEY": "", "DEEPSEEK_MODEL": "deepseek-flash"}), self.assertRaises(AgentError):
            DeepSeekClient()

    def test_auth_error_does_not_leak_body_or_key(self):
        client = DeepSeekClient(api_key="sensitive-key")
        error = HTTPError("https://api.deepseek.com", 401, "sensitive-key", {}, None)
        with patch("solution_agent.client.urlopen", side_effect=error) as request:
            with self.assertRaises(AgentError) as caught:
                client.search("test")
        self.assertNotIn("sensitive-key", str(caught.exception))
        self.assertEqual(request.call_count, 1)

    def test_429_retry_is_bounded(self):
        client = DeepSeekClient(api_key="test")
        error = HTTPError("https://api.deepseek.com", 429, "limited", {}, None)
        with patch("solution_agent.client.urlopen", side_effect=error) as request, \
             patch("solution_agent.client.time.sleep"):
            with self.assertRaises(AgentError):
                client.search("test")
        self.assertEqual(request.call_count, 2)

    def test_network_timeout_not_replayed(self):
        client = DeepSeekClient(api_key="test")
        with patch("solution_agent.client.urlopen", side_effect=URLError("timeout")) as request:
            with self.assertRaises(AgentError):
                client.fetch("https://example.com")
        self.assertEqual(request.call_count, 1)

    def test_chat_payload_enables_thinking_and_tracks_usage(self):
        client = DeepSeekClient(api_key="test")
        payload = {"model": "returned-model", "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                   "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]}
        with patch.object(client, "post", return_value=payload) as post:
            client.chat([{"role": "user", "content": "test"}])
        self.assertEqual(post.call_args[0][1]["thinking"], {"type": "enabled"})
        self.assertEqual(post.call_args[0][1]["reasoning_effort"], "high")
        self.assertNotIn("temperature", post.call_args[0][1])
        self.assertEqual(client.metrics["usage"]["prompt_tokens"], 11)
        self.assertIn("returned-model", client.returned_models)

    def test_thinking_can_be_disabled_for_comparison(self):
        client = DeepSeekClient(api_key="test", thinking=False)
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
        with patch.object(client, "post", return_value=payload) as post:
            client.chat([])
        self.assertEqual(post.call_args[0][1]["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", post.call_args[0][1])

    def test_truncated_model_response_is_failure(self):
        client = DeepSeekClient(api_key="test")
        with patch.object(client, "post", return_value={"choices": [{"finish_reason": "length"}]}):
            with self.assertRaises(AgentError):
                client.chat([])


class ConfigTests(unittest.TestCase):
    def test_local_settings_and_unknown_lines(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {}, clear=True):
            path = Path(temp) / ".env"
            path.write_text('# comment\nDEEPSEEK_API_KEY="local-test"\nDEEPSEEK_MODEL=local-model\nOTHER=ignored\n', encoding="utf-8")
            settings = load_settings(path)
            self.assertEqual(settings, {"DEEPSEEK_API_KEY": "local-test", "DEEPSEEK_MODEL": "local-model"})

    def test_environment_precedence(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"DEEPSEEK_API_KEY": "env-test"}):
            path = Path(temp) / ".env"
            path.write_text("DEEPSEEK_API_KEY=local-test", encoding="utf-8")
            self.assertEqual(load_settings(path)["DEEPSEEK_API_KEY"], "env-test")

    def test_missing_file_does_not_require_key_for_demo(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {}, clear=True):
            self.assertEqual(load_settings(Path(temp) / ".env")["DEEPSEEK_API_KEY"], "")


class TextClient(DemoClient):
    def __init__(self, outputs):
        super().__init__()
        self.outputs = iter(outputs)
        self.inputs = []

    def chat(self, messages, tools=None, json_output=False, stop=None):
        if stop:
            self.metrics["model_calls"] += 1
            self.inputs.append(copy.deepcopy(messages))
            return {"role": "assistant", "content": next(self.outputs),
                    "reasoning_content": "provider-test-reasoning"}
        return super().chat(messages, tools, json_output, stop)


class TextReActTests(unittest.TestCase):
    def test_real_text_protocol_records_only_executed_observations(self):
        events = []
        state = ActualResearchAgent(DemoClient(), log=events.append).run(QUESTION)
        self.assertEqual(state["research_style"], "react")
        self.assertEqual(state["research_steps"], 3)
        self.assertEqual(state["tool_calls"], 2)
        self.assertEqual(state["stop_reason"], "explicit_finish")
        self.assertFalse(state["research_completion_verified"])
        self.assertEqual([x["step"] for x in state["trajectory"]], [1, 2, 3])
        self.assertIn("Search[", state["trajectory"][0]["action"])
        self.assertEqual(state["trajectory"][1]["observation"]["id"], "S001")

    def test_actual_observation_enters_next_prompt(self):
        client = TextClient(["Thought 1: 查资料。\nAction 1: Search[资料]",
                             "Thought 2: 信息不足。\nAction 2: Finish[未读取正文，无法推荐。]"])
        state = ActualResearchAgent(client).run(QUESTION)
        observation = client.inputs[1][-2]["content"]
        self.assertTrue(observation.startswith("Observation 1:"))
        self.assertIn("fictional-demo", observation)
        self.assertEqual(state["stop_reason"], "explicit_finish_without_sources")

    def test_parser_rejects_fabricated_observation_and_future_actions(self):
        invalid = ["Thought 1: x\nAction 1: Search[x]\nObservation 1: fabricated",
                   "Thought 1: x\nAction 1: Search[x]\nThought 2: y\nAction 2: Finish[y]",
                   "Thought 2: wrong step\nAction 2: Finish[x]", "Done", "Thought 1: x\nAction 1: Shell[command]"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_react(value, 1)

    def test_step_limit_is_truncation_not_completion(self):
        client = TextClient(["Thought 1: 查资料。\nAction 1: Search[资料]"])
        state = ActualResearchAgent(client, max_steps=1).run(QUESTION)
        self.assertEqual(state["stop_reason"], "step_budget_exhausted")
        self.assertIsNone(state["finish_summary"])
        self.assertIn("部分结果", render(state))

    def test_two_errors_do_not_end_default_protocol(self):
        client = TextClient(["invalid", "also invalid",
                             "Thought 3: 当前信息不足。\nAction 3: Finish[缺少证据。]"])
        state = ActualResearchAgent(client).run(QUESTION)
        self.assertEqual(state["research_steps"], 3)
        self.assertEqual(state["stop_reason"], "explicit_finish_without_sources")
        self.assertEqual(state["tool_calls"], 0)

    def test_optional_stall_limit_is_reported_separately(self):
        client = TextClient(["invalid", "also invalid"])
        state = ActualResearchAgent(client, max_stalls=2).run(QUESTION)
        self.assertEqual(state["stop_reason"], "repeated_no_progress")

    def test_text_protocol_can_finish_after_a_tool_error(self):
        client = TextClient(["Thought 1: 读取资料。\nAction 1: Fetch[https://example.com/a]",
                             "Thought 2: 页面不可读。\nAction 2: Finish[证据不足，需补充可访问资料。]"])
        with patch.object(client, "fetch", side_effect=AgentError("HTTP 404")):
            state = ActualResearchAgent(client).run(QUESTION)
        self.assertEqual(state["stop_reason"], "explicit_finish_without_sources")
        self.assertIn("error", state["trajectory"][0]["observation"])

    def test_lookup_advances_and_exposes_tail_to_report(self):
        agent = ActualResearchAgent(DemoClient())
        with patch.object(agent.client, "fetch", return_value={"content": "x" * 13000 + "许可条款一。许可条款二。"}):
            agent.execute("fetch", {"url": "https://example.com/license"})
        first = agent.execute("lookup", {"keyword": "许可"})
        second = agent.execute("lookup", {"keyword": "许可"})
        last = agent.execute("lookup", {"keyword": "许可"})
        self.assertGreater(second["match_offset"], first["match_offset"])
        self.assertEqual(last["matches"], [])
        self.assertEqual(len(agent.sources[0]["lookup_excerpts"]), 2)
        self.assertIn("许可条款", first["excerpt"])

    def test_native_tools_preserve_provider_reasoning(self):
        client = ScriptedClient([
            {"role": "assistant", "content": "", "reasoning_content": "exact-provider-field",
             "tool_calls": [call("1", "search", {"query": "x"})]},
            {"role": "assistant", "content": "", "reasoning_content": "next-provider-field",
             "tool_calls": [call("2", "finish", {"summary": "没有正文，证据不足。"})]}])
        state = ActualResearchAgent(client, research_style="tools").run(QUESTION)
        self.assertEqual(client.inputs[1][-2]["reasoning_content"], "exact-provider-field")
        self.assertEqual(state["stop_reason"], "explicit_finish_without_sources")

    def test_native_finish_requires_summary_and_sole_call(self):
        client = ScriptedClient([
            {"role": "assistant", "tool_calls": [call("1", "finish", {"wrong": "x"})]},
            {"role": "assistant", "tool_calls": [call("2", "finish", {"summary": "信息不足。"})]}])
        state = ActualResearchAgent(client, research_style="tools").run(QUESTION)
        self.assertEqual(state["research_steps"], 2)
        self.assertEqual(state["stop_reason"], "explicit_finish_without_sources")

    def test_trajectory_file_is_saved(self):
        state = ActualResearchAgent(DemoClient()).run(QUESTION)
        with tempfile.TemporaryDirectory() as temp:
            save_run(temp, state)
            text = (Path(temp) / "trajectory.md").read_text(encoding="utf-8")
            self.assertIn("Thought 1:", text)
            self.assertIn("Observation 2:", text)
            self.assertIn("explicit\\_finish", text)


if __name__ == "__main__":
    unittest.main()
