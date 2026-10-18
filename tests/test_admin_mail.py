from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock, patch

from PIL import Image

from conftest import csrf, login


def test_admin_can_create_only_the_two_supported_roles(client):
    login(client, "amministratore", "password-admin-test-molto-lunga")
    response = client.post("/admin/users", data={
        "_csrf": csrf(client), "username": "nuovo", "password": "una-password-sufficientemente-lunga",
        "role": "user",
    })
    assert response.status_code == 302
    invalid = client.post("/admin/users", data={
        "_csrf": csrf(client), "username": "ruolo", "password": "una-password-sufficientemente-lunga",
        "role": "superuser",
    }, follow_redirects=True)
    assert "Ruolo non valido" in invalid.get_data(as_text=True)


def test_smtp_secret_is_encrypted_and_test_send_does_not_log_credentials(client, app):
    login(client, "amministratore", "password-admin-test-molto-lunga")
    response = client.post("/admin/settings", data={
        "_csrf": csrf(client),
        "app_name": "WebImpresa",
        "theme_primary": "#174b71", "theme_accent": "#2563eb", "theme_background": "#f4f6fa",
        "provider_name": "Professionista", "provider_tax_code": "", "provider_vat": "",
        "provider_address": "", "provider_email": "", "provider_pec": "", "provider_recipient_code": "",
        "smtp_host": "smtp.example.test", "smtp_port": "587", "smtp_security": "starttls",
        "smtp_username": "mailer@example.test", "smtp_password": "secret-smtp-password",
        "smtp_sender_email": "mailer@example.test", "smtp_sender_name": "WebImpresa", "smtp_reply_to": "",
    })
    assert response.status_code == 302
    from app.db import get_db
    with app.app_context():
        secret = get_db().execute("SELECT value FROM settings WHERE key='smtp_secret'").fetchone()[0]
        assert secret != "secret-smtp-password" and secret
    settings_page = client.get("/admin/settings")
    assert b"secret-smtp-password" not in settings_page.data
    assert b"Salvata (vuoto = invariata)" in settings_page.data

    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    with patch("app.mailer.smtplib.SMTP", return_value=smtp):
        test_response = client.post("/admin/smtp-test", data={
            "_csrf": csrf(client), "recipient": "admin@example.test",
        })
    assert test_response.status_code == 302
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("mailer@example.test", "secret-smtp-password")
    with app.app_context():
        log = get_db().execute("SELECT outcome,error_code FROM email_logs ORDER BY id DESC LIMIT 1").fetchone()
        assert log["outcome"] == "sent"


def test_admin_color_settings_reject_low_contrast(client):
    login(client, "amministratore", "password-admin-test-molto-lunga")
    response = client.post("/admin/settings", data={
        "_csrf": csrf(client), "app_name": "WebImpresa",
        "theme_primary": "#ffffff", "theme_accent": "#000000", "theme_background": "#ffffff",
        "provider_name": "", "provider_tax_code": "", "provider_vat": "", "provider_address": "",
        "provider_email": "", "provider_pec": "", "provider_recipient_code": "",
        "smtp_host": "", "smtp_port": "", "smtp_security": "starttls", "smtp_username": "",
        "smtp_password": "", "smtp_sender_email": "", "smtp_sender_name": "", "smtp_reply_to": "",
    }, follow_redirects=True)
    assert "colore principale deve contrastare almeno 4.5:1" in response.get_data(as_text=True), (
        response.status_code, response.get_data(as_text=True)
    )


def test_admin_can_upload_and_remove_a_sanitized_logo(client):
    assert client.get("/brand/signature").status_code == 302
    login(client, "amministratore", "password-admin-test-molto-lunga")
    image = BytesIO()
    Image.new("RGBA", (32, 16), (23, 75, 113, 255)).save(image, format="PNG")
    image.seek(0)
    signature = BytesIO()
    Image.new("RGBA", (80, 32), (23, 75, 113, 180)).save(signature, format="PNG")
    signature.seek(0)
    response = client.post("/admin/settings", data={
        "_csrf": csrf(client), "app_name": "WebImpresa",
        "theme_primary": "#174b71", "theme_accent": "#2563eb", "theme_background": "#f4f6fa",
        "provider_name": "Studio Prova", "provider_pec": "studio@pec.example.test",
        "provider_city": "Roma", "provider_iban": "IT00TESTBANK0000000000000000",
        "provider_account_holder": "Studio Prova", "smtp_security": "starttls",
        "logo_file": (image, "brand.png"),
        "signature_file": (signature, "signature.png"),
    })
    assert response.status_code == 302
    logo = client.get("/brand/logo")
    assert logo.status_code == 200
    assert logo.mimetype == "image/png"
    assert logo.data.startswith(b"\x89PNG\r\n\x1a\n")
    signature_response = client.get("/brand/signature")
    assert signature_response.status_code == 200
    assert signature_response.data.startswith(b"\x89PNG\r\n\x1a\n")
    with client.application.app_context():
        from app.db import get_db
        saved = get_db().execute(
            "SELECT key,value FROM settings WHERE key IN ('provider_name','provider_pec','provider_city','provider_iban')"
        ).fetchall()
        assert {row["key"]: row["value"] for row in saved} == {
            "provider_name": "Studio Prova",
            "provider_pec": "studio@pec.example.test",
            "provider_city": "Roma",
            "provider_iban": "IT00TESTBANK0000000000000000",
        }
    assert b"brand.png" not in client.get("/admin/settings").data

    removed = client.post("/admin/settings", data={
        "_csrf": csrf(client), "app_name": "WebImpresa",
        "theme_primary": "#174b71", "theme_accent": "#2563eb", "theme_background": "#f4f6fa",
        "smtp_security": "starttls", "remove_logo": "1",
    })
    assert removed.status_code == 302
    assert client.get("/brand/logo").status_code == 404
    assert client.get("/brand/signature").status_code == 200
