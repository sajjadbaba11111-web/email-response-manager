"""Admin dashboard for viewing email-response activity.

Protected by username/password configured via environment variables.  The UI is
server-rendered and reads from the same database used by the assistant, so it
works with both PostgreSQL (Docker/production) and SQLite fallback.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import db
from app.config import PROJECT_ROOT, get_settings

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "templates"))

SESSION_COOKIE = "admin_session"
SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours


def _serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    return URLSafeTimedSerializer(settings.admin_secret_key, salt="admin-session")


def _is_authed(request: Request) -> bool:
    settings = get_settings()
    if not settings.admin_enabled:
        return False
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return isinstance(data, dict) and data.get("u") == settings.admin_username


def _require_admin(request: Request):
    settings = get_settings()
    if not settings.admin_enabled:
        return HTMLResponse(
            "<h3>Admin UI disabled</h3><p>Set ADMIN_PASSWORD and ADMIN_SECRET_KEY.</p>",
            status_code=404,
        )
    if not _is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


def _base_context(request: Request) -> dict[str, Any]:
    settings = get_settings()
    return {
        "request": request,
        "db_backend": "postgresql" if settings.use_postgres else "sqlite",
    }


@router.get("", include_in_schema=False)
async def admin_root(request: Request):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, error: str = ""):
    settings = get_settings()
    if not settings.admin_enabled:
        return HTMLResponse(
            "<h3>Admin UI disabled</h3><p>Set ADMIN_PASSWORD and ADMIN_SECRET_KEY.</p>",
            status_code=404,
        )
    if _is_authed(request):
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    return templates.TemplateResponse(
        "admin/login.html", {**_base_context(request), "error": error}
    )


@router.post("/login", include_in_schema=False)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    settings = get_settings()
    if not settings.admin_enabled:
        return HTMLResponse("Admin UI disabled", status_code=404)
    ok_user = secrets.compare_digest(username, settings.admin_username)
    ok_pass = secrets.compare_digest(password, settings.admin_password)
    if not (ok_user and ok_pass):
        db.log_activity(
            "admin_login_failed",
            "Failed admin login attempt",
            actor=username[:100],
        )
        return RedirectResponse(url="/admin/login?error=Invalid+credentials", status_code=303)
    token = _serializer().dumps({"u": settings.admin_username})
    response = RedirectResponse(url="/admin/dashboard", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,  # set True behind HTTPS in production
    )
    db.log_activity("admin_login", "Admin logged in", actor=settings.admin_username)
    return response


@router.post("/logout", include_in_schema=False)
async def logout(request: Request):
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    if _is_authed(request):
        db.log_activity("admin_logout", "Admin logged out", actor=get_settings().admin_username)
    return response


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    activities = db.list_activities(limit=300)
    drafts = db.list_all_drafts(limit=200)
    emails = db.list_email_snapshots(limit=200)
    stats = db.stats()
    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            **_base_context(request),
            "stats": stats,
            "activities": activities,
            "drafts": drafts,
            "emails": emails,
        },
    )


@router.get("/draft/{draft_id}", response_class=HTMLResponse, include_in_schema=False)
async def draft_detail(request: Request, draft_id: int):
    guard = _require_admin(request)
    if guard is not None:
        return guard
    item = db.get_draft_with_email(draft_id)
    if not item:
        return HTMLResponse("Draft not found", status_code=404)
    return templates.TemplateResponse(
        "admin/draft_detail.html", {**_base_context(request), "item": item}
    )
