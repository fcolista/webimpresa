from __future__ import annotations

from datetime import date
import json
import re
import sqlite3

from flask import Blueprint, abort, flash, make_response, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from .branding import provider_snapshot
from .db import get_db, transaction
from .documents import quote_pdf, receipt_pdf
from .finance import parse_quote_lines, parse_receipt_lines, quote_totals, receipt_totals
from .security import login_required, require_csrf
from .ui import escape_like, get_settings

bp = Blueprint("operations", __name__)
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
    hydrated = []
    from decimal import Decimal, ROUND_HALF_UP
    for line in lines:
        item = dict(line)
        gross = int((Decimal(item["quantity"]) * Decimal(item["unit_price_cents"]) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)
        discount = int((Decimal(gross) * Decimal(item["discount_bp"]) / 10000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        item["net_cents"] = gross - discount
        hydrated.append(item)
    return quote, hydrated


def _get_receipt(receipt_id):
    receipt = get_db().execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if not receipt:
        abort(404)
    lines = get_db().execute("SELECT * FROM receipt_lines WHERE receipt_id=? ORDER BY position", (receipt_id,)).fetchall()
    return receipt, [dict(line) for line in lines]


@bp.get("/app")
@login_required("user")
def dashboard():
    db = get_db()
    stats = {
        "clients": db.execute("SELECT COUNT(*) FROM clients WHERE archived=0").fetchone()[0],
        "quotes": db.execute("SELECT COUNT(*) FROM quotes WHERE strftime('%Y', issue_date)=?", (str(date.today().year),)).fetchone()[0],
        "receipts": db.execute("SELECT COUNT(*) FROM receipts WHERE strftime('%Y', issue_date)=?", (str(date.today().year),)).fetchone()[0],
    }
    recent = db.execute("SELECT id, number, status, issue_date, 'Preventivo' AS kind FROM quotes UNION ALL SELECT id, number, status, issue_date, 'Ricevuta' AS kind FROM receipts ORDER BY issue_date DESC LIMIT 8").fetchall()
    return render_template("dashboard.html", stats=stats, recent=recent)


@bp.get("/clients")
@login_required("user")
def clients():
    query = request.args.get("q", "").strip()[:100]
    kind = request.args.get("kind", "")
    sort = request.args.get("sort", "name")
    sort_column = {"name": "name, surname", "city": "city, name", "updated": "updated_at DESC, name"} .get(sort, "name, surname")
    db = get_db()
    if query:
        needle = f"%{escape_like(query)}%"
        filter_kind = " AND kind=?" if kind in {"person", "company"} else ""
        rows = db.execute(
            """SELECT clients.*,
                 (SELECT COUNT(*) FROM client_contacts cc WHERE cc.client_id=clients.id) AS contact_count
               FROM clients WHERE archived=0 AND
               (name LIKE ? ESCAPE '\\' OR surname LIKE ? ESCAPE '\\' OR tax_code LIKE ? ESCAPE '\\' OR vat_number LIKE ? ESCAPE '\\' OR email LIKE ? ESCAPE '\\'
                OR EXISTS (SELECT 1 FROM client_contacts cc WHERE cc.client_id=clients.id AND
                  (cc.first_name LIKE ? ESCAPE '\\' OR cc.last_name LIKE ? ESCAPE '\\' OR cc.role LIKE ? ESCAPE '\\'
                   OR cc.email LIKE ? ESCAPE '\\' OR cc.phone LIKE ? ESCAPE '\\' OR cc.mobile_phone LIKE ? ESCAPE '\\')))"""
            + filter_kind + f" ORDER BY {sort_column} LIMIT 200",
            (needle,) * 11 + ((kind,) if filter_kind else ()),
        ).fetchall()
    else:
        filter_kind = " AND kind=?" if kind in {"person", "company"} else ""
        params = (kind,) if filter_kind else ()
        rows = db.execute(
            "SELECT clients.*,(SELECT COUNT(*) FROM client_contacts cc WHERE cc.client_id=clients.id) AS contact_count FROM clients WHERE archived=0"
            + filter_kind + f" ORDER BY {sort_column} LIMIT 200", params
        ).fetchall()
    return render_template("clients.html", clients=rows, query=query, kind=kind, sort=sort)


@bp.route("/clients/new", methods=["GET", "POST"])
@login_required("user")
def client_new():
    if request.method == "POST":
        require_csrf()
        data, contacts, error = _client_form()
        if error:
            flash(error, "error")
        else:
            try:
                with transaction() as db:
                    cur = db.execute(
                        """INSERT INTO clients(kind,name,surname,tax_code,vat_number,address,postal_code,city,province,country,email,phone,pec,recipient_code,notes)
                           VALUES (:kind,:name,:surname,:tax_code,:vat_number,:address,:postal_code,:city,:province,:country,:email,:phone,:pec,:recipient_code,:notes)""",
                        data,
                    )
                    _save_client_contacts(db, cur.lastrowid, contacts)
                flash("Cliente creato.", "success")
                return redirect(url_for("operations.client_edit", client_id=cur.lastrowid))
            except sqlite3.IntegrityError:
                flash("Esiste già un cliente con questo codice fiscale o partita IVA.", "error")
    else:
        data, contacts = None, []
    return render_template("client_form.html", client=None, form_values=data, contacts=contacts)


@bp.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
@login_required("user")
def client_edit(client_id):
    client = get_db().execute("SELECT * FROM clients WHERE id=? AND archived=0", (client_id,)).fetchone()
    if not client:
        abort(404)
    if request.method == "POST":
        require_csrf()
        data, contacts, error = _client_form()
        if error:
            flash(error, "error")
        else:
            for field in ("tax_code", "vat_number"):
                if _normalize_tax_identifier(data[field]) == _normalize_tax_identifier(client[field]):
                    data[field] = client[field]
            data["id"] = client_id
            try:
                with transaction() as db:
                    db.execute(
                        """UPDATE clients SET kind=:kind,name=:name,surname=:surname,tax_code=:tax_code,vat_number=:vat_number,
                           address=:address,postal_code=:postal_code,city=:city,province=:province,country=:country,email=:email,
                           phone=:phone,pec=:pec,recipient_code=:recipient_code,notes=:notes,updated_at=CURRENT_TIMESTAMP WHERE id=:id""",
                        data,
                    )
                    _save_client_contacts(db, client_id, contacts)
                flash("Cliente aggiornato. I documenti conservano i propri dati storici.", "success")
                return redirect(url_for("operations.client_edit", client_id=client_id))
            except sqlite3.IntegrityError:
                flash("Esiste già un cliente con questo codice fiscale o partita IVA.", "error")
    documents = get_db().execute(
        """SELECT id,number,status,issue_date,'Preventivo' AS kind FROM quotes WHERE client_id=?
           UNION ALL SELECT id,number,status,issue_date,'Ricevuta occasionale' AS kind FROM receipts WHERE client_id=?
           ORDER BY issue_date DESC LIMIT 50""",
        (client_id, client_id),
    ).fetchall()
    if request.method == "GET":
        contacts = get_db().execute(
            "SELECT first_name,last_name,role,email,phone,mobile_phone FROM client_contacts WHERE client_id=? ORDER BY position,id",
            (client_id,),
        ).fetchall()
        data = None
    return render_template("client_form.html", client=client, documents=documents, form_values=data, contacts=contacts)


@bp.post("/clients/<int:client_id>/delete")
@login_required("user")
def client_delete(client_id):
    require_csrf()
    with transaction() as db:
        client = db.execute(
            "SELECT name, surname FROM clients WHERE id=? AND archived=0",
            (client_id,),
        ).fetchone()
        if not client:
            abort(404)
        db.execute("DELETE FROM clients WHERE id=?", (client_id,))
    label = f"{client['name']} {client['surname']}".strip()
    flash(
        f"Cliente {label} eliminato. I preventivi e le ricevute esistenti restano conservati "
        "con i dati storici e non sono più collegati all'anagrafica.",
        "success",
    )
    return redirect(url_for("operations.clients"))


def _client_form():
    kind = request.form.get("kind", "").strip()
    data = {key: request.form.get(key, "").strip() for key in (
        "kind", "name", "surname", "tax_code", "vat_number", "address", "postal_code", "city",
        "province", "country", "email", "phone", "pec", "recipient_code", "notes",
    )}
    if kind == "company":
        data["name"] = request.form.get("company_name", "").strip()
    first_names = request.form.getlist("contact_first_name[]")
    last_names = request.form.getlist("contact_last_name[]")
    roles = request.form.getlist("contact_role[]")
    emails = request.form.getlist("contact_email[]")
    phones = request.form.getlist("contact_phone[]")
    mobile_phones = request.form.getlist("contact_mobile[]")
    contacts = []
    if data["kind"] not in {"person", "company"}:
        return data, contacts, "Seleziona persona fisica o azienda."
    if not data["name"] or len(data["name"]) > 180:
        return data, contacts, "Nome o ragione sociale obbligatori (massimo 180 caratteri)."
    if data["kind"] == "person" and not data["surname"]:
        return data, contacts, "Il cognome è obbligatorio per una persona fisica."
    if len(data["notes"]) > 4000:
        return data, contacts, "Le note superano la lunghezza consentita."
    limits = {
        "surname": 180, "tax_code": 32, "vat_number": 32, "address": 300,
        "postal_code": 20, "city": 120, "province": 80, "country": 100,
        "email": 254, "phone": 100, "pec": 254, "recipient_code": 20,
    }
    if any(len(data[key]) > limit for key, limit in limits.items()):
        return data, contacts, "Uno o più campi superano la lunghezza consentita."
    if data["email"] and not EMAIL_RE.fullmatch(data["email"]):
        return data, contacts, "Indirizzo email non valido."
    if data["pec"] and not EMAIL_RE.fullmatch(data["pec"]):
        return data, contacts, "Indirizzo PEC non valido."
    if data["kind"] == "company":
        if not (len(first_names) == len(last_names) == len(roles) == len(emails) == len(phones) == len(mobile_phones)) or len(first_names) > 20:
            return data, contacts, "I contatti aziendali non sono validi."
        for first_name, last_name, role, email, phone, mobile_phone in zip(
            first_names, last_names, roles, emails, phones, mobile_phones
        ):
            contact = {
                "first_name": first_name.strip(),
                "last_name": last_name.strip(),
                "role": role.strip(),
                "email": email.strip(),
                "phone": phone.strip(),
                "mobile_phone": mobile_phone.strip(),
            }
            if not any(contact.values()):
                continue
            if not contact["first_name"] or not contact["last_name"] or not contact["role"]:
                return data, contacts, "Per ogni contatto indica nome, cognome e ruolo."
            if (
                len(contact["first_name"]) > 90 or len(contact["last_name"]) > 90
                or len(contact["role"]) > 100 or len(contact["email"]) > 254
                or len(contact["phone"]) > 100 or len(contact["mobile_phone"]) > 100
            ):
                return data, contacts, "Uno o più dati dei contatti superano la lunghezza consentita."
            if contact["email"] and not EMAIL_RE.fullmatch(contact["email"]):
                return data, contacts, "Indirizzo email non valido in un contatto aziendale."
            contacts.append(contact)
        if not contacts:
            return data, contacts, "Aggiungi almeno un contatto con nome e ruolo per l'azienda."
        data["surname"] = ""
        data["email"] = ""
        data["phone"] = ""
    elif any((first_names, last_names, roles, emails, phones, mobile_phones)):
        return data, contacts, "I contatti aggiuntivi sono disponibili per le aziende."
    for field in ("tax_code", "vat_number"):
        data[field] = _normalize_tax_identifier(data[field])
    return data, contacts, None


def _normalize_tax_identifier(value):
    return re.sub(r"\s+", "", value).upper()


def _save_client_contacts(db, client_id, contacts):
    db.execute("DELETE FROM client_contacts WHERE client_id=?", (client_id,))
    db.executemany(
        """INSERT INTO client_contacts(client_id,name,role,email,phone,first_name,last_name,mobile_phone,position)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        [(client_id, " ".join(part for part in (contact["first_name"], contact["last_name"]) if part),
          contact["role"], contact["email"], contact["phone"], contact["first_name"], contact["last_name"],
          contact["mobile_phone"], position)
         for position, contact in enumerate(contacts)],
    )


@bp.get("/quotes")
@login_required("user")
def quotes():
    query = request.args.get("q", "").strip()[:100]
    status = request.args.get("status", "")
    where, params = [], []
    if query:
        where.append("(q.number LIKE ? OR q.client_snapshot LIKE ?)")
        needle = f"%{escape_like(query)}%"
        params.extend((needle, needle))
    if status in {"draft", "sent", "accepted", "rejected", "expired"}:
        where.append("q.status=?")
        params.append(status)
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = get_db().execute(
        f"SELECT q.id,q.number,q.status,q.issue_date,q.valid_until,q.totals,q.client_snapshot FROM quotes q{clause} ORDER BY q.issue_date DESC,q.id DESC LIMIT 250",
        params,
    ).fetchall()
    return render_template("quotes.html", quotes=rows, query=query, status=status)


@bp.route("/quotes/new", methods=["GET", "POST"])
@login_required("user")
def quote_new():
    quote = None
    lines = []
    if request.method == "POST":
        require_csrf()
        try:
            client_id, snapshot = _snapshot(request.form)
            withholding_rate = request.form.get("withholding_rate", "0")
            amount_mode = request.form.get("amount_mode", "gross")
            lines = parse_quote_lines(request.form, amount_mode, withholding_rate)
            totals = quote_totals(lines, withholding_rate)
            issue_date = request.form.get("issue_date", "")
            year = date.fromisoformat(issue_date).year
            valid_until = request.form.get("valid_until", "")
            _validate_date(valid_until, optional=True)
            payment_terms = _limited(request.form.get("payment_terms", ""), 500, "Condizioni di pagamento")
            notes = _limited(request.form.get("notes", ""), 4000, "Note")
            commercial_terms = _limited(request.form.get("commercial_terms", ""), 4000, "Condizioni commerciali")
            provider = provider_snapshot(get_settings())
            with transaction() as db:
                number = _next_number(db, "quote", year)
                cur = db.execute(
                    """INSERT INTO quotes(number,year,client_id,client_snapshot,status,issue_date,valid_until,payment_terms,notes,commercial_terms,tax_config,totals,provider_snapshot,created_by)
                       VALUES(?,?,?,?,'draft',?,?,?,?,?,?,?,?,?)""",
                    (number, year, client_id, json.dumps(snapshot), issue_date, valid_until, payment_terms, notes, commercial_terms,
                     _quote_tax_config(withholding_rate, amount_mode, lines), json.dumps(totals), json.dumps(provider), g_user_id()),
                )
                quote_id = cur.lastrowid
                _insert_quote_lines(db, quote_id, lines)
            flash("Preventivo salvato come bozza.", "success")
            return redirect(url_for("operations.quote_detail", quote_id=quote_id))
        except (ValueError, TypeError) as exc:
            flash(str(exc), "error")
        except sqlite3.IntegrityError:
            flash("Errore di integrità durante il salvataggio.", "error")
    clients_list = get_db().execute("SELECT id,kind,name,surname FROM clients WHERE archived=0 ORDER BY name LIMIT 500").fetchall()
    return render_template("quote_form.html", quote=quote, lines=lines, clients=clients_list, issue_date=request.form.get("issue_date", date.today().isoformat()))


@bp.route("/quotes/<int:quote_id>/edit", methods=["GET", "POST"])
@login_required("user")
def quote_edit(quote_id):
    quote, saved_lines = _get_quote(quote_id)
    if quote["status"] != "draft":
        abort(409, "Solo le bozze possono essere modificate.")
    if request.method == "POST":
        require_csrf()
        try:
            client_id, snapshot = _snapshot(request.form)
            rate = request.form.get("withholding_rate", "0")
            amount_mode = request.form.get("amount_mode", "gross")
            lines = parse_quote_lines(request.form, amount_mode, rate)
            totals = quote_totals(lines, rate)
            issue_date = request.form.get("issue_date", "")
            year = date.fromisoformat(issue_date).year
            _validate_date(request.form.get("valid_until", ""), optional=True)
            number = quote["number"]
            if year != quote["year"]:
                number = None
            provider = provider_snapshot(get_settings())
            data = (
                number, client_id, json.dumps(snapshot), issue_date, request.form.get("valid_until", ""),
                _limited(request.form.get("payment_terms", ""), 500, "Condizioni di pagamento"),
                _limited(request.form.get("notes", ""), 4000, "Note"),
                _limited(request.form.get("commercial_terms", ""), 4000, "Condizioni commerciali"),
                _quote_tax_config(rate, amount_mode, lines), json.dumps(totals), json.dumps(provider), year, quote_id,
            )
            with transaction() as db:
                if number is None:
                    number = _next_number(db, "quote", year)
                db.execute(
                    """UPDATE quotes SET number=?,client_id=?,client_snapshot=?,issue_date=?,valid_until=?,payment_terms=?,notes=?,commercial_terms=?,
                       tax_config=?,totals=?,provider_snapshot=?,year=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (number, *data[1:]),
                )
                db.execute("DELETE FROM quote_lines WHERE quote_id=?", (quote_id,))
                _insert_quote_lines(db, quote_id, lines)
            flash("Bozza aggiornata.", "success")
            return redirect(url_for("operations.quote_detail", quote_id=quote_id))
        except ValueError as exc:
            flash(str(exc), "error")
    clients_list = get_db().execute("SELECT id,kind,name,surname FROM clients WHERE archived=0 ORDER BY name LIMIT 500").fetchall()
    return render_template("quote_form.html", quote=quote, lines=saved_lines, clients=clients_list, issue_date=quote["issue_date"])


def _quote_tax_config(rate, amount_mode, lines):
    config = {"withholding_rate": rate, "amount_mode": amount_mode}
    if amount_mode == "net":
        config["entered_unit_prices_cents"] = [line["entered_unit_price_cents"] for line in lines]
    return json.dumps(config)


def _insert_quote_lines(db, quote_id, lines):
    db.executemany(
        """INSERT INTO quote_lines(quote_id,position,description,quantity,unit_price_cents,discount_bp,vat_bp,withholding)
           VALUES(?,?,?,?,?,?,?,?)""",
        [(quote_id, l["position"], l["description"], l["quantity"], l["unit_price_cents"], l["discount_bp"], l["vat_bp"], l["withholding"]) for l in lines],
    )


def g_user_id():
    from flask import g
    return g.user["id"]


@bp.get("/quotes/<int:quote_id>")
@login_required("user")
def quote_detail(quote_id):
    quote, lines = _get_quote(quote_id)
    client = json.loads(quote["client_snapshot"])
    totals = json.loads(quote["totals"])
    acceptance = get_db().execute(
        "SELECT original_filename,uploaded_at FROM quote_acceptances WHERE quote_id=?",
        (quote_id,),
    ).fetchone()
    return render_template("quote_detail.html", quote=quote, lines=lines, client=client, totals=totals, acceptance=acceptance)


@bp.post("/quotes/<int:quote_id>/delete")
@login_required("user")
def quote_delete(quote_id):
    require_csrf()
    with transaction() as db:
        quote = db.execute("SELECT number FROM quotes WHERE id=?", (quote_id,)).fetchone()
        if not quote:
            abort(404)
        db.execute("DELETE FROM email_logs WHERE document_kind='quote' AND document_id=?", (quote_id,))
        db.execute("DELETE FROM quotes WHERE id=?", (quote_id,))
    flash(f"Preventivo {quote['number']} eliminato.", "success")
    return redirect(url_for("operations.quotes"))


@bp.post("/quotes/<int:quote_id>/status")
@login_required("user")
def quote_status(quote_id):
    require_csrf()
    status = request.form.get("status")
    if status not in {"sent", "accepted", "rejected", "expired"}:
        abort(400)
    signed_copy, upload_error = _read_signed_pdf(request.files.get("signed_copy"))
    if upload_error:
        flash(upload_error, "error")
        return redirect(url_for("operations.quote_detail", quote_id=quote_id))
    if signed_copy and status != "accepted":
        abort(400, "La copia firmata si può allegare solo quando il preventivo viene accettato.")
    with transaction() as db:
        cur = db.execute("UPDATE quotes SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('draft','sent')", (status, quote_id))
        if cur.rowcount != 1:
            abort(409, "Stato non modificabile.")
        if signed_copy:
            _save_signed_copy(db, quote_id, signed_copy, g_user_id())
    flash("Stato preventivo aggiornato.", "success")
    return redirect(url_for("operations.quote_detail", quote_id=quote_id))


@bp.post("/quotes/<int:quote_id>/signed-copy")
@login_required("user")
def quote_upload_signed_copy(quote_id):
    require_csrf()
    quote = get_db().execute("SELECT status FROM quotes WHERE id=?", (quote_id,)).fetchone()
    if not quote:
        abort(404)
    if quote["status"] != "accepted":
        abort(409, "Accetta il preventivo prima di allegare la copia firmata.")
    signed_copy, error = _read_signed_pdf(request.files.get("signed_copy"))
    if error:
        flash(error, "error")
    else:
        with transaction() as db:
            _save_signed_copy(db, quote_id, signed_copy, g_user_id())
        flash("Copia firmata salvata.", "success")
    return redirect(url_for("operations.quote_detail", quote_id=quote_id))


def _read_signed_pdf(upload):
    if not upload or not upload.filename:
        return None, None
    filename = secure_filename(upload.filename)
    content = upload.read(1_500_001)
    if len(content) > 1_500_000:
        return None, "La copia firmata non può superare 1,5 MB."
    if not filename.lower().endswith(".pdf") or not content.startswith(b"%PDF-") or b"%%EOF" not in content[-2048:]:
        return None, "La copia firmata deve essere un PDF valido."
    return (filename[:180], content), None


def _save_signed_copy(db, quote_id, signed_copy, user_id):
    if not signed_copy:
        raise ValueError("Copia firmata mancante.")
    filename, content = signed_copy
    db.execute(
        """INSERT INTO quote_acceptances(quote_id,original_filename,pdf_data,uploaded_by)
           VALUES(?,?,?,?) ON CONFLICT(quote_id) DO UPDATE SET
           original_filename=excluded.original_filename,pdf_data=excluded.pdf_data,
           uploaded_by=excluded.uploaded_by,uploaded_at=CURRENT_TIMESTAMP""",
        (quote_id, filename, content, user_id),
    )


@bp.get("/quotes/<int:quote_id>/signed-copy")
@login_required("user")
def quote_signed_copy(quote_id):
    acceptance = get_db().execute(
        "SELECT original_filename,pdf_data FROM quote_acceptances WHERE quote_id=?",
        (quote_id,),
    ).fetchone()
    if not acceptance:
        abort(404)
    response = make_response(acceptance["pdf_data"])
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'attachment; filename="{acceptance["original_filename"]}"'
    return response


@bp.post("/quotes/<int:quote_id>/duplicate")
@login_required("user")
def quote_duplicate(quote_id):
    require_csrf()
    quote, lines = _get_quote(quote_id)
    with transaction() as db:
        issue_date = date.today().isoformat()
        year = date.today().year
        number = _next_number(db, "quote", year)
        cur = db.execute(
            """INSERT INTO quotes(number,year,client_id,client_snapshot,status,issue_date,valid_until,payment_terms,notes,commercial_terms,tax_config,totals,provider_snapshot,source_quote_id,created_by)
               VALUES(?,?,?,?, 'draft', ?, '', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (number, year, quote["client_id"], quote["client_snapshot"], issue_date, quote["payment_terms"], quote["notes"],
             quote["commercial_terms"], quote["tax_config"], quote["totals"], quote["provider_snapshot"], quote_id, g_user_id()),
        )
        new_id = cur.lastrowid
        db.executemany(
            "INSERT INTO quote_lines(quote_id,position,description,quantity,unit_price_cents,discount_bp,vat_bp,withholding) VALUES(?,?,?,?,?,?,?,?)",
            [(new_id, l["position"], l["description"], l["quantity"], l["unit_price_cents"], l["discount_bp"], l["vat_bp"], l["withholding"]) for l in lines],
        )
    flash(f"Creata nuova bozza {number}.", "success")
    return redirect(url_for("operations.quote_edit", quote_id=new_id))


@bp.route("/quotes/<int:quote_id>/convert", methods=["GET", "POST"])
@login_required("user")
def quote_convert(quote_id):
    quote, lines = _get_quote(quote_id)
    if quote["status"] != "accepted":
        abort(409, "Solo un preventivo accettato può essere usato come base.")
    if request.method == "POST":
        require_csrf()
        settings = get_settings()
        if not settings.get("provider_name"):
            flash("Prima configura i dati del prestatore nel pannello Admin.", "error")
            return redirect(url_for("operations.quote_detail", quote_id=quote_id))
        source_lines = [{"description": l["description"], "amount_cents": l["net_cents"], "kind": "service"} for l in lines]
        with transaction() as db:
            number = _next_number(db, "receipt", date.today().year)
            quote_tax_config = json.loads(quote["tax_config"] or "{}")
            cur = db.execute(
                """INSERT INTO receipts(number,year,client_id,client_snapshot,provider_snapshot,status,issue_date,subject,tax_config,totals,quote_id,created_by)
                   VALUES(?,?,?,?,?,'draft',?,?,?,?,?,?)""",
                (number, date.today().year, quote["client_id"], quote["client_snapshot"], json.dumps(provider_snapshot(settings)), date.today().isoformat(),
                 f"Da preventivo {quote['number']}", json.dumps({
                     "withholding_rate": quote_tax_config.get("withholding_rate", "20"),
                     "contribution_rate": "0", "stamp_amount": "2", "stamp_threshold": "77.47",
                 }),
                 json.dumps({"net_due_cents": 0}), quote_id, g_user_id()),
            )
            receipt_id = cur.lastrowid
            db.executemany(
                "INSERT INTO receipt_lines(receipt_id,position,description,amount_cents,kind) VALUES(?,?,?,?,?)",
                [(receipt_id, i, l["description"], l["amount_cents"], l["kind"]) for i, l in enumerate(source_lines)],
            )
        flash("Bozza di ricevuta creata. Verifica cliente e impostazioni fiscali prima dell'emissione.", "success")
        return redirect(url_for("operations.receipt_edit", receipt_id=receipt_id))
    return render_template("confirm_convert.html", quote=quote)


@bp.get("/quotes/<int:quote_id>/pdf")
@login_required("user")
def quote_download(quote_id):
    quote, lines = _get_quote(quote_id)
    settings = get_settings()
    provider = json.loads(quote["provider_snapshot"] or "{}") or provider_snapshot(settings)
    response = make_response(quote_pdf(quote, lines, provider, settings.get("provider_logo", "")))
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'attachment; filename="Preventivo-{quote["number"]}.pdf"'
    return response


@bp.get("/receipts")
@login_required("user")
def receipts():
    query = request.args.get("q", "").strip()[:100]
    if query:
        needle = f"%{escape_like(query)}%"
        rows = get_db().execute(
            "SELECT id,number,status,issue_date,subject,client_snapshot,totals FROM receipts WHERE number LIKE ? OR client_snapshot LIKE ? ORDER BY issue_date DESC LIMIT 250",
            (needle, needle),
        ).fetchall()
    else:
        rows = get_db().execute("SELECT id,number,status,issue_date,subject,client_snapshot,totals FROM receipts ORDER BY issue_date DESC LIMIT 250").fetchall()
    return render_template("receipts.html", receipts=rows, query=query)


@bp.route("/receipts/new", methods=["GET", "POST"])
@login_required("user")
def receipt_new():
    return _receipt_form(None)


@bp.route("/receipts/<int:receipt_id>/edit", methods=["GET", "POST"])
@login_required("user")
def receipt_edit(receipt_id):
    return _receipt_form(receipt_id)


def _receipt_form(receipt_id):
    receipt = None
    saved_lines = []
    if receipt_id is not None:
        receipt, saved_lines = _get_receipt(receipt_id)
        if receipt["status"] != "draft":
            abort(409, "Una ricevuta emessa non può essere modificata; crea un documento di correzione secondo la procedura applicabile.")
    if request.method == "POST":
        require_csrf()
        try:
            client_id, snapshot = _snapshot(request.form)
            lines = parse_receipt_lines(request.form)
            saved_lines = lines
            quote_id = _accepted_quote_id(request.form.get("quote_id", ""), client_id)
            issue_date = request.form.get("issue_date", "")
            year = date.fromisoformat(issue_date).year
            subject = _limited(request.form.get("subject", ""), 240, "Oggetto")
            if not subject:
                raise ValueError("Oggetto obbligatorio.")
            cfg = {
                "withholding_rate": request.form.get("withholding_rate", "20"),
                "contribution_rate": request.form.get("contribution_rate", "0"),
                "stamp_amount": request.form.get("stamp_amount", "2"),
                "stamp_threshold": request.form.get("stamp_threshold", "77.47"),
                "amount_mode": request.form.get("amount_mode", "gross"),
            }
            number = _validate_receipt_number(
                request.form.get("number", "").strip(), year, receipt_id
            )
            totals = receipt_totals(
                lines, cfg["withholding_rate"], cfg["contribution_rate"], cfg["stamp_amount"],
                cfg["stamp_threshold"], cfg["amount_mode"],
            )
            with transaction() as db:
                if receipt_id:
                    if number is None and year == receipt["year"]:
                        number = receipt["number"]
                    elif number is None:
                        number = _next_number(db, "receipt", year)
                    db.execute(
                        "UPDATE receipts SET number=?,client_id=?,client_snapshot=?,issue_date=?,subject=?,tax_config=?,totals=?,year=?,quote_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='draft'",
                        (number, client_id, json.dumps(snapshot), issue_date, subject, json.dumps(cfg), json.dumps(totals), year, quote_id, receipt_id),
                    )
                    db.execute("DELETE FROM receipt_lines WHERE receipt_id=?", (receipt_id,))
                    target_id = receipt_id
                else:
                    number = number or _next_number(db, "receipt", year)
                    settings = get_settings()
                    cur = db.execute(
                        """INSERT INTO receipts(number,year,client_id,client_snapshot,provider_snapshot,status,issue_date,subject,tax_config,totals,quote_id,created_by)
                           VALUES(?,?,?,?,?,'draft',?,?,?,?,?,?)""",
                        (number, year, client_id, json.dumps(snapshot), json.dumps(provider_snapshot(settings)), issue_date, subject,
                         json.dumps(cfg), json.dumps(totals), quote_id, g_user_id()),
                    )
                    target_id = cur.lastrowid
                db.executemany(
                    "INSERT INTO receipt_lines(receipt_id,position,description,amount_cents,kind) VALUES(?,?,?,?,?)",
                    [(target_id, l["position"], l["description"], l["amount_cents"], l["kind"]) for l in lines],
                )
            flash("Bozza di ricevuta salvata.", "success")
            return redirect(url_for("operations.receipt_detail", receipt_id=target_id))
        except sqlite3.IntegrityError:
            flash("Esiste già una ricevuta con questo numero.", "error")
        except ValueError as exc:
            flash(str(exc), "error")
    clients_list = get_db().execute("SELECT id,kind,name,surname FROM clients WHERE archived=0 ORDER BY name LIMIT 500").fetchall()
    accepted_quotes = get_db().execute(
        "SELECT id,number,client_id,client_snapshot,tax_config FROM quotes WHERE status='accepted' ORDER BY issue_date DESC,id DESC LIMIT 500"
    ).fetchall()
    cfg = json.loads(receipt["tax_config"]) if receipt else {}
    if request.method == "POST":
        cfg = {
            key: request.form.get(key, default)
            for key, default in (
                ("withholding_rate", "20"),
                ("contribution_rate", "0"),
                ("stamp_amount", "2"),
                ("stamp_threshold", "77.47"),
                ("amount_mode", "gross"),
            )
        }
    return render_template("receipt_form.html", receipt=receipt, lines=saved_lines, clients=clients_list,
                           accepted_quotes=accepted_quotes,
                           issue_date=request.form.get("issue_date", receipt["issue_date"] if receipt else date.today().isoformat()),
                           number=request.form.get("number", receipt["number"] if receipt else ""),
                           cfg=cfg)


@bp.get("/receipts/<int:receipt_id>")
@login_required("user")
def receipt_detail(receipt_id):
    receipt, lines = _get_receipt(receipt_id)
    linked_quote = get_db().execute("SELECT id,number FROM quotes WHERE id=?", (receipt["quote_id"],)).fetchone() if receipt["quote_id"] else None
    return render_template("receipt_detail.html", receipt=receipt, lines=lines,
                           client=json.loads(receipt["client_snapshot"]), totals=json.loads(receipt["totals"]), linked_quote=linked_quote)


@bp.post("/receipts/<int:receipt_id>/delete")
@login_required("user")
def receipt_delete(receipt_id):
    require_csrf()
    with transaction() as db:
        receipt = db.execute("SELECT number FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if not receipt:
            abort(404)
        db.execute("DELETE FROM email_logs WHERE document_kind='receipt' AND document_id=?", (receipt_id,))
        db.execute("DELETE FROM receipts WHERE id=?", (receipt_id,))
    flash(f"Ricevuta {receipt['number']} eliminata.", "success")
    return redirect(url_for("operations.receipts"))


@bp.post("/receipts/<int:receipt_id>/issue")
@login_required("user")
def receipt_issue(receipt_id):
    require_csrf()
    _get_receipt(receipt_id)
    with transaction() as db:
        cur = db.execute("UPDATE receipts SET status='issued',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='draft'", (receipt_id,))
        if cur.rowcount != 1:
            abort(409, "La ricevuta non è più una bozza.")
    flash("Ricevuta emessa e resa non modificabile.", "success")
    return redirect(url_for("operations.receipt_detail", receipt_id=receipt_id))


@bp.get("/receipts/<int:receipt_id>/pdf")
@login_required("user")
def receipt_download(receipt_id):
    receipt, lines = _get_receipt(receipt_id)
    settings = get_settings()
    response = make_response(receipt_pdf(
        receipt, lines, settings.get("provider_logo", ""), settings.get("provider_signature", "")
    ))
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'attachment; filename="Ricevuta-{receipt["number"]}.pdf"'
    return response


@bp.route("/email/<kind>/<int:document_id>", methods=["GET", "POST"])
@login_required("user")
def send_document_email(kind, document_id):
    from .mailer import send_document
    if kind == "quote":
        document, lines = _get_quote(document_id)
        settings = get_settings()
        provider = json.loads(document["provider_snapshot"] or "{}") or provider_snapshot(settings)
        attachment = quote_pdf(document, lines, provider, settings.get("provider_logo", ""))
        title = f"Preventivo {document['number']}"
        default_email = json.loads(document["client_snapshot"]).get("email", "")
    elif kind == "receipt":
        document, lines = _get_receipt(document_id)
        settings = get_settings()
        attachment = receipt_pdf(
            document, lines, settings.get("provider_logo", ""), settings.get("provider_signature", "")
        )
        title = f"Ricevuta {document['number']}"
        default_email = json.loads(document["client_snapshot"]).get("email", "")
    else:
        abort(404)
    if request.method == "POST":
        require_csrf()
        recipient = request.form.get("recipient", "").strip()
        subject = _limited(request.form.get("subject", ""), 200, "Oggetto")
        body = _limited(request.form.get("body", ""), 5000, "Messaggio")
        if not EMAIL_RE.fullmatch(recipient):
            flash("Indirizzo destinatario non valido.", "error")
        else:
            try:
                send_document(kind, document_id, recipient, subject, body, attachment, title, g_user_id())
                flash("Email inviata.", "success")
                return redirect(url_for("operations.quote_detail" if kind == "quote" else "operations.receipt_detail", **({"quote_id": document_id} if kind == "quote" else {"receipt_id": document_id})))
            except Exception:
                from flask import current_app
                current_app.logger.exception("Document email send failed (kind=%s, id=%s)", kind, document_id)
                flash("Invio non riuscito. Verifica la configurazione SMTP nell'area amministrativa.", "error")
    return render_template("email_form.html", recipient=default_email, subject=title, document_title=title)


def _validate_date(value, *, optional=False):
    if not value and optional:
        return
    date.fromisoformat(value)


def _limited(value, maximum, label):
    if len(value) > maximum:
        raise ValueError(f"{label}: massimo {maximum} caratteri.")
    return value.strip()
