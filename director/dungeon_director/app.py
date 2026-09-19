"""Director service HTTP boundary.

Endpoints:

``GET  /health``       liveness probe
``GET  /v1/config``    default and registered provider/model identifiers (no secrets)
``POST /v1/generate``  canonical :class:`GenerationRequest` in, canonical
                       :class:`GenerationResponse` out

Provider/model selection travels in the query string (``?provider=...&model=...``)
so the shared request contract is unchanged. Every ``/v1/generate`` answer,
including validation and provider failures, is a ``GenerationResponse`` body;
the HTTP status says which kind of failure it was (404 unknown provider/model,
422 invalid request, 429 rate limited, 502 bad provider output, 503 provider
unavailable, 504 timeout).

``create_app`` builds a fully isolated app (own registry, settings and
service), which is what tests use to inject fake providers. The module-level
``app`` is the production instance served by ``make run-director``.

Lifecycle ownership: the app closes the registry it serves — including an
**injected** one — when its lifespan shuts down (uvicorn exit, TestClient
context exit). Callers that want to keep using a registry after the app that
served it has shut down must not pass that registry to ``create_app``;
provider ``aclose`` implementations must tolerate being closed exactly once
per registry sweep (the Jev provider's is idempotent).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from dungeon_director.contracts import ErrorKind, GenerationRequest, GenerationResponse
from dungeon_director.registry import ProviderDescriptor, ProviderRegistry, default_registry
from dungeon_director.service import DirectorService
from dungeon_director.settings import DirectorSettings

__all__ = ["DirectorConfig", "app", "create_app"]

_SERVICE_NAME = "dungeon-director"
_GENERATE_PATH = "/v1/generate"
_MAX_SELECTOR_CHARS = 128


class DirectorConfig(BaseModel):
    """What ``GET /v1/config`` returns: identifiers and flags only."""

    default_provider: str
    default_model: str
    providers: list[ProviderDescriptor]


def get_service(request: Request) -> DirectorService:
    return request.app.state.service


def create_app(
    settings: DirectorSettings | None = None,
    registry: ProviderRegistry | None = None,
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
    service = DirectorService(registry, settings)
    default_model = registry.select(settings.default_provider, settings.default_model).model

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            # Shutdown is best-effort per provider; cancellation propagates.
            await registry.aclose()

    app = FastAPI(title="Dungeon Director", lifespan=lifespan)
    app.state.service = service

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": _SERVICE_NAME}

    @app.get("/v1/config", response_model=DirectorConfig)
    def config() -> DirectorConfig:
        return DirectorConfig(
            default_provider=settings.default_provider,
            default_model=default_model,
            providers=registry.describe(),
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
        outcome = await service.generate(
            body, provider=_normalize_selector(provider), model=_normalize_selector(model)
        )
        return JSONResponse(
            status_code=outcome.status_code, content=outcome.response.model_dump(mode="json")
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path != _GENERATE_PATH:
            return await request_validation_exception_handler(request, exc)
        return _invalid_request_response(exc)

    return app


def _normalize_selector(value: str | None) -> str | None:
    """Blank or whitespace-only selectors mean "not given"; padding is trimmed."""
    if value is None:
        return None
    return value.strip() or None


def _invalid_request_response(exc: RequestValidationError) -> JSONResponse:
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
    return JSONResponse(status_code=422, content=response.model_dump(mode="json"))


#: Production instance, served by ``make run-director`` (``dungeon_director.app:app``).
app = create_app()
