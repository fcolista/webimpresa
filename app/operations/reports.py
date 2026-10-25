from __future__ import annotations

import csv
from datetime import date
from io import StringIO
import json

from flask import abort, make_response, render_template, request

from ..branding import client_label
from ..db import get_db
from ..security import login_required
from .common import bp

MONTHS = (
    "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre",
)
CSV_KINDS = {"receipts", "withholding-clients", "withholding-months"}
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _requested_year(years):
    try:
        year = int(request.args.get("year", ""))
    except ValueError:
        year = None
    if year is not None and 2000 <= year <= 2100:
        return year
    current = date.today().year
    return current if current in years or not years else years[0]


def _available_years():
    rows = get_db().execute(
        "SELECT DISTINCT CAST(strftime('%Y', issue_date) AS INTEGER) AS y FROM receipts WHERE status='issued' ORDER BY y DESC"
    ).fetchall()
    return [row["y"] for row in rows]


def _issued_receipts(year):
    rows = get_db().execute(
        """SELECT id,number,issue_date,client_id,client_snapshot,subject,totals FROM receipts
           WHERE status='issued' AND strftime('%Y', issue_date)=? ORDER BY issue_date,number""",
        (str(year),),
    ).fetchall()
    records = []
    for row in rows:
        client = json.loads(row["client_snapshot"])
        totals = json.loads(row["totals"] or "{}")
        records.append({
            "id": row["id"],
            "number": row["number"],
            "issue_date": row["issue_date"],
            "month": int(row["issue_date"][5:7]),
            "client_key": row["client_id"] if row["client_id"] is not None else (client.get("tax_code") or client.get("vat_number") or client_label(client)),
            "client": client_label(client),
            "tax_code": client.get("tax_code", ""),
            "vat_number": client.get("vat_number", ""),
            "subject": row["subject"],
            "services": totals.get("services_cents", 0),
            "expenses": totals.get("expenses_cents", 0),
            "contribution": totals.get("contribution_cents", 0),
            "stamp": totals.get("stamp_cents", 0),
            "withholding_base": totals.get("withholding_base_cents", 0),
            "withholding": totals.get("withholding_cents", 0),
            "gross": totals.get("gross_cents", 0),
            "net_due": totals.get("net_due_cents", 0),
        })
    return records


def _group(records, key, label):
    groups = {}
    for record in records:
        group = groups.setdefault(key(record), {**label(record), "count": 0, "services": 0, "withholding_base": 0, "withholding": 0, "net_due": 0})
        group["count"] += 1
        for field in ("services", "withholding_base", "withholding", "net_due"):
            group[field] += record[field]
    return groups


def build_summary(records):
    months = _group(records, lambda r: r["month"], lambda r: {"month": r["month"], "name": MONTHS[r["month"] - 1]})
    clients = _group(
        records, lambda r: r["client_key"],
        lambda r: {"client": r["client"], "tax_code": r["tax_code"], "vat_number": r["vat_number"]},
    )
    totals = {"count": len(records)}
    for field in ("services", "expenses", "contribution", "stamp", "withholding_base", "withholding", "gross", "net_due"):
        totals[field] = sum(record[field] for record in records)
    return {
        "months": [months[month] for month in sorted(months)],
        "clients": sorted(clients.values(), key=lambda group: group["client"].casefold()),
        "totals": totals,
    }


@bp.get("/reports")
@login_required("user")
def reports():
    years = _available_years()
    year = _requested_year(years)
    summary = build_summary(_issued_receipts(year))
    return render_template("reports.html", year=year, years=sorted(set(years) | {year}, reverse=True), summary=summary)


def _cell(value):
    if isinstance(value, int):
        return f"{value / 100:.2f}".replace(".", ",")
    text = str(value if value is not None else "")
    # Neutralize spreadsheet formulas coming from user-entered text.
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text


def _csv_response(filename, header, rows):
    buffer = StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(value) for value in row])
    response = make_response(("\ufeff" + buffer.getvalue()).encode("utf-8"))
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@bp.get("/reports/<kind>.csv")
@login_required("user")
def report_csv(kind):
    if kind not in CSV_KINDS:
        abort(404)
    try:
        year = int(request.args.get("year", ""))
    except ValueError:
        abort(400, "Anno non valido.")
    if not 2000 <= year <= 2100:
        abort(400, "Anno non valido.")
    records = _issued_receipts(year)
    summary = build_summary(records)
    if kind == "receipts":
        return _csv_response(
            f"ricevute-{year}.csv",
            ["Numero", "Data", "Cliente", "Codice fiscale", "Partita IVA", "Oggetto", "Compenso lordo", "Contributo",
             "Rimborsi spese", "Bollo", "Base ritenuta", "Ritenuta d'acconto", "Totale documento", "Netto corrisposto"],
            [[r["number"], r["issue_date"], r["client"], r["tax_code"], r["vat_number"], r["subject"], r["services"],
              r["contribution"], r["expenses"], r["stamp"], r["withholding_base"], r["withholding"], r["gross"], r["net_due"]]
             for r in records] + [["Totale", "", "", "", "", "", summary["totals"]["services"], summary["totals"]["contribution"],
                                   summary["totals"]["expenses"], summary["totals"]["stamp"], summary["totals"]["withholding_base"],
                                   summary["totals"]["withholding"], summary["totals"]["gross"], summary["totals"]["net_due"]]],
        )
    if kind == "withholding-clients":
        return _csv_response(
            f"ritenute-per-cliente-{year}.csv",
            ["Cliente", "Codice fiscale", "Partita IVA", "Ricevute", "Compensi lordi", "Base ritenuta", "Ritenuta d'acconto", "Netto corrisposto"],
            [[g["client"], g["tax_code"], g["vat_number"], str(g["count"]), g["services"], g["withholding_base"], g["withholding"], g["net_due"]]
             for g in summary["clients"]],
        )
    return _csv_response(
        f"ritenute-per-mese-{year}.csv",
        ["Mese", "Ricevute", "Compensi lordi", "Base ritenuta", "Ritenuta d'acconto", "Netto corrisposto"],
        [[g["name"], str(g["count"]), g["services"], g["withholding_base"], g["withholding"], g["net_due"]] for g in summary["months"]],
    )
