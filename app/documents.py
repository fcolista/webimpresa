from __future__ import annotations

from io import BytesIO
import json
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .finance import hydrate_quote_line
from .branding import client_label, decode_logo


def _text(value):
    return escape(str(value or ""))


def _money(cents_value):
    return f"€ {int(cents_value) / 100:,.2f}"


def _address(data, prefix=""):
    keys = ("address", "postal_code", "city", "province", "country")
    return ", ".join(str(data.get(prefix + key, "")).strip() for key in keys if data.get(prefix + key))


def _provider_paragraph(provider, styles):
    lines = []
    if provider.get("provider_name"):
        lines.append(f"<b>{_text(provider['provider_name'])}</b>")
    tax_data = [
        f"Codice fiscale: {_text(provider['provider_tax_code'])}" if provider.get("provider_tax_code") else "",
        f"Partita IVA: {_text(provider['provider_vat'])}" if provider.get("provider_vat") else "",
    ]
    lines.extend(value for value in tax_data if value)
    address = _address(provider, "provider_")
    if address:
        lines.append(_text(address))
    for label, key in (
        ("Email", "provider_email"),
        ("Telefono", "provider_phone"),
        ("PEC", "provider_pec"),
        ("Codice destinatario", "provider_recipient_code"),
    ):
        if provider.get(key):
            lines.append(f"{label}: {_text(provider[key])}")
    return Paragraph("<br/>".join(lines) or "Dati del fornitore non configurati", styles["BodyText"])


def _payment_paragraph(provider, styles):
    lines = []
    for label, key in (
        ("Intestatario conto", "provider_account_holder"),
        ("Banca", "provider_bank_name"),
        ("IBAN", "provider_iban"),
        ("BIC / SWIFT", "provider_bic"),
    ):
        if provider.get(key):
            lines.append(f"<b>{label}:</b> {_text(provider[key])}")
    return Paragraph("<br/>".join(lines), styles["BodyText"])


def _document_header(story, title, number, client, provider, styles, line_kind, logo_data, document):
    logo = decode_logo(provider.get("logo_data") or logo_data)
    if logo:
        image = Image(BytesIO(logo), width=42 * mm, height=22 * mm, kind="proportional")
    else:
        image = Spacer(42 * mm, 1)
    header = Table([[image, _provider_paragraph(provider, styles)]], colWidths=[48 * mm, 126 * mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, -1), 0.7, colors.HexColor("#d5dce5")),
    ]))
    story.extend([
        header,
        Spacer(1, 7 * mm),
        Paragraph(_text(title), styles["Title"]),
        Paragraph(f"Numero {_text(number)}", styles["Heading2"]),
    ])
    details = [f"Data: {_text(document.get('issue_date', ''))}"]
    if line_kind == "quote" and document.get("valid_until"):
        details.append(f"Valido fino al: {_text(document['valid_until'])}")
    story.extend([Paragraph(" · ".join(details), styles["BodyText"]), Spacer(1, 5 * mm)])

    story.append(Paragraph("<b>Cliente / committente</b>", styles["Heading3"]))
    story.append(Paragraph(_text(client_label(client)), styles["BodyText"]))
    address = _address(client)
    if address:
        story.append(Paragraph(_text(address), styles["BodyText"]))
    for label, key in (
        ("Codice fiscale", "tax_code"),
        ("Partita IVA", "vat_number"),
        ("Email", "email"),
        ("Telefono", "phone"),
        ("PEC", "pec"),
    ):
        if client.get(key):
            story.append(Paragraph(f"<b>{label}:</b> {_text(client[key])}", styles["BodyText"]))
    contact_name = client.get("contact_name", "")
    contacts = client.get("contacts", [])
    if not contact_name and contacts:
        first = contacts[0]
        contact_name = " ".join(v for v in (first.get("first_name"), first.get("last_name")) if v) or first.get("name", "")
    if contact_name:
        story.append(Paragraph(f"<b>Referente:</b> {_text(contact_name)}", styles["BodyText"]))
    story.append(Spacer(1, 7 * mm))


