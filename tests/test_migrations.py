import sqlite3

from app import create_app
from app.db import MIGRATIONS, get_db, init_db


def test_upgrade_preserves_legacy_company_contacts(tmp_path):
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    for version, migration in enumerate(MIGRATIONS[:2], start=1):
        connection.executescript(
            f"BEGIN IMMEDIATE;\n{migration}\nPRAGMA user_version={version};\nCOMMIT;"
        )
    connection.execute(
        """INSERT INTO clients(kind,name,surname,email,phone)
           VALUES('company','Studio Legacy','Studio Legacy','legacy@example.test','123456')"""
    )
    connection.commit()
    connection.close()

    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "migration-test-secret",
        "SMTP_FERNET_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        "SESSION_COOKIE_SECURE": False,
        "DATABASE": str(database),
        "INSTANCE_PATH": str(tmp_path / "instance"),
    })
    with app.app_context():
        init_db()
        client = get_db().execute(
            "SELECT name,surname FROM clients WHERE kind='company'"
        ).fetchone()
        contact = get_db().execute(
            "SELECT name,role,email,phone,first_name,last_name,mobile_phone FROM client_contacts"
        ).fetchone()
        assert client["name"] == "Studio Legacy"
        assert client["surname"] == ""
        assert contact["name"] == "Contatto esistente"
        assert contact["role"] == "Contatto storico"
        assert contact["email"] == "legacy@example.test"
        assert contact["phone"] == "123456"
        assert contact["first_name"] == "Contatto"
        assert contact["last_name"] == "esistente"
        assert contact["mobile_phone"] == ""
        columns = {row["name"] for row in get_db().execute("PRAGMA table_info(quotes)")}
        assert "provider_snapshot" in columns
