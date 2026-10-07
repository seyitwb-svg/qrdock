"""Auth: argon2 passwords, signed-cookie sessions, CSRF, sliding-window rate limit."""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from fastapi import HTTPException, Request
from itsdangerous import URLSafeSerializer, BadSignature

from . import db

ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)

_DATA = Path(os.environ.get("GR_DATA_DIR", "/data"))
_SESSION_NAME = "gr_session"
_CSRF_NAME = "gr_csrf"

_RL: dict[tuple[str, str], list[float]] = {}
_RATE_MAX = int(os.environ.get("GR_RATE_MAX", "5"))
_RATE_WINDOW = int(os.environ.get("GR_RATE_WINDOW", "600"))


def _secret() -> bytes:
    env = os.environ.get("APP_SECRET")
    if env:
        return env.encode()
    f = _DATA / "secret.key"
    if not f.exists():
        _DATA.mkdir(parents=True, exist_ok=True)
        f.write_text(secrets.token_urlsafe(48))
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
    return f.read_text().strip().encode()


def _ser() -> URLSafeSerializer:
    return URLSafeSerializer(_secret(), salt="gr-session-v1")


# ---------- passwords ----------

def hash_pw(pw: str) -> str:
    return ph.hash(pw)


def verify_pw(pw_hash: str, pw: str) -> bool:
    try:
        return ph.verify(pw_hash, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ---------- rate limit ----------

def _client_ip(request: Request) -> str:
    # apps sit behind the trusted gr-router: the leftmost X-Forwarded-For
    # entry is the real visitor ip; fall back to the peer (direct/tests).
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()[:45]
    return request.client.host if request.client else "?"


def rate_limit(request: Request, action: str,
               max_hits: int | None = None) -> None:
    ip = _client_ip(request)
    key = (ip, action)
    now = time.time()
    hits = [t for t in _RL.get(key, []) if now - t < _RATE_WINDOW]
    if len(hits) >= (max_hits or _RATE_MAX):
        raise HTTPException(status_code=429, detail="Too many attempts, try again later")
    hits.append(now)
    _RL[key] = hits


# ---------- sessions ----------

def make_session(user_id: int, email: str) -> str:
    return _ser().dumps({"uid": user_id, "email": email, "csrf": secrets.token_urlsafe(16)})


def read_session(request: Request) -> dict | None:
    raw = request.cookies.get(_SESSION_NAME)
    if not raw:
        return None
    try:
        data = _ser().loads(raw)
        return data if isinstance(data, dict) and "uid" in data else None
    except BadSignature:
        return None


def current_user(request: Request):
    s = read_session(request)
    if not s:
        return None
    return db.one("SELECT id,email,plan,notify_url,created_at FROM users WHERE id=?", (s["uid"],))


def require_user(request: Request):
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return u


# ---------- csrf ----------

def csrf_token(request: Request) -> str:
    s = read_session(request)
    if s and s.get("csrf"):
        return s["csrf"]
    return getattr(request.state, "csrf_anon", "") \
        or request.cookies.get(_CSRF_NAME) or secrets.token_urlsafe(16)


def verify_csrf(request: Request, token: str | None) -> None:
    s = read_session(request)
    expected = ((s or {}).get("csrf") or getattr(request.state, "csrf_anon", "")
                or request.cookies.get(_CSRF_NAME))
    if not expected or not token or not secrets.compare_digest(expected, token):
        raise HTTPException(status_code=403, detail="CSRF check failed")


SESSION_COOKIE = _SESSION_NAME
CSRF_COOKIE = _CSRF_NAME

# Secure cookies only over HTTPS; local dev on http needs GR_COOKIE_SECURE=0.
_COOKIE_SECURE = os.environ.get("GR_COOKIE_SECURE", "1") == "1"


def csrf_cookie_kwargs() -> dict:
    return {
        "httponly": True,
        "secure": _COOKIE_SECURE,
        "samesite": "lax",
        "max_age": 60 * 60 * 24,
        "path": "/",
    }


def session_cookie_kwargs() -> dict:
    return {
        "httponly": True,
        "secure": _COOKIE_SECURE,
        "samesite": "lax",
        "max_age": 60 * 60 * 24 * 30,
        "path": "/",
    }
