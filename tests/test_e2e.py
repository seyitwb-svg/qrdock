#!/usr/bin/env python3
"""E2E core-flow test for a gr-* app.

Runs the public funnel end-to-end against the ROUTER (the same path users take):
  landing -> register -> dashboard -> pricing -> mock checkout -> paid plan.

Usage:  BASE_URL=http://127.0.0.1:8100 HOST=<slug>.gr.local python3 test_e2e.py
        (HOST is sent as Host header; use the real tunnel URL for remote runs)
Browser: playwright chromium if installed, else system chromium binary
        (own --user-data-dir; NEVER connects to an existing browser/CDP).
"""
from __future__ import annotations

import os
import random
import sys
import time
import uuid

HOST = os.environ.get("HOST", "")
SLUG = os.environ.get("SLUG", HOST.split(".")[0] or "app")
PORT = os.environ.get("PORT", "8100")
BASE = os.environ.get("BASE_URL", f"http://{HOST or '127.0.0.1'}:{PORT}")


def browser_ctx(pw, headers=None):
    """Persistent context = isolated user-data-dir; never touches another browser.

    HOST is mapped onto 127.0.0.1 via --host-resolver-rules so the browser
    uses the real hostname (cookies/Host header) end-to-end.
    """
    udd = os.path.join(os.environ.get("TMPDIR","/tmp"),f"pw-e2e-{SLUG}-{int(time.time())}")
    exe = os.environ.get("GR_BROWSER")
    args = ["--no-sandbox", "--disable-dev-shm-usage"]
    if HOST:
        args.append(f"--host-resolver-rules=MAP {HOST} 127.0.0.1")
    kwargs = dict(headless=True, args=args)
    if headers:
        kwargs["extra_http_headers"] = headers
    if exe:
        kwargs["executable_path"] = exe
    else:
        for cand in ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"):
            if os.path.exists(cand):
                kwargs["executable_path"] = cand
                break
    return pw.chromium.launch_persistent_context(udd, **kwargs)


def main() -> int:
    from playwright.sync_api import sync_playwright

    email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    with sync_playwright() as pw:
        extra = {"Host": HOST} if HOST else {}
        extra["X-Forwarded-For"] = f"198.51.100.{random.randint(2, 250)}"
        browser = browser_ctx(pw, extra)
        page = browser.new_page()

        page.goto(f"{BASE}/", timeout=20000)
        check("landing loads", page.title() != "" and page.locator("h1").count() >= 1)
        check("noindex present (beta)", "noindex" in (page.content().lower()))

        page.goto(f"{BASE}/register")
        page.fill("input[name=email]", email)
        page.fill("input[name=password]", "TestPass123!")
        page.click("button[type=submit]")
        page.wait_for_url("**/dashboard", timeout=15000)
        check("register -> dashboard", "/dashboard" in page.url)

        page.goto(f"{BASE}/pricing")
        check("pricing shows plans", page.locator(".plan").count() >= 2)

        page.locator("form[action='/billing/checkout'] button").first.click()
        page.wait_for_load_state("networkidle")
        check("mock checkout page", "checkout" in page.url or "test" in page.content().lower())

        pay = page.locator("form[action='/billing/mock-pay'] button")
        if pay.count():
            pay.first.click()
            page.wait_for_url("**/dashboard**", timeout=15000)
            check("mock pay -> dashboard", "/dashboard" in page.url)
            check("plan upgraded", "pro" in page.content().lower() or "paid" in page.content().lower())
        else:
            check("mock pay reachable", False)

        # --- qrdock core: link -> 302 scan -> qr png/svg ---
        page.goto(f"{BASE}/dashboard")
        page.fill("input[name=name]", "Flyer")
        page.fill("input[name=dest]", "https://example.com/target")
        page.locator("form[action='/app/links'] button").click()
        page.wait_for_load_state("networkidle")
        link = page.locator("a[href^='/app/link/']").first.get_attribute("href")
        check("link created", bool(link))
        page.goto(f"{BASE}{link}")

        # redirect + scan count
        slug = page.locator("code").filter(has_text="/r/").first \
            .text_content().split("/r/")[1].strip()
        r0 = page.request.get(f"{BASE}/r/{slug}", max_redirects=0)
        check("302 to dest", r0.status == 302 and
              r0.headers.get("location") == "https://example.com/target")
        page.goto(f"{BASE}{link}")
        import re as _re
        m = _re.search(r'class="stat">\s*(\d+)', page.content())
        check("scan counted", m and int(m.group(1)) >= 1)

        # qr endpoints
        token = page.locator("img[src^='/qr/']").first.get_attribute("src")
        token = token.split("/qr/")[1].split(".")[0]
        rq = page.request.get(f"{BASE}/qr/{token}.png")
        check("qr png served", rq.ok and rq.body()[:4] == b"\x89PNG")
        rs = page.request.get(f"{BASE}/qr/{token}.svg")
        check("qr svg served", rs.ok and b"<svg" in rs.body())

        # retarget dest
        page.fill("input[name=dest]", "https://example.com/new")
        page.locator("form[action$='/dest'] button").click()
        page.wait_for_load_state("networkidle")
        r1 = page.request.get(f"{BASE}/r/{slug}", max_redirects=0)
        check("retarget works", r1.headers.get("location") ==
              "https://example.com/new")

        # free static qr (no account needed)
        rf = page.request.get(f"{BASE}/qr.png?text=hello")
        check("free static qr", rf.ok and rf.body()[:4] == b"\x89PNG")

        # bad slug 404
        r3 = page.request.get(f"{BASE}/r/zzzzzz", max_redirects=0)
        check("bad slug 404", r3.status == 404)

        page.goto(f"{BASE}/impressum")
        check("impressum", "impressum" in page.content().lower())
        page.goto(f"{BASE}/datenschutz")
        check("datenschutz", "datenschutz" in page.content().lower() or "data" in page.content().lower())

        resp = page.request.get(f"{BASE}/health")
        check("health 200", resp.status == 200)

        browser.close()

    print("E2E:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
