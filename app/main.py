"""FastAPI application: HTTP API, webhook, MCP mount and scheduler lifecycle."""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import ConfigurationError, get_settings
from app.db import close_db, init_db, ping, stats
from app.gmail_client import get_gmail_client
from app.logging_config import setup_logging
from app.mcp_server import create_mcp_asgi_app, create_mcp_server
from app.routes_whatsapp import router as whatsapp_router
from app.scheduler import scheduler_status, start_scheduler, stop_scheduler

try:
    from app.routes_admin import router as admin_router
except Exception:  # pragma: no cover - optional admin deps
    admin_router = None

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"


class MCPBearerAuthMiddleware:
    """Serve the MCP endpoint and require a bearer token.

    A plain Starlette ``Mount`` answers ``/mcp`` with a redirect to
    ``/mcp/``; this middleware forwards the request to the MCP ASGI app
    directly, so both spellings work.
    """

    def __init__(self, app: ASGIApp, mcp_app: ASGIApp) -> None:
        self.app = app
        self.mcp_app = mcp_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path != MCP_PATH and not path.startswith(f"{MCP_PATH}/"):
            await self.app(scope, receive, send)
            return

        expected = get_settings().mcp_api_key.encode("utf-8")
        provided = Headers(scope=scope).get("authorization", "").encode("utf-8")
        if not secrets.compare_digest(provided, b"Bearer " + expected):
            logger.warning("Rejected an unauthenticated MCP request")
            response = JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "detail": "a valid bearer token is required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.mcp_app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop every background component of the application."""

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        setup_logging("INFO")
        logger.error("Startup aborted: %s", exc)
        raise

    setup_logging(
        settings.log_level,
        secrets=[
            settings.anthropic_api_key,
            settings.whatsapp_token,
            settings.whatsapp_app_secret,
            settings.whatsapp_verify_token,
            settings.mcp_api_key,
        ],
    )
    logger.info(
        "Starting the assistant",
        extra={"model": settings.claude_model, "summary_timezone": settings.summary_timezone},
    )

    init_db()

    try:
        from app.services import seed_initial_unread

        await seed_initial_unread()
    except Exception:
        logger.exception("Seeding the initial unread mail failed")

    start_scheduler()

    # The MCP session manager may only be run once per instance; the server
    # belongs to this application instance, so this is safe.
    try:
        async with app.state.mcp_server.session_manager.run():
            yield
    finally:
        stop_scheduler()
        close_db()
        logger.info("Assistant stopped")


def create_app() -> FastAPI:
    """Build the FastAPI application."""

    application = FastAPI(
        title="Email <-> WhatsApp AI Agent",
        version="0.1.0",
        description=(
            "Personal assistant bridging Gmail and WhatsApp with Claude, "
            "exposed over MCP for any MCP client."
        ),
        lifespan=lifespan,
    )

    mcp_server = create_mcp_server()
    application.state.mcp_server = mcp_server
    application.add_middleware(MCPBearerAuthMiddleware, mcp_app=create_mcp_asgi_app(mcp_server))
    application.include_router(whatsapp_router)
    if admin_router is not None:
        application.include_router(admin_router)

    @application.get("/health", tags=["ops"], summary="Health check")
    async def health() -> dict[str, Any]:
        """Report the state of the database, the Gmail token and the scheduler."""

        settings = get_settings()
        gmail = get_gmail_client()
        return {
            "status": "ok",
            "database": {"ok": ping(), **stats()},
            "gmail": gmail.token_status(),
            "scheduler": scheduler_status(),
            "model": settings.claude_model,
            "mcp_path": MCP_PATH,
        }

    return application


app = create_app()
