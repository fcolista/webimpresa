from __future__ import annotations

from email.message import EmailMessage
import json
import re
import smtplib
import ssl
from email.headerregistry import Address

from cryptography.fernet import Fernet
from flask import current_app

from .db import get_db, transaction

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _fernet():
    key = current_app.config["SMTP_FERNET_KEY"]
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise RuntimeError("SMTP_FERNET_KEY must be a valid Fernet key.") from None


def smtp_configuration():
    db = get_db()
    row = db.execute("SELECT key,value FROM settings WHERE key IN ('smtp_config','smtp_secret')").fetchall()
    values = {item["key"]: item["value"] for item in row}
    return json.loads(values.get("smtp_config", "{}")), values.get("smtp_secret", "")


def _send(recipient: str, subject: str, body: str, attachment: bytes | None = None, filename: str = "document.pdf"):
    config, encrypted_password = smtp_configuration()
    required = ("host", "port", "security", "sender_email", "sender_name")
    if not all(config.get(key) for key in required):
        raise ValueError("SMTP configuration incomplete")
    if not EMAIL_RE.fullmatch(recipient) or not EMAIL_RE.fullmatch(config["sender_email"]):
        raise ValueError("Invalid email address")
    if any(char in config["sender_name"] for char in "\r\n"):
        raise ValueError("Invalid sender name")
    security = config["security"]
    if security not in {"starttls", "ssl", "none"}:
        raise ValueError("Invalid SMTP security mode")
    if security == "none" and not current_app.config["SMTP_ALLOW_PLAINTEXT"]:
        raise ValueError("Unencrypted SMTP is disabled")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = Address(display_name=config["sender_name"], addr_spec=config["sender_email"])
    msg["To"] = recipient
    if config.get("reply_to"):
        if not EMAIL_RE.fullmatch(config["reply_to"]):
            raise ValueError("Invalid reply-to email")
        msg["Reply-To"] = config["reply_to"]
    msg.set_content(body)
    if attachment is not None:
        msg.add_attachment(attachment, maintype="application", subtype="pdf", filename=filename)
    context = ssl.create_default_context()
    port = int(config["port"])
    if security == "ssl":
        smtp = smtplib.SMTP_SSL(config["host"], port, timeout=15, context=context)
    else:
        smtp = smtplib.SMTP(config["host"], port, timeout=15)
    with smtp:
        if security == "starttls":
            smtp.starttls(context=context)
        if config.get("username"):
            if not encrypted_password:
                raise ValueError("SMTP credentials are incomplete")
            password = _fernet().decrypt(encrypted_password.encode()).decode()
            smtp.login(config["username"], password)
        smtp.send_message(msg)


def send_document(kind, document_id, recipient, subject, body, attachment, filename, user_id):
    try:
        _send(recipient, subject, body, attachment, filename + ".pdf")
    except Exception as exc:
        log_email(user_id, kind, document_id, recipient, "failed", type(exc).__name__)
        raise
    log_email(user_id, kind, document_id, recipient, "sent", "")


def send_test_email(recipient, user_id):
    try:
        _send(recipient, "Test configurazione WebImpresa", "Email di prova della configurazione SMTP.")
    except Exception as exc:
        log_email(user_id, "test", None, recipient, "failed", type(exc).__name__)
        raise
    log_email(user_id, "test", None, recipient, "sent", "")


def log_email(user_id, kind, document_id, recipient, outcome, error_code):
    with transaction() as db:
        db.execute(
            "INSERT INTO email_logs(user_id,document_kind,document_id,recipient,outcome,error_code) VALUES(?,?,?,?,?,?)",
            (user_id, kind, document_id, recipient, outcome, error_code[:80]),
        )
