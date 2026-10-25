from __future__ import annotations

import json

from flask import abort, flash, redirect, render_template, request, url_for

from ..branding import provider_snapshot
from ..documents import quote_pdf, receipt_pdf
from ..security import login_required, require_csrf
from ..ui import get_settings
from .common import EMAIL_RE, _get_quote, _get_receipt, _limited, bp, g_user_id


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
