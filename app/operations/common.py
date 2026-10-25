from __future__ import annotations

from datetime import date
import re

from flask import Blueprint, abort

from ..db import get_db
from ..finance import hydrate_quote_line

bp = Blueprint("operations", __name__)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RECEIPT_NUMBER_RE = re.compile(r"^(\d{4})-(\d{3,})$")


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


RECEIPT_NUMBER_RE = re.compile(r"^(\d{4})-(\d{3,})$")


def _client_data(client_id: int):
    client = get_db().execute("SELECT * FROM clients WHERE id=? AND archived=0", (client_id,)).fetchone()
    if not client:
        abort(400, "Seleziona un cliente attivo.")
    data = {key: client[key] for key in (
        "kind", "name", "surname", "tax_code", "vat_number", "address", "postal_code",
        "city", "province", "country", "email", "phone", "pec", "recipient_code",
    )}
    contacts = get_db().execute(
        "SELECT first_name,last_name,role,email,phone,mobile_phone FROM client_contacts WHERE client_id=? ORDER BY position,id",
        (client_id,),
    ).fetchall()
    data["contacts"] = [dict(contact) for contact in contacts]
    if client["kind"] == "company" and contacts:
        data["email"] = next((contact["email"] for contact in contacts if contact["email"]), "")
        data["phone"] = next((contact["phone"] for contact in contacts if contact["phone"]), "")
    return data


def _snapshot(form):
    client_id = form.get("client_id", type=int)
    if client_id is None:
        raise ValueError("Seleziona un cliente.")
    return client_id, _client_data(client_id)


def _accepted_quote_id(value, client_id):
    if not value:
        return None
    try:
        quote_id = int(value)
    except (TypeError, ValueError):
        raise ValueError("Seleziona un preventivo accettato valido.") from None
    quote = get_db().execute(
        "SELECT status,client_id FROM quotes WHERE id=?",
        (quote_id,),
    ).fetchone()
    if not quote or quote["status"] != "accepted":
        raise ValueError("La ricevuta può essere collegata solo a un preventivo accettato.")
    if quote["client_id"] != client_id:
        raise ValueError("Il preventivo e la ricevuta devono appartenere allo stesso cliente.")
    return quote_id


def _next_number(db, kind, year):
    table = {"quote": "quotes", "receipt": "receipts"}.get(kind)
    if table is None:
        raise ValueError("Tipo documento non valido.")
    existing_numbers = db.execute(
        f"SELECT number FROM {table} WHERE year=?",
        (year,),
    ).fetchall()
    maximum = max(
        (
            int(match.group(1))
            for row in existing_numbers
            if (match := re.fullmatch(rf"{year}-(\d+)", row["number"]))
        ),
        default=0,
    )
    db.execute(
        """INSERT INTO counters(kind, year, value) VALUES (?, ?, ?)
           ON CONFLICT(kind, year) DO UPDATE SET value=MAX(value, excluded.value)""",
        (kind, year, maximum),
    )
    while True:
        row = db.execute(
            "UPDATE counters SET value=value+1 WHERE kind=? AND year=? RETURNING value",
            (kind, year),
        ).fetchone()
        number = f"{year}-{row['value']:03d}"
        if not db.execute(f"SELECT 1 FROM {table} WHERE number=?", (number,)).fetchone():
            return number


def _validate_receipt_number(value, year, receipt_id=None):
    if not value:
        return None
    match = RECEIPT_NUMBER_RE.fullmatch(value)
    if (
        not match or int(match.group(1)) != year or int(match.group(2)) < 1
        or f"{year}-{int(match.group(2)):03d}" != value
    ):
        raise ValueError(f"Il numero deve essere nel formato {year}-001 e coerente con l'anno della data.")
    duplicate_sql = "SELECT 1 FROM receipts WHERE number=?"
    params = [value]
    if receipt_id is not None:
        duplicate_sql += " AND id!=?"
        params.append(receipt_id)
    if get_db().execute(duplicate_sql, params).fetchone():
        raise ValueError("Esiste già una ricevuta con questo numero.")
    return value


def _get_quote(quote_id):
    quote = get_db().execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
    if not quote:
        abort(404)
    lines = get_db().execute("SELECT * FROM quote_lines WHERE quote_id=? ORDER BY position", (quote_id,)).fetchall()
    hydrated = [hydrate_quote_line(dict(line)) for line in lines]
    return quote, hydrated


def _get_receipt(receipt_id):
    receipt = get_db().execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if not receipt:
        abort(404)
    lines = get_db().execute("SELECT * FROM receipt_lines WHERE receipt_id=? ORDER BY position", (receipt_id,)).fetchall()
    return receipt, [dict(line) for line in lines]


def g_user_id():
    from flask import g
    return g.user["id"]


def _validate_date(value, *, optional=False, label="Data"):
    if not value and optional:
        return
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} non valida.") from None


def _limited(value, maximum, label):
    if len(value) > maximum:
        raise ValueError(f"{label}: massimo {maximum} caratteri.")
    return value.strip()
