from __future__ import annotations

from datetime import date
import json
import sqlite3

from flask import abort, flash, make_response, redirect, render_template, request, url_for

from ..branding import provider_snapshot
from ..db import get_db, transaction
from ..documents import receipt_pdf
from ..finance import LineError, parse_receipt_lines, receipt_totals
from ..security import login_required, require_csrf
from ..ui import escape_like, get_settings
from .common import (
    _accepted_quote_id, _get_receipt, _limited, _next_number, _snapshot,
    _validate_date, _validate_receipt_number, bp, g_user_id,
)


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


RECEIPT_CFG_DEFAULTS = {
    "withholding_rate": "20", "contribution_rate": "0", "stamp_amount": "2",
    "stamp_threshold": "77.47", "amount_mode": "gross",
}


def _blank_receipt_row():
    return {"description": "", "amount": "0", "kind": "service"}


def _receipt_rows_from_form(form):
    columns = [form.getlist("description[]"), form.getlist("amount[]"), form.getlist("kind[]")]
    blank = _blank_receipt_row()
    count = min(max(len(column) for column in columns), 100)
    rows = [
        {field: (column[index] if index < len(column) else blank[field]) for field, column in zip(blank, columns)}
        for index in range(count)
    ]
    return rows or [_blank_receipt_row()]


def _receipt_rows_from_saved(lines):
    rows = [
        {"description": line["description"], "amount": f"{line['amount_cents'] / 100:.2f}", "kind": line["kind"]}
        for line in lines
    ]
    return rows or [_blank_receipt_row()]


def _receipt_values(receipt, form):
    if form is not None:
        return (
            {
                "client_id": form.get("client_id", ""), "quote_id": form.get("quote_id", ""),
                "subject": form.get("subject", ""), "issue_date": form.get("issue_date", ""),
                "number": form.get("number", ""),
            },
            {key: form.get(key, default) for key, default in RECEIPT_CFG_DEFAULTS.items()},
        )
    if receipt is None:
        return (
            {"client_id": "", "quote_id": "", "subject": "", "issue_date": date.today().isoformat(), "number": ""},
            dict(RECEIPT_CFG_DEFAULTS),
        )
    return (
        {
            "client_id": str(receipt["client_id"] or ""), "quote_id": str(receipt["quote_id"] or ""),
            "subject": receipt["subject"], "issue_date": receipt["issue_date"], "number": receipt["number"],
        },
        {**RECEIPT_CFG_DEFAULTS, **json.loads(receipt["tax_config"] or "{}")},
    )


def _render_receipt_form(receipt, saved_lines, form=None, error=None):
    rows = _receipt_rows_from_form(form) if form is not None else _receipt_rows_from_saved(saved_lines)
    if isinstance(error, LineError) and error.row is not None and error.row < len(rows):
        rows[error.row]["error"] = error.field or "row"
    values, cfg = _receipt_values(receipt, form)
    db = get_db()
    clients_list = db.execute("SELECT id,kind,name,surname FROM clients WHERE archived=0 ORDER BY name LIMIT 500").fetchall()
    accepted_quotes = db.execute(
        "SELECT id,number,client_id,client_snapshot,tax_config FROM quotes WHERE status='accepted' ORDER BY issue_date DESC,id DESC LIMIT 500"
    ).fetchall()
    return render_template(
        "receipt_form.html", receipt=receipt, rows=rows, values=values, cfg=cfg,
        clients=clients_list, accepted_quotes=accepted_quotes,
    )


def _save_receipt(receipt):
    form = request.form
    client_id, snapshot = _snapshot(form)
    lines = parse_receipt_lines(form)
    quote_id = _accepted_quote_id(form.get("quote_id", ""), client_id)
    issue_date = form.get("issue_date", "")
    _validate_date(issue_date, label="Data")
    year = date.fromisoformat(issue_date).year
    subject = _limited(form.get("subject", ""), 240, "Oggetto")
    if not subject:
        raise ValueError("Oggetto obbligatorio.")
    cfg = {key: form.get(key, default) for key, default in RECEIPT_CFG_DEFAULTS.items()}
    receipt_id = receipt["id"] if receipt else None
    number = _validate_receipt_number(form.get("number", "").strip(), year, receipt_id)
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
            cur = db.execute(
                """INSERT INTO receipts(number,year,client_id,client_snapshot,provider_snapshot,status,issue_date,subject,tax_config,totals,quote_id,created_by)
                   VALUES(?,?,?,?,?,'draft',?,?,?,?,?,?)""",
                (number, year, client_id, json.dumps(snapshot), json.dumps(provider_snapshot(get_settings())), issue_date, subject,
                 json.dumps(cfg), json.dumps(totals), quote_id, g_user_id()),
            )
            target_id = cur.lastrowid
        db.executemany(
            "INSERT INTO receipt_lines(receipt_id,position,description,amount_cents,kind) VALUES(?,?,?,?,?)",
            [(target_id, l["position"], l["description"], l["amount_cents"], l["kind"]) for l in lines],
        )
    return target_id


def _receipt_form(receipt_id):
    receipt, saved_lines = None, []
    if receipt_id is not None:
        receipt, saved_lines = _get_receipt(receipt_id)
        if receipt["status"] != "draft":
            abort(409, "Una ricevuta emessa non può essere modificata; crea un documento di correzione secondo la procedura applicabile.")
    if request.method != "POST":
        return _render_receipt_form(receipt, saved_lines)
    require_csrf()
    error = None
    try:
        target_id = _save_receipt(receipt)
        flash("Bozza di ricevuta salvata.", "success")
        return redirect(url_for("operations.receipt_detail", receipt_id=target_id))
    except sqlite3.IntegrityError:
        flash("Esiste già una ricevuta con questo numero.", "error")
    except ValueError as exc:
        error = exc
        flash(str(exc), "error")
    return _render_receipt_form(receipt, saved_lines, request.form, error)


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
