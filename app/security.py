from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from functools import wraps

from flask import abort, current_app, g, redirect, request, session, url_for

from .db import get_db, transaction


def csrf_token() -> str:
    token = session.get("_csrf")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def require_csrf():
    expected = session.get("_csrf", "")
    received = request.form.get("_csrf", "")
    if not expected or not hmac.compare_digest(expected, received):
        abort(400, "CSRF token non valido.")


def login_required(role: str | None = None):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.get("user") is None:
                return redirect(url_for("auth.login", next=request.path))
            if role and g.user["role"] != role:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorate


def record_login_attempt(ip: str, successful: bool):
    key = hmac.new(
        current_app.config["SECRET_KEY"].encode(),
        ip.encode(),
        hashlib.sha256,
    ).hexdigest()
    now = int(time.time())
    with transaction() as db:
        db.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (now - 86400,))
        db.execute(
            "INSERT INTO login_attempts(ip_hash, attempted_at, successful) VALUES (?, ?, ?)",
            (key, now, int(successful)),
        )


def login_is_limited(ip: str) -> bool:
    key = hmac.new(
        current_app.config["SECRET_KEY"].encode(),
        ip.encode(),
        hashlib.sha256,
    ).hexdigest()
    cutoff = int(time.time()) - 900
    failures = get_db().execute(
        "SELECT COUNT(*) FROM login_attempts WHERE ip_hash=? AND attempted_at>=? AND successful=0",
        (key, cutoff),
    ).fetchone()[0]
    return failures >= 10
