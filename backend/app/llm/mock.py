"""The mock LLM provider. `LLM_PROVIDER=mock` is the committed default —
every Wave-1 agent builds and tests against this file and MUST NOT make a
real network call to any LLM provider.

`MockAgent` duck-types the surface of a Pydantic AI `Agent` that callers
actually use: `await agent.run(user_prompt, deps=...)` returning an object
with `.output` (validated against the `output_type` passed to
`get_agent`) and `.usage`. It never touches the network, is fully
deterministic (same input -> same output), and still writes an
`audit_log` row so the throttle/budget-counting machinery in `throttle.py`
gets exercised even when nothing real is being called.

Fixtures live in `seed/mock_responses.json`, keyed by the `name=` passed
to `get_agent`. See that file for the exact six keys and their shapes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import types as _pytypes
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, TypeAdapter

from app.db import session_scope
from app.llm.throttle import record_llm_call

MOCK_MODEL_NAME = "mock-fixture-v1"

# Identity-ish fields we'll happily take from `deps` (dict or object) when
# present, so a mock evaluation/citation can still be persisted against a
# real resume_id/job_id/etc. instead of the fixture's placeholder UUID.
_IDENTITY_FIELDS = ("id", "resume_id", "job_id", "evaluation_id", "document_id")

# Generic aliases some Wave-1 agent might register a single chat agent
# under, letting mock decide grounded-vs-refusal from `deps` the same way
# a real model would decide it from retrieved context in its prompt.
_CHAT_ALIASES = {"chat", "chat_response", "policy_chat"}


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------

_FIXTURES: dict[str, Any] | None = None


def _resolve_fixtures_path() -> Path:
    candidates: list[Path] = []
    override = os.environ.get("MOCK_RESPONSES_PATH")
    if override:
        candidates.append(Path(override))
    candidates += [
        Path.cwd() / "seed" / "mock_responses.json",
        Path("/app/seed/mock_responses.json"),
        Path(__file__).resolve().parents[3] / "seed" / "mock_responses.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "seed/mock_responses.json not found. Tried: " + ", ".join(str(c) for c in candidates)
    )


def _load_fixtures() -> dict[str, Any]:
    global _FIXTURES
    if _FIXTURES is None:
        with _resolve_fixtures_path().open("r", encoding="utf-8") as f:
            _FIXTURES = json.load(f)
    return _FIXTURES


def _deps_get(deps: Any, key: str) -> Any:
    if deps is None:
        return None
    if isinstance(deps, dict):
        return deps.get(key)
    return getattr(deps, key, None)


def _deps_has_context(deps: Any) -> bool:
    """Heuristic for the generic "chat" alias: does `deps` carry retrieved
    context? Optimistic (True) when deps is absent entirely, since a
    caller that wants refusal behavior from mock should pass an explicit
    empty context list/attr."""
    if deps is None:
        return True
    for attr in ("citations", "chunks", "context", "retrieved", "retrieved_chunks"):
        value = _deps_get(deps, attr)
        if value:
            return True
        if value is not None:
            return False
    return True


def _select_fixture(name: str, *, deps: Any) -> dict[str, Any]:
    fixtures = _load_fixtures()
    if name in fixtures:
        return fixtures[name]
    if name in _CHAT_ALIASES:
        key = "chat_answer" if _deps_has_context(deps) else "chat_refusal"
        return fixtures[key]
    known = sorted(k for k in fixtures if not k.startswith("_"))
    raise KeyError(f"No mock fixture registered for get_agent(name={name!r}). Known keys: {known}")


# ---------------------------------------------------------------------------
# Deterministic variation helpers
# ---------------------------------------------------------------------------


def _seed_from_text(text: str) -> int:
    return int(hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:8], 16)


def _deterministic_uuid(seed: str) -> uuid.UUID:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return uuid.UUID(bytes=digest[:16])


def _looks_like_evaluation(data: Any) -> bool:
    return isinstance(data, dict) and "criteria" in data and "fit_score" in data


def _vary_evaluation(data: dict[str, Any], *, seed_text: str) -> dict[str, Any]:
    """Same resume text -> same result; different resumes -> different
    fit_score and different criteria come out "strong", so five resumes
    scored against one JD don't render as five identical rows on the
    board or in the analytics charts."""
    rng = random.Random(_seed_from_text(seed_text))
    criteria = [dict(c) for c in data.get("criteria", [])]
    total_weight = 0.0
    weighted_sum = 0.0
    for c in criteria:
        base_score = int(c.get("score", 5))
        score = min(10, max(0, base_score + rng.randint(-3, 3)))
        if score <= 3 and rng.random() < 0.5:
            c["evidence_quote"] = None
            c["reasoning"] = "Not evidenced in the redacted resume text."
        c["score"] = score
        weight = float(c.get("weight", 0.0))
        total_weight += weight
        weighted_sum += score * weight
    fit_score = round((weighted_sum / total_weight) * 10) if total_weight > 0 else 50
    fit_score = min(100, max(0, fit_score))
    if fit_score >= 85:
        recommendation = "strong_yes"
    elif fit_score >= 70:
        recommendation = "yes"
    elif fit_score >= 50:
        recommendation = "maybe"
    else:
        recommendation = "no"
    out = dict(data)
    out["criteria"] = criteria
    out["fit_score"] = fit_score
    out["recommendation"] = recommendation
    return out


def _coerce_citation(c: Any) -> dict[str, Any]:
    def get(key: str, default: Any = None) -> Any:
        return c.get(key, default) if isinstance(c, dict) else getattr(c, key, default)

    return {
        "chunk_id": str(get("chunk_id") or get("id") or uuid.uuid4()),
        "document_title": get("document_title") or get("title") or "Untitled document",
        "section": get("section") or "",
        "quote": get("quote") or get("text") or "",
        "start_char": int(get("start_char") or 0),
        "end_char": int(get("end_char") or 0),
    }


def _apply_deps_overrides(data: Any, deps: Any) -> Any:
    if deps is None or not isinstance(data, dict):
        return data
    data = dict(data)
    for field in _IDENTITY_FIELDS:
        value = _deps_get(deps, field)
        if value is not None and field in data:
            data[field] = str(value)
    if "citations" in data:
        real = _deps_get(deps, "citations") or _deps_get(deps, "chunks")
        if real:
            n = max(len(data["citations"]), 1)
            data["citations"] = [_coerce_citation(c) for c in list(real)[:n]]
    return data


# ---------------------------------------------------------------------------
# Generic "fill missing required fields" safety net
# ---------------------------------------------------------------------------


def _default_for_annotation(annotation: Any, *, seed: str) -> Any:
    origin = get_origin(annotation)
    if origin is Union or origin is _pytypes.UnionType:
        args = get_args(annotation)
        if type(None) in args:
            return None
        return _default_for_annotation(args[0], seed=seed) if args else None
    if origin is Literal:
        args = get_args(annotation)
        return args[0] if args else None
    if origin in (list, tuple, set, frozenset):
        return []
    if origin is dict:
        return {}
    if annotation is str:
        return ""
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    if annotation is bool:
        return False
    if annotation is uuid.UUID:
        return str(_deterministic_uuid(seed))
    if annotation is datetime:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _fill_model_defaults(annotation, {}, seed_text=seed)
    return None


def _fill_model_defaults(model_cls: type[BaseModel], data: dict[str, Any], *, seed_text: str) -> dict[str, Any]:
    data = dict(data)
    for name, field in model_cls.model_fields.items():
        if name in data or not field.is_required():
            continue
        data[name] = _default_for_annotation(field.annotation, seed=f"{model_cls.__name__}.{name}:{seed_text}")
    return data


def _fill_defaults(output_type: Any, data: Any, *, seed_text: str) -> Any:
    if isinstance(output_type, type) and issubclass(output_type, BaseModel) and isinstance(data, dict):
        return _fill_model_defaults(output_type, data, seed_text=seed_text)
    return data


def _stamp_provider_fields(output: Any) -> Any:
    """Force provider/model fields (top-level and nested `meta`) to say
    "mock", regardless of what placeholder text is in the fixture."""
    if not isinstance(output, BaseModel):
        return output
    updates: dict[str, Any] = {}
    if "provider" in output.model_fields:
        updates["provider"] = "mock"
    if "model" in output.model_fields:
        updates["model"] = MOCK_MODEL_NAME
    if "meta" in output.model_fields:
        meta = getattr(output, "meta", None)
        if isinstance(meta, BaseModel):
            meta_updates: dict[str, Any] = {}
            if "provider" in meta.model_fields:
                meta_updates["provider"] = "mock"
            if "model" in meta.model_fields:
                meta_updates["model"] = MOCK_MODEL_NAME
            if meta_updates:
                updates["meta"] = meta.model_copy(update=meta_updates)
    return output.model_copy(update=updates) if updates else output


# ---------------------------------------------------------------------------
# Agent-shaped result objects
# ---------------------------------------------------------------------------


class MockUsage:
    """Duck-types pydantic_ai's `Usage` closely enough for
    `provider.py`'s token extraction (`request_tokens`/`response_tokens`/
    `total_tokens`), plus `input_tokens`/`output_tokens` aliases."""

    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.requests = 1
        self.request_tokens = input_tokens
        self.response_tokens = output_tokens
        self.total_tokens = input_tokens + output_tokens
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class MockResult:
    def __init__(self, output: Any, usage: MockUsage) -> None:
        self.output = output
        self.data = output  # older pydantic_ai alias some callers may still read
        self._usage = usage

    @property
    def usage(self) -> MockUsage:
        # A property, NOT a method, because pydantic_ai's real
        # `AgentRunResult.usage` is a property. When this was a method the
        # whole app passed its tests against the mock and raised
        # `'RunUsage' object is not callable` on the first real provider
        # call. A mock that diverges from the SDK it stands in for hides
        # exactly the bugs it exists to catch.
        return self._usage


class MockAgent:
    """Same call surface as a Pydantic AI `Agent`, minus the network."""

    def __init__(
        self,
        *,
        name: str,
        output_type: Any,
        system_prompt: str = "",
        tools: list[Any] | None = None,
        model: str | None = None,
    ) -> None:
        self._name = name
        self._output_type = output_type
        self._system_prompt = system_prompt
        self._tools = tools or []
        self._model = model or MOCK_MODEL_NAME

    async def run(self, user_prompt: str = "", *, deps: Any = None, **kwargs: Any) -> MockResult:
        fixture = _select_fixture(self._name, deps=deps)
        data: Any = copy.deepcopy(fixture)
        if isinstance(data, dict):
            data.pop("_comment", None)

        # Fixtures are stored as objects (so they can carry a `_comment`
        # and so every criterion/question is easy to read/edit). If the
        # caller's output_type is a bare `list[...]` rather than a
        # wrapper model, unwrap the fixture's single list-valued field
        # (e.g. {"questions": [...]}  ->  [...]) so validation still
        # lines up with whatever shape Wave-1 chose for the agent output.
        if isinstance(data, dict) and get_origin(self._output_type) in (list, tuple, set, frozenset):
            list_values = [v for v in data.values() if isinstance(v, list)]
            if len(list_values) == 1:
                data = list_values[0]

        seed_text = user_prompt or json.dumps(data, sort_keys=True, default=str)
        if _looks_like_evaluation(data):
            data = _vary_evaluation(data, seed_text=seed_text)
        data = _apply_deps_overrides(data, deps)
        data = _fill_defaults(self._output_type, data, seed_text=seed_text)

        output = TypeAdapter(self._output_type).validate_python(data)
        output = _stamp_provider_fields(output)

        input_tokens = max(1, len(user_prompt) // 4)
        output_tokens = max(1, len(json.dumps(data, default=str)) // 4)
        await self._write_audit(input_tokens, output_tokens)

        return MockResult(output=output, usage=MockUsage(input_tokens, output_tokens))

    async def _write_audit(self, input_tokens: int, output_tokens: int) -> None:
        async with session_scope() as session:
            await record_llm_call(
                session,
                feature=self._name,
                provider="mock",
                model=self._model,
                latency_ms=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )


def get_agent(
    *,
    name: str,
    output_type: Any,
    system_prompt: str = "",
    tools: list[Any] | None = None,
    model: str | None = None,
) -> MockAgent:
    """Same signature as `app.llm.provider.get_agent`. `provider.get_agent`
    delegates here whenever `settings.llm_provider == "mock"` so callers
    never branch on provider."""
    return MockAgent(name=name, output_type=output_type, system_prompt=system_prompt, tools=tools, model=model)
