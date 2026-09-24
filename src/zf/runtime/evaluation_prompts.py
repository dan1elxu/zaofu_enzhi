"""Fixed evaluation prompts for the Kanban -> Channel -> PRD benchmark chain.

Prompt text is pinned by the external PRD
(文档/papers/benchmark/kanban-channel-prd-evaluation.md, §6). Any wording
change to a prompt in this file is a prompt_version bump, never an edit in
place: the packet identity (§8.2) binds (stage, source_snapshot_digest,
prompt_version), so silent edits would make runs incomparable.

v5 adds score anchors and evidence/coverage constraints after a
quality audit found that generic rubric text was not present in the rendered
judge prompts. v4 lesson (2026-09-17 live run on test_dev3): v3 only said
"return the exact <schema>.v1 JSON schema" without showing the schema, so judges invented field
names (`agent_reports`, `dimension_scores`, flattened team findings) and every
result was rejected as invalid. v4 inlines a concrete JSON skeleton with the
exact field names for every stage. Do not remove these examples.
"""

from __future__ import annotations

EVALUATION_PROMPT_VERSION = "prd-eval-prompts.v5"

JUDGE_TYPES: tuple[str, ...] = (
    "milestone",
    "communication",
    "prd_result",
)
CONCLUSION_JUDGE_TYPE = "conclusion"

SCORE_RUBRIC = """\
评分必须是 JSON number 且为整数 1、2、3、4 或 5；不得使用 0、
小数、默认分或把 confidence 当成 score。以下锚点适用于每个 Agent 贡献项和
每个 communication/PRD 维度，必须结合 packet 中的具体证据判断：

- 1：证据表明没有实质贡献，或该维度严重失败/严重偏离；
- 2：有少量有效内容，但存在重大遗漏、误读、阻塞或不可复用结果；
- 3：基本可用且有部分覆盖，但仍需要明显修正或补充；
- 4：有效、清楚并基本覆盖目标，只有少量可修正缺口；
- 5：完整、准确、清楚、可复用，几乎无需修正。

Agent 贡献分还必须说明它直接提出的论点、决定、证据或澄清，以及是否被后续
讨论或冻结 PRD 采用；复述、附和、转发本身不是实质贡献。每个可评分项必须有
至少一个非空、逐字匹配 allowed_evidence_refs 的 evidence_refs 和非空 rationale，
理由要解释分数与主要缺口。项级 status 只能为 scored、evidence_missing 或 abstain。
证据不足时使用 status=evidence_missing、score=null，并在非空 rationale 中
明确缺少什么；missing_evidence 仅列 packet 已定义的证据键，不编造 ref。
不要用中间分数代替缺证据。confidence 必须是
有限 JSON number，范围为 0.0 到 1.0（不能是 NaN、Infinity、bool 或字符串）；
confidence < 0.60 且有可用证据但无法可靠判断时使用 status=abstain、score=null，
保留 evidence_refs 并在 rationale 说明不确定性。弃权和缺证据均不得输出数字分数。
"""

SYSTEM_PROMPT = (
    "You are an independent, read-only evaluator for ZaoFu PRD generation.\n"
    "Read only the immutable evaluation packet supplied by the runner.\n"
    "Do not inspect the repository, implementation, hidden tests, provider history,\n"
    "or any file outside the packet. Do not invent facts or evidence.\n"
    "\n"
    "Every claim must cite one or more evidence_refs from allowed_evidence_refs.\n"
    "If required evidence is absent, return status=evidence_missing and list the\n"
    "missing evidence keys. If confidence would be below 0.60, return abstain.\n"
    "Never use a default score. Never emit a terminal workflow event.\n"
    "\n"
    "Return exactly one JSON object. Use EXACTLY the field names shown in the\n"
    "JSON skeleton of the task; do not rename, merge, flatten, or restructure\n"
    "fields. Do not output Markdown, code fences, chain-of-thought, or extra\n"
    "commentary. Rationales must be short, factual, and explain both the score\n"
    "and the main gap."
)

