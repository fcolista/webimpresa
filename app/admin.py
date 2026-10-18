from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file, url_for
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

from .branding import clean_logo
from .backups import backup_directory, create_database_backup, restore_database_backup, validate_database_backup
from .db import get_db, transaction
from .mailer import _fernet, send_test_email, smtp_configuration
from .security import login_required, require_csrf
from .ui import contrast_ratio, get_settings, valid_color

bp = Blueprint("admin", __name__, url_prefix="/admin")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@bp.get("/")
@login_required("admin")
def index():
    db = get_db()
    counts = {
        "users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
    }
    logs = db.execute("SELECT action,target_id,created_at FROM admin_logs ORDER BY id DESC LIMIT 10").fetchall()
    directory = backup_directory(current_app.config["DATABASE"])
    backups = sorted(
        (
            {"name": path.name, "size": path.stat().st_size}
            for path in directory.glob("webimpresa-*.db")
            if path.is_file()
        ),
        key=lambda backup: backup["name"],
        reverse=True,
    )[:10]
    return render_template("admin.html", counts=counts, settings=get_settings(), logs=logs, backups=backups)


@bp.post("/backups/create")
@login_required("admin")
def create_backup():
    require_csrf()
    try:
        destination = create_database_backup(current_app.config["DATABASE"])
    except (sqlite3.Error, OSError):
        current_app.logger.exception("Database backup failed")
        flash("Creazione del backup non riuscita. Verifica lo spazio disponibile e i log del server.", "error")
        return redirect(url_for("admin.index"))
    with transaction() as db:
        _audit(db, "database_backup_created", None)
    return send_file(destination, as_attachment=True, download_name=destination.name)


@bp.get("/backups/<path:backup_name>")
@login_required("admin")
def download_backup(backup_name):
    path = _stored_backup_path(backup_name)
    return send_file(path, as_attachment=True, download_name=path.name)


@bp.post("/backups/<path:backup_name>/delete")
@login_required("admin")
def delete_backup(backup_name):
    require_csrf()
    path = _stored_backup_path(backup_name)
    try:
        path.unlink()
    except OSError:
        current_app.logger.exception("Database backup deletion failed: %s", path.name)
        flash("Eliminazione del backup non riuscita. Verifica i permessi e i log del server.", "error")
        return redirect(url_for("admin.index"))
    with transaction() as db:
        _audit(db, "database_backup_deleted", backup_name)
    flash(f"Backup {backup_name} eliminato.", "success")
    return redirect(url_for("admin.index"))


@bp.post("/backups/restore")
@login_required("admin")
def restore_backup():
    request.max_content_length = 64 * 1024 * 1024
    require_csrf()
    if request.form.get("confirm_restore") != "1":
        flash("Conferma la sovrascrittura del database prima di continuare.", "error")
        return redirect(url_for("admin.index"))
    upload = request.files.get("backup_file")
    filename = secure_filename(upload.filename or "") if upload else ""
    if not upload or not filename or Path(filename).suffix.lower() != ".db":
        flash("Seleziona un file di backup WebImpresa con estensione .db.", "error")
        return redirect(url_for("admin.index"))

    directory = backup_directory(current_app.config["DATABASE"])
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".restore-", suffix=".db", dir=directory, delete=False) as staged:
        staged_path = Path(staged.name)
    try:
        upload.save(staged_path)
        try:
            validate_database_backup(staged_path)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin.index"))

        source = sqlite3.connect(f"{staged_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            operator_exists = source.execute(
                "SELECT 1 FROM users WHERE id=? AND role='admin' AND active=1",
                (g.user["id"],),
            ).fetchone()
        finally:
            source.close()
        if not operator_exists:
            flash("Il backup non contiene l'account amministrativo attivo che sta eseguendo il ripristino.", "error")
            return redirect(url_for("admin.index"))

        try:
            safety_backup = create_database_backup(current_app.config["DATABASE"])
            connection = g.pop("db", None)
            if connection is not None:
                connection.close()
            restore_database_backup(current_app.config["DATABASE"], staged_path)
        except (sqlite3.Error, OSError):
            current_app.logger.exception("Database restore failed")
            flash(
                "Ripristino non riuscito. Il database precedente è stato conservato nel backup automatico "
                "creato prima dell'operazione.",
                "error",
            )
            return redirect(url_for("admin.index"))

        with transaction() as db:
            _audit(db, "database_restored", None)
        flash(
            f"Ripristino completato. Backup di sicurezza precedente: {safety_backup.name}. "
            "Le sessioni degli altri utenti potrebbero dover accedere di nuovo.",
            "success",
        )
        return redirect(url_for("admin.index"))
    finally:
        staged_path.unlink(missing_ok=True)


@bp.get("/users")
@login_required("admin")
def users():
    return render_template("admin_users.html", users=get_db().execute("SELECT id,username,role,active,created_at FROM users ORDER BY id").fetchall())


