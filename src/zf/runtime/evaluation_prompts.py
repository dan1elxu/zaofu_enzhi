"""Fixed evaluation prompts for the Kanban -> Channel -> PRD benchmark chain.

Prompt text is pinned by the external PRD
(文档/papers/benchmark/kanban-channel-prd-evaluation.md, §6). Any wording
change to a prompt in this file is a prompt_version bump, never an edit in
place: the packet identity (§8.2) binds (stage, source_snapshot_digest,
prompt_version), so silent edits would make runs incomparable.

v4 lesson (2026-09-17 live run on test_dev3): v3 only said "return the exact
<schema>.v1 JSON schema" without showing the schema, so judges invented field
names (`agent_reports`, `dimension_scores`, flattened team findings) and every
result was rejected as invalid. v4 inlines a concrete JSON skeleton with the
exact field names for every stage. Do not remove these examples.
"""

from __future__ import annotations

EVALUATION_PROMPT_VERSION = "prd-eval-prompts.v4"

JUDGE_TYPES: tuple[str, ...] = (
    "milestone",
    "communication",
    "prd_result",
)
CONCLUSION_JUDGE_TYPE = "conclusion"

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

For EVERY participating Agent, report:
- what it actually did (key arguments, decisions, evidence, or clarifications
  it directly contributed; agreement and relay do not count);
- whether its contributions were adopted by later discussion or the frozen PRD;
- a score from 1 to 5;
- necessity: necessary, optional, unnecessary, or misfit.

Do NOT mark an Agent unnecessary only because it was quiet. Mark unnecessary
only if none of its messages were substantively referenced by the discussion
or the frozen PRD. Cite evidence refs for every claim.

Return exactly one JSON object with EXACTLY these field names:
{{
  "schema_version": "milestone_result.v1",
  "judge_type": "milestone",
  "agent_findings": [
    {{
      "agent_id": "<agent id from the profiles>",
      "score": <integer 1-5>,
      "necessity": "necessary|optional|unnecessary|misfit",
      "role_summary": "<what it actually did, one or two sentences>",
      "evidence_refs": ["<refs from allowed evidence>"],
      "rationale": "<why this score and necessity>",
      "confidence": <number 0.0-1.0>
    }}
  ],
  "missing_evidence": []
}}
"""

COMMUNICATION_PROMPT = """\
Evaluate the complete Channel discussion after PRD freeze, not one message.

Task: {task}
Agent profiles: {agent_profiles}
Agent relationships: {agent_relationships}
Milestone results: {milestone_results}
Complete communication log: {channel_messages}
Channel frozen PRD: {channel_frozen_prd}

Score each dimension from 1 to 5:
information_exchange, clarity, task_assistance, efficiency, necessity.
Base each dimension score on concrete messages and the frozen PRD's consumed
decisions/evidence. Do not score individual Agents here (per-Agent contribution
and necessity come from the Milestone Judge's post-freeze per-Agent analysis).
Do not score PRD-to-Workflow consistency here.

Return exactly one JSON object with EXACTLY these field names:
{{
  "schema_version": "communication_result.v1",
  "judge_type": "communication",
  "dimensions": [
    {{
      "dimension_id": "information_exchange",
      "score": <integer 1-5>,
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
each as its own entry with dimension_id set to that name.
"""

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

Return exactly one JSON object with EXACTLY these field names:
{{
  "schema_version": "prd_result.v1",
  "judge_type": "prd_result",
  "dimensions": [
    {{
      "dimension_id": "original_to_channel_prd",
      "score": <integer 1-5>,
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
"""

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
  "status": "complete",
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
top-level fields.
"""

STAGE_PROMPTS: dict[str, str] = {
    "milestone": MILESTONE_PROMPT,
    "communication": COMMUNICATION_PROMPT,
    "prd_result": PRD_RESULT_PROMPT,
    "conclusion": CONCLUSION_PROMPT,
}

SCORE_RUBRIC = (
    "1 = failure or severe deviation; "
    "2 = major problems, only a little valid content; "
    "3 = basically usable but needs obvious correction; "
    "4 = effective, only minor gaps; "
    "5 = complete, clear, reusable, almost no correction needed."
)

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