def _pdf(title: str, number: str, client: dict, provider: dict, lines: list[dict], totals: dict,
         line_kind: str, logo_data: str = "", signature_data: str = "", document: dict | None = None) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    story = []
    _document_header(story, title, number, client, provider, styles, line_kind, logo_data, document or {})
    document = document or {}
    gross_up = (
        line_kind == "quote"
        and json.loads(document.get("tax_config") or "{}").get("amount_mode") == "net"
        and bool(totals.get("withholding_cents", 0))
    )
    if line_kind == "quote":
        rows = [["Descrizione", "Quantità", "Compenso lordo unit." if gross_up else "Prezzo unit. netto",
                 "Totale lordo" if gross_up else "Totale netto"]]
        for line in lines:
            rows.append([
                Paragraph(_text(line["description"]), styles["BodyText"]),
                line.get("quantity", "1"),
                _money(line.get("unit_price_cents", 0)),
                _money(line.get("net_cents", 0)),
            ])
        widths = [76 * mm, 20 * mm, 36 * mm, 42 * mm]
    else:
        rows = [["Descrizione", "Tipo"]]
        for line in lines:
            rows.append([
                Paragraph(_text(line["description"]), styles["BodyText"]),
                "Rimborso spese" if line.get("kind") == "expense" else "Prestazione",
            ])
        widths = [134 * mm, 40 * mm]
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#174b71")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d5dce5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT" if line_kind == "quote" else "LEFT"),
        ("PADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([table, Spacer(1, 7 * mm)])

    if line_kind == "quote":
        total_rows = [
            ["Imponibile", _money(totals.get("taxable_cents", 0))],
            ["IVA", _money(totals.get("vat_cents", 0))],
        ]
        if totals.get("withholding_cents", 0):
            rate = totals.get("withholding_rate_bp", 0) / 100
            total_rows.append([f"Ritenuta d'acconto ({rate:g}%)", f"- {_money(totals['withholding_cents'])}"])
        total_rows.append(["Totale preventivo", _money(totals.get("total_cents", 0))])
        if totals.get("withholding_cents", 0):
            total_rows.append(["Importo al netto della ritenuta", _money(totals.get("net_due_cents", 0))])
    else:
        total_rows = []
        for label, key in (
            ("Rimborsi spese", "expenses_cents"),
            ("Contributo previdenziale", "contribution_cents"),
            ("Imposta di bollo", "stamp_cents"),
            ("Ritenuta d'acconto", "withholding_cents"),
            ("Netto da corrispondere", "net_due_cents"),
        ):
            if key in totals:
                if key == "withholding_cents" and not totals[key]:
                    continue
                amount = _money(totals[key])
                if key == "withholding_cents":
                    rate = totals.get("withholding_rate_bp", 0) / 100
                    label = f"Ritenuta d'acconto ({rate:g}%)"
                    amount = f"- {amount}"
                total_rows.append([label, amount])
        if totals.get("services_cents") is not None:
            total_rows.insert(0, ["Compenso lordo", _money(totals["services_cents"])])

    totals_table = Table(total_rows, colWidths=[130 * mm, 44 * mm], hAlign="RIGHT")
    totals_table.setStyle(TableStyle([
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor("#174b71")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(totals_table)

    gross_up = gross_up or (
        line_kind == "receipt" and totals.get("amount_mode") == "net"
        and bool(totals.get("withholding_cents", 0) or totals.get("contribution_cents", 0))
    )
    if gross_up:
        story.extend([
            Spacer(1, 3 * mm),
            Paragraph(
                "Gli importi lordi sono calcolati in modo che, dedotta la ritenuta d'acconto, "
                "l'importo netto da corrispondere sia quello concordato.",
                styles["BodyText"],
            ),
        ])
    if line_kind == "quote":
        for label, key in (
            ("Condizioni di pagamento", "payment_terms"),
            ("Note", "notes"),
            ("Condizioni commerciali", "commercial_terms"),
        ):
            if document.get(key):
                story.extend([Spacer(1, 3 * mm), Paragraph(f"<b>{label}:</b> {_text(document[key])}", styles["BodyText"])])

    if line_kind == "receipt":
        signature = decode_logo(provider.get("signature_data") or signature_data)
        if signature:
            story.append(KeepTogether([
                Spacer(1, 12 * mm),
                Paragraph("Firma", styles["BodyText"]),
                Spacer(1, 2 * mm),
                Image(BytesIO(signature), width=48 * mm, height=22 * mm, kind="proportional", hAlign="LEFT"),
            ]))
        if any(provider.get(key) for key in ("provider_account_holder", "provider_bank_name", "provider_iban", "provider_bic")):
            story.append(KeepTogether([
                Spacer(1, 10 * mm),
                Paragraph("Estremi per il pagamento", styles["Heading3"]),
                _payment_paragraph(provider, styles),
            ]))

    doc.build(story)
    return buffer.getvalue()


def quote_pdf(quote, lines, provider=None, logo_data=""):
    hydrated = [hydrate_quote_line(dict(line)) for line in lines]
    provider_data = provider or json.loads(quote["provider_snapshot"] or "{}")
    return _pdf(
        "Preventivo", quote["number"], json.loads(quote["client_snapshot"]), provider_data,
        hydrated, json.loads(quote["totals"]), "quote", logo_data, document=dict(quote),
    )


def receipt_pdf(receipt, lines, logo_data="", signature_data=""):
    provider = json.loads(receipt["provider_snapshot"])
    return _pdf(
        "Ricevuta per prestazione occasionale", receipt["number"],
        json.loads(receipt["client_snapshot"]), provider, lines,
        json.loads(receipt["totals"]), "receipt", logo_data, signature_data, dict(receipt),
    )
