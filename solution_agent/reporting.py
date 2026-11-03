import html
import json
import re
from pathlib import Path


def md(value):
    value = html.escape(str(value), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", value)


def render(state):
    label = "离线模拟示例（虚构方案，无联网）" if state.get("demo") else "AI 技术方案调研"
    lines = ["# " + label, "", "业务需求：" + md(state["requirement"]), "",
             "运行模式：`{}`；结束原因：`{}`".format(state["mode"], state["stop_reason"]), "",
             "## 需求与未知项", "", "业务目标：" + md(state["brief"]["goal"]), ""]
    if state["mode"] == "agent":
        submitted = state.get("stop_reason") in ("explicit_finish", "explicit_finish_without_sources")
        lines[6:6] = ["模型已显式提交研究，但未通过独立完成度验证。" if submitted else
                      "研究未正常提交：以下为已取得资料的部分结果。", ""]
    for key, title in (("hard_constraints", "硬性约束"), ("preferences", "偏好"), ("unknowns", "需求未知项")):
        lines.append("- {}：{}".format(title, md("；".join(state["brief"][key]) or "未提供")))
    if state["mode"] == "direct":
        lines.extend(["", "## 直接回答对照组", "", "本对照组未检索，也未做证据核验。", "",
                      md(state["direct_answer"])])
        return "\n".join(lines) + "\n"
    report = state["report"]
    sources = {s["id"]: s for s in state["sources"]}
    lines.extend(["", "## 候选方案", "",
                  "以下事实仅通过引文存在检查，尚未核验语义支持、实际部署或性能。", ""])
    if not report["candidates"]:
        lines.append("没有足够证据形成候选方案。")
    for candidate in report["candidates"]:
        lines.extend(["### " + md(candidate["name"]), "", md(candidate["eligibility"]), ""])
        for fact in candidate["facts"]:
            source = sources[fact["source_id"]]
            lines.extend(["- {} [{}](<{}>)".format(md(fact["statement"]), fact["source_id"], source["url"]),
                          "", "  原文：" + md(fact["quote"]), ""])
        for check in candidate["constraint_checks"]:
            status = {"met": "模型判断符合", "unmet": "模型判断不符合", "unknown": "未知"}[check["status"]]
            related = [f for f in candidate["facts"] if f["original_index"] in check["fact_indexes"]]
            references = "、".join("[{}](<{}>)".format(f["source_id"], sources[f["source_id"]]["url"])
                                  for f in related) or "待补充"
            lines.append("- {}：{}；证据：{}".format(md(check["constraint"]), status, references))
        lines.extend(["", "适配判断（模型推断，待人工核验）：" + md(candidate["assessment"]), "",
                      "复用建议（尚未实测）：" + md(candidate["reuse_plan"]), ""])
    lines.extend(["## 待确认事项", ""])
    lines.extend("- " + md(x) for x in report["unknowns"])
    lines.extend(["", "## PoC 验证计划（建议，尚未执行）", ""])
    lines.extend("{}. {}".format(i, md(x)) for i, x in enumerate(report["poc_steps"], 1))
    provenance = report["provenance"]
    lines.extend(["", "## 溯源检查", "",
                  "生成事实 {} 条；引文存在 {} 条；剔除 {} 条。此统计不代表语义支持率或答案正确率。".format(
                      provenance["proposed"], provenance["traceable"], len(report["rejected_facts"])), "",
                  "## 来源", ""])
    for source in state["sources"]:
        lines.append("- [{}：{}](<{}>)；获取时间 {}".format(
            source["id"], md(source["title"]), source["url"], source["retrieved_at"]))
    lines.extend(["", "模型请求数：{}；研究工具动作数：{}；耗时：{} 秒。".format(
        state["metrics"].get("model_calls", 0), state["tool_calls"], state.get("elapsed_seconds", "未知"))])
    return "\n".join(lines) + "\n"


def review_template(state):
    sources = {s["id"]: s for s in state["sources"]}
    claims, constraints = [], []
    for ci, candidate in enumerate(state.get("report", {}).get("candidates", [])):
        for fact in candidate["facts"]:
            claims.append({"id": "{}:{}".format(ci, fact["original_index"]),
                           "candidate": candidate["name"], "statement": fact["statement"],
                           "quote": fact["quote"], "url": sources[fact["source_id"]]["url"],
                           "human_supported": None, "notes": ""})
        for check in candidate["constraint_checks"]:
            constraints.append({"candidate": candidate["name"], "constraint": check["constraint"],
                                "suggested_status": check["status"], "eligibility": candidate["eligibility"],
                                "human_constraint_violation_in_recommendation": None, "notes": ""})
    return {"mode": state["mode"], "requirement": state["requirement"],
            "instructions": "人工打开来源并核对：human_supported 填 true/false；未知留 null。约束违反字段仅当被推荐方案违反约束时填 true。",
            "claims": claims, "constraints": constraints,
            "direct_answer_review": {"human_supported": None, "notes": "直接回答模式需人工自行拆分并检查事实"},
            "utility_1_to_5": None, "notes": ""}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def save_run(directory, state):
    directory = Path(directory)
    write_json(directory / "state.json", state)
    (directory / "report.md").write_text(render(state), encoding="utf-8")
    write_json(directory / "review.json", review_template(state))
    lines = ["# 研究交互轨迹", "", "仅记录本次真实模型输出和工具观察；不把示范或未来步骤当执行结果。",
             "模型内部 reasoning_content 与显式 Thought 是不同字段；前者保存在 trace.jsonl。", ""]
    for item in state.get("trajectory", []):
        lines.extend(["## Step {}".format(item["step"]), "",
                      "Thought {}: {}".format(item["step"], md(item["thought"])), "",
                      "Action {}: {}".format(item["step"], md(item["action"])), "",
                      "Observation {}: {}".format(item["step"], md(json.dumps(item["observation"], ensure_ascii=False))), ""])
    lines.extend(["结束原因：" + md(state["stop_reason"]), "",
                  "Finish 只表示模型提交；预算耗尽表示截断；两者都不证明答案正确。", ""])
    (directory / "trajectory.md").write_text("\n".join(lines), encoding="utf-8")