@bp.post("/users")
@login_required("admin")
def create_user():
    require_csrf()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", username):
        flash("Username: usa 3-80 caratteri alfanumerici, punto, trattino o underscore.", "error")
    elif len(password) < 14 or len(password) > 256:
        flash("La password deve avere almeno 14 caratteri (massimo 256).", "error")
    elif role not in {"admin", "user"}:
        flash("Ruolo non valido.", "error")
    else:
        try:
            with transaction() as db:
                cur = db.execute(
                    "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                    (username, generate_password_hash(password, method="scrypt"), role),
                )
                _audit(db, "user_created", cur.lastrowid)
            flash("Utente creato.", "success")
        except sqlite3.IntegrityError:
            flash("Username già in uso.", "error")
    return redirect(url_for("admin.users"))


@bp.post("/users/<int:user_id>")
@login_required("admin")
def update_user(user_id):
    require_csrf()
    role = request.form.get("role", "")
    active = request.form.get("active", "")
    password = request.form.get("password", "")
    if role not in {"admin", "user"} or active not in {"0", "1"}:
        abort(400)
    if password and (len(password) < 14 or len(password) > 256):
        flash("La nuova password deve avere almeno 14 caratteri (massimo 256).", "error")
        return redirect(url_for("admin.users"))
    if user_id == g.user["id"] and (role != "admin" or active != "1"):
        flash("Non puoi disabilitare o declassare il tuo account amministrativo attivo.", "error")
    else:
        with transaction() as db:
            existing = db.execute("SELECT role,active FROM users WHERE id=?", (user_id,)).fetchone()
            if not existing:
                abort(404)
            if existing["role"] == "admin" and existing["active"] and (role != "admin" or active != "1"):
                admins = db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
                if admins <= 1:
                    flash("Deve rimanere almeno un amministratore attivo.", "error")
                    return redirect(url_for("admin.users"))
            if password:
                cur = db.execute(
                    "UPDATE users SET role=?,active=?,password_hash=? WHERE id=?",
                    (role, int(active), generate_password_hash(password, method="scrypt"), user_id),
                )
            else:
                cur = db.execute("UPDATE users SET role=?,active=? WHERE id=?", (role, int(active), user_id))
            if cur.rowcount != 1:
                abort(404)
            _audit(db, "user_updated", user_id)
        flash("Account aggiornato.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/settings", methods=["GET", "POST"])
