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

from typing import Any

EVALUATION_PROMPT_VERSION = "prd-eval-prompts.v8"

JUDGE_TYPES: tuple[str, ...] = (
    "task_type",
    "contribution",
    "discussion",
    "prd_fidelity",
    "conclusion",
)
CONCLUSION_JUDGE_TYPE = "conclusion"

# Three-lane PRD eval stages (docs/ideas/three-lane-prd-eval.md).  They are a
# separate prompt family: their versions are pinned per stage so rewording the
# cross-compare rubric never re-identifies channel or per-lane blind results.
LANE_PRD_FIDELITY_JUDGE_TYPE = "lane_prd_fidelity"
CROSS_COMPARE_JUDGE_TYPE = "cross_compare"
LANE_JUDGE_TYPES: tuple[str, ...] = (
    LANE_PRD_FIDELITY_JUDGE_TYPE,
    CROSS_COMPARE_JUDGE_TYPE,
)
LANE_PRD_FIDELITY_PROMPT_VERSION = "lane-prd-eval-prompts.v2"
CROSS_COMPARE_PROMPT_VERSION = "cross-compare-prompts.v2"

CHANNEL_EVALUATION_CONTRACT = "channel-prd-eval.v1"
LANE_EVALUATION_CONTRACT = "three-lane-prd-eval.v1"

_STAGE_PROMPT_VERSIONS: dict[str, str] = {
    LANE_PRD_FIDELITY_JUDGE_TYPE: LANE_PRD_FIDELITY_PROMPT_VERSION,
    CROSS_COMPARE_JUDGE_TYPE: CROSS_COMPARE_PROMPT_VERSION,
}


def prompt_version_for_stage(judge_type: str) -> str:
    """Pin the prompt identity of every stage; lane stages version apart."""

    return _STAGE_PROMPT_VERSIONS.get(judge_type, EVALUATION_PROMPT_VERSION)


def require_stage_prompt_version(judge_type: str, packet: Any) -> str:
    """Resolve a packet's prompt version, rejecting cross-stage claims.

    Packet identity binds the prompt version; a packet that claims a foreign
    version must never mint a request under this stage.
    """

    get = packet.get if hasattr(packet, "get") else lambda _key, default=None: default
    expected = prompt_version_for_stage(judge_type)
    claimed = str(get("prompt_version") or "") or expected
    if claimed != expected:
        raise ValueError(
            f"packet prompt_version {claimed!r} does not match "
            f"stage {judge_type} ({expected!r})"
        )
    return expected


def evaluation_contract_for_stage(judge_type: str) -> str:
    """Route records/events of lane stages to the eval-run contract family."""

    return (
        LANE_EVALUATION_CONTRACT
        if judge_type in LANE_JUDGE_TYPES
        else CHANNEL_EVALUATION_CONTRACT
    )

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
  "discussion_meaningful": <true|false>,
  "discussion_progressed": <true|false>,
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
  "prd_ready_for_next_stage": <true|false>,
  "missing_evidence": []
}}
""" + "\n" + SCORE_RUBRIC


CONCLUSION_PROMPT = """\
You are the main Judge summarizer. Use only the three immutable role-bound
subagent results below. You may summarize conflicts and limitations, but you
must copy every subagent score and necessity exactly. You cannot adjust,
average, replace, or silently overwrite a subagent score. Do not add an
overall/team contribution score.

Copy `discussion_meaningful` and `discussion_progressed` exactly from the
Discussion Effectiveness result, and copy `prd_ready_for_next_stage` exactly
from the PRD Fidelity result. Do not infer, reverse, or replace these three
role-bound determinations.

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


LANE_SCORE_RUBRIC = """\
Every scored dimension must cite refs from allowed_evidence_refs. Missing
evidence means score=null with status=evidence_missing. Confidence below 0.60
means status=abstain with score=null. Judge how well the PRD satisfies the
prompt's requirements, never its format, template, or length.
"""

