# v0.2 研究依据与实现映射

## 证据标准

本项目要求关键机制有可追溯论文依据。以下区分作者的实验结果、开源实现、我们的适配和本地验证。未完成第三方独立复现实验的核查，不把“有论文/有代码”写成“被独立复现”。论文在其原始任务上有效，也不证明换成 DeepSeek 后对技术方案选型有效。

v0.2 默认开启思考并实现原 ReAct 的编号 Thought / Action / Observation 文本交互协议，但不是整篇论文的基准复现。业务需求字段、方案卡片和硬约束分类来自产品需求。只有任务相关、可用现有 API 实施的机制进入基线。

## 机制与论文

| 机制 | 原始依据与验证 | 当前实现 | 差异与待验证事项 |
| --- | --- | --- | --- |
| 根据观察选择工具并继续研究 | [ReAct，ICLR 2023，§2–3、附录C.1](https://arxiv.org/html/2210.03629v3)：作者在问答、事实核验及交互任务比较不同基线；[作者代码](https://github.com/ysymyth/ReAct) | 默认 `research_react` 解析单步 Thought/Action，实际执行后构造 Observation；使用停止字符串阻止模型生成观察，Finish 为显式提交。另保留原生 tools 对照 | 协议按原文实现，示例改写为两个虚构选型案例；原六个HotpotQA示例、Wikipedia搜索语义和模型不同。新增Fetch，Lookup返回网页匹配片段，Finish摘要交给后续报告阶段。因此是协议级实现与任务适配，不是原论文结果复现 |
| 根据阶段性信息调整检索 | [IRCoT，ACL 2023](https://aclanthology.org/2023.acl-long.557/)：作者在四个多跳问答数据集测试交替检索与推理；[作者代码](https://github.com/stonybrooknlp/ircot) | 在工具观察后允许新的定向查询；与固定检索 `single` 对照 | 仅作为“多步检索可能有用”的研究动机；没有实现 IRCoT 的逐句 CoT 检索算法，不把它列为已复现机制，也不把它叠加成另一个独立 Agent |
| 从检索资料生成带引用的回答 | [ALCE，EMNLP 2023，§2、§4](https://arxiv.org/html/2305.14627v2)：检索后综合生成，并对比交互检索及事后补引文；[作者代码与数据](https://github.com/princeton-nlp/ALCE) | 同一个报告提示把来源正文作为上下文，生成事实、来源编号与逐字引文；`single` 为固定检索基线 | 这里是开放网页与方案卡，不是 ALCE 原检索语料、原模型或原输出。固定检索和循环都需实测，不能预设循环更优 |
| 评价引用是否支持结论，以及支持是否完整 | ALCE §3、§6：作者提出引用质量评价，并与人工判断比较 | `evidence_gate` 只检查引文存在；`review_template` 和 `evaluate.summarize` 提供人工语义支持及约束评审 | 字符匹配仅是可追溯性的必要条件，不是 ALCE 的 NLI 语义判定。当前未实现完整引用 precision/recall；人工支持率只是自定义指标 |

ALCE §5.1 的交互检索在其设置下未带来提升。这是保留简单基线的直接理由，不将“更多循环”当作必然收益。

## 明确不作的研究声明

- [Self-RAG](https://arxiv.org/abs/2310.11511) 涉及训练的 reflection tokens。本版未训练这些能力，不声称实现 Self-RAG，也未增加一个没有实验证明收益的自我打分循环。
- [Large Language Models Cannot Self-Correct Reasoning Yet，ICLR 2024](https://arxiv.org/abs/2310.01798) 报告其测试设置下无外部反馈的自纠错局限。本版优先保留外部观察和人工评审；这个结果不被推广成“所有模型都不能纠错”。
- JSON 字段、来源编号和字面引文校验是工程保障，不是语义正确性的学术证明。
- 模型对“符合硬约束”的判断仍可能错；程序只保证覆盖字段、证据缺失降级与显式状态处理，没有证明部署能力或许可证结论正确。

## 状态、循环与停止

状态包含需求、brief、工具历史、sources、trajectory、finish_summary、停止原因、预算、思考开关/强度、报告与调用统计。信息不跨运行自动积累。网页摘要只用于发现线索，来源只有成功fetch后才编号。

每轮：模型输出当前 Thought/Action → 程序解析与执行 → 保存真实Observation → 将观察返回模型。正文与本地Lookup片段供报告阶段使用。默认必须显式Finish才能正常提交；没有正文的提交单独标记。预算或可选故障策略中断则为部分结果；`research_completion_verified` 始终为false，避免将模型提交等同于独立验证通过。

默认 max_steps=7 参考作者HotpotQA代码的 `range(1,8)`；包含文本Finish和格式错误。max_tools=8是独立的工具动作保护，未证明最优。max_stalls=0默认关闭旧的连续两次无进展退出规则，显式开启才适用。12,000字符上下文、结构修正1次、HTTP重试1次、思考输出上限16,384 token也都是工程参数。它们需要任务内验证，不构成算法效果证明。

正文完整存储；模型见前段和实际Lookup的额外片段。我们不声称看过网页所有内容、来源本身真实可靠或PoC性能已验证。接口reasoning_content是服务返回字段，与显式Thought不同，原样记录而不当作事实证据。原生工具路径按[DeepSeek官方要求](https://api-docs.deepseek.com/guides/thinking_mode/)回传该字段，文本路径不携带tools，API不会把旧reasoning_content拼入上下文，显式Thought则随普通消息保留。

## 他人的停止条件与迁移边界

| 来源 | 主动或任务结束 | 上限或异常处理 | 能说明什么 |
| --- | --- | --- | --- |
| [ReAct §3.1与§3.2](https://arxiv.org/html/2210.03629v3) | `Finish[answer]` 在环境中设置done；不是先判答案正确再允许停止 | 论文ReAct→CoT-SC组合实验使用HotpotQA七步、FEVER五步；作者报告更长轨迹收益有限 | 步数有原任务经验依据，不能直接推广为网页选型任务的最优预算 |
| [作者HotpotQA notebook](https://github.com/ysymyth/ReAct/blob/master/hotpotqa.ipynb)及[wikienv.py](https://github.com/ysymyth/ReAct/blob/master/wikienv.py) | 环境收到finish后done=True；运行中检查done退出 | notebook最多七轮，未结束则调用空finish；正确性评分由任务包装器另行计算 | “终止”和“答案正确”是不同概念；本项目不伪造空Finish，而记录截断 |
| [Open Deep Research研究控制代码](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/deep_researcher.py)及[配置](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/configuration.py) | 主管可调用ResearchComplete，也可能因没有工具调用结束 | 主管默认迭代参数6，研究员默认工具迭代参数10；具体比较运算见代码 | 这是公开工程实现的参考，不是论文证明6或10最优；循环迭代数也不是单个工具动作数 |

本项目默认研究提交信号用Finish；原生tools没有工具调用但也没有finish时记missing_finish。没有独立的语义完成度裁判，也没有套用其他任务的训练停止器。预算中断、模型主动提交、程序/接口故障在日志中分别记录。

## 本项目验证协议

1. **控制流程测试**：用可控响应替身运行同一套 engine，覆盖工具协议、预算、重复、非法参数、网页失败、空证据、引文错误及未知状态。该测试证明程序行为，不证明模型效果。
2. **真实业务对照**：`direct`、`single`、`agent` 使用同一模型和需求。single/agent 共享报告与门槛，比较检索完整性、人工事实支持、约束违反、可操作性，以及耗时/token/动作数。direct 单独人工拆分主张，避免用不同格式的机械统计做伪公平比较。
3. **资料与题集**：初始十题分dev/holdout。先在dev用步数5/7/10/15作预算曲线、思考开关和text/tools作对照，记录Finish比例、截断率、人工支持、约束违反、token、耗时；固定其他变量后再比较。文本步数扫描时将max_tools统一设为30，避免八动作上限掩盖更长步数的效果；协议对照则同时报告动作和轮次，不能把轮数当相同工作量。参数冻结后测holdout，每题至少三次。由技术人员标注可接受条件、参考资料和不可接受错误；题目本身不是标准答案。
4. **验收判断**：无 API Key 时只报告离线测试状态。真实试验后才决定循环是否优于固定检索；未标注项不算通过，样本规模与不确定性一并报告。没有提升时保留 single，依据轨迹定位问题，再调整设计。

目前没有真实模型质量结论或第三方复现分数。部署前需要完成上述在线对照和人工评审。本文件给出可检查的设计依据，不构成已验证整个产品有效的声明。

### 2026-11-03 接口烟雾测试记录

- 开启思考、low强度、文本协议、max_steps=4/max_tools=3的一次实际研究，走通需求JSON、搜索、读取、再搜索与报告阶段。五次模型响应均返回reasoning_content，结束为tool_budget_exhausted。读取的页面是错误/挑战提示，报告没有生成候选，未将错误页当作产品能力证据。记录目录：`runs/2026-11-03T170941_0800_4531a594`。
- 原生工具协议两轮实际请求成功：第一轮获得工具调用及reasoning_content，回传原字段和本地测试工具结果后，第二轮正确返回测试结果。另一次文本协议测试显式Finish提交“证据不足”，未调用网页工具。摘要在`runs/thinking_protocol_smoke.json`。
- 以上只验证接口和控制协议；不是选型质量、停止策略优劣或原论文结果的评测。
