"""The API application.

Read this file to see the shape of the service: what is checked at startup,
what middleware every request passes through, and which routers exist. Anything
security-relevant that is not visible here is visible in `deps.py`.

Startup refuses rather than degrades. A missing secret, a malformed encryption
key, or a tool that accepts a caller-supplied user id all stop the process from
booting. The alternative -- starting and failing later -- means the failure
arrives as a 500 in production instead of as a red deploy.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response

from . import db, errors
from .observability import configure, logger
from .routers import account, agent, auth, integrations
from .security.http import CSRF_HEADER, SecurityHeadersMiddleware
from .services import agent_runtime
from .settings import settings

log = logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = settings()

    # Order matters: configuration first, because everything below reads it.
    config.validate()

    # A tool that takes a user id would let the model choose whose data to
    # touch. Checked before the first request rather than hoped about.
    agent_runtime.assert_no_identity_parameters()

    await db.connect()
    log.info("api ready (%s)", config.environment)
    try:
        yield
    finally:
        await db.disconnect()


def create_app() -> FastAPI:
    configure()
    config = settings()

    app = FastAPI(
        title="Lumen API",
        version="1.0",
        lifespan=lifespan,
        # No interactive docs in production. They are a map of the auth surface
        # and there is no reason to publish one.
        docs_url=None if config.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if config.is_production else "/openapi.json",
    )

    errors.install(app)

    # Registered here rather than in the router: it exists so /refresh can both
    # clear a cookie and report 401 from inside an except block.
    from .routers.auth import _ResponseCarrier

    @app.exception_handler(_ResponseCarrier)
    async def _carrier(request: Request, exc: _ResponseCarrier) -> Response:
        return exc.response

    # Outermost first. Security headers wrap everything, including CORS
    # rejections and error responses.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        # Never "*": these endpoints are credentialed, and a wildcard with
        # credentials is both forbidden by the spec and a mistake worth being
        # unable to make.
        allow_origins=list(config.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", CSRF_HEADER, "X-Lumen-Client"],
        max_age=600,
    )

    app.include_router(auth.router)
    app.include_router(account.router)
    app.include_router(integrations.router)
    app.include_router(agent.router)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness and readiness. Says nothing about configuration or version.

        A health endpoint that reports which components are unhealthy is a
        reconnaissance endpoint. This one answers up or down.
        """
        ok = await db.healthy()
        return JSONResponse({"status": "ok" if ok else "degraded"}, status_code=200 if ok else 503)

    return app


app = create_app()
