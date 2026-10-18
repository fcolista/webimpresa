from io import BytesIO
from pathlib import Path

from app.db import get_db
from conftest import csrf, login


def test_online_backup_and_integrity_checked_restore(app):
    runner = app.test_cli_runner()
    backup = runner.invoke(args=["backup-db"])
    assert backup.exit_code == 0, backup.output
    backup_file = backup.output.strip().split()[-1]

    with app.app_context():
        db = get_db()
        db.execute("DELETE FROM users WHERE username='operatore'")
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1

    restored = runner.invoke(args=["restore-db", backup_file.rsplit("/", 1)[-1]])
    assert restored.exit_code == 0, restored.output
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_admin_can_download_and_restore_backup_with_safety_copy(app, client):
    response = login(client, "amministratore", "password-admin-test-molto-lunga")
    assert response.status_code == 302
    assert client.get("/admin/").status_code == 200

    runner = app.test_cli_runner()
    result = runner.invoke(args=["backup-db"])
    assert result.exit_code == 0, result.output
    backup_path = result.output.strip().split()[-1]
    backup_name = backup_path.rsplit("/", 1)[-1]

    assert client.get(f"/admin/backups/{backup_name}").status_code == 200
    with app.app_context():
        get_db().execute("DELETE FROM users WHERE username='operatore'")

    restored = client.post("/admin/backups/restore", data={
        "_csrf": csrf(client),
        "confirm_restore": "1",
        "backup_file": (BytesIO(Path(backup_path).read_bytes()), backup_name),
    }, content_type="multipart/form-data")
    assert restored.status_code == 302

    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
        assert get_db().execute(
            "SELECT COUNT(*) FROM admin_logs WHERE action='database_restored'"
        ).fetchone()[0] == 1

    page = client.get("/admin/")
    assert page.status_code == 200
    assert "Ripristino completato" in page.get_data(as_text=True)


def test_operational_user_cannot_access_admin_backup_controls(user_client):
    assert user_client.post("/admin/backups/create", data={"_csrf": csrf(user_client)}).status_code == 403
    assert user_client.get("/admin/backups/anything.db").status_code == 403
    assert user_client.post(
        "/admin/backups/webimpresa-20261017-112420.db/delete",
        data={"_csrf": csrf(user_client)},
    ).status_code == 403


def test_admin_can_delete_saved_backup_and_panel_uses_button_actions(app, client):
    response = login(client, "amministratore", "password-admin-test-molto-lunga")
    assert response.status_code == 302
    result = app.test_cli_runner().invoke(args=["backup-db"])
    assert result.exit_code == 0, result.output
    backup_path = Path(result.output.strip().split()[-1])
    backup_name = backup_path.name

    panel = client.get("/admin/").get_data(as_text=True)
    assert 'class="button secondary compact" href="/admin/users">Gestisci →</a>' in panel
    assert 'class="button secondary compact" href="/admin/health">Stato tecnico →</a>' in panel
    assert 'class="button secondary" href="/admin/health">Stato tecnico del sistema (JSON)</a>' in panel
    assert f'class="button secondary compact" href="/admin/backups/{backup_name}">Scarica</a>' in panel
    assert f'action="/admin/backups/{backup_name}/delete"' in panel
    assert ">Elimina</button>" in panel

    deleted = client.post(
        f"/admin/backups/{backup_name}/delete",
        data={"_csrf": csrf(client)},
        follow_redirects=True,
    )
    assert deleted.status_code == 200
    assert "Backup" in deleted.get_data(as_text=True)
    assert not backup_path.exists()
    with app.app_context():
        assert get_db().execute(
            "SELECT COUNT(*) FROM admin_logs WHERE action='database_backup_deleted' AND target_id=?",
            (backup_name,),
        ).fetchone()[0] == 1


def test_backup_delete_requires_csrf_and_rejects_non_backup_names(app, client):
    assert login(client, "amministratore", "password-admin-test-molto-lunga").status_code == 302
    assert client.post("/admin/backups/webimpresa-20261017-112420.db/delete").status_code == 400
    assert client.post(
        "/admin/backups/../webimpresa-20261017-112420.db/delete",
        data={"_csrf": csrf(client)},
    ).status_code in {400, 404}


def test_admin_can_download_and_delete_layout_named_backup(app, client):
    assert login(client, "amministratore", "password-admin-test-molto-lunga").status_code == 302
    created = app.test_cli_runner().invoke(args=["backup-db"])
    assert created.exit_code == 0, created.output
    original = Path(created.output.strip().split()[-1])
    layout_backup = original.with_name("webimpresa-layout-test.db")
    original.rename(layout_backup)

    downloaded = client.get("/admin/backups/webimpresa-layout-test.db")
    assert downloaded.status_code == 200
    assert downloaded.data.startswith(b"SQLite format 3")

    deleted = client.post(
        "/admin/backups/webimpresa-layout-test.db/delete",
        data={"_csrf": csrf(client)},
        follow_redirects=True,
    )
    assert deleted.status_code == 200
    assert not layout_backup.exists()


def test_admin_accounts_page_uses_individual_aligned_edit_cards(client):
    assert login(client, "amministratore", "password-admin-test-molto-lunga").status_code == 302
    page = client.get("/admin/users")
    assert page.status_code == 200
    assert b'class="account-list"' in page.data
    assert b'class="account-card"' in page.data
    assert b'class="account-edit-form"' in page.data
    assert b'<td colspan="3">' not in page.data