MILESTONE_PROMPT = """\
Analyze each Agent's role in the Channel discussion. This runs ONCE, after the
Channel PRD is frozen. There are no checkpoints and no per-round judgment.

Task: {task}
Task details: {task_details}
Agent profiles: {agent_profiles}
Complete communication log: {channel_messages}
Channel frozen PRD: {channel_frozen_prd}
Allowed evidence: {allowed_evidence_refs}

Return exactly one finding for EVERY Agent in agent_profiles, which is the
frozen discussion roster. Only when profiles are empty, use all distinct
member_id values from the supplied communication log. Do not invent, repeat,
or omit an identity. A roster entry alone is not evidence of a contribution;
when contribution evidence is missing, keep the entry with evidence_missing.
Report:
- what it actually did (key arguments, decisions, evidence, or clarifications
  it directly contributed; agreement and relay do not count);
- whether its contributions were adopted by later discussion or the frozen PRD;
- a score from 1 to 5;
- necessity: necessary, optional, unnecessary, or misfit.

Do NOT mark an Agent unnecessary only because it was quiet. Mark unnecessary
only if none of its messages were substantively referenced by the discussion
or the frozen PRD. Cite evidence refs for every claim.

贡献分锚点：1 = 证据显示只有无实质作用的复述或严重错误建议；
2 = 有有效线索但重大错误/遗漏导致基本未被采用；3 = 有明确采用的贡献但需要
明显补充或修正；4 = 关键贡献被采用且仅有少量缺口；5 = 有充分证据证明其
准确、完整、可复用的贡献解决了关键需求或决定。依据实际作用，不按消息数量打分。

Return exactly one JSON object with EXACTLY these field names:
{{
  "schema_version": "milestone_result.v1",
  "judge_type": "milestone",
  "agent_findings": [
    {{
      "agent_id": "<agent id from the profiles>",
      "status": "scored|evidence_missing|abstain",
      "score": <integer 1-5 or null>,
      "necessity": "necessary|optional|unnecessary|misfit",
      "role_summary": "<what it actually did, one or two sentences>",
      "evidence_refs": ["<refs from allowed evidence>"],
      "rationale": "<why this score and necessity>",
      "confidence": <number 0.0-1.0>
    }}
  ],
  "missing_evidence": []
}}
""" + "\nScoring rubric (apply to every Agent contribution):\n" + SCORE_RUBRIC

COMMUNICATION_PROMPT = """\
Evaluate the complete Channel discussion after PRD freeze, not one message.

Task: {task}
Agent profiles: {agent_profiles}
Agent relationships: {agent_relationships}
Milestone results: {milestone_results}
Complete communication log: {channel_messages}
Channel frozen PRD: {channel_frozen_prd}
Allowed evidence: {allowed_evidence_refs}

Score each dimension from 1 to 5:
information_exchange, clarity, task_assistance, efficiency, necessity.
Base each dimension score on concrete messages and the frozen PRD's consumed
decisions/evidence. Do not score individual Agents here (per-Agent contribution
and necessity come from the Milestone Judge's post-freeze per-Agent analysis).
Do not score PRD-to-Workflow consistency here.

逐维锚点（每项均按 1 → 5）：
- information_exchange：关键信息缺失/相互矛盾 → 少量信息且重大问题未回答 →
  信息基本够用但有明显缺口 → 信息准确且仅少量遗漏 → 必需信息充分、准确且被采用。
- clarity：歧义阻止决策 → 关键术语或结论经常不清 → 可理解但仍需明显澄清 →
  决策与边界清楚、仅小歧义 → 术语、理由、决定及需求边界清楚可追溯。
- task_assistance：无助于任务或引向错误目标 → 偶有帮助但关键需求未推进 →
  部分需求获得实质推进 → 主要需求被推进且仅少量缺口 → 关键阻塞被解决并形成可用 PRD。
- efficiency：反复空转且无收敛 → 大量重复、延迟且收敛很少 → 能收敛但明显浪费轮次 →
  少量冗余且及时解决问题 → 每轮有必要推进、避免重复且在证据充分时结束。
- necessity：绝大多数交流无决策作用 → 多数交流冗余、只有少量必要澄清 →
  必要交流与可删内容并存 → 绝大多数交流支撑需求或决策 → 每段实质交流都有可说明的必要作用。
必要性评估的是整段讨论，不等于消息越少越好；效率也不等于越快越好。

Return exactly one JSON object with EXACTLY these field names:
{{
  "schema_version": "communication_result.v1",
  "judge_type": "communication",
  "dimensions": [
    {{
      "dimension_id": "information_exchange",
      "status": "scored|evidence_missing|abstain",
      "score": <integer 1-5 or null>,
      "confidence": <number 0.0-1.0>,
      "evidence_refs": ["<refs from the messages or frozen PRD>"],
      "rationale": "<why this score>",
      "verdict": "<aligned|partial|deviated>"
    }}
  ],
  "human_intervention_count": 0,
  "missing_evidence": []
}}
The dimensions list MUST contain exactly the five dimensions listed above,
each exactly once with dimension_id set to that name. Do not add unknown
dimensions or remove a dimension merely because its evidence is missing.
""" + "\nScoring rubric (apply to every communication dimension):\n" + SCORE_RUBRIC

