"""Billing: mock checkout (default) or stripe_test via REST when a test key exists."""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import auth, db

PROVIDER = os.environ.get("BILLING_PROVIDER", "mock")
STRIPE_SK = os.environ.get("STRIPE_TEST_SK", "")
STRIPE_PK = os.environ.get("STRIPE_TEST_PK", "")


def router(product: dict, render) -> APIRouter:
    rt = APIRouter(prefix="/billing")
    plans = {p["id"]: p for p in product["plans"]}

    def _activate(user_id: int, plan: str, ref: str):
        db.exec_(
            "INSERT INTO subscriptions(user_id,plan,provider,status,ref) VALUES(?,?,?,'active',?) "
            "ON CONFLICT(user_id) DO UPDATE SET plan=excluded.plan, provider=excluded.provider, "
            "status='active', ref=excluded.ref, updated_at=datetime('now')",
            (user_id, plan, PROVIDER, ref),
        )
        db.exec_("UPDATE users SET plan=? WHERE id=?", (plan, user_id))
        db.log_event(user_id, "checkout", f"{PROVIDER}:{plan}")

    @rt.post("/checkout")
    async def checkout(request: Request, plan: str = Form(...), csrf: str = Form("")):
        auth.verify_csrf(request, csrf)
        user = auth.require_user(request)
        p = plans.get(plan)
        if not p:
            raise HTTPException(404, "unknown plan")
        if PROVIDER == "stripe_test" and STRIPE_SK.startswith("sk_test_"):
            async with httpx.AsyncClient(timeout=20) as cl:
                resp = await cl.post(
                    "https://api.stripe.com/v1/checkout/sessions",
                    auth=(STRIPE_SK, ""),
                    data={
                        "mode": "subscription",
                        "line_items[0][price]": p["stripe_price"],
                        "line_items[0][quantity]": "1",
                        "success_url": str(request.base_url) + "billing/stripe-return?session_id={CHECKOUT_SESSION_ID}",
                        "cancel_url": str(request.base_url) + "pricing",
                        "customer_email": user["email"],
                    },
                )
            if resp.status_code != 200:
                raise HTTPException(502, "stripe checkout failed")
            return RedirectResponse(resp.json()["url"], status_code=303)
        # mock: render local checkout page
        return render(request, "checkout_mock.html", plan=p)

    @rt.post("/mock-pay")
    async def mock_pay(request: Request, plan: str = Form(...), csrf: str = Form("")):
        auth.verify_csrf(request, csrf)
        user = auth.require_user(request)
        if plan not in plans:
            raise HTTPException(404, "unknown plan")
        _activate(user["id"], plan, "mock-" + os.urandom(4).hex())
        return RedirectResponse("/dashboard?m=Payment+successful+%28test%29", status_code=303)

    @rt.get("/stripe-return")
    async def stripe_return(request: Request, session_id: str = ""):
        user = auth.require_user(request)
        if not session_id or not STRIPE_SK.startswith("sk_test_"):
            return RedirectResponse("/pricing", status_code=303)
        async with httpx.AsyncClient(timeout=20) as cl:
            resp = await cl.get(
                f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
                auth=(STRIPE_SK, ""),
            )
        if resp.status_code == 200 and resp.json().get("payment_status") == "paid":
            plan = "pro"
            _activate(user["id"], plan, session_id)
            return RedirectResponse("/dashboard?m=Payment+successful", status_code=303)
        return RedirectResponse("/pricing?m=Checkout+incomplete", status_code=303)

    return rt
