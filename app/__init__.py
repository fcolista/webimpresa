from __future__ import annotations

import os
import secrets
from datetime import datetime
from datetime import timedelta

import click
from flask import Flask, Response, g, redirect, render_template, request, session, url_for

from .security import login_required

from .db import close_db, get_db


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(
        __name__,
        instance_relative_config=True,
        instance_path=os.environ.get("INSTANCE_PATH") or (test_config or {}).get("INSTANCE_PATH")
        or os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"),
    )
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", ""),
        DATABASE=os.environ.get("DATABASE_PATH", os.path.join(app.instance_path, "webimpresa.db")),
        SMTP_FERNET_KEY=os.environ.get("SMTP_FERNET_KEY", ""),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true",
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        TRUSTED_HOSTS=[
            host.strip() for host in os.environ["TRUSTED_HOSTS"].split(",") if host.strip()
        ] if os.environ.get("TRUSTED_HOSTS") else None,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        SMTP_ALLOW_PLAINTEXT=os.environ.get("SMTP_ALLOW_PLAINTEXT", "false").lower() == "true",
    )
    if test_config:
        app.config.update(test_config)
    if not app.config["SECRET_KEY"] and not app.config.get("TESTING"):
        raise RuntimeError("SECRET_KEY must be set outside testing.")
    if not app.config["SMTP_FERNET_KEY"] and not app.config.get("TESTING"):
        raise RuntimeError("SMTP_FERNET_KEY must be set outside testing.")

    os.makedirs(app.instance_path, exist_ok=True)
    app.teardown_appcontext(close_db)

    from . import admin, auth, operations

    app.register_blueprint(auth.bp)
    app.register_blueprint(operations.bp)
    app.register_blueprint(admin.bp)

    @app.template_filter("from_json")
    def from_json(value):
        import json
        return json.loads(value)

    from .ui import status_label
    app.template_filter("status_label")(status_label)

    @app.template_filter("client_label")
    def client_label_filter(client):
        from .branding import client_label
        return client_label(client)

    @app.context_processor
    def common_template_values():
        return {"now": datetime.now}

    @app.before_request
    def load_current_user():
        g.csp_nonce = secrets.token_urlsafe(18)
        user_id = session.get("user_id")
        g.user = None
        if user_id is not None:
            user = get_db().execute(
                "SELECT id, username, role, active FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if user and user["active"]:
                g.user = user
            else:
                session.clear()
        if g.user and g.user["role"] == "admin" and request.endpoint not in {
            "admin.index", "admin.users", "admin.create_user", "admin.toggle_user",
            "admin.update_user", "admin.settings", "admin.test_smtp", "admin.health", "auth.logout",
            "admin.create_backup", "admin.download_backup", "admin.delete_backup", "admin.restore_backup",
            "static", "brand_logo", "brand_signature",
        }:
            return redirect(url_for("admin.index"))
        if g.user and g.user["role"] == "user" and request.endpoint and request.endpoint.startswith("admin."):
            return render_template("error.html", code=403, message="Accesso riservato agli amministratori."), 403

    @app.after_request
    def security_headers(response):
        nonce = getattr(g, "csp_nonce", "")
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; style-src 'self' 'nonce-{nonce}'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; object-src 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(413)
    def too_large(_error):
        return render_template("error.html", code=413, message="La richiesta supera la dimensione consentita."), 413

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="La pagina richiesta non esiste."), 404

    @app.get("/healthz")
    def healthz():
        get_db().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/brand/logo")
    def brand_logo():
        return brand_image("provider_logo")

    @app.get("/brand/signature")
    @login_required()
    def brand_signature():
        return brand_image("provider_signature")

    def brand_image(setting_key):
        from .branding import decode_logo
        from werkzeug.exceptions import NotFound

        encoded = get_db().execute(
            "SELECT value FROM settings WHERE key=?",
            (setting_key,),
        ).fetchone()
        logo = decode_logo(encoded["value"] if encoded else "")
        if logo is None:
            raise NotFound()
        return Response(logo, mimetype="image/png")

    @app.errorhandler(500)
    def server_error(_error):
        app.logger.exception("Unexpected application error")
        return render_template("error.html", code=500, message="Errore interno. Riprova più tardi."), 500

    @app.cli.command("init-db")
    def init_db_command():
        from .db import init_db
        init_db()
        print("Database migrations applied.")

    @app.cli.command("create-admin")
    def create_admin_command():
        from getpass import getpass
        from werkzeug.security import generate_password_hash

        username = input("Admin username: ").strip()
        password = getpass("Admin password (min 14 characters): ")
        if len(password) < 14:
            raise SystemExit("Password must be at least 14 characters.")
        db = get_db()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO users(username, password_hash, role) VALUES (?, ?, 'admin')",
                (username, generate_password_hash(password, method="scrypt")),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        print("Administrator account created.")

    @app.cli.command("backup-db")
    def backup_db_command():
        from .backups import create_database_backup

        destination = create_database_backup(app.config["DATABASE"])
        print(f"Backup created: {destination}")

    @app.cli.command("restore-db")
    @click.argument("backup_name")
    def restore_db_command(backup_name):
        from .backups import (
            backup_directory,
            create_database_backup,
            restore_database_backup,
            validate_database_backup,
        )

        backup_dir = backup_directory(app.config["DATABASE"])
        source_path = (backup_dir / backup_name).resolve()
        if source_path.parent != backup_dir or not source_path.is_file():
            raise SystemExit("Choose an existing backup file located directly in /data/backups.")
        validate_database_backup(source_path)
        safety_backup = create_database_backup(app.config["DATABASE"])
        restore_database_backup(app.config["DATABASE"], source_path)
        print(f"Database restored. Previous database safety backup: {safety_backup}")

    return app
