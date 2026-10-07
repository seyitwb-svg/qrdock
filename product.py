"""QRDock — QR codes that stay alive.

Core flow (paid): create a dynamic link -> download its QR (PNG/SVG) ->
print it anywhere -> scans 302 to a destination you can change anytime.
Static one-off QRs work instantly, no account needed.
qr-code-generator.com ~10 EUR/mo for dynamic codes, Bitly QR ~$35/mo.
"""
import io
import secrets
import time

import re
import segno
from fastapi import Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from grkit import auth, db, notify, web

PRODUCT = {
    "slug": "qrdock",
    "name": "QRDock",
    "tagline": "QR codes that stay alive",
    "sub": "Generate a QR, print it on the poster, change where it points "
           "tomorrow. Dynamic links + scan counts; static QRs free forever, "
           "no signup.",
    "cta": "Create your first QR",
    "core_placeholder": "Create your first QR link.",
    "pricing_note": "qr-code-generator.com ~10 EUR/mo for codes that keep "
                    "working, Bitly QR ~$35/mo. QRDock Pro is 7 €/mo flat.",
    "quotes": [
        {"text": "Show HN: Free simple QR code generator 'without the "
                 "faff' — because the big ones lock codes behind paywalls",
         "source": "https://news.ycombinator.com/item?id=29270387"},
        {"text": "Dual-Link QR Code Generator — one code, two "
                 "destinations; people keep building around QR limits",
         "source": "https://news.ycombinator.com/item?id=42824553"},
        {"text": "Client-side QR code generator with SVG output — "
                 "176 points; print-quality QRs shouldn't need a suite",
         "source": "https://news.ycombinator.com/item?id=41410442"},
    ],
    "features": [
        {"title": "Dynamic links", "text": "The QR encodes your QRDock link — retarget the destination anytime without reprinting."},
        {"title": "Scan counts", "text": "Every scan is counted with referrer so you see which poster works."},
        {"title": "PNG + SVG", "text": "Screen PNGs and print-sharp SVGs in any size, no watermark."},
    ],
    "plans": [
        {"id": "free", "name": "Free", "price": "0 €",
         "bullets": ["5 dynamic links", "Unlimited static QRs",
                     "PNG + SVG download"],
         "featured": False},
        {"id": "pro", "name": "Pro", "price": "7 €/mo",
         "bullets": ["Unlimited links", "Custom slugs",
                     "Scan history per link"],
         "featured": True, "stripe_price": ""},
    ],
    "core_home": None,
    "register_core": None,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    token TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    dest TEXT NOT NULL,
    scans INTEGER NOT NULL DEFAULT 0,
    last_referrer TEXT NOT NULL DEFAULT '',
    created INTEGER NOT NULL DEFAULT 0
);
"""

PLAN_LINKS = {"free": 5, "pro": 100000}
_SLUG_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"


def _slug(n=6):
    return "".join(secrets.choice(_SLUG_CHARS) for _ in range(n))


def core_home(request: Request, user) -> str:
    rows = db.query(
        "SELECT id, token, slug, name, dest, scans FROM links "
        "WHERE owner_id=? ORDER BY id DESC", (user["id"],))
    base = web.public_base(request, "qrdock")
    table = ("<table><tr><th>QR link</th><th>Destination</th>"
             "<th>Scans</th><th></th></tr>"
             + "".join(
                 f"<tr><td><a href='/app/link/{x['id']}'>"
                 f"{web.esc(x['name'])}</a>"
                 f"<br><code class='dim'>{base}/r/{x['slug']}</code></td>"
                 f"<td class='dim'>{web.esc(x['dest'][:38])}</td>"
                 f"<td>{x['scans']}</td>"
                 f"<td class='row-form'><form method='post' "
                 f"action='/app/link/{x['id']}/del'>"
                 f"<input type='hidden' name='csrf' "
                 f"value='{auth.csrf_token(request)}'>"
                 f"<button class='btn btn-sm btn-danger'>✕</button></form></td>"
                 f"</tr>" for x in rows) + "</table>") \
        if rows else "<p class='dim'>No QR links yet.</p>"
    csrf = auth.csrf_token(request)
    return f"""
