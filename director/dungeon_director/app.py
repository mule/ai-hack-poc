"""Director service HTTP boundary.

Endpoints:

``GET  /health``       liveness probe
``GET  /v1/config``    default and registered provider/model identifiers (no secrets);
                       plus a ``shadow`` object only while shadow evaluation is enabled
``GET  /v1/telemetry/health``
                        safe telemetry diagnostics: per-signal configuration validity
                        and export state (fixed words, endpoint origins only — never
                        header values or credentials); present regardless of state so
                        operators can tell "off" from "failing"
``POST /v1/generate``  canonical :class:`GenerationRequest` in, canonical
                       :class:`GenerationResponse` out

Provider/model selection travels in the query string (``?provider=...&model=...``)
so the shared request contract is unchanged. Every ``/v1/generate`` answer,
including validation and provider failures, is a ``GenerationResponse`` body;
the HTTP status says which kind of failure it was (404 unknown provider/model,
422 invalid request, 429 rate limited, 502 bad provider output, 503 provider
unavailable, 504 timeout).

When shadow evaluation is enabled (``DIRECTOR_SHADOW_TARGETS``; see
``docs/shadow-mode.md``) a generate answer that reached a provider also carries
an ``X-Shadow-Comparison-Id`` response header linking it to the shadow records.
The body is byte-for-byte what it would be without shadow mode.

``create_app`` builds a fully isolated app (own registry, settings and
service), which is what tests use to inject fake providers. The module-level
``app`` is the production instance served by ``make run-director``.

Lifecycle ownership: the app closes the registry it serves — including an
**injected** one — when its lifespan shuts down (uvicorn exit, TestClient
context exit). Callers that want to keep using a registry after the app that
served it has shut down must not pass that registry to ``create_app``;
provider ``aclose`` implementations must tolerate being closed exactly once
per registry sweep (the Jev provider's is idempotent). Shutdown order matters:
running shadow calls are drained (then cancelled) *before* the providers they
use are closed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel

from dungeon_director.comparison_telemetry import ComparisonTelemetry, telemetry_context
from dungeon_director.contracts import ErrorKind, GenerationRequest, GenerationResponse
from dungeon_director.game_telemetry import game_telemetry_router
from dungeon_director.registry import ProviderDescriptor, ProviderRegistry, default_registry
from dungeon_director.service import DirectorService
from dungeon_director.settings import DirectorSettings
from dungeon_director.telemetry import DirectorTelemetry, TelemetrySettings, setup_telemetry

__all__ = ["DirectorConfig", "ShadowConfigView", "app", "create_app"]

logger = logging.getLogger(__name__)

_SERVICE_NAME = "dungeon-director"
_GENERATE_PATH = "/v1/generate"
_MAX_SELECTOR_CHARS = 128
#: Hard cap on telemetry export shutdown; exporter timeouts are bounded to
#: at most 30 s each, so 30 s only ever trips with a pathologically stuck SDK.
TELEMETRY_SHUTDOWN_TIMEOUT_SECONDS = 30.0
COMPARISON_ID_HEADER = "X-Shadow-Comparison-Id"


class ShadowTargetView(BaseModel):
    provider: str
    model: str | None


class ShadowConfigView(BaseModel):
    """Configured shadow targets and how many config entries were dropped."""

    targets: list[ShadowTargetView]
    rejected_config_entries: int


class DirectorConfig(BaseModel):
    """What ``GET /v1/config`` returns: identifiers and flags only.

    ``shadow`` is omitted from the JSON entirely unless shadow evaluation is
    enabled, so the shape is unchanged for deployments that do not use it.
    """

    default_provider: str
    default_model: str
    providers: list[ProviderDescriptor]
    shadow: ShadowConfigView | None = None


def get_service(request: Request) -> DirectorService:
    return request.app.state.service


def create_app(
    settings: DirectorSettings | None = None,
    registry: ProviderRegistry | None = None,
    telemetry: DirectorTelemetry | None = None,
) -> FastAPI:
    """Build an isolated director app.

    ``settings`` defaults to :meth:`DirectorSettings.from_env` and ``registry``
    to :func:`default_registry`. Raises ``DirectorConfigError`` if the default
    provider/model is not usable, so misconfiguration fails at startup.

    The returned app closes ``registry``'s providers (any that expose
    ``aclose``) on lifespan shutdown — including an injected registry: passing
    one in hands its shutdown to the app. Startup behaviour is unchanged and
    nothing is closed while the app is running.
    """
    settings = settings if settings is not None else DirectorSettings.from_env()
    registry = registry if registry is not None else default_registry()
    telemetry = telemetry if telemetry is not None else _default_telemetry()
    observers = ()
    if settings.shadow.enabled:
        try:
            observers = (
                ComparisonTelemetry(telemetry, max_comparisons=settings.shadow.store_size),
            )
        except Exception as exc:
            logger.warning("comparison telemetry setup failed (%s)", type(exc).__name__)
    service = DirectorService(registry, settings, telemetry=telemetry, shadow_observers=observers)
    default_model = registry.select(settings.default_provider, settings.default_model).model

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            # Shadow calls use the providers, so they end first; then the
            # providers close. Cancellation propagates, but the registry is
            # still closed on the way out. Telemetry flushes last so completed
            # shadow spans are included.
            try:
                await service.aclose()
            finally:
                try:
                    # Best-effort per provider; cancellation propagates.
                    await registry.aclose()
                finally:
                    # Export shutdown can block on an unreachable collector;
                    # exporter timeouts are bounded, and this daemon-thread
                    # join with a hard cap keeps a dead collector from hanging
                    # director shutdown (an abandoned export thread dies with
                    # the process instead).
                    def _bound_telemetry_shutdown() -> None:
                        worker = threading.Thread(
                            target=telemetry.shutdown,
                            name="telemetry-shutdown",
                            daemon=True,
                        )
                        worker.start()
                        worker.join(TELEMETRY_SHUTDOWN_TIMEOUT_SECONDS)
                        if worker.is_alive():
                            logger.warning("telemetry shutdown timed out; export abandoned")

                    try:
                        await asyncio.to_thread(_bound_telemetry_shutdown)
                    except Exception as exc:
                        logger.warning("telemetry shutdown failed (%s)", type(exc).__name__)

    app = FastAPI(title="Dungeon Director", lifespan=lifespan)
    app.state.service = service
    app.state.telemetry = telemetry
    app.include_router(
        game_telemetry_router(telemetry, {p.id: set(p.models) for p in registry.describe()})
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": _SERVICE_NAME}

    @app.get("/v1/telemetry/health")
    def telemetry_health() -> dict[str, Any]:
        """Safe telemetry diagnostics (see ``DirectorTelemetry.describe``)."""
        try:
            return app.state.telemetry.describe()
        except Exception as exc:
            logger.warning("telemetry diagnostics failed (%s)", type(exc).__name__)
            return {"enabled": False}

    @app.get("/v1/config", response_model=DirectorConfig, response_model_exclude_none=True)
    def config() -> DirectorConfig:
        shadow = service.shadow
        return DirectorConfig(
            default_provider=settings.default_provider,
            default_model=default_model,
            providers=registry.describe(),
            shadow=(
                ShadowConfigView(
                    targets=[
                        ShadowTargetView(provider=t.provider, model=t.model)
                        for t in shadow.registered_targets
                    ],
                    # Malformed entries plus targets naming things that are not
                    # registered: counted, never shown (could be a pasted secret).
                    rejected_config_entries=shadow.rejected_config_entries
                    + len(shadow.targets)
                    - len(shadow.registered_targets),
                )
                if shadow is not None
                else None
            ),
        )

    @app.post(
        _GENERATE_PATH,
        response_model=GenerationResponse,
        responses={
            status: {"model": GenerationResponse, "description": description}
            for status, description in {
                404: "Unknown provider or model",
                422: "Invalid GenerationRequest",
                429: "Provider rate limited or over budget",
                500: "Internal error",
                502: "Provider failed or returned invalid output",
                503: "Provider unavailable",
                504: "Provider timed out",
            }.items()
        },
    )
    async def generate(
        body: GenerationRequest,
        request: Request,
        service: Annotated[DirectorService, Depends(get_service)],
        provider: Annotated[
            str | None,
            Query(max_length=_MAX_SELECTOR_CHARS, description="Provider id; default if omitted"),
        ] = None,
        model: Annotated[
            str | None,
            Query(
                max_length=_MAX_SELECTOR_CHARS, description="Model id; provider default if omitted"
            ),
        ] = None,
    ) -> JSONResponse:
        parent = Context()
        try:
            parent = TraceContextTextMapPropagator().extract(request.headers, context=parent)
        except Exception as exc:
            logger.warning("trace context extraction failed (%s)", type(exc).__name__)
        with telemetry_context(parent_context=parent):
            outcome = await service.generate(
                body, provider=_normalize_selector(provider), model=_normalize_selector(model)
            )
        headers = {COMPARISON_ID_HEADER: outcome.comparison_id} if outcome.comparison_id else {}
        if outcome.traceparent:
            headers["traceparent"] = outcome.traceparent
        return JSONResponse(
            status_code=outcome.status_code,
            content=outcome.response.model_dump(mode="json"),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path != _GENERATE_PATH:
            return await request_validation_exception_handler(request, exc)
        response, ids = _invalid_request_response(exc)
        try:
            request.app.state.telemetry.record_invalid_request(
                request_id=ids.get("request_id"), run_id=ids.get("run_id")
            )
        except Exception as tel_exc:
            logger.warning("telemetry call failed (%s)", type(tel_exc).__name__)
        return JSONResponse(status_code=422, content=response.model_dump(mode="json"))

    return app


def _normalize_selector(value: str | None) -> str | None:
    """Blank or whitespace-only selectors mean "not given"; padding is trimmed."""
    if value is None:
        return None
    return value.strip() or None


def _default_telemetry() -> DirectorTelemetry:
    """Telemetry from the environment; any failure degrades to no-op telemetry."""
    try:
        return setup_telemetry(TelemetrySettings.from_env())
    except Exception as exc:
        logger.warning("telemetry setup failed (%s); telemetry disabled", type(exc).__name__)
        return DirectorTelemetry(enabled=False)


def _invalid_request_response(
    exc: RequestValidationError,
) -> tuple[GenerationResponse, dict[str, Any]]:
    errors = exc.errors()
    types = {error.get("type") for error in errors}
    if "unsupported_contract_version" in types:
        code = ErrorKind.UNSUPPORTED_CONTRACT_VERSION
    elif "json_invalid" in types:
        code = ErrorKind.INVALID_JSON
    else:
        code = ErrorKind.SCHEMA_VIOLATION

    # Field paths and messages only: never echo submitted values.
    shown = "; ".join(
        f"{'.'.join(str(part) for part in error.get('loc', ())) or 'request'}: {error.get('msg')}"
        for error in errors[:3]
    )
    body: Any = exc.body
    ids = body if isinstance(body, dict) else {}
    response = GenerationResponse.failure(
        request_id=ids.get("request_id"),
        run_id=ids.get("run_id"),
        provider=None,
        model=None,
        code=code,
        message=f"Invalid generation request: {shown}",
    )
    return response, ids


#: Production instance, served by ``make run-director`` (``dungeon_director.app:app``).
app = create_app()
