from __future__ import annotations

import sys

import pytest
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

sys.path.insert(0, "/app")

from app import create_app
from app.db import init_db


@pytest.fixture()
def app(tmp_path):
    application = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret-key",
        "SMTP_FERNET_KEY": Fernet.generate_key().decode(),
        "SESSION_COOKIE_SECURE": False,
        "DATABASE": str(tmp_path / "webimpresa.db"),
        "INSTANCE_PATH": str(tmp_path / "instance"),
    })
    with application.app_context():
        init_db()
        from app.db import get_db
        connection = get_db()
        connection.execute(
            "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
            ("operatore", generate_password_hash("password-operatore-test-lunga", method="scrypt"), "user"),
        )
        connection.execute(
            "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
            ("amministratore", generate_password_hash("password-admin-test-molto-lunga", method="scrypt"), "admin"),
        )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf(client):
    client.get("/login")
    with client.session_transaction() as session:
        return session["_csrf"]


def login(client, username="operatore", password="password-operatore-test-lunga"):
    return client.post("/login", data={
        "_csrf": csrf(client),
        "username": username,
        "password": password,
    }, follow_redirects=False)


@pytest.fixture()
def user_client(client):
    response = login(client)
    assert response.status_code == 302
    return client
