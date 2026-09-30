"""Fixed evaluation prompts for the Kanban -> Channel -> PRD benchmark chain.

Prompt text is pinned by the Channel/PRD evaluation contract. Any wording
change to a prompt in this file is a prompt_version bump, never an edit in
place: the packet identity (§8.2) binds (stage, source_snapshot_digest,
prompt_version), so silent edits would make runs incomparable.

v7 pins the canonical 0–4 prompt family.  The only quality prompts are the
bounded task-type classifier, the three role-bound subagents (contribution,
discussion, PRD fidelity), and the main Judge conclusion.  Prompt text is
versioned because packet identity binds the prompt version.
"""

from __future__ import annotations

EVALUATION_PROMPT_VERSION = "prd-eval-prompts.v7"

JUDGE_TYPES: tuple[str, ...] = (
    "task_type",
    "contribution",
    "discussion",
    "prd_fidelity",
    "conclusion",
)
CONCLUSION_JUDGE_TYPE = "conclusion"

SCORE_RUBRIC = """\
角色绑定评估统一使用 0–4 分整数：
- 0：有充分证据表明完全失败、没有实际作用或严重偏离目标；
- 1：明显不足，仅有极少有效内容；
- 2：部分达到目标，但仍有明显遗漏、重复或修正成本；
- 3：基本达到目标，只有可控缺口；
- 4：完整、准确、可复用，几乎无需修正。

Agent 贡献分与必要性是两个独立判断。贡献分描述实际工作的质量和影响；
necessity 只回答该 Agent 对完成任务是否需要：must、optional、not_needed。
证据不足时使用 necessity=undetermined、necessity_status=incomplete，不能猜测。
评分项必须引用 allowed_evidence_refs 中的证据；缺证据使用 score=null、
status=evidence_missing。confidence < 0.60 使用 status=abstain、score=null。
主 Judge 只能汇总 subagent 原始分数，不能重算、平均、修正或覆盖任何分数。
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

TASK_TYPE_PROMPT = """\
You are the bounded task-type classifier for a frozen Channel/PRD packet.
Read only the supplied packet. Classify the task as exactly one of bugfix,
refactor, greenfield, or ambiguous. Use the task requirements and discussion
evidence, never provider identity or message volume. If evidence is missing,
or confidence is below 0.60, return ambiguous and do not authorize any role
subagent. This classifier chooses a pre-registered role; it must not invent a
new role or rubric.

Task: {task}
Task details: {task_details}
Frozen discussion log: {channel_messages}
Channel frozen PRD: {channel_frozen_prd}
Allowed evidence: {allowed_evidence_refs}

Return exactly one JSON object with EXACTLY these fields:
{{
  "schema_version": "task_type_result.v1",
  "judge_type": "task_type",
  "status": "scored|ambiguous|evidence_missing|abstain",
  "task_type": "bugfix|refactor|greenfield|ambiguous",
  "confidence": <number 0.0-1.0>,
  "evidence_refs": ["<refs from allowed evidence>"],
  "rationale": "<short evidence-bound explanation>",
  "alternative": "<optional alternative type>",
  "missing_evidence": []
}}
"""


CONTRIBUTION_PROMPT = """\
You are the isolated role-bound Agent Contribution evaluator. Use only the
immutable packet and the pre-registered role metadata in it. Do not read other
subagent results. Evaluate every Agent in the frozen roster. Contribution
quality and Agent necessity are independent: a required Agent may perform
poorly, and a useful Agent may still be optional or not needed because its
responsibility is redundant.

Task: {task}
Task details and selected role metadata: {task_details}
Agent profiles: {agent_profiles}
Complete communication log: {channel_messages}
Channel frozen PRD: {channel_frozen_prd}
Allowed evidence: {allowed_evidence_refs}

For each Agent, cite concrete arguments, decisions, evidence, clarifications,
or artifacts. Relay and agreement alone are not contribution. Necessity is
must when that Agent's capability is required by acceptance or risk coverage;
optional when it improves quality/efficiency but the task can pass without it;
not_needed when no unique capability is required or the responsibility is
redundant. If necessity cannot be established, use undetermined with
necessity_status=incomplete. Do not infer necessity from message count.

Return exactly one JSON object with EXACTLY these fields:
{{
  "schema_version": "contribution_result.v2",
  "judge_type": "contribution",
  "task_type": "bugfix|refactor|greenfield",
  "role_id": "<registered role id>",
  "role_version": "<role revision>",
  "role_metadata": {{}},
  "score_scale": "0-4",
  "agent_findings": [
    {{
      "agent_id": "<agent id from profiles>",
      "status": "scored|evidence_missing|abstain",
      "score": <integer 0-4 or null>,
      "necessity": "must|optional|not_needed|undetermined",
      "necessity_status": "complete|incomplete",
      "role_summary": "<what it actually did>",
      "evidence_refs": ["<refs from allowed evidence>"],
      "rationale": "<why this contribution score and necessity>",
      "confidence": <number 0.0-1.0>
    }}
  ],
  "missing_evidence": []
}}
""" + "\n" + SCORE_RUBRIC


DISCUSSION_PROMPT = """\
You are the isolated role-bound Discussion Effectiveness evaluator. Use only
the immutable packet and selected role metadata. Do not read Agent Contribution
or PRD-fidelity subagent results; judge this dimension independently.

Task: {task}
Task details and selected role metadata: {task_details}
Agent profiles: {agent_profiles}
Agent relationships: {agent_relationships}
Complete communication log: {channel_messages}
Channel frozen PRD: {channel_frozen_prd}
Allowed evidence: {allowed_evidence_refs}