@login_required("admin")
def settings():
    if request.method == "POST":
        require_csrf()
        try:
            values = {
                "app_name": _field("app_name", 80, required=True),
            "theme_primary": request.form.get("theme_primary", ""),
            "theme_accent": request.form.get("theme_accent", ""),
            "theme_background": request.form.get("theme_background", ""),
            "provider_name": _field("provider_name", 180),
            "provider_tax_code": _field("provider_tax_code", 32),
            "provider_vat": _field("provider_vat", 32),
            "provider_address": _field("provider_address", 300),
                "provider_postal_code": _field("provider_postal_code", 20),
                "provider_city": _field("provider_city", 120),
                "provider_province": _field("provider_province", 80),
                "provider_country": _field("provider_country", 100),
                "provider_email": _field("provider_email", 254),
                "provider_phone": _field("provider_phone", 100),
                "provider_pec": _field("provider_pec", 254),
                "provider_recipient_code": _field("provider_recipient_code", 20),
                "provider_bank_name": _field("provider_bank_name", 180),
                "provider_account_holder": _field("provider_account_holder", 180),
                "provider_iban": _field("provider_iban", 34),
                "provider_bic": _field("provider_bic", 11),
                }
            smtp_config = {
                "host": _field("smtp_host", 253),
                "port": _field("smtp_port", 5),
                "security": request.form.get("smtp_security", ""),
                "username": _field("smtp_username", 254),
                "sender_email": _field("smtp_sender_email", 254),
                "sender_name": _field("smtp_sender_name", 180),
                "reply_to": _field("smtp_reply_to", 254),
            }
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin.settings"))
        password = request.form.get("smtp_password", "")
        clear_password = request.form.get("clear_smtp_password") == "1"
        image_uploads = {}
        image_errors = []
        for field, form_field, label in (
            ("provider_logo", "logo_file", "logo"),
            ("provider_signature", "signature_file", "firma"),
        ):
            upload = request.files.get(form_field)
            image_uploads[field] = None
            if upload and upload.filename:
                try:
                    if secure_filename(upload.filename) == "":
                        raise ValueError(f"Nome del file {label} non valido.")
                    image_uploads[field] = clean_logo(upload)
                except ValueError as exc:
                    image_errors.append(str(exc))
        remove_images = {
            "provider_logo": request.form.get("remove_logo") == "1",
            "provider_signature": request.form.get("remove_signature") == "1",
        }
        errors = [key for key in ("theme_primary", "theme_accent", "theme_background") if not valid_color(values[key])]
        if all(valid_color(values[key]) for key in ("theme_primary", "theme_accent", "theme_background")):
            if contrast_ratio(values["theme_primary"], "#ffffff") < 4.5:
                errors.append("il colore principale deve contrastare almeno 4.5:1 con il testo bianco")
            if contrast_ratio(values["theme_accent"], "#ffffff") < 4.5:
                errors.append("il colore accento deve contrastare almeno 4.5:1 con il testo bianco")
            if contrast_ratio("#182230", values["theme_background"]) < 4.5:
                errors.append("lo sfondo deve contrastare almeno 4.5:1 con il testo")
        if values["provider_email"] and not EMAIL_RE.fullmatch(values["provider_email"]):
            errors.append("provider_email")
        if values["provider_pec"] and not EMAIL_RE.fullmatch(values["provider_pec"]):
            errors.append("provider_pec")
        for field, label in (("provider_logo", "logo"), ("provider_signature", "firma")):
            if remove_images[field] and image_uploads[field] is not None:
                errors.append(f"Scegli se caricare o rimuovere {label}, non entrambe le operazioni.")
        errors.extend(image_errors)
        if smtp_config["host"] and (not re.fullmatch(r"[A-Za-z0-9.-]+", smtp_config["host"]) or ".." in smtp_config["host"]):
            errors.append("smtp_host")
        if smtp_config["port"] and (not smtp_config["port"].isdigit() or not 1 <= int(smtp_config["port"]) <= 65535):
            errors.append("smtp_port")
        if smtp_config["security"] not in {"starttls", "ssl", "none"}:
            errors.append("smtp_security")
        if smtp_config["security"] == "none" and not current_app.config.get("SMTP_ALLOW_PLAINTEXT"):
            errors.append("SMTP non cifrato disabilitato sul server.")
        for key in ("sender_email", "reply_to"):
            if smtp_config[key] and not EMAIL_RE.fullmatch(smtp_config[key]):
                errors.append(key)
        if errors:
            flash("Controlla i campi non validi: " + ", ".join(errors), "error")
        else:
            with transaction() as db:
                db.executemany("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", values.items())
                for field in image_uploads:
                    if image_uploads[field] is not None or remove_images[field]:
                        db.execute(
                            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (field, image_uploads[field] or ""),
                        )
                _, old_password = smtp_configuration()
                if password:
                    encrypted_password = _fernet().encrypt(password.encode()).decode()
                elif clear_password:
                    encrypted_password = ""
                else:
                    encrypted_password = old_password
                db.execute("INSERT INTO settings(key,value) VALUES('smtp_config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(smtp_config),))
                db.execute("INSERT INTO settings(key,value) VALUES('smtp_secret',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (encrypted_password,))
                _audit(db, "settings_updated", None)
            flash("Impostazioni salvate. La password SMTP non viene mai mostrata.", "success")
            return redirect(url_for("admin.settings"))
    smtp_config, encrypted_password = smtp_configuration()
    has_smtp_password = bool(encrypted_password)
    return render_template("admin_settings.html", settings=get_settings(), smtp=smtp_config, has_smtp_password=has_smtp_password)


@bp.post("/smtp-test")
@login_required("admin")
def test_smtp():
    require_csrf()
    recipient = request.form.get("recipient", "").strip()
    if not EMAIL_RE.fullmatch(recipient):
        flash("Inserisci un destinatario email valido.", "error")
    else:
        try:
            send_test_email(recipient, g.user["id"])
            flash("Email di prova inviata.", "success")
        except Exception as exc:
            current_app.logger.exception("SMTP test failed (%s)", type(exc).__name__)
            flash(f"Test SMTP non riuscito ({type(exc).__name__}). Verifica host, credenziali, rete e certificato.", "error")
    return redirect(url_for("admin.settings"))


@bp.get("/health")
@login_required("admin")
def health():
    db = get_db()
    return {
        "database": "ok",
        "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
        "sqlite": db.execute("SELECT sqlite_version()").fetchone()[0],
    }


def _stored_backup_path(backup_name):
    directory = backup_directory(current_app.config["DATABASE"])
    path = (directory / backup_name).resolve()
    if (
        path.parent != directory or path.name != backup_name
        or not re.fullmatch(r"webimpresa-[A-Za-z0-9][A-Za-z0-9_-]*\.db", backup_name)
        or not path.is_file()
    ):
        abort(404)
    return path


def _field(name, maximum, *, required=False):
    value = request.form.get(name, "").strip()
    if len(value) > maximum or (required and not value) or "\r" in value or "\n" in value:
        raise ValueError(f"Campo {name} non valido.")
    return value


def _audit(db, action, target_id):
    db.execute("INSERT INTO admin_logs(admin_id,action,target_id) VALUES(?,?,?)", (g.user["id"], action, target_id))
