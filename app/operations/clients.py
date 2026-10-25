from __future__ import annotations

import re
import sqlite3

from flask import abort, flash, redirect, render_template, request, url_for

from ..db import get_db, transaction
from ..security import login_required, require_csrf
from ..ui import escape_like
from .common import EMAIL_RE, bp


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
            contacts = _raw_contacts()
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
            contacts = _raw_contacts()
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


def _raw_contacts():
    columns = [request.form.getlist(f"contact_{name}[]") for name in ("first_name", "last_name", "role", "email", "phone", "mobile")]
    contacts = [
        dict(zip(("first_name", "last_name", "role", "email", "phone", "mobile_phone"), values))
        for values in zip(*columns)
    ]
    return contacts[:20]


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
