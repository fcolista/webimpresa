from __future__ import annotations

from datetime import date
import json
import sqlite3

from flask import abort, flash, make_response, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from ..branding import provider_snapshot
from ..db import get_db, transaction
from ..documents import quote_pdf
from ..finance import LineError, parse_quote_lines, quote_totals
from ..security import login_required, require_csrf
from ..ui import escape_like, get_settings
from .common import _get_quote, _limited, _next_number, _snapshot, _validate_date, bp, g_user_id


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


ROW_FIELDS = ("description", "quantity", "unit_price", "discount", "vat", "withholding")
FORM_FIELDS = (
    "client_id", "issue_date", "valid_until", "withholding_rate", "amount_mode",
    "payment_terms", "notes", "commercial_terms", "contact_name",
)


def _blank_row():
    return {"description": "", "quantity": "1", "unit_price": "0", "discount": "0", "vat": "0", "withholding": "0"}


def _rows_from_form(form):
    columns = [form.getlist(f"{field}[]") for field in ROW_FIELDS]
    blank = _blank_row()
    count = min(max(len(column) for column in columns), 100)
    rows = [
        {field: (column[index] if index < len(column) else blank[field]) for field, column in zip(ROW_FIELDS, columns)}
        for index in range(count)
    ]
    return rows or [_blank_row()]


def _rows_from_quote(quote, lines):
    if quote is None:
        return [_blank_row()]
    config = json.loads(quote["tax_config"] or "{}")
    entered = config.get("entered_unit_prices_cents") if config.get("amount_mode") == "net" else None
    rows = []
    for index, line in enumerate(lines):
        price = entered[index] if entered and index < len(entered) else line["unit_price_cents"]
        rows.append({
            "description": line["description"],
            "quantity": line["quantity"],
            "unit_price": f"{price / 100:.2f}",
            "discount": f"{line['discount_bp'] / 100:g}",
            "vat": f"{line['vat_bp'] / 100:g}",
            "withholding": "1" if line["withholding"] else "0",
        })
    return rows or [_blank_row()]


def _form_values(quote, form):
    if form is not None:
        values = {key: form.get(key, "") for key in FORM_FIELDS}
        values["withholding_rate"] = values["withholding_rate"] or "0"
        values["amount_mode"] = values["amount_mode"] or "gross"
        return values
    if quote is None:
        return {
            "client_id": "", "issue_date": date.today().isoformat(), "valid_until": "",
            "withholding_rate": "0", "amount_mode": "gross", "payment_terms": "", "notes": "", "commercial_terms": "",
            "contact_name": "",
        }
    config = json.loads(quote["tax_config"] or "{}")
    return {
        "client_id": str(quote["client_id"] or ""),
        "issue_date": quote["issue_date"],
        "valid_until": quote["valid_until"] or "",
        "withholding_rate": config.get("withholding_rate", "0"),
        "amount_mode": config.get("amount_mode", "gross"),
        "payment_terms": quote["payment_terms"] or "",
        "notes": quote["notes"] or "",
        "commercial_terms": quote["commercial_terms"] or "",
        "contact_name": json.loads(quote["client_snapshot"] or "{}").get("contact_name", ""),
    }


def _render_form(quote, saved_lines, form=None, error=None):
    rows = _rows_from_form(form) if form is not None else _rows_from_quote(quote, saved_lines)
    if isinstance(error, LineError) and error.row is not None and error.row < len(rows):
        rows[error.row]["error"] = error.field or "row"
    clients_list = get_db().execute("SELECT id,kind,name,surname FROM clients WHERE archived=0 ORDER BY name LIMIT 500").fetchall()
    return render_template("quote_form.html", quote=quote, values=_form_values(quote, form), rows=rows, clients=clients_list)


def _save_quote(quote):
    form = request.form
    client_id, snapshot = _snapshot(form)
    rate = form.get("withholding_rate", "0")
    amount_mode = form.get("amount_mode", "gross")
    lines = parse_quote_lines(form, amount_mode, rate)
    totals = quote_totals(lines, rate)
    issue_date = form.get("issue_date", "")
    _validate_date(issue_date, label="Data di emissione")
    year = date.fromisoformat(issue_date).year
    valid_until = form.get("valid_until", "")
    _validate_date(valid_until, optional=True, label="Data di validità")
    payment_terms = _limited(form.get("payment_terms", ""), 500, "Condizioni di pagamento")
    notes = _limited(form.get("notes", ""), 4000, "Note")
    commercial_terms = _limited(form.get("commercial_terms", ""), 4000, "Condizioni commerciali")
    provider = json.dumps(provider_snapshot(get_settings()))
    tax_config = _quote_tax_config(rate, amount_mode, lines)
    with transaction() as db:
        if quote is None:
            number = _next_number(db, "quote", year)
            cur = db.execute(
                """INSERT INTO quotes(number,year,client_id,client_snapshot,status,issue_date,valid_until,payment_terms,notes,commercial_terms,tax_config,totals,provider_snapshot,created_by)
                   VALUES(?,?,?,?,'draft',?,?,?,?,?,?,?,?,?)""",
                (number, year, client_id, json.dumps(snapshot), issue_date, valid_until, payment_terms, notes, commercial_terms,
                 tax_config, json.dumps(totals), provider, g_user_id()),
            )
            quote_id = cur.lastrowid
        else:
            quote_id = quote["id"]
            number = quote["number"] if year == quote["year"] else _next_number(db, "quote", year)
            db.execute(
                """UPDATE quotes SET number=?,client_id=?,client_snapshot=?,issue_date=?,valid_until=?,payment_terms=?,notes=?,commercial_terms=?,
                   tax_config=?,totals=?,provider_snapshot=?,year=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (number, client_id, json.dumps(snapshot), issue_date, valid_until, payment_terms, notes, commercial_terms,
                 tax_config, json.dumps(totals), provider, year, quote_id),
            )
            db.execute("DELETE FROM quote_lines WHERE quote_id=?", (quote_id,))
        _insert_quote_lines(db, quote_id, lines)
    return quote_id


@bp.route("/quotes/new", methods=["GET", "POST"])
@login_required("user")
def quote_new():
    if request.method != "POST":
        return _render_form(None, [])
    require_csrf()
    error = None
    try:
        quote_id = _save_quote(None)
        flash("Preventivo salvato come bozza.", "success")
        return redirect(url_for("operations.quote_detail", quote_id=quote_id))
    except (ValueError, TypeError) as exc:
        error = exc
        flash(str(exc), "error")
    except sqlite3.IntegrityError:
        flash("Errore di integrità durante il salvataggio.", "error")
    return _render_form(None, [], request.form, error)


@bp.route("/quotes/<int:quote_id>/edit", methods=["GET", "POST"])
@login_required("user")
def quote_edit(quote_id):
    quote, saved_lines = _get_quote(quote_id)
    if quote["status"] != "draft":
        abort(409, "Solo le bozze possono essere modificate.")
    if request.method != "POST":
        return _render_form(quote, saved_lines)
    require_csrf()
    error = None
    try:
        _save_quote(quote)
        flash("Bozza aggiornata.", "success")
        return redirect(url_for("operations.quote_detail", quote_id=quote_id))
    except (ValueError, TypeError) as exc:
        error = exc
        flash(str(exc), "error")
    except sqlite3.IntegrityError:
        flash("Errore di integrità durante il salvataggio.", "error")
    return _render_form(quote, saved_lines, request.form, error)


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
