# 业务需求驱动的 AI 方案调研 Agent · v0.2

面向技术负责人：输入业务目标与条件，寻找可复用方案，生成附原文引证的比较结果和 PoC 计划。第一批场景为企业知识库问答与客服。

这是本地命令行程序，支持 Python 3.8+，只用标准库，不需要安装第三方依赖。模型、搜索、网页读取使用 DeepSeek 官方 API。

## 先运行离线示例

在本项目目录运行：

```powershell
python -m solution_agent --demo
```

示例不联网、不收费，使用明确标注的虚构产品和脚本响应，**只检查程序流程，不展示真实模型质量**。

程序打印输出目录，包含：

| 文件 | 用途 |
| --- | --- |
| `report.md` | 可阅读的方案报告 |
| `state.json` | 需求、原始正文、报告、剔除的事实、调用统计 |
| `trace.jsonl` | 每次模型响应、工具请求与结果，便于排查 |
| `trajectory.md` | 本次真实执行的 Thought / Action / Observation 轨迹与结束原因 |
| `review.json` | 供人工填写的证据与约束评审表 |
| `failure.json` | 失败或中断时保存错误、已取得的来源与统计 |

## 接入真实 API

在 PowerShell **本地**配置环境变量。不要把真实 Key 写进代码、提交到 Git 或发送给聊天：

```powershell
$env:DEEPSEEK_API_KEY = "在本机填写你的Key"
$env:DEEPSEEK_MODEL = "deepseek-flash"
python -m solution_agent --check-config
```

也可在项目根目录的 `.env` 中设置 `DEEPSEEK_API_KEY=你的Key` 与 `DEEPSEEK_MODEL=deepseek-flash`。程序只读取这两个字段，不执行文件内容；非空环境变量优先于 `.env`。`.env` 已被 Git 忽略，不应手动强制提交。

`--check-config` 只检查配置是否存在，不联网、不验证 Key 有效性，也不会显示 Key。

官方当前 `deepseek-flash` 对应 DeepSeek-V4.1-Flash-1017，但这是可更新的服务别名，不是版本锁定。每次运行记录请求模型名、接口返回模型名与时间；如服务商不能提供固定版本，不能保证之后完全复现。

单次运行（会调用付费模型与搜索/读取接口）：

```powershell
python -m solution_agent --question "我们希望为客服建设企业知识库，必须本地部署，回答附原文引用。团队两名开发，请比较可以复用的方案。"
```

需求较长时，可以写入 UTF-8 文本，用 `--request-file 需求.txt` 读取。单次模式不追问，未提供的信息保留为未知。

交互使用：

```powershell
python -m solution_agent
```

先输入业务需求，回答最多三个澄清问题；不知道可以回车。报告生成后可补充条件继续研究，`/new` 新建需求，`/exit` 退出。每轮新建记录目录并重新检索；它没有跨运行的长期记忆。

## 研究机制与边界

主流程：需求结构化 → 可选澄清 → 研究 → 基于正文生成候选 → 程序溯源检查 → 报告与人工评审表。

- 默认开启 DeepSeek 思考模式，`reasoning_effort=high`。接口返回的 `reasoning_content` 保存在原始响应日志中；它与显式输出的 Thought 不同，不作为事实证据或正确性证明。
- 默认 `--research-style react`：按论文的编号文本协议，一次生成一个 `Thought n` 与 `Action n`，由程序生成真实 `Observation n`，再进入下一步。支持 `Search`、`Fetch`、`Lookup`、`Finish`。模型不能自行填充 Observation；请求使用对应的 stop 序列阻止继续生成观察文本。
- 提示词包含两个手写的虚构领域示例，最后报告只引用真实读取的资料。它复现交互协议，未照搬原论文的六个 HotpotQA 示例、Wikipedia 环境、模型或实验分数。`Fetch` 是网页任务的扩展，`Lookup` 对当前读取页面依次定位关键词。
- `--research-style tools` 保留原生工具调用对照，必须调用 `finish(summary)` 显式提交；模型不调用工具但也没有 finish 时记为 `missing_finish`。思考模式下原生工具消息会原样回传 `reasoning_content`，遵循官方协议。
- 只允许成功读取的正文作为报告证据。引文不存在或来源编号错误的事实会被剔除；依赖这些事实的约束状态降为未知。
- 约束为“不符合”的候选标记排除；存在未知约束时只列为条件候选。约束判断由模型产生，仍需人工复核。
- 原文存在检查不等于语义支持，更不等于产品实测。报告始终显示这个边界，不输出百分比“可信度”。
- 网页资料按不可信数据处理；只开放读取和提交动作。正文前 12,000 字符送入模型，完整正文保存在记录中；`Lookup` 找到的额外原文片段也会提供给报告阶段。未读取的尾部信息仍可能被遗漏。
- 没有取得正文时输出证据不足，不凭模型记忆补齐有依据的推荐。

