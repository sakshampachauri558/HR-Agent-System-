"""F3 — JD Studio generation agent.

One model request produces the *entire* feature: a full job description
(summary, responsibilities, requirements, benefits) **and** the inclusive-
language pass (gendered/ageist/exclusionary phrase flags with suggested
rewrites), folded into a single typed call per PRD §4 F3 / §10 ("JD
generation (F3) | 1 | Single typed call, inclusive-language pass folded
into the same output"). A second request would double this feature's cost
against the 50-req/day budget for something one call already does.

`GeneratedJD` also carries normalized `must_haves` / `nice_to_haves` /
`min_years` — these are what `routers/jobs.py` writes straight into the
`jobs` row, since the JD is F2's (the evaluator's) input and those fields
must be machine-usable, not just prose baked into `description_md`.

Field names here intentionally mirror the `jd` fixture in
`seed/mock_responses.json` (see that file's header comment) so
`LLM_PROVIDER=mock` round-trips real fixture content instead of empty
defaults — Pydantic v2 ignores whatever extra keys the fixture carries
that we don't declare, but any field we *do* declare must exist in the
fixture under the same name or `mock.py`'s safety net fills it with an
empty placeholder.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.llm.provider import get_agent
from app.schemas import JobDraft

# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


class InclusiveLanguageFlag(BaseModel):
    """One phrase flagged by the inclusive-language pass."""

    phrase: str
    reason: str
    suggestion: str


class GeneratedJD(BaseModel):
    """Typed output of the single JD-generation call.

    Superset-friendly: every field here is present under the same name in
    the `jd` mock fixture, so `LLM_PROVIDER=mock` validates against real
    fixture content end to end.
    """

    title: str
    level: str | None = None
    department: str | None = None
    location: str | None = None
    summary: str
    responsibilities: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    nice_to_haves: list[str] = Field(default_factory=list)
    benefits: list[str] = Field(default_factory=list)
    # Normalized, machine-usable fields — these are what F2's evaluator
    # reads back out of `jobs.must_haves` / `jobs.min_years`.
    must_haves: list[str] = Field(default_factory=list)
    min_years: int | None = None
    comp_min: int | None = None
    comp_max: int | None = None
    description_md: str
    inclusive_language_flags: list[InclusiveLanguageFlag] = Field(default_factory=list)


SYSTEM_PROMPT = """You are an expert HR job-description writer for PeopleOps \
Copilot, a hiring platform.

Given a role title, level, must-have skills, nice-to-have skills, location, \
and compensation band, produce a complete, ready-to-post job description \
AND, in the same response, an inclusive-language review of that job \
description. Do this in a single pass — do not ask follow-up questions.

Job description requirements:
- `summary`: 2-4 sentences pitching the role.
- `responsibilities`: 4-7 concrete bullet points.
- `requirements`: 4-7 concrete bullet points, consistent with the supplied \
must-haves and minimum years of experience.
- `nice_to_haves`: 2-5 bullet points, consistent with the supplied \
nice-to-haves.
- `benefits`: 3-6 bullet points appropriate to the role and location.
- `must_haves`: the normalized, deduplicated list of must-have skills as \
short canonical phrases (e.g. "Python", not "must know Python well") — \
this is written straight into a structured database column and read back \
by a downstream resume-evaluation agent, so keep each entry short and \
machine-usable, not a sentence.
- `min_years`: the minimum years of relevant experience implied by the \
level and requirements (an integer).
- `description_md`: the full job description rendered as Markdown, \
composed from the fields above (heading, summary, responsibilities, \
requirements, nice-to-haves, benefits sections).

Inclusive-language pass (mandatory, same response):
- Scan the job description you just wrote for gendered, ageist, or \
exclusionary phrasing (e.g. "rockstar", "digital native", "recent \
graduate", "young and energetic", "he/she will").
- For every phrase found, emit an entry in `inclusive_language_flags` with \
`phrase` (the exact wording found), `reason` (why it's exclusionary or \
biased), and `suggestion` (a neutral rewrite).
- If nothing is flagged, return an empty list — never invent a flag that \
isn't actually present in the text.
- Never fabricate requirements the input didn't ask for; never infer \
protected attributes (age, gender, nationality) into the posting.
"""


def _format_list(values: list[str]) -> str:
    return ", ".join(values) if values else "(none specified)"


def _build_prompt(draft: JobDraft) -> str:
    comp_band = "(not specified)"
    if draft.comp_min is not None or draft.comp_max is not None:
        comp_band = f"{draft.comp_min or '?'} - {draft.comp_max or '?'}"

    return (
        f"Role title: {draft.title}\n"
        f"Level: {draft.level or '(not specified)'}\n"
        f"Must-have skills: {_format_list(draft.must_haves)}\n"
        f"Nice-to-have skills: {_format_list(draft.nice_to_haves)}\n"
        f"Location: {draft.location or '(not specified)'}\n"
        f"Department: {draft.department or '(not specified)'}\n"
        f"Compensation band: {comp_band}\n"
        f"Minimum years of experience (if specified by the caller): "
        f"{draft.min_years if draft.min_years is not None else '(infer from level)'}\n\n"
        "Generate the job description and inclusive-language flags now."
    )


async def generate_jd(draft: JobDraft) -> GeneratedJD:
    """Single LLM request: full JD + inclusive-language flags.

    Never constructs a client directly — goes through
    `app.llm.provider.get_agent`, which transparently returns the mock
    agent when `LLM_PROVIDER=mock` (the committed default).
    """
    agent = get_agent(
        name="jd",
        output_type=GeneratedJD,
        system_prompt=SYSTEM_PROMPT,
        tools=None,
    )
    result = await agent.run(_build_prompt(draft))
    return result.output
