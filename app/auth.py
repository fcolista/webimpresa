from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db
from .security import csrf_token, login_is_limited, record_login_attempt, require_csrf

bp = Blueprint("auth", __name__)
DUMMY_PASSWORD_HASH = generate_password_hash("not-a-real-password-used-for-timing", method="scrypt")


@bp.app_context_processor
def inject_layout():
    from .ui import get_settings
    settings = get_settings()
    return {
        "csrf": csrf_token,
        "ui_settings": {key: settings[key] for key in ("app_name", "theme_primary", "theme_accent", "theme_background") if key in settings},
        "has_provider_logo": bool(settings.get("provider_logo")),
        "has_provider_signature": bool(settings.get("provider_signature")),
    }


@bp.get("/")
def index():
    if not g.get("user"):
        return redirect(url_for("auth.login"))
    if g.user["role"] == "admin":
        return redirect(url_for("admin.index"))
    return redirect(url_for("operations.dashboard"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user"):
        return redirect(url_for("admin.index" if g.user["role"] == "admin" else "operations.dashboard"))
    if request.method == "POST":
        require_csrf()
        ip = request.remote_addr or "unknown"
        if login_is_limited(ip):
            flash("Troppi tentativi. Attendi 15 minuti prima di riprovare.", "error")
            return render_template("login.html"), 429
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT id, username, password_hash, role, active FROM users WHERE username=? COLLATE NOCASE",
            (username,),
        ).fetchone()
        password_matches = check_password_hash(user["password_hash"] if user else DUMMY_PASSWORD_HASH, password)
        valid = bool(user and user["active"] and password_matches)
        record_login_attempt(ip, valid)
        if not valid:
            flash("Credenziali non valide o account disabilitato.", "error")
            return render_template("login.html"), 401
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        session["_csrf"] = csrf_token()
        return redirect(url_for("admin.index" if user["role"] == "admin" else "operations.dashboard"))
    return render_template("login.html")


@bp.post("/logout")
def logout():
    require_csrf()
    session.clear()
    flash("Sei uscito dall'applicazione.", "success")
    return redirect(url_for("auth.login"))
