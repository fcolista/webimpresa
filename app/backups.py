from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3

from .db import MIGRATIONS


REQUIRED_TABLES = {
    "admin_logs",
    "client_contacts",
    "clients",
    "counters",
    "email_logs",
    "quote_acceptances",
    "quote_lines",
    "quotes",
    "receipt_lines",
    "receipts",
    "settings",
    "users",
}


def backup_directory(database_path):
    return Path(database_path).resolve().parent / "backups"


def create_database_backup(database_path):
    directory = backup_directory(database_path)
    directory.mkdir(parents=True, exist_ok=True)
    prefix = datetime.now().strftime("webimpresa-%Y%m%d-%H%M%S")
    destination = directory / f"{prefix}.db"
    suffix = 1
    while destination.exists():
        suffix += 1
        destination = directory / f"{prefix}-{suffix}.db"

    source = sqlite3.connect(database_path, timeout=30)
    target = None
    failed = False
    try:
        target = sqlite3.connect(destination, timeout=30)
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"Backup integrity check failed: {integrity}")
    except sqlite3.Error:
        failed = True
        raise
    finally:
        if target is not None:
            target.close()
        source.close()
        if failed:
            destination.unlink(missing_ok=True)
    return destination


def validate_database_backup(path):
    source = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"Il controllo d'integrità SQLite non è riuscito: {integrity}")

        version = source.execute("PRAGMA user_version").fetchone()[0]
        if version != len(MIGRATIONS):
            raise ValueError(
                f"Versione dello schema non compatibile ({version}); "
                f"questa installazione richiede la versione {len(MIGRATIONS)}."
            )

        tables = {
            row[0]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = REQUIRED_TABLES - tables
        if missing:
            raise ValueError("Il file non contiene tutte le tabelle di WebImpresa.")
        if source.execute(
            "SELECT 1 FROM users WHERE role='admin' AND active=1 LIMIT 1"
        ).fetchone() is None:
            raise ValueError("Il backup non contiene un amministratore attivo.")
    except sqlite3.DatabaseError as exc:
        raise ValueError("Il file selezionato non è un backup SQLite valido.") from exc
    finally:
        source.close()


def restore_database_backup(database_path, backup_path):
    source = sqlite3.connect(
        f"{Path(backup_path).resolve().as_uri()}?mode=ro", uri=True, timeout=30
    )
    target = None
    try:
        target = sqlite3.connect(database_path, timeout=30)
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"Restore integrity check failed: {integrity}")
    finally:
        if target is not None:
            target.close()
        source.close()