论文与代码的对应、原实验范围、适配差异和验证方法见 [研究设计说明](docs/research.md)。

## 提交与停止

`Finish[摘要与未知项]` 表示模型主动提交，不证明答案正确或调查完整。达到步数或工具预算时标记截断，报告明确可能遗漏；它不伪造一次 Finish。没有来源的 Finish 会单独记为 `explicit_finish_without_sources`，并输出证据不足。

- 默认 `--max-steps 7`，每次文本 Thought/Action 算一步，包含 Finish 和格式错误。这个数参考 ReAct 作者 HotpotQA 的七步设置；换成网页调研后不是经验证的最优值。
- `--max-tools 8` 是另一道读取动作上限。文本模式下一步最多一个动作；原生工具模式一轮可有多个动作，两者的步数与工具数不能混用。
- 旧版默认“连续两次失败退出”已取消。现在 `--max-stalls 0` 默认关闭；需要研究该策略时显式用 `--max-stalls 2`，它是额外工程中断，不称为研究完成。
- 默认思考模式的每次输出上限为 16,384 token，给推理与输出留空间；达到长度上限会报失败，而不使用被截断的 JSON。

对比思考开关与协议时保持其他参数相同，例如：

```powershell
python -m solution_agent --question "同一道需求" --thinking enabled --research-style react
python -m solution_agent --question "同一道需求" --thinking disabled --research-style react
python -m solution_agent --question "同一道需求" --thinking enabled --research-style tools
```

思考强度可通过 `--reasoning-effort low/high/max` 调整。现有已打开的交互进程需退出重启才能加载新版代码。

## 对照实验

同一道题、同一模型分别运行：

```powershell
python -m solution_agent --mode direct --question "你的同一道业务需求"
python -m solution_agent --mode single --question "你的同一道业务需求"
python -m solution_agent --mode agent --question "你的同一道业务需求"
```

| 模式 | 行为 |
| --- | --- |
| `direct` | 整理需求后直接回答，无外部证据，也不通过溯源门槛 |
| `single` | 一次固定搜索，按结果顺序读取最多三个页面，然后生成报告 |
| `agent` | 模型根据观察选择后续搜索、读取或结束 |

single 与 agent 共用报告生成提示及溯源检查；direct 是不同输出形态的能力参照，不能拿它的空引文数与另两组比较为“零错误”。实验必须同时报告模型调用数、token、工具数、耗时与人工质量；这三种模式不是严格等成本实验。

步数、工具预算、可选无进展退出、正文截断与重试次数均需在本任务验证。它们不是人民币预算上限；模型请求、思考输出、最终报告与 HTTP 重试会产生额外开销。应同时记录截断率与人工质量，不能用更大预算的提升来单独证明算法更好。

题集见 `evals/cases.jsonl`。先用 dev 题调整程序，再用 holdout 题测试，避免针对测试题调提示词。对照时建议使用 `--request-file`，让三组获得完全相同的需求和人工补充条件。保存各组运行目录与同一题的对应关系；题集本身不包含已经查证的标准产品答案。

人工打开 `review.json` 内的来源，并结合 `state.json` 完整正文填写：

- `human_supported`：原文是否支持该事实，填 `true` / `false`；尚未核验保留 `null`。
- `human_constraint_violation_in_recommendation`：是否把违反硬约束的方案作为可采用推荐。
- `utility_1_to_5`：推荐是否能帮助决定下一步，并在 notes 写明理由。

汇总评审结果：

```powershell
python -m solution_agent.evaluate "runs\某次运行\review.json"
```

未评审的指标显示 `null`，不会自动当作通过。这是借鉴 ALCE 的自定义人工评审，尚未实现完整 ALCE 的 NLI 评分、引用 precision/recall 或自动正确性评价。

## 测试与状态

```powershell
python -m unittest discover -s tests -v
```

本地测试使用模拟接口，验证控制程序的协议、停止条件、溯源门槛、失败保存和报告输出，不测试模型聪明程度。真实接口连通测试与小规模烟雾测试也不等于业务质量评测，不承诺推荐质量或已复现论文分数。

HTTP 显式 429/5xx 最多重试一次，可能额外计费；网络超时不自动重放，避免无意重复计费。工具错误进入观察记录；模型接口失败或连续两次结构错误会保存失败记录。日志不记录 Key 或 Authorization，但会包含需求和抓取资料，保存在本地 `runs/`，默认不进入 Git。

接口依据：[模型说明](https://api-docs.deepseek.com/quick_start/pricing/)、[思考模式与思考字段回传](https://api-docs.deepseek.com/guides/thinking_mode/)、[工具调用](https://api-docs.deepseek.com/guides/tool_calls/)、[JSON 输出](https://api-docs.deepseek.com/guides/json_mode/)、[Search / Fetch](https://api-docs.deepseek.com/guides/search/)。核对日期：2026-11-03。
