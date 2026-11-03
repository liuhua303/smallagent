import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from .client import AgentError, DeepSeekClient
from .config import load_settings
from .demo import DemoClient, QUESTION
from .engine import ResearchAgent, now
from .reporting import save_run, write_json


def arguments():
    parser = argparse.ArgumentParser(description="从业务需求发现、比较可复用 AI 方案。Python 3.8+，无需额外依赖。")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--question", help="单次业务需求；省略则进入交互模式")
    group.add_argument("--request-file", type=Path, help="从 UTF-8 文本文件读取需求")
    group.add_argument("--demo", action="store_true", help="运行虚构的离线示例，不调用 API")
    group.add_argument("--check-config", action="store_true", help="只检查本地配置，不联网")
    parser.add_argument("--mode", choices=("agent", "single", "direct"), default="agent")
    parser.add_argument("--max-tools", type=int, default=8, help="研究工具动作上限（实验参数，默认8）")
    parser.add_argument("--research-style", choices=("react", "tools"), default="react",
                        help="react 使用论文文本轨迹，tools 保留原生工具调用对照")
    parser.add_argument("--thinking", choices=("enabled", "disabled"), default="enabled")
    parser.add_argument("--reasoning-effort", choices=("low", "high", "max"), default="high")
    parser.add_argument("--max-steps", type=int, default=7,
                        help="研究步数上限，包含 Finish 和格式错误；7参考ReAct HotpotQA实验，不代表本任务最优")
    parser.add_argument("--max-stalls", type=int, default=0,
                        help="连续错误/空结果的可选中断阈值，0关闭（默认）")
    parser.add_argument("--output-dir", type=Path, default=Path("runs"))
    args = parser.parse_args()
    if not 2 <= args.max_tools <= 30:
        parser.error("--max-tools 必须在2到30之间")
    if not 1 <= args.max_steps <= 50 or not 0 <= args.max_stalls <= 30:
        parser.error("--max-steps 必须在1到50之间，--max-stalls 必须在0到30之间")
    if args.demo and args.mode != "agent":
        parser.error("--demo 仅演示 agent 模式")
    return args


def ask(questions):
    answers = []
    print("\n这些条件会影响选型；不清楚可以直接回车：")
    for question in questions:
        answer = input(question + "\n> ").strip()
        if answer:
            answers.append(question + "：" + answer)
    return "\n".join(answers)


def main():
    # Keep Chinese output readable in terminals and UTF-8 log collectors on Windows.
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    args = arguments()
    if args.check_config:
        settings = load_settings()
        configured = bool(settings["DEEPSEEK_API_KEY"].strip())
        print("DEEPSEEK_API_KEY：" + ("已配置（未验证有效性）" if configured else "未配置"))
        print("模型：" + settings["DEEPSEEK_MODEL"])
        return 0 if configured else 2
    interactive = not (args.question is not None or args.request_file or args.demo)
    if interactive and not sys.stdin.isatty():
        print("非交互运行请使用 --question 或 --request-file。", file=sys.stderr)
        return 2
    try:
        client = DemoClient() if args.demo else DeepSeekClient(
            thinking=args.thinking == "enabled", reasoning_effort=args.reasoning_effort)
        requirement = QUESTION if args.demo else (
            args.request_file.read_text(encoding="utf-8-sig") if args.request_file else args.question)
        if interactive:
            print("AI 方案调研 Agent；输入业务目标，输入 /exit 结束。")
            requirement = input("业务需求 > ").strip()
        if requirement is not None and not requirement.strip():
            raise AgentError("业务需求不能为空。")
        while requirement and requirement != "/exit":
            if not requirement.strip():
                raise AgentError("业务需求不能为空。")
            stamp = now().replace(":", "").replace("+", "_")
            directory = args.output_dir.resolve() / (stamp + "_" + uuid.uuid4().hex[:8])
            directory.mkdir(parents=True, exist_ok=False)
            print("运行记录：" + str(directory))
            if args.demo:
                print("离线脚本演示：所有方案及搜索结果均为虚构，不调用网络。")
            # A fresh client keeps metrics scoped to each follow-up run.
            if interactive:
                client = DeepSeekClient(thinking=args.thinking == "enabled", reasoning_effort=args.reasoning_effort)
            with (directory / "trace.jsonl").open("a", encoding="utf-8") as trace:
                def log(event):
                    trace.write(json.dumps(event, ensure_ascii=False) + "\n")
                    trace.flush()
                agent = ResearchAgent(client, args.max_tools, log=log, progress=print,
                                      research_style=args.research_style, max_steps=args.max_steps,
                                      max_stalls=args.max_stalls)
                try:
                    state = agent.run(requirement, args.mode, clarify=ask if interactive else None)
                    state["demo"] = args.demo
                    save_run(directory, state)
                except (AgentError, OSError, ValueError, KeyboardInterrupt, EOFError) as error:
                    write_json(directory / "failure.json", {
                        "time": now(), "status": "failed", "error": str(error),
                        "requirement": requirement, "sources": agent.sources,
                        "metrics": client.metrics})
                    raise
            print("\n报告：" + str(directory / "report.md"))
            print("结束原因：" + state["stop_reason"])
            if state["trajectory"]:
                print("研究轨迹：" + str(directory / "trajectory.md"))
            if not interactive:
                return 0
            followup = input("\n补充条件继续研究，/new 新建需求，/exit 结束 > ").strip()
            if followup == "/new":
                requirement = input("业务需求 > ").strip()
            elif followup and followup != "/exit":
                requirement = state["requirement"] + "\n用户后续补充：" + followup
            else:
                break
        return 0
    except (AgentError, OSError, ValueError) as error:
        print("运行失败：" + str(error), file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\n已结束。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