LANE_PRD_FIDELITY_PROMPT = """\
You are an independent blind PRD quality evaluator. You see exactly one PRD
answering one prompt. You do not know whether other PRDs exist, and you must
not speculate about how or by whom this PRD was produced. Score how well the
PRD satisfies the prompt's requirements; format, template, and stylistic
differences are irrelevant and must not affect the score.

Entry prompt: {prompt_statement}
PRD under review: {prd_document}
Allowed evidence: {allowed_evidence_refs}

Work in two steps:
1. Requirement checklist: extract every explicit requirement from the entry
   prompt into a numbered checklist. Give each item a stable item_id
   (req-1, req-2, ...) and restate the requirement in one sentence.
2. Item-by-item verification: check every checklist item against the PRD and
   assign exactly one verdict:
   - pass: the PRD fully answers the requirement; evidence_refs must cite
     the PRD ref.
   - partial: the PRD answers the requirement only partly or ambiguously;
     evidence_refs must cite the PRD ref.
   - missing: the PRD does not answer the requirement; evidence_refs must
     cite the prompt ref.
   Every verdict must carry evidence refs from allowed_evidence_refs.

Then score exactly these four dimensions, each once, with equal weight:
- coverage: every requirement explicitly stated in the prompt is answered by
  the PRD, as proven by the checklist; each omitted explicit requirement
  costs score.
- fidelity: the PRD stays faithful to the prompt and never widens or rewrites
  its scope. Unsupported major additions, changed constraints, or distorted
  intent cost score.
- actionability: a developer can start work directly from this PRD.
  Acceptance criteria are checkable (step -> verify style), boundaries and
  non-goals are stated, dependencies and risks are identified.
- structure: the PRD is complete and self-consistent as a document:
  background, goals, scope, feature descriptions, acceptance, and risks are
  present, without internal contradictions or unresolved placeholders.

Scoring is deduction-based. Start every dimension at 4 and deduct only for
defects proven by checklist verdicts or cited PRD evidence:
- 4: every checklist item passes AND an adversarial close reading still finds
  no substantive defect; the rationale MUST briefly describe what you checked
  (the verification process), never merely state the PRD looks complete.
- 3: at most one checklist item is partial, or only minor defects that do not
  block use.
- 2: several checklist items are partial, or exactly one item is missing.
- 1: several checklist items are missing.
- 0: the PRD fails or is off-topic.
从 4 分开始按证据扣分；找不到缺口时必须简述核查过程；禁止仅以“整体看起来
完整”为由直接给满分。

All rationale texts MUST be written in Chinese（rationale 一律用中文书写）。

Return exactly one JSON object with EXACTLY these fields:
{{
  "schema_version": "lane_prd_fidelity_result.v2",
  "judge_type": "lane_prd_fidelity",
  "score_scale": "0-4",
  "checklist": [
    {{
      "item_id": "req-1",
      "requirement": "<one explicit requirement from the prompt>",
      "verdict": "pass|partial|missing",
      "evidence_refs": ["<refs from allowed evidence>"]
    }}
  ],
  "dimensions": [
    {{
      "dimension_id": "coverage|fidelity|actionability|structure",
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
""" + "\n" + LANE_SCORE_RUBRIC


