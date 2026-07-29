import re
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.db.repositories.users import UserRepository
from app.db.session import async_session_factory

router = APIRouter()


def _redirect_uri() -> str:
    settings = get_settings()
    return (
        settings.dashboard_google_redirect_uri
        or f"{settings.public_base_url.rstrip('/')}/auth/google/callback"
    )


@router.get("/auth/google/start")
async def google_auth_start(
    request: Request,
    link_token: str | None = None,
) -> RedirectResponse:
    settings = get_settings()
    if not settings.google_client_id:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID is not configured.")

    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    if link_token:
        request.session["dashboard_link_token"] = link_token

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    }
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}")


@router.get("/auth/google/callback")
async def google_auth_callback(request: Request, code: str, state: str) -> RedirectResponse:
    settings = get_settings()
    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=500, detail="Google OAuth credentials are not configured.")

    async with httpx.AsyncClient(timeout=10.0) as client:
        token_response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": _redirect_uri(),
            },
        )
        token_response.raise_for_status()
        access_token = token_response.json()["access_token"]
        user_response = await client.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        user_response.raise_for_status()
        profile = user_response.json()

    email = str(profile.get("email") or "").strip().casefold()
    if not email:
        raise HTTPException(status_code=400, detail="Google did not return an email address.")

    async with async_session_factory() as session:
        repository = UserRepository(session)
        link_token = request.session.pop("dashboard_link_token", None)
        if link_token:
            user = await repository.link_google_email_by_token(
                token=str(link_token),
                google_email=email,
            )
            if user is None:
                raise HTTPException(status_code=403, detail="Dashboard link expired or invalid.")
        else:
            user = await repository.get_by_google_email(google_email=email)
            if user is None:
                request.session.clear()
                return RedirectResponse("/auth/not-invited", status_code=303)

    request.session["google_email"] = email
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/auth/not-invited", response_class=HTMLResponse)
async def not_invited() -> str:
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dashboard access</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: system-ui, sans-serif; background: #f5f6f8; color: #17181c; }
    main { width: min(420px, calc(100% - 32px)); background: white; border: 1px solid #e5e7eb; border-radius: 18px; padding: 24px; box-shadow: 0 16px 40px rgba(15,23,42,.06); }
    h1 { margin: 0 0 10px; font-size: 22px; }
    p { color: #70737c; line-height: 1.45; }
  </style>
</head>
<body>
  <main>
    <h1>Not invited yet</h1>
    <p>Ask a household member to invite you in Telegram, then use /dashboard_link to connect your Google account.</p>
  </main>
</body>
</html>
"""


@router.post("/auth/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/dashboard", status_code=303)
