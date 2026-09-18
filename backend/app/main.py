"""FastAPI application shell for PeopleOps Copilot.

Wave-1 agents drop routers into app/routers/*.py *after* this file is
written. This module must boot green with zero routers present, and pick
each one up automatically as it appears (a dev-server reload re-runs
lifespan startup, which re-scans ROUTER_MODULES). Nothing here may
hard-depend on another agent's module existing at import time -- every
cross-agent import is guarded with try/except ImportError.
"""

import importlib
import inspect
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("peopleops")

# Routers Wave-1/2 agents add under app/routers/<name>.py, each exposing a
# module-level `router = APIRouter()`. Mounted under the /api prefix here,
# so a router should register routes WITHOUT the /api prefix itself, e.g.
#   @router.get("/chat")     -> served at /api/chat
# not
#   @router.get("/api/chat") -> would be served at /api/api/chat
ROUTER_MODULES = [
    "policies",
    "chat",
    "jobs",
    "resumes",
    "evaluate",
    "interview_kit",
    "analytics",
]

routers_loaded: set[str] = set()


def _mount_routers(app: FastAPI) -> None:
    """Discover and mount routers dynamically; missing modules are expected
    during the parallel build and must never prevent startup."""
    for name in ROUTER_MODULES:
        if name in routers_loaded:
            continue
        try:
            module = importlib.import_module(f"app.routers.{name}")
        except ModuleNotFoundError:
            logger.info("router '%s' not present yet", name)
            continue
        except Exception:
            logger.exception("router '%s' failed to import; skipping", name)
            continue

        router = getattr(module, "router", None)
        if router is None:
            logger.warning("router '%s' module has no `router` attribute; skipping", name)
            continue

        app.include_router(router, prefix="/api")
        routers_loaded.add(name)
        logger.info("router '%s' mounted at /api", name)


async def _llm_health() -> dict[str, Any]:
    """Best-effort call into A0's `app.llm.provider.health()`. Never raises
    -- returns a dict describing why it couldn't reach the provider instead,
    so /api/health can still answer 200."""
    try:
        from app.llm import provider as llm_provider  # type: ignore[import-not-found]
    except ImportError as exc:
        return {
            "ok": False,
            "provider": "unknown",
            "model": "unknown",
            "error": f"app.llm.provider not wired yet: {exc}",
        }

    health_fn = getattr(llm_provider, "health", None)
    if health_fn is None:
        return {"ok": False, "provider": "unknown", "model": "unknown", "error": "app.llm.provider has no health()"}

    try:
        result = health_fn()
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # noqa: BLE001 - a health check must never crash the app
        return {"ok": False, "provider": "unknown", "model": "unknown", "error": str(exc)}

    if not isinstance(result, dict):
        return {"ok": False, "provider": "unknown", "model": "unknown", "error": "health() returned a non-dict"}

    result.setdefault("ok", True)
    return result


async def _db_health() -> str:
    """Best-effort check of A0's `app.db` module. Purely informational --
    db.py may not be wired yet, which is fine this early in the build."""
    try:
        from app import db as db_module  # type: ignore[import-not-found]
    except ImportError:
        return "not_wired"

    check = getattr(db_module, "health", None) or getattr(db_module, "ping", None)
    if check is None:
        return "unknown"

    try:
        result = check()
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"

    return "ok" if result else "error"


@asynccontextmanager
async def lifespan(app: FastAPI):
    llm_info = await _llm_health()
    provider_name = llm_info.get("provider", "unknown")

    if provider_name == "mock":
        logger.warning("=" * 64)
        logger.warning("LLM PROVIDER IS 'mock' -- NO REAL LLM CALLS WILL BE MADE")
        logger.warning("Set LLM_PROVIDER in .env to run against a real free-tier model.")
        logger.warning("=" * 64)
    else:
        logger.info("LLM provider active: %s (model=%s)", provider_name, llm_info.get("model", "unknown"))

    _mount_routers(app)
    logger.info("routers loaded at startup: %s", sorted(routers_loaded) or "(none yet)")

    yield


app = FastAPI(title="PeopleOps Copilot API", lifespan=lifespan)

# Permissive in dev; harmless. nginx (prod) and the Vite dev proxy put the
# browser on the same origin as the API anyway, so the browser never
# actually makes a cross-origin request -- this is a safety net, not a real
# attack surface.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Container healthcheck target. Must return 200 even when the db and/or
    llm layers aren't wired up yet -- that's the whole point while the rest
    of the build is still in flight. `routers_loaded` gives the integrator
    build progress at a glance."""
    llm_info = await _llm_health()
    db_status = await _db_health()

    status = "ok" if llm_info.get("ok") and db_status == "ok" else "degraded"

    return {
        "status": status,
        "db": db_status,
        "llm_provider": llm_info.get("provider", "unknown"),
        "llm_model": llm_info.get("model", "unknown"),
        "llm_reachable": bool(llm_info.get("ok")),
        "budget_used_today": llm_info.get("budget_used_today"),
        "budget_limit": llm_info.get("budget_limit"),
        "routers_loaded": sorted(routers_loaded),
    }


def _error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    body.update(extra)
    return {"error": body}


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content=_error_body("validation_error", str(exc)))


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "HTTP error"
    return JSONResponse(status_code=exc.status_code, content=_error_body("http_error", detail))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Map A0's typed provider errors when that module exists. Guarded so
    # this file never hard-depends on A0 finishing first.
    try:
        from app.llm.throttle import (  # type: ignore[import-not-found]
            BudgetExhausted,
            LLMRateLimited,
        )

        if isinstance(exc, BudgetExhausted):
            return JSONResponse(
                status_code=429,
                content=_error_body("llm_budget_exhausted", str(exc) or "Daily LLM request budget exhausted."),
            )
        if isinstance(exc, LLMRateLimited):
            retry_after = getattr(exc, "retry_after", None)
            return JSONResponse(
                status_code=429,
                content=_error_body(
                    "llm_rate_limited",
                    str(exc) or "Rate limit hit, retrying shortly.",
                    retry_after=retry_after,
                ),
            )
    except ImportError:
        pass

    logger.exception("unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content=_error_body("internal_error", "Something went wrong."))