<div class='card'><h2>QR links</h2>{table}</div>
<div class='card'><h2>New QR link</h2>
<form method='post' action='/app/links'>
<input type='hidden' name='csrf' value='{csrf}'>
<div class='inline-form'>
<input name='name' placeholder='Flyer #1' required maxlength='60'>
<input name='dest' type='url' placeholder='https://…' required
       maxlength='500' style='min-width:220px'>
<button class='btn' type='submit'>Create</button>
</div>
<p class='dim'>The QR encodes <code>/r/&lt;slug&gt;</code> — retarget the
destination later without reprinting. Static QR for anything:
<code>/qr.png?text=…</code></p>
</form></div>"""


def _qr_bytes(text: str, kind: str, scale: int = 10,
              dark: str = "#101828") -> bytes:
    q = segno.make(text, error="m")
    buf = io.BytesIO()
    if kind == "svg":
        q.save(buf, kind="svg", scale=scale, xmldecl=False,
               dark=dark, light=None)
    else:
        q.save(buf, kind="png", scale=scale, dark=dark)
    return buf.getvalue()


def register_core(app, r, auth, db):
    db.connect().executescript(SCHEMA)
    db.exec_("""CREATE TABLE IF NOT EXISTS scans_log(
                 link_id INTEGER NOT NULL, day TEXT NOT NULL,
                 n INTEGER NOT NULL DEFAULT 0,
                 UNIQUE(link_id, day))""")

    def own(request: Request, lid: int):
        user = auth.require_user(request)
        x = db.one("SELECT * FROM links WHERE id=? AND owner_id=?",
                   (lid, user["id"]))
        if not x:
            raise HTTPException(404, "link not found")
        return user, x

    @app.post("/app/links")
    def create_link(request: Request, name: str = Form(...),
                    dest: str = Form(...), slug: str = Form(""),
                    csrf: str = Form("")):
        auth.verify_csrf(request, csrf)
        user = auth.require_user(request)
        if db.one("SELECT COUNT(*) n FROM links WHERE owner_id=?",
                  (user["id"],))["n"] >= PLAN_LINKS.get(user["plan"], 5):
            return RedirectResponse("/dashboard?m=Link+limit+reached", 303)
        slug = slug.strip().lower()
        if slug:
            if user["plan"] != "pro":
                return RedirectResponse(
                    "/dashboard?m=Custom+slugs+are+a+Pro+feature", 303)
            if not all(c in _SLUG_CHARS + "-" for c in slug) or len(slug) > 40:
                return RedirectResponse("/dashboard?m=Bad+slug", 303)
            if db.one("SELECT id FROM links WHERE slug=?", (slug,)):
                return RedirectResponse("/dashboard?m=Slug+taken", 303)
        else:
            while True:
                slug = _slug()
                if not db.one("SELECT id FROM links WHERE slug=?", (slug,)):
                    break
        db.exec_("INSERT INTO links(owner_id,token,slug,name,dest,created) "
                 "VALUES(?,?,?,?,?,?)",
                 (user["id"], secrets.token_urlsafe(9), slug,
                  name.strip()[:60], dest.strip()[:500], int(time.time())))
        return RedirectResponse("/dashboard?m=QR+link+created", 303)

    @app.get("/app/link/{lid}")
    def link_admin(request: Request, lid: int):
        user, x = own(request, lid)
        base = web.public_base(request, "qrdock")
        rows = {r["day"]: r["n"] for r in db.query(
            "SELECT day, n FROM scans_log WHERE link_id=? AND "
            "day>=date('now','-13 days')", (lid,))}
        import datetime as _dt
        days = [(_dt.date.today() - _dt.timedelta(days=i)).isoformat()
                for i in range(13, -1, -1)]
        vals = [rows.get(d, 0) for d in days]
        mx = max(vals + [1])
        bars = [{"day": d[5:], "n": v, "h": max(2, int(v * 44 / mx))}
                for d, v in zip(days, vals)]
        return r(request, "link_admin.html", x=x, base=base, bars=bars)

    @app.post("/app/link/{lid}/dest")
    def set_dest(request: Request, lid: int, dest: str = Form(...),
                 csrf: str = Form("")):
        auth.verify_csrf(request, csrf)
        own(request, lid)
        db.exec_("UPDATE links SET dest=? WHERE id=?",
                 (dest.strip()[:500], lid))
        return RedirectResponse(f"/app/link/{lid}?m=Destination+updated", 303)

    @app.post("/app/link/{lid}/del")
    def del_link(request: Request, lid: int, csrf: str = Form("")):
        auth.verify_csrf(request, csrf)
        own(request, lid)
        db.exec_("DELETE FROM links WHERE id=?", (lid,))
        return RedirectResponse("/dashboard?m=Link+deleted", 303)

    # ---------- public ----------
    @app.get("/r/{slug}")
    def follow(request: Request, slug: str):
        auth.rate_limit(request, "follow", 60)
        x = db.one("SELECT * FROM links WHERE slug=?", (slug,))
        if not x:
            raise HTTPException(404, "not found")
        ref = request.headers.get("referer", "")[:200]
        db.exec_("UPDATE links SET scans=scans+1, last_referrer=? "
                 "WHERE id=?", (ref, x["id"]))
        day = time.strftime("%Y-%m-%d")
        db.exec_("INSERT INTO scans_log(link_id,day,n) VALUES(?,?,1) "
                 "ON CONFLICT(link_id,day) DO UPDATE SET n=n+1",
                 (x["id"], day))
        notify.send(x["owner_id"],
                    f"QRDock: '{x['name']}' was just scanned")
        return RedirectResponse(x["dest"], 302)

    @app.get("/qr/{token}.{kind}")
    def link_qr(request: Request, token: str, kind: str,
                size: int = 10, fg: str = ""):
        x = db.one("SELECT * FROM links WHERE token=?", (token,))
        if not x or kind not in ("png", "svg"):
            raise HTTPException(404, "not found")
        base = web.public_base(request, "qrdock")
        scale = max(1, min(size, 20))
        dark = "#101828"
        if fg:
            owner = db.one("SELECT plan FROM users WHERE id=?",
                           (x["owner_id"],))
            if owner and owner["plan"] == "pro" and                     re.fullmatch(r"#[0-9a-fA-F]{6}", fg):
                dark = fg
        data = _qr_bytes(f"{base}/r/{x['slug']}", kind, scale, dark)
        mt = "image/svg+xml" if kind == "svg" else "image/png"
        return Response(data, media_type=mt,
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/qr.{kind}")
    def free_qr(request: Request, kind: str, text: str = "",
                size: int = 10):
        auth.rate_limit(request, "freeqr", 30)
        if kind not in ("png", "svg") or not text.strip():
            raise HTTPException(404, "not found")
        scale = max(1, min(size, 20))
        data = _qr_bytes(text.strip()[:500], kind, scale)
        mt = "image/svg+xml" if kind == "svg" else "image/png"
        return Response(data, media_type=mt,
                        headers={"Cache-Control": "public, max-age=3600"})


PRODUCT["core_home"] = core_home
PRODUCT["register_core"] = register_core


def _wipe_user(uid: int) -> None:
    db.exec_("""DELETE FROM scans_log WHERE link_id IN
                (SELECT id FROM links WHERE owner_id=?)""", (uid,))
    db.exec_("DELETE FROM links WHERE owner_id=?", (uid,))


PRODUCT["wipe_user"] = _wipe_user
