from __future__ import annotations

from datetime import date

from flask import render_template

from ..db import get_db
from ..security import login_required
from .common import bp


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
