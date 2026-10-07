"""notify.py — owner webhooks: one Slack/Discord/HTTP URL per account.

Products call send(db, owner_id, "…") on paid-flow events (new submission,
signup, booking, accepted quote). Disabled by default (empty notify_url).
Failures are swallowed — notifications must never break the core flow.
"""
import httpx

from . import db as _db


def send(owner_id: int, text: str) -> bool:
    try:
        _db.log_event(owner_id, "notify", text)
    except Exception:
        pass
    try:
        u = _db.one("SELECT notify_url FROM users WHERE id=?", (owner_id,))
        url = (u["notify_url"] or "").strip() if u else ""
        if not url.startswith(("http://", "https://")):
            return False
        httpx.post(url, json={"text": text[:1000]}, timeout=5)
        return True
    except Exception:
        return False
