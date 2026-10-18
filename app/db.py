from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

from flask import current_app, g


MIGRATIONS = [
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL COLLATE NOCASE UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE clients (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('person', 'company')),
        name TEXT NOT NULL,
        surname TEXT NOT NULL DEFAULT '',
        tax_code TEXT,
        vat_number TEXT,
        address TEXT NOT NULL DEFAULT '',
        postal_code TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '',
        province TEXT NOT NULL DEFAULT '',
        country TEXT NOT NULL DEFAULT 'Italia',
        email TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '',
        pec TEXT NOT NULL DEFAULT '',
        recipient_code TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE UNIQUE INDEX clients_tax_code_unique ON clients(lower(tax_code)) WHERE tax_code IS NOT NULL AND tax_code != '';
    CREATE UNIQUE INDEX clients_vat_number_unique ON clients(vat_number) WHERE vat_number IS NOT NULL AND vat_number != '';
    CREATE INDEX clients_name_idx ON clients(name, surname);
    CREATE TABLE counters (
        kind TEXT NOT NULL CHECK(kind IN ('quote', 'receipt')),
        year INTEGER NOT NULL,
        value INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(kind, year)
    );
    CREATE TABLE quotes (
        id INTEGER PRIMARY KEY,
        number TEXT NOT NULL UNIQUE,
        year INTEGER NOT NULL,
        client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
        client_snapshot TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('draft', 'sent', 'accepted', 'rejected', 'expired')),
        issue_date TEXT NOT NULL,
        valid_until TEXT NOT NULL DEFAULT '',
        payment_terms TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        commercial_terms TEXT NOT NULL DEFAULT '',
        tax_config TEXT NOT NULL,
        totals TEXT NOT NULL,
        source_quote_id INTEGER REFERENCES quotes(id) ON DELETE SET NULL,
        created_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX quotes_client_idx ON quotes(client_id, issue_date DESC);
    CREATE INDEX quotes_status_idx ON quotes(status, issue_date DESC);
    CREATE TABLE quote_lines (
        id INTEGER PRIMARY KEY,
        quote_id INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        description TEXT NOT NULL,
        quantity TEXT NOT NULL,
        unit_price_cents INTEGER NOT NULL,
        discount_bp INTEGER NOT NULL DEFAULT 0,
        vat_bp INTEGER NOT NULL DEFAULT 0,
        withholding INTEGER NOT NULL DEFAULT 0 CHECK(withholding IN (0, 1))
    );
    CREATE TABLE receipts (
        id INTEGER PRIMARY KEY,
        number TEXT NOT NULL UNIQUE,
        year INTEGER NOT NULL,
        client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
        client_snapshot TEXT NOT NULL,
        provider_snapshot TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('draft', 'issued')),
        issue_date TEXT NOT NULL,
        subject TEXT NOT NULL,
        tax_config TEXT NOT NULL,
        totals TEXT NOT NULL,
        quote_id INTEGER REFERENCES quotes(id) ON DELETE SET NULL,
        created_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX receipts_client_idx ON receipts(client_id, issue_date DESC);
    CREATE TABLE receipt_lines (
        id INTEGER PRIMARY KEY,
        receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        description TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('service', 'expense'))
    );
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE email_logs (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        document_kind TEXT NOT NULL CHECK(document_kind IN ('quote', 'receipt', 'test')),
        document_id INTEGER,
        recipient TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK(outcome IN ('sent', 'failed')),
        error_code TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE admin_logs (
        id INTEGER PRIMARY KEY,
        admin_id INTEGER NOT NULL REFERENCES users(id),
        action TEXT NOT NULL,
        target_id INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE login_attempts (
        id INTEGER PRIMARY KEY,
        ip_hash TEXT NOT NULL,
        attempted_at INTEGER NOT NULL,
        successful INTEGER NOT NULL CHECK(successful IN (0, 1))
    );
    CREATE INDEX login_attempts_window_idx ON login_attempts(ip_hash, attempted_at);
    """,
    """
    CREATE TABLE client_contacts (
        id INTEGER PRIMARY KEY,
        client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        email TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '',
        position INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX client_contacts_client_idx ON client_contacts(client_id, position, id);
    INSERT INTO client_contacts(client_id,name,role,email,phone,position)
        SELECT id,'Contatto esistente','Contatto storico',email,phone,0
        FROM clients WHERE kind='company' AND (email!='' OR phone!='');
    UPDATE clients SET surname='' WHERE kind='company';

    ALTER TABLE quotes ADD COLUMN provider_snapshot TEXT NOT NULL DEFAULT '{}';
    CREATE TABLE quote_acceptances (
        quote_id INTEGER PRIMARY KEY REFERENCES quotes(id) ON DELETE CASCADE,
        original_filename TEXT NOT NULL,
        pdf_data BLOB NOT NULL,
        uploaded_by INTEGER NOT NULL REFERENCES users(id),
        uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    ALTER TABLE client_contacts ADD COLUMN first_name TEXT NOT NULL DEFAULT '';
    ALTER TABLE client_contacts ADD COLUMN last_name TEXT NOT NULL DEFAULT '';
    ALTER TABLE client_contacts ADD COLUMN mobile_phone TEXT NOT NULL DEFAULT '';
    UPDATE client_contacts
       SET first_name=CASE
               WHEN instr(trim(name),' ')>0 THEN substr(trim(name),1,instr(trim(name),' ')-1)
               ELSE trim(name)
           END,
           last_name=CASE
               WHEN instr(trim(name),' ')>0 THEN trim(substr(trim(name),instr(trim(name),' ')+1))
               ELSE ''
           END;
    """,
    """
    DROP INDEX clients_tax_code_unique;
    DROP INDEX clients_vat_number_unique;
    CREATE UNIQUE INDEX clients_tax_code_unique
        ON clients(lower(tax_code))
        WHERE archived=0 AND tax_code IS NOT NULL AND tax_code!='';
    CREATE UNIQUE INDEX clients_vat_number_unique
        ON clients(vat_number)
        WHERE archived=0 AND vat_number IS NOT NULL AND vat_number!='';
    """,
]


def connect_db():
    db = sqlite3.connect(current_app.config["DATABASE"], timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 15000")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def get_db():
    if "db" not in g:
        g.db = connect_db()
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def transaction(db=None):
    connection = db or get_db()
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def init_db():
    db_path = current_app.config["DATABASE"]
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    db = connect_db()
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        for index, migration in enumerate(MIGRATIONS[version:], start=version + 1):
            db.executescript("BEGIN IMMEDIATE;\n" + migration + f"\nPRAGMA user_version = {index};\nCOMMIT;")
        defaults = {
            "app_name": "WebImpresa",
            "theme_primary": "#174b71",
            "theme_accent": "#2563eb",
            "theme_background": "#f4f6fa",
            "provider_name": "",
            "provider_tax_code": "",
            "provider_vat": "",
            "provider_address": "",
            "provider_postal_code": "",
            "provider_city": "",
            "provider_province": "",
            "provider_country": "Italia",
            "provider_email": "",
            "provider_phone": "",
            "provider_pec": "",
            "provider_recipient_code": "",
            "provider_bank_name": "",
            "provider_account_holder": "",
            "provider_iban": "",
            "provider_bic": "",
            "provider_logo": "",
            "provider_signature": "",
            "smtp_config": "{}",
            "smtp_secret": "",
            "smtp_salt": "",
        }
        db.executemany("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", defaults.items())
    finally:
        db.close()
