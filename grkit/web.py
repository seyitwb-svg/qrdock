"""web.py — app factory: security headers, noindex gate, routes, templates."""
from __future__ import annotations

import html as _html
import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, db
from .billing import router as billing_router

BASE = Path(__file__).resolve().parent.parent


def esc(s) -> str:
    return _html.escape(str(s), quote=True)


def public_base(request: Request, slug: str = "") -> str:
    """Public origin for user-facing links/snippets.

    Behind the slug router + quick tunnel the request host is the
    internal `<slug>.gr.local`, so prefer the recorded tunnel URL
    (/tunnels/<slug>.url, mounted read-only) and fall back to the
    request's base URL (direct access, tests, real domains).
    """
    if slug:
        try:
            u = Path(f"/tunnels/{slug}.url").read_text().strip().rstrip("/")
            if u.startswith("https://"):
                return u
        except Exception:
            pass
    return str(request.base_url).rstrip("/")


def make_app(product: dict) -> FastAPI:
    app = FastAPI(title=product["name"], docs_url=None, redoc_url=None, openapi_url=None)
    tpl = Jinja2Templates(directory=str(BASE / "templates"))
    noindex = os.environ.get("LAUNCH_PUBLIC", "0") != "1"

    @app.middleware("http")
    async def headers(request: Request, call_next):
        # ensure an anonymous CSRF cookie exists so public forms validate
        anon = request.cookies.get(auth.CSRF_COOKIE)
        if not anon:
            import secrets as _sec
            anon = _sec.token_urlsafe(16)
            request.state._set_csrf_cookie = anon
        request.state.csrf_anon = anon
        resp = await call_next(request)
        new_csrf = getattr(request.state, "_set_csrf_cookie", None)
        if new_csrf:
            resp.set_cookie(auth.CSRF_COOKIE, new_csrf, **auth.csrf_cookie_kwargs())
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: https://modular-nascar-arkansas-program.trycloudflare.com; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'"
        )
        if noindex:
            resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        return resp

    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    def _favicon():
        from fastapi.responses import FileResponse
        return FileResponse(BASE / "static" / "favicon.png",
                            media_type="image/png")

    def _og_image(request: Request, product: dict) -> str:
        key = os.environ.get("GR_OG_KEY", "")
        base = public_base(request, "ogdock")
        if not key or not base:
            return ""
        import urllib.parse as _up
        t = _up.quote(product.get("name", "")[:80])
        st = _up.quote(product.get("tagline", "")[:120])
        return f"{base}/og/{key}.png?t={t}&s={st}"

    def _portfolio(request: Request) -> str:
        base = public_base(request, "markdock")
        return f"{base}/d/grandline" if base else ""

    def _shop(request: Request) -> str:
        base = public_base(request, "goldshop")
        return f"{base}/shop" if base else ""

    def ctx(request: Request, **kw):
        user = auth.current_user(request)
        base = {
            "request": request,
            "product": product,
            "user": user,
            "csrf": auth.csrf_token(request),
            "beta": noindex,
            "flash": request.query_params.get("m"),
            "og_image": _og_image(request, product),
            "portfolio": _portfolio(request),
            "shop": _shop(request),
        }
        base.update(kw)
        return base

    def r(request: Request, page: str, **kw):
        return tpl.TemplateResponse(request, page, ctx(request, **kw))

    # ---------- pages ----------

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request):
        return r(request, "landing.html")

    @app.get("/pricing", response_class=HTMLResponse)
    def pricing(request: Request):
        return r(request, "pricing.html", plans=product["plans"])

    @app.get("/register", response_class=HTMLResponse)
    def register_form(request: Request):
        return r(request, "register.html")

    @app.post("/register")
    def register(request: Request, email: str = Form(...), password: str = Form(...),
                 csrf: str = Form("")):
        auth.verify_csrf(request, csrf)
        auth.rate_limit(request, "register")
        email = email.strip().lower()
        if "@" not in email or len(email) > 200:
            return r(request, "register.html", error="Enter a valid email.")
        if len(password) < 8:
            return r(request, "register.html", error="Password needs at least 8 characters.")
        if db.one("SELECT id FROM users WHERE email=?", (email,)):
            return r(request, "register.html", error="This email is already registered. Log in instead.")
        uid = db.exec_("INSERT INTO users(email,pw_hash) VALUES(?,?)",
                       (email, auth.hash_pw(password)))
        db.log_event(uid, "register", product["slug"])
        resp = RedirectResponse("/dashboard", status_code=303)
        resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(uid, email), **auth.session_cookie_kwargs())
        return resp

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return r(request, "login.html")

    @app.post("/login")
    def login(request: Request, email: str = Form(...), password: str = Form(...),
              csrf: str = Form("")):
        auth.verify_csrf(request, csrf)
        auth.rate_limit(request, "login")
        user = db.one("SELECT * FROM users WHERE email=?", (email.strip().lower(),))
        if not user or not auth.verify_pw(user["pw_hash"], password):
            return r(request, "login.html", error="Invalid email or password.")
        resp = RedirectResponse("/dashboard", status_code=303)
        resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(user["id"], user["email"]),
                        **auth.session_cookie_kwargs())
        return resp

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(auth.SESSION_COOKIE, path="/")
        return resp

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request):
        user = auth.require_user(request)
        core = product.get("core_home")
        core_html = core(request, user) if core else None
        fleet_url = ""
        try:
            _u = Path("/tunnels/markdock.url").read_text().strip().rstrip("/")
            if _u.startswith("https://"):
                fleet_url = _u + "/d/grandline"
        except Exception:
            pass
        evs = db.query(
            "SELECT kind, detail, at FROM events WHERE user_id=? "
            "ORDER BY id DESC LIMIT 10", (user["id"],))
        return r(request, "dashboard.html", core_html=core_html,
                 fleet_url=fleet_url, events=evs)

    @app.post("/app/notify-url")
    def set_notify_url(request: Request, url: str = Form(""),
                       csrf: str = Form("")):
        auth.verify_csrf(request, csrf)
        user = auth.require_user(request)
        url = url.strip()[:300]
        if url and not url.startswith(("http://", "https://")):
            return RedirectResponse("/dashboard?m=URL+must+start+http(s)", 303)
        db.exec_("UPDATE users SET notify_url=? WHERE id=?",
                 (url, user["id"]))
        return RedirectResponse("/dashboard?m=Notification+saved", 303)

    @app.post("/app/delete-account")
    def delete_account(request: Request, password: str = Form(""),
                       csrf: str = Form("")):
        auth.verify_csrf(request, csrf)
        user = auth.require_user(request)
        row = db.one("SELECT pw_hash FROM users WHERE id=?", (user["id"],))
        if not row or not auth.verify_pw(row["pw_hash"], password):
            return RedirectResponse("/dashboard?m=Wrong+password", 303)
        uid = user["id"]
        wipe = product.get("wipe_user")
        if wipe:
            wipe(uid)
        db.exec_("DELETE FROM subscriptions WHERE user_id=?", (uid,))
        db.exec_("DELETE FROM events WHERE user_id=?", (uid,))
        db.exec_("DELETE FROM users WHERE id=?", (uid,))
        resp = RedirectResponse("/", 303)
        resp.delete_cookie(auth.SESSION_COOKIE, path="/")
        return resp

    @app.get("/impressum", response_class=HTMLResponse)
    def impressum(request: Request):
        return r(request, "impressum.html")

    @app.get("/datenschutz", response_class=HTMLResponse)
    def datenschutz(request: Request):
        return r(request, "datenschutz.html")

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots():
        return "User-agent: *\nDisallow: /\n" if noindex else "User-agent: *\nAllow: /\n"

    @app.get("/health")
    def health():
        try:
            db.one("SELECT 1")
            return {"status": "ok", "app": product["slug"], "db": "ok"}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(e))

    # billing routes (mock checkout + optional stripe_test switch)
    app.include_router(billing_router(product, render=r))

    # product core flow (the thing people pay for)
    if product.get("register_core"):
        product["register_core"](app, r, auth, db)

    @app.exception_handler(303)
    async def redirect_auth(request: Request, exc: HTTPException):
        return RedirectResponse(exc.headers.get("Location", "/login"), status_code=303)

    @app.exception_handler(404)
    async def nf(request: Request, exc):
        return tpl.TemplateResponse(request, "404.html", ctx(request), status_code=404)

    return app