CROSS_COMPARE_PROMPT = """\
You are the independent cross-comparison judge for anonymized PRD candidates.
You receive one entry prompt, {candidate_count} de-identified PRDs labeled
{candidate_labels}, and each candidate's blind per-dimension scores. You never
learn which production lane produced a candidate; do not speculate about it.
Decisiveness is required: ties are not allowed. Only when two candidates are
genuinely indistinguishable on a dimension may you mark the pair "similar",
and "similar" never counts as an agreement conclusion.

Entry prompt: {prompt_statement}
Candidates (label, PRD body, blind score summary): {candidates}
Allowed evidence: {allowed_evidence_refs}

Work in four steps:
1. Pairwise matrix: for every dimension (coverage, fidelity, actionability,
   structure) and every pair of candidates, decide the winner, the advantage
   margin (large|medium|small), and one short reason. A decided pair needs a
   confidence of 1-4; use 0 only when comparison is impossible, and then the
   winner must be "similar" with margin "none".
2. Dimension ranking: combine each dimension's pair outcomes into one strict
   ranking of all candidates (best first). The ranking must agree with your
   own pairwise decisions. Rankings must stay consistent with the blind
   per-dimension scores; where your close reading disagrees with a blind
   score, record the conflict explicitly and arbitrate it with paragraph-level
   evidence instead of silently picking one.
3. Difference attribution: for every decided pair on every dimension, cite
   the concrete sections and verbatim excerpts of BOTH PRDs that cause the
   difference, and classify the cause as coverage_gap, scope_drift,
   acceptance_missing, or ambiguity (mapped to the four dimensions).
4. Recommendation: the overall ranking follows dimension votes (winning a
   dimension earns one vote; ties break by average blind score). If your
   recommendation intentionally diverges from the vote order, explain why in
   divergence_explanation. List the top candidate's key gaps over every other
   candidate; each gap must trace to attribution evidence.

Confidence uses 0-4 integers: 4 = clear and uncontroversial; 3 = clear but
small gap; 2 = winner exists but rests on subjective trade-offs; 1 =
candidates are essentially equivalent; 0 = cannot compare (insufficient
information or both too poor).

Language requirement (hard): every free-text field you emit — dimension
rationale, pairwise rationale, attribution summary, conflict resolution,
recommendation rationale, divergence_explanation, and key_gaps — MUST be
written in Chinese（一律用中文书写）. Candidate labels, enum values, field
names, and evidence refs stay exactly as given.

Return exactly one JSON object with EXACTLY these fields:
{{
  "schema_version": "cross_compare_result.v1",
  "judge_type": "cross_compare",
  "status": "scored|evidence_missing",
  "score_scale": "0-4",
  "candidates": ["<labels in the order supplied above>"],
  "dimension_rankings": [
    {{
      "dimension_id": "coverage|fidelity|actionability|structure",
      "ranking": ["<best label first>"],
      "confidence": <integer 0-4>,
      "evidence_refs": ["<refs from allowed evidence>"],
      "rationale": "<why this order>"
    }}
  ],
  "pairwise": [
    {{
      "pair": "A-B",
      "dimension_id": "coverage|fidelity|actionability|structure",
      "winner": "<one label of the pair>|similar",
      "margin": "large|medium|small|none",
      "confidence": <integer 0-4>,
      "evidence_refs": ["<refs from allowed evidence>"],
      "rationale": "<one-sentence reason>"
    }}
  ],
  "attributions": [
    {{
      "dimension_id": "coverage|fidelity|actionability|structure",
      "pair": "A-B",
      "category": "coverage_gap|scope_drift|acceptance_missing|ambiguity",
      "summary": "<section + verbatim excerpt of both PRDs>",
      "evidence_refs": ["<both candidates' PRD refs>"]
    }}
  ],
  "conflicts": [
    {{
      "dimension_id": "coverage|fidelity|actionability|structure",
      "blind_favorite": "<label the blind score prefers>",
      "pairwise_favorite": "<label your ranking prefers>",
      "resolution": "<arbitration conclusion with paragraph evidence>",
      "evidence_refs": ["<refs from allowed evidence>"]
    }}
  ],
  "recommendation": {{
    "ranking": ["<best label first>"],
    "top": "<best label>",
    "confidence": <integer 0-4>,
    "rationale": "<one-sentence reason>",
    "divergence_explanation": "<required when ranking ignores the vote order>",
    "key_gaps": [
      {{"candidate": "<non-top label>", "gap": "<key gap vs the top PRD>",
        "evidence_refs": ["<refs from allowed evidence>"]}}
    ]
  }},
  "missing_evidence": []
}}
"""


LANE_EVALUATION_PROMPTS: dict[str, str] = {
    LANE_PRD_FIDELITY_JUDGE_TYPE: LANE_PRD_FIDELITY_PROMPT,
    CROSS_COMPARE_JUDGE_TYPE: CROSS_COMPARE_PROMPT,
}


def prompt_for_stage(judge_type: str) -> str:
    """Return the pinned prompt template of any supported stage."""

    try:
        return EVALUATION_PROMPTS[judge_type]
    except KeyError:
        try:
            return LANE_EVALUATION_PROMPTS[judge_type]
        except KeyError as exc:
            raise KeyError(f"unknown evaluation prompt stage: {judge_type}") from exc


__all__ = [
    "CHANNEL_EVALUATION_CONTRACT",
    "CONCLUSION_JUDGE_TYPE",
    "CONCLUSION_PROMPT",
    "CONTRIBUTION_PROMPT",
    "CROSS_COMPARE_JUDGE_TYPE",
    "CROSS_COMPARE_PROMPT",
    "CROSS_COMPARE_PROMPT_VERSION",
    "DISCUSSION_PROMPT",
    "EVALUATION_PROMPT_VERSION",
    "EVALUATION_PROMPTS",
    "JUDGE_TYPES",
    "LANE_EVALUATION_CONTRACT",
    "LANE_EVALUATION_PROMPTS",
    "LANE_JUDGE_TYPES",
    "LANE_PRD_FIDELITY_JUDGE_TYPE",
    "LANE_PRD_FIDELITY_PROMPT",
    "LANE_PRD_FIDELITY_PROMPT_VERSION",
    "LANE_SCORE_RUBRIC",
    "PRD_FIDELITY_PROMPT",
    "SCORE_RUBRIC",
    "SYSTEM_PROMPT",
    "TASK_TYPE_PROMPT",
    "evaluation_contract_for_stage",
    "prompt_for_stage",
    "prompt_version_for_stage",
    "require_stage_prompt_version",
]