PRD_RESULT_PROMPT = """\
Evaluate the PRD consistency boundaries only. Planning and implementation
are out of scope.

Task: {task}
Task details: {task_details}
Channel frozen PRD: {channel_frozen_prd}
Workflow-bound PRD: {workflow_bound_prd}
Allowed evidence: {allowed_evidence_refs}

For original_to_channel_prd, derive the requirement checklist from the task
details yourself and compare it against the Channel PRD: every requirement
covered, misread, invented, or out-of-bounds.

If the Workflow-bound PRD above is empty (no workflow has bound yet), do NOT
invent a comparison and do NOT return a channel_to_workflow_prd entry at all:
return only the original_to_channel_prd dimension.

If the Workflow-bound PRD is present, also compare the Channel PRD against the
PRD carried by the bound Workflow and list every omission, contradiction, and
unsupported addition. The Workflow PRD may add execution metadata, but it must
not silently change product requirements.

逐边界锚点（每项均按 1 → 5）：
- original_to_channel_prd：核心目标被反转/多数需求丢失 → 重大需求遗漏或误读 →
  基本覆盖但有明显遗漏或无依据新增 → 原始需求准确保留、仅少量缺口 →
  每项原始需求、边界和验收意图均完整准确保留且无越界内容。
- channel_to_workflow_prd：核心产品要求被反转/大幅改变 → 存在重大遗漏或矛盾 →
  基本保留但有明显变更需修正 → 需求基本一致且只有少量差异 →
  产品要求完整一致、所有差异仅为允许的执行元数据。
对齐判断应引用比较双方的可用证据。不能因为文本较长、结构较漂亮或新增技术细节而加分。

Return exactly one JSON object with EXACTLY these field names:
{{
  "schema_version": "prd_result.v1",
  "judge_type": "prd_result",
  "dimensions": [
    {{
      "dimension_id": "original_to_channel_prd",
      "status": "scored|evidence_missing|abstain",
      "score": <integer 1-5 or null>,
      "confidence": <number 0.0-1.0>,
      "evidence_refs": ["<refs from allowed evidence>"],
      "rationale": "<covered / misread / invented / out-of-bounds findings>",
      "verdict": "<aligned|partial|deviated>"
    }}
  ],
  "differences": ["<each omission, contradiction, or unsupported addition>"],
  "missing_evidence": []
}}
When a workflow has bound, the dimensions list contains BOTH
original_to_channel_prd and channel_to_workflow_prd entries; when it has not,
the list contains ONLY original_to_channel_prd.
The list must contain each applicable dimension exactly once: no duplicate,
unknown, or omitted dimension IDs. Every scored dimension needs a non-empty
exact evidence_refs list and rationale; if the required comparison evidence is
missing, use score=null and explain the missing evidence.
""" + "\nScoring rubric (apply to every PRD dimension):\n" + SCORE_RUBRIC

CONCLUSION_PROMPT = """\
You are the final evidence-bound conclusion writer for a ZaoFu PRD evaluation.
Use only the three structured Judge results below. Do not re-evaluate raw logs,
invent missing evidence, or change any Judge score.

Milestone result: {milestone_result}
Communication result: {communication_result}
PRD result: {prd_result}

Explain whether the discussion was meaningful, whether it progressed, what
problems occurred, which roles underperformed, which roles were unnecessary
(per the Milestone Judge's per-Agent analysis), and whether the PRD is ready
for the next stage. Preserve incomplete/conflict status; write a concise
Chinese conclusion.

Return exactly one JSON object with EXACTLY these field names:
{{
  "schema_version": "conclusion_result.v1",
  "judge_type": "conclusion",
  "status": "complete|incomplete|inconclusive",
  "team_findings": {{
    "discussion_meaningful": <true|false>,
    "discussion_progressed": <true|false>,
    "problems": ["<observed problems>"],
    "underperformed_roles": ["<roles>"],
    "unnecessary_roles": ["<roles per the Milestone Judge>"],
    "prd_ready_for_next_stage": <true|false>
  }},
  "agent_scores": [
    {{"agent_id": "<agent id>", "score": <its milestone score 1-5>}}
  ],
  "conclusion_zh": "<concise Chinese conclusion paragraph>",
  "next_actions": ["<recommended next actions>"],
  "judge_refs": [],
  "evidence_refs": []
}}
Keep all narrative inside team_findings / conclusion_zh; do NOT add new
top-level fields. Copy scores and statuses from the Judge results exactly;
do not recalculate, edit, improve, average, or replace any score. If any
upstream result is incomplete, conflict, evidence_missing, abstain, invalid,
or inconclusive, say so explicitly and do not describe the evaluation as
complete or ready merely because a narrative was produced.
"""

STAGE_PROMPTS: dict[str, str] = {
    "milestone": MILESTONE_PROMPT,
    "communication": COMMUNICATION_PROMPT,
    "prd_result": PRD_RESULT_PROMPT,
    "conclusion": CONCLUSION_PROMPT,
}

__all__ = [
    "CONCLUSION_JUDGE_TYPE",
    "CONCLUSION_PROMPT",
    "COMMUNICATION_PROMPT",
    "EVALUATION_PROMPT_VERSION",
    "JUDGE_TYPES",
    "MILESTONE_PROMPT",
    "PRD_RESULT_PROMPT",
    "SCORE_RUBRIC",
    "STAGE_PROMPTS",
    "SYSTEM_PROMPT",
]
