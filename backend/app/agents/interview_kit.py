"""F4 — Interview Kit Generator. PRD §4 F4, §10 ("Interview kit (F4) | 1 |").

One model request per evaluation: takes an F2 `EvaluationResult` and asks
for 6-8 candidate-specific interview questions -- most of them aimed at the
*gaps* the evaluator actually found, plus at least 2 validating the
candidate's claimed strengths (matched skills / high-scoring, evidenced
criteria). PRD §4 F4's acceptance bar: the kit's questions must reference
>=2 named gaps from that evaluation.

Idempotent by construction: `routers/interview_kit.py` checks the
`interview_kits` table for an existing row *before* this module is ever
called, so a second "Generate interview kit" click on the same evaluation
costs zero LLM budget (PRD §10 -- the daily request cap is the binding
constraint on this whole app).

Never constructs an LLM client directly -- goes through
`app.llm.provider.get_agent`, which transparently returns the mock agent
when `LLM_PROVIDER=mock` (the committed default, see seed/mock_responses.json's
`interview_kit` fixture).
"""

from __future__ import annotations

from uuid import UUID

from app.agents.evaluator import row_to_evaluation_result
from app.db import execute, fetch_one, session_scope
from app.llm.provider import get_agent
from app.schemas import EvaluationResult, InterviewKitResponse

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """You are helping an interviewer prepare for a screen with \
a specific candidate, based on a completed resume-evaluation report for a \
specific job.

Produce 6-8 interview questions as a single structured `InterviewKitResponse` \
(a `questions` list). Rules:

- Most questions (at least 4) must target the SPECIFIC gaps this evaluation \
found, listed under "Gaps found by the evaluator" below. Reference at least \
2 distinct named gaps across the whole kit -- do not ask generic, \
role-agnostic questions; every question must be traceable to something \
actually in this candidate's evaluation.
- Exactly 2 questions (the last 2) must validate the candidate's claimed \
strengths -- pick from the "Matched / claimed strengths" list or a \
high-scoring, evidenced rubric criterion, and probe whether the claim holds \
up under direct questioning rather than taking it at face value.
- Every question needs all five fields:
  - `question`: the interview question itself, specific to this candidate \
(not a generic role question).
  - `targets`: names the exact gap or strength this question probes, close \
enough in wording that a reviewer can match it back to the evaluation.
  - `good_answer`: what a strong, credible answer looks like.
  - `follow_up`: one probing follow-up question to ask if the first answer \
is vague.
  - `scoring_anchor`: what separates a strong answer from a weak one, so \
different interviewers score the same answer consistently.

Never invent a gap or strength that isn't present in the evaluation you were \
given -- never fabricate. Never ask about protected attributes (age, \
gender, nationality, marital status, disability, religion) -- this \
candidate was already scored on a redacted resume and the interview must \
stay on that same footing.
"""


def _format_criteria(evaluation: EvaluationResult) -> str:
    lines: list[str] = []
    for c in evaluation.criteria:
        if c.evidence_quote:
            evidence = f' Evidence: "{c.evidence_quote}"'
        else:
            evidence = " Evidence: none (not evidenced)."
        lines.append(f"- {c.criterion}: score {c.score}/10.{evidence} {c.reasoning}")
    return "\n".join(lines) if lines else "(no rubric detail available)"


def _build_user_prompt(evaluation: EvaluationResult) -> str:
    gaps = "\n".join(f"- {g}" for g in evaluation.gaps) or "(none listed)"
    strengths = "\n".join(f"- {s}" for s in evaluation.matched_skills) or "(none listed)"
    return (
        f"Recommendation: {evaluation.recommendation} (fit score {evaluation.fit_score}/100)\n"
        f"Summary: {evaluation.summary}\n\n"
        f"Gaps found by the evaluator (target these first, cover >=2 of them):\n{gaps}\n\n"
        f"Matched / claimed strengths (validate at least 2 of these in the last "
        f"2 questions):\n{strengths}\n\n"
        f"Full rubric detail:\n{_format_criteria(evaluation)}\n\n"
        "Generate the interview kit now."
    )


async def generate_interview_kit(evaluation_id: UUID) -> InterviewKitResponse:
    """Load the evaluation, generate the kit in one model request, persist
    it to `interview_kits`, and return it. Callers (the router) are
    responsible for the idempotency check -- this function always spends a
    request."""
    async with session_scope() as session:
        row = await fetch_one(session, "SELECT * FROM evaluations WHERE id = :id", {"id": evaluation_id})
    if row is None:
        raise ValueError(f"evaluation {evaluation_id} not found")

    evaluation = row_to_evaluation_result(row)

    agent = get_agent(
        name="interview_kit",
        output_type=InterviewKitResponse,
        system_prompt=SYSTEM_PROMPT,
        tools=None,
    )
    result = await agent.run(
        _build_user_prompt(evaluation),
        deps={"evaluation_id": evaluation_id},
    )
    output: InterviewKitResponse = result.output

    async with session_scope() as session:
        await execute(
            session,
            "INSERT INTO interview_kits (evaluation_id, questions) VALUES (:evaluation_id, :questions)",
            {
                "evaluation_id": evaluation_id,
                "questions": [q.model_dump(mode="json") for q in output.questions],
            },
        )

    return output