Score exactly these dimensions: information_exchange, clarity, task_assistance,
efficiency, necessity. Consider whether discussion covered requirements and
constraints, drove explicit decisions, used evidence, resolved risks and
ambiguity, and avoided repeated or divergent work. Do not score individual
Agents here.

Return exactly one JSON object with EXACTLY these fields:
{{
  "schema_version": "discussion_result.v2",
  "judge_type": "discussion",
  "task_type": "bugfix|refactor|greenfield",
  "role_id": "<registered role id>",
  "role_version": "<role revision>",
  "role_metadata": {{}},
  "score_scale": "0-4",
  "dimensions": [
    {{
      "dimension_id": "information_exchange|clarity|task_assistance|efficiency|necessity",
      "status": "scored|evidence_missing|abstain",
      "score": <integer 0-4 or null>,
      "confidence": <number 0.0-1.0>,
      "evidence_refs": ["<refs from allowed evidence>"],
      "rationale": "<why this score>",
      "verdict": "aligned|partial|deviated"
    }}
  ],
  "human_intervention_count": 0,
  "missing_evidence": []
}}
""" + "\n" + SCORE_RUBRIC


PRD_FIDELITY_PROMPT = """\
You are the isolated role-bound PRD Fidelity evaluator. Use only the immutable
packet and selected role metadata. Do not read other subagent results. Compare
the original requirements with the frozen Channel PRD, and when present also
compare the Workflow-bound PRD. Do not reward text length or technical detail.

Task: {task}
Task details and selected role metadata: {task_details}
Channel frozen PRD: {channel_frozen_prd}
Workflow-bound PRD: {workflow_bound_prd}
Allowed evidence: {allowed_evidence_refs}

Evaluate original_to_channel_prd by comparing the original request and its
accepted clarifications with the frozen Channel PRD. Cite evidence from both
sides and list every omission, contradiction or unsupported addition.

If Workflow-bound PRD is empty, return ONLY original_to_channel_prd.
If it is present, the dimensions list MUST contain BOTH
original_to_channel_prd and channel_to_workflow_prd, each exactly once.
For channel_to_workflow_prd, compare the frozen Channel PRD with the bound
Workflow PRD. Execution metadata may be added, but product requirements must
not silently change. Missing comparison evidence means score=null with an
explicit evidence_missing finding, never an invented comparison or 0 score.

Return exactly one JSON object with EXACTLY these fields:
{{
  "schema_version": "prd_fidelity_result.v2",
  "judge_type": "prd_fidelity",
  "task_type": "bugfix|refactor|greenfield",
  "role_id": "<registered role id>",
  "role_version": "<role revision>",
  "role_metadata": {{}},
  "score_scale": "0-4",
  "dimensions": [
    {{
      "dimension_id": "original_to_channel_prd",
      "status": "scored|evidence_missing|abstain",
      "score": <integer 0-4 or null>,
      "confidence": <number 0.0-1.0>,
      "evidence_refs": ["<refs from allowed evidence>"],
      "rationale": "<covered, omitted, contradicted or unsupported findings>",
      "verdict": "aligned|partial|deviated"
    }}
  ],
  "differences": [],
  "missing_evidence": []
}}
""" + "\n" + SCORE_RUBRIC


CONCLUSION_PROMPT = """\
You are the main Judge summarizer. Use only the three immutable role-bound
subagent results below. You may summarize conflicts and limitations, but you
must copy every subagent score and necessity exactly. You cannot adjust,
average, replace, or silently overwrite a subagent score. Do not add an
overall/team contribution score.

Task type: {task_type}
Selected role reference: {role_ref}

Agent Contribution result: {contribution_result}
Discussion Effectiveness result: {discussion_result}
PRD Fidelity result: {prd_fidelity_result}

Return exactly one JSON object with EXACTLY these fields:
{{
  "schema_version": "conclusion_result.v2",
  "judge_type": "conclusion",
  "status": "complete|incomplete|inconclusive",
  "score_scale": "0-4",
  "agent_scores": [
    {{"agent_id": "<id>", "score": <copied integer 0-4 or null>, "necessity": "<copied label>", "necessity_status": "complete|incomplete"}}
  ],
  "team_findings": {{
    "discussion_meaningful": <true|false>,
    "discussion_progressed": <true|false>,
    "problems": [],
    "underperformed_roles": [],
    "unnecessary_roles": [],
    "prd_ready_for_next_stage": <true|false>
  }},
  "conclusion_zh": "<concise Chinese summary>",
  "next_actions": [],
  "judge_refs": [],
  "evidence_refs": []
}}
"""


EVALUATION_PROMPTS: dict[str, str] = {
    "task_type": TASK_TYPE_PROMPT,
    "contribution": CONTRIBUTION_PROMPT,
    "discussion": DISCUSSION_PROMPT,
    "prd_fidelity": PRD_FIDELITY_PROMPT,
    "conclusion": CONCLUSION_PROMPT,
}

__all__ = [
    "CONCLUSION_JUDGE_TYPE",
    "CONCLUSION_PROMPT",
    "CONTRIBUTION_PROMPT",
    "DISCUSSION_PROMPT",
    "EVALUATION_PROMPT_VERSION",
    "EVALUATION_PROMPTS",
    "JUDGE_TYPES",
    "PRD_FIDELITY_PROMPT",
    "SCORE_RUBRIC",
    "SYSTEM_PROMPT",
    "TASK_TYPE_PROMPT",
]
