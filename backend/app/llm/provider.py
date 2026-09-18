"""Provider abstraction — PRD §10.

`get_agent(...)` is the *only* way any feature agent (F1-F6) should ever
get an LLM-backed Pydantic AI `Agent`. It never constructs an OpenAI
client itself, never hardcodes a model name outside `PROVIDERS`, and
never branches on provider — when `LLM_PROVIDER=mock` it transparently
returns `app.llm.mock.MockAgent` instead, which exposes the identical
`await agent.run(user_prompt, deps=...)` -> `.output` / `.usage`
surface.

Every completed call (mock or real) writes one `audit_log` row via
`throttle.record_llm_call`, which is what both the daily budget counter
and F6's usage chart read.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from app.config import settings
from app.db import session_scope
from app.llm import mock
from app.llm.throttle import (
    LLMRateLimited,
    call_with_retry,
    check_budget,
    is_retriable,
    record_llm_call,
    status_code_of,
    suggested_retry_after,
)
from app.llm.throttle import acquire as throttle_acquire


class LLMConfigError(Exception):
    """A provider is selected that is missing a required API key."""


@dataclass(frozen=True)
class Provider:
    base_url: str
    api_key_env: str | None
    default_model: str
    extra_headers: dict[str, str] = field(default_factory=dict)


PROVIDERS: dict[str, Provider] = {
    "openrouter": Provider(
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        # Verified 2026-09-19: reachable AND supports tool calling, which F2's
        # agent requires. llama-3.3-70b-instruct:free was the original default
        # and moved behind payment ("This model is unavailable for free").
        # Free slugs churn; override with LLM_MODEL rather than editing code.
        default_model="deepseek/deepseek-v4-flash-0731:free",
        extra_headers={"HTTP-Referer": settings.app_url, "X-Title": "PeopleOps Copilot"},
    ),
    "groq": Provider(
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
    ),
    "gemini": Provider(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GEMINI_API_KEY",
        default_model="gemini-2.0-flash",
    ),
    "cerebras": Provider(
        base_url="https://api.cerebras.ai/v1",
        api_key_env="CEREBRAS_API_KEY",
        default_model="llama-3.3-70b",
    ),
    "ollama": Provider(
        base_url="http://ollama:11434/v1",
        api_key_env=None,
        default_model="qwen2.5:7b-instruct",
    ),
    "mock": Provider(
        base_url="",
        api_key_env=None,
        default_model=mock.MOCK_MODEL_NAME,
    ),
}


def _api_key_field(provider_key: str) -> str | None:
    env_name = PROVIDERS[provider_key].api_key_env
    return env_name.lower() if env_name else None


def _has_api_key(provider_key: str) -> bool:
    field_name = _api_key_field(provider_key)
    if field_name is None:
        return True  # no key required (ollama, mock)
    return bool(getattr(settings, field_name, None))


def _resolve_api_key(provider_key: str) -> str:
    field_name = _api_key_field(provider_key)
    if field_name is None:
        return "not-needed"
    key = getattr(settings, field_name, None)
    if not key:
        cfg = PROVIDERS[provider_key]
        raise LLMConfigError(f"{cfg.api_key_env} is not set but provider {provider_key!r} requires it")
    return key


def _extract_tokens(usage_obj: Any) -> tuple[int, int]:
    input_tokens = (
        getattr(usage_obj, "request_tokens", None)
        or getattr(usage_obj, "input_tokens", None)
        or 0
    )
    output_tokens = (
        getattr(usage_obj, "response_tokens", None)
        or getattr(usage_obj, "output_tokens", None)
        or 0
    )
    return int(input_tokens or 0), int(output_tokens or 0)


class _ThrottledAgent:
    """Wraps a real Pydantic AI `Agent` (one per provider it might run
    against) with the budget check, RPM throttle, retry/backoff,
    provider failover, and audit logging PRD §10 requires — all invisible
    to the caller, who just does `await agent.run(prompt, deps=...)`.
    """

    def __init__(
        self,
        *,
        feature: str,
        output_type: Any,
        system_prompt: str,
        tools: list[Any] | None,
        explicit_model: str | None,
    ) -> None:
        self._feature = feature
        self._output_type = output_type
        self._system_prompt = system_prompt
        self._tools = tools or []
        self._explicit_model = explicit_model
        self._agents: dict[str, tuple[Agent, str]] = {}

    def _agent_for(self, provider_key: str) -> tuple[Agent, str]:
        cached = self._agents.get(provider_key)
        if cached is not None:
            return cached
        cfg = PROVIDERS[provider_key]
        model_name = self._explicit_model or settings.llm_model or cfg.default_model
        api_key = _resolve_api_key(provider_key)
        client = AsyncOpenAI(
            base_url=cfg.base_url,
            api_key=api_key,
            default_headers=cfg.extra_headers or None,
        )
        openai_model = OpenAIChatModel(model_name, provider=OpenAIProvider(openai_client=client))
        agent: Agent = Agent(
            openai_model,
            output_type=self._output_type,
            system_prompt=self._system_prompt,
            tools=self._tools,
            retries=2,
        )
        self._agents[provider_key] = (agent, model_name)
        return agent, model_name

    async def _attempt(self, provider_key: str, user_prompt: str, kwargs: dict[str, Any]) -> Any:
        agent, _model_name = self._agent_for(provider_key)
        await throttle_acquire()
        return await call_with_retry(lambda: agent.run(user_prompt, **kwargs))

    async def run(self, user_prompt: str = "", **kwargs: Any) -> Any:
        async with session_scope() as session:
            await check_budget(session)  # raises BudgetExhausted if today's cap is hit

        primary = settings.llm_provider
        provider_used, model_used = primary, (
            self._explicit_model or settings.llm_model or PROVIDERS[primary].default_model
        )
        start = time.monotonic()
        try:
            result = await self._attempt(primary, user_prompt, kwargs)
            _, model_used = self._agent_for(primary)
        except Exception as primary_exc:
            fallback = settings.llm_fallback_provider
            can_failover = (
                is_retriable(primary_exc)
                and fallback
                and fallback != primary
                and fallback in PROVIDERS
                and _has_api_key(fallback)
            )
            if not can_failover:
                if is_retriable(primary_exc):
                    raise LLMRateLimited(retry_after=suggested_retry_after()) from primary_exc
                raise
            try:
                result = await self._attempt(fallback, user_prompt, kwargs)
                provider_used = fallback
                _, model_used = self._agent_for(fallback)
            except Exception as fallback_exc:
                status = status_code_of(fallback_exc)
                if status is not None and is_retriable(fallback_exc):
                    raise LLMRateLimited(retry_after=suggested_retry_after()) from fallback_exc
                raise

        latency_ms = int((time.monotonic() - start) * 1000)
        input_tokens, output_tokens = _extract_tokens(result.usage)
        async with session_scope() as session:
            await record_llm_call(
                session,
                feature=self._feature,
                provider=provider_used,
                model=model_used,
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        # Surface which provider/model actually served this call -- after a
        # failover (primary -> `settings.llm_fallback_provider`) this can
        # differ from the configured default, and the `audit_log` row above
        # already knows the truth. `pydantic_ai`'s `AgentRunResult` is a
        # plain, non-frozen, non-slotted dataclass, so attaching extra
        # attributes here is safe and additive: nothing existing is
        # renamed, removed, or restructured, and `.output`/`.usage` are
        # untouched. Callers (e.g. `agents/evaluator.py`) should prefer
        # these over `settings.llm_provider` when recording
        # `evaluations.provider` / `evaluations.model`.
        result.serving_provider = provider_used
        result.serving_model = model_used
        return result


def get_agent(
    *,
    name: str,
    output_type: Any,
    system_prompt: str,
    tools: list[Any] | None = None,
    model: str | None = None,
) -> Agent:
    """Return a configured agent for feature `name`.

    Real callers never see the difference between this and a bare
    `pydantic_ai.Agent`: both expose `await agent.run(prompt, deps=...)`
    returning an object with `.output` and `.usage`. When
    `LLM_PROVIDER=mock` (the committed default) this returns
    `app.llm.mock.MockAgent` and no network call is made.
    """
    if settings.llm_provider == "mock":
        return mock.get_agent(name=name, output_type=output_type, system_prompt=system_prompt, tools=tools, model=model)  # type: ignore[return-value]
    return _ThrottledAgent(  # type: ignore[return-value]
        feature=name,
        output_type=output_type,
        system_prompt=system_prompt,
        tools=tools,
        explicit_model=model,
    )


async def health() -> dict[str, Any]:
    """`{provider, model, reachable, budget_used_today, budget_limit}` —
    never makes a model request. `reachable` is a config-presence check
    (API key set, or no key needed), not a live ping."""
    from app.llm.throttle import budget_used_today

    provider_key = settings.llm_provider
    cfg = PROVIDERS.get(provider_key)
    model_name = settings.llm_model or (cfg.default_model if cfg else "unknown")
    async with session_scope() as session:
        used = await budget_used_today(session)
    reachable = provider_key == "mock" or (cfg is not None and _has_api_key(provider_key))
    return {
        "provider": provider_key,
        "model": model_name,
        "reachable": reachable,
        "budget_used_today": used,
        "budget_limit": settings.llm_daily_budget,
    }


async def probe_tool_calling() -> dict[str, Any]:
    """One real request asserting the configured model can call a
    trivial tool. Used only by the integrator at the T+10 gate — never
    called from a request path. Loud, not fatal: callers log the result."""
    from pydantic import BaseModel as _BaseModel

    class _ProbeOutput(_BaseModel):
        ok: bool

    def ping(x: int) -> int:
        """Return x + 1. The model must call this to answer correctly."""
        return x + 1

    agent = get_agent(
        name="tool_calling_probe",
        output_type=_ProbeOutput,
        system_prompt=(
            "Call the `ping` tool with x=1, then respond with ok=true if it "
            "returned 2, otherwise ok=false."
        ),
        tools=[ping],
    )
    try:
        result = await agent.run("Use the ping tool on the number 1 and report back.")
        return {"ok": bool(result.output.ok), "provider": settings.llm_provider}
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic, never raise
        return {"ok": False, "provider": settings.llm_provider, "error": str(exc)}
