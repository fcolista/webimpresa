from __future__ import annotations

import base64
from io import BytesIO
import json

from PIL import Image
from pypdf import PdfReader

from conftest import csrf


def create_client(user_client, *, name="Studio Prova", tax_code="ABCDEF1234567890", vat_number="IT12345678901"):
    response = user_client.post("/clients/new", data={
        "_csrf": csrf(user_client),
        "kind": "company",
        "company_name": name,
        "contact_first_name[]": ["Maria"],
        "contact_last_name[]": ["Rossi"],
        "contact_role[]": ["Amministrazione"],
        "contact_email[]": ["cliente@example.test"],
        "contact_phone[]": [""],
        "contact_mobile[]": ["3331234567"],
        "tax_code": tax_code,
        "vat_number": vat_number,
        "address": "Via Roma 1",
        "postal_code": "00100",
        "city": "Roma",
        "province": "RM",
        "country": "Italia",
        "email": "",
        "phone": "",
        "pec": "",
        "recipient_code": "",
        "notes": "Nota interna che non deve finire nei documenti",
    })
    assert response.status_code == 302
    return int(response.location.rstrip("/").split("/")[-2])


def test_customer_search_and_snapshot_are_shared_but_immutable(user_client):
    assert user_client.get("/clients/new").status_code == 200
    assert user_client.get("/quotes/new").status_code == 200
    assert user_client.get("/receipts/new").status_code == 200
    client_id = create_client(user_client)
    assert b"Studio Prova" in user_client.get("/clients?q=Studio").data
    quote = user_client.post("/quotes/new", data={
        "_csrf": csrf(user_client),
        "client_id": str(client_id),
        "issue_date": "2026-10-16",
        "valid_until": "2026-11-16",
        "withholding_rate": "20",
        "description[]": ["Consulenza & sviluppo"],
        "quantity[]": ["1.5"],
        "unit_price[]": ["100.01"],
        "discount[]": ["0"],
        "vat[]": ["22"],
        "withholding[]": ["1"],
        "payment_terms": "30 giorni",
        "notes": "",
        "commercial_terms": "",
    })
    assert quote.status_code == 302
    detail = user_client.get(quote.location)
    assert b"Consulenza &amp; sviluppo" in detail.data
    pdf = user_client.get(quote.location + "/pdf")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")
    user_client.post(f"/clients/{client_id}/edit", data={
        "_csrf": csrf(user_client),
        "kind": "company", "company_name": "Nome Aggiornato", "tax_code": "ABCDEF1234567890",
        "contact_first_name[]": ["Maria"], "contact_last_name[]": ["Rossi"],
        "contact_role[]": ["Amministrazione"], "contact_email[]": ["cliente@example.test"],
        "contact_phone[]": [""], "contact_mobile[]": ["3331234567"],
        "vat_number": "IT12345678901", "address": "", "postal_code": "", "city": "", "province": "",
        "country": "Italia", "email": "", "phone": "", "pec": "", "recipient_code": "", "notes": "",
    })
    assert b"Studio Prova" in user_client.get(quote.location).data
    stored = user_client.application.config["DATABASE"]
    from sqlite3 import connect
    db = connect(stored)
    snapshot = json.loads(db.execute("SELECT client_snapshot FROM quotes").fetchone()[0])
    db.close()
    assert snapshot["name"] == "Studio Prova"
    assert snapshot["contacts"][0]["first_name"] == "Maria"
    assert snapshot["contacts"][0]["last_name"] == "Rossi"
    assert snapshot["contacts"][0]["mobile_phone"] == "3331234567"
    assert "notes" not in snapshot
    contact_page = user_client.get(f"/clients/{client_id}/edit")
    assert b'contact_first_name[]' in contact_page.data
    assert b'contact_last_name[]' in contact_page.data
    assert b'contact_mobile[]' in contact_page.data
    assert b">Archivia<" not in contact_page.data
    assert user_client.get("/clients?q=3331234567").status_code == 200
    assert user_client.post(f"/clients/{client_id}/archive", data={"_csrf": csrf(user_client)}).status_code == 404


def test_deleting_client_preserves_document_snapshot_and_unlinks_history(user_client):
    client_id = create_client(user_client, tax_code="DELETECLIENT123456")
    created = user_client.post("/quotes/new", data={
        "_csrf": csrf(user_client),
        "client_id": str(client_id),
        "issue_date": "2026-10-16",
        "valid_until": "",
        "withholding_rate": "0",
        "description[]": ["Consulenza"],
        "quantity[]": ["1"],
        "unit_price[]": ["100"],
        "discount[]": ["0"],
        "vat[]": ["0"],
        "withholding[]": ["0"],
        "payment_terms": "",
        "notes": "",
        "commercial_terms": "",
    })
    assert created.status_code == 302

    deleted = user_client.post(
        f"/clients/{client_id}/delete",
        data={"_csrf": csrf(user_client)},
        follow_redirects=True,
    )
    assert deleted.status_code == 200
    assert b"restano conservati" in deleted.data
    assert b"Studio Prova" not in user_client.get("/clients").data

    from sqlite3 import connect
    db = connect(user_client.application.config["DATABASE"])
    row = db.execute("SELECT client_id,client_snapshot FROM quotes").fetchone()
    contact_count = db.execute("SELECT COUNT(*) FROM client_contacts").fetchone()[0]
    db.close()
    assert row[0] is None
    assert json.loads(row[1])["name"] == "Studio Prova"
    assert contact_count == 0

    assert user_client.post(f"/clients/{client_id}/delete", data={"_csrf": csrf(user_client)}).status_code == 404


def test_client_edit_preserves_unchanged_legacy_tax_identifiers(user_client):
    client_id = create_client(user_client, tax_code="LEGACYTAX12345678")
    with user_client.application.app_context():
        from app.db import get_db
        get_db().execute(
            "UPDATE clients SET tax_code='LEGACY TAX 12345678',vat_number='it12345678901' WHERE id=?",
            (client_id,),
        )
        get_db().execute(
            """INSERT INTO clients(kind,name,tax_code,vat_number)
               VALUES('company','Altra azienda','OTHERLEGACY12345','IT12345678901')"""
        )

    response = user_client.post(f"/clients/{client_id}/edit", data={
        "_csrf": csrf(user_client),
        "kind": "company",
        "company_name": "Studio Prova aggiornato",
        "tax_code": "LEGACY TAX 12345678",
        "vat_number": "it12345678901",
        "contact_first_name[]": ["Maria"],
        "contact_last_name[]": ["Rossi"],
        "contact_role[]": ["Amministrazione"],
        "contact_email[]": ["cliente@example.test"],
        "contact_phone[]": [""],
        "contact_mobile[]": ["3331234567"],
        "address": "Via aggiornata 2",
        "postal_code": "00100",
        "city": "Roma",
        "province": "RM",
        "country": "Italia",
        "email": "",
        "phone": "",
        "pec": "",
        "recipient_code": "",
        "notes": "",
    })

    assert response.status_code == 302
    with user_client.application.app_context():
        from app.db import get_db
        row = get_db().execute(
            "SELECT name,tax_code,vat_number,address FROM clients WHERE id=?",
            (client_id,),
        ).fetchone()
        assert tuple(row) == (
            "Studio Prova aggiornato",
            "LEGACY TAX 12345678",
            "it12345678901",
            "Via aggiornata 2",
        )


def test_document_lists_show_consistent_edit_and_delete_buttons(user_client):
    client_id = create_client(user_client, tax_code="LISTBUTTONS123456")
    user_client.post("/quotes/new", data={
        "_csrf": csrf(user_client),
        "client_id": str(client_id),
        "issue_date": "2026-10-16",
        "withholding_rate": "0",
        "description[]": ["Consulenza"],
        "quantity[]": ["1"],
        "unit_price[]": ["100"],
        "discount[]": ["0"],
        "vat[]": ["0"],
        "withholding[]": ["0"],
        "payment_terms": "",
        "notes": "",
        "commercial_terms": "",
    })
    user_client.post("/receipts/new", data={
        "_csrf": csrf(user_client),
        "client_id": str(client_id),
        "issue_date": "2026-10-16",
        "subject": "Consulenza",
        "description[]": ["Consulenza"],
        "amount[]": ["100"],
        "kind[]": ["service"],
        "withholding_rate": "20",
        "contribution_rate": "0",
        "stamp_amount": "2",
        "stamp_threshold": "77.47",
    })

    for path in ("/clients", "/quotes", "/receipts"):
        page = user_client.get(path).get_data(as_text=True)
        assert 'class="button secondary compact"' in page
        assert 'class="button danger compact"' in page
        assert ">Modifica</a>" in page or ">Apri</a>" in page
        assert ">Elimina</button>" in page


def test_receipt_party_and_metadata_fields_share_one_aligned_section(user_client):
    page = user_client.get("/receipts/new").get_data(as_text=True)
    assert page.count("Committente e dati della ricevuta") == 1
    assert "receipt-party-fields" in page
    assert "receipt-metadata-fields" in page
    assert 'class="form-grid receipt-metadata"' not in page
    assert "receipt-party-label" not in page


def test_form_field_proportions_and_dashboard_consistency(user_client):
    dashboard = user_client.get("/app").get_data(as_text=True)
    assert "<h1>Dashboard</h1>" in dashboard
    assert ">Dashboard</a>" in dashboard
    assert 'class="button secondary" href="/quotes">Tutti i preventivi</a>' in dashboard
    assert 'class="button secondary compact" href="/clients">Apri anagrafica →</a>' in dashboard
    assert dashboard.count('class="button secondary compact"') == 3

    quote_form = user_client.get("/quotes/new")
    assert b'class="line-row"' in quote_form.data
    assert b'name="withholding[]"' in quote_form.data
    css = user_client.get("/static/app.css").get_data(as_text=True)
    assert "input[type=file]::file-selector-button" in css
    assert "minmax(280px,4fr)" in css
    assert "minmax(64px,.55fr)" in css
    assert "minmax(0,3fr) minmax(120px,1fr)" in css
    assert "grid-template-columns: minmax(135px,155px) minmax(165px,185px) minmax(0,1fr)" in css
    assert ".receipt-tax-fields input { max-width: 110px; }" in css
    assert ".quote-withholding { max-width: 90px; }" in css


def test_receipt_number_can_be_customized_on_drafts_and_rejects_invalid_or_duplicate(user_client):
    client_id = create_client(user_client, tax_code="CUSTOMNUMBER123456")

    def post_receipt(number, issue_date="2026-10-16"):
        return user_client.post("/receipts/new", data={
            "_csrf": csrf(user_client),
            "client_id": str(client_id),
            "issue_date": issue_date,
            "number": number,
            "subject": "Prestazione numerata",
            "description[]": ["Lavoro"],
            "amount[]": ["100"],
            "kind[]": ["service"],
            "withholding_rate": "20",
            "contribution_rate": "0",
            "stamp_amount": "2",
            "stamp_threshold": "77.47",
        })

    created = post_receipt("2026-042")
    assert created.status_code == 302
    assert "2026-042" in user_client.get(created.location).get_data(as_text=True)
    edit_form = user_client.get(created.location + "/edit")
    assert b'name="number" value="2026-042"' in edit_form.data

    duplicate = post_receipt("2026-042", "2026-10-17")
    assert duplicate.status_code == 200
    assert "Esiste già una ricevuta con questo numero." in duplicate.get_data(as_text=True)

    wrong_year = post_receipt("2027-043")
    assert wrong_year.status_code == 200
    assert "Il numero deve essere nel formato" in wrong_year.get_data(as_text=True)

    malformed = post_receipt("2026-000")
    assert malformed.status_code == 200
    assert "Il numero deve essere nel formato" in malformed.get_data(as_text=True)

    receipt_id = int(created.location.rsplit("/", 1)[-1])
    updated = user_client.post(f"/receipts/{receipt_id}/edit", data={
        "_csrf": csrf(user_client),
        "client_id": str(client_id),
        "issue_date": "2026-10-16",
        "number": "2026-1000",
        "subject": "Prestazione numerata",
        "description[]": ["Lavoro"],
        "amount[]": ["100"],
        "kind[]": ["service"],
        "withholding_rate": "20",
        "contribution_rate": "0",
        "stamp_amount": "2",
        "stamp_threshold": "77.47",
    })
    assert updated.status_code == 302
    assert "2026-1000" in user_client.get(updated.location).get_data(as_text=True)
    user_client.post(f"/receipts/{receipt_id}/issue", data={"_csrf": csrf(user_client)})
    assert user_client.get(f"/receipts/{receipt_id}/edit").status_code == 409


def test_receipt_is_draft_then_immutable_after_issue(user_client):
    client_id = create_client(user_client, tax_code="ZYXWVU1234567890")
    response = user_client.post("/receipts/new", data={
        "_csrf": csrf(user_client),
        "client_id": str(client_id),
        "issue_date": "2026-10-16",
        "subject": "Prestazione professionale",
        "description[]": ["Lavoro"],
        "amount[]": ["123.45"],
        "kind[]": ["service"],
        "withholding_rate": "20",
        "contribution_rate": "4",
        "stamp_amount": "2",
        "stamp_threshold": "77.47",
    })
    assert response.status_code == 302
    detail = user_client.get(response.location)
    assert b"Bozza" in detail.data
    issue = user_client.post(response.location + "/issue", data={"_csrf": csrf(user_client)})
    assert issue.status_code == 302
    assert user_client.get(response.location + "/edit").status_code == 409
    pdf = user_client.get(response.location + "/pdf")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")


def test_accepted_quote_rate_is_available_for_receipt_form_and_conversion(user_client):
    client_id = create_client(user_client, tax_code="LINKEDRATE1234567")
    quote_response = user_client.post("/quotes/new", data={
        "_csrf": csrf(user_client), "client_id": str(client_id), "issue_date": "2026-10-16",
        "withholding_rate": "15", "description[]": ["Consulenza"], "quantity[]": ["1"],
        "unit_price[]": ["100"], "discount[]": ["0"], "vat[]": ["0"], "withholding[]": ["1"],
        "payment_terms": "", "notes": "", "commercial_terms": "",
    })
    quote_id = int(quote_response.location.rsplit("/", 1)[-1])
    user_client.post(f"/quotes/{quote_id}/status", data={
        "_csrf": csrf(user_client), "status": "accepted",
    })

    receipt_form = user_client.get("/receipts/new")
    assert b'data-withholding-rate="15"' in receipt_form.data
    assert b'id="receipt-withholding-rate"' in receipt_form.data

    with user_client.application.app_context():
        from app.db import get_db
        get_db().execute(
            "UPDATE settings SET value='Prestatore Test' WHERE key='provider_name'"
        )
    converted = user_client.post(f"/quotes/{quote_id}/convert", data={"_csrf": csrf(user_client)})
    receipt_edit = user_client.get(converted.location)
    assert b'name="withholding_rate"' in receipt_edit.data
    assert b'value="15"' in receipt_edit.data


def test_document_forms_use_compact_metadata_layout(user_client):
    quote_form = user_client.get("/quotes/new")
    assert b'class="form-grid quote-metadata"' in quote_form.data
    assert b'class="quote-date" type="date" name="issue_date"' in quote_form.data
    receipt_form = user_client.get("/receipts/new")
    assert b'class="form-grid receipt-party-fields"' in receipt_form.data
    assert b'class="receipt-metadata-fields"' in receipt_form.data
    assert b'class="receipt-date" type="date" name="issue_date"' in receipt_form.data


def test_invalid_or_duplicate_customer_data_is_rejected(user_client):
    create_client(user_client)
    duplicate = user_client.post("/clients/new", data={
        "_csrf": csrf(user_client), "kind": "company", "company_name": "Duplicato",
        "contact_first_name[]": ["Mario"], "contact_last_name[]": ["Rossi"],
        "contact_role[]": ["Contabilità"], "contact_email[]": [""],
        "contact_phone[]": [""], "contact_mobile[]": [""],
        "tax_code": "abcdef1234567890", "vat_number": "IT99887766554",
    }, follow_redirects=True)
    assert "codice fiscale o partita IVA" in duplicate.get_data(as_text=True)


def test_archived_customer_tax_identifiers_can_be_reused_by_active_customer(user_client):
    from app.db import get_db

    with user_client.application.app_context():
        get_db().execute(
            """INSERT INTO clients(kind,name,tax_code,vat_number,archived)
               VALUES('company','Data Italia archiviata','ARCHIVEDTAX12345','IT11122233344',1)"""
        )

    created = user_client.post("/clients/new", data={
        "_csrf": csrf(user_client), "kind": "company", "company_name": "Data Italia",
        "contact_first_name[]": ["Mario"], "contact_last_name[]": ["Rossi"],
        "contact_role[]": ["Amministrazione"], "contact_email[]": [""],
        "contact_phone[]": [""], "contact_mobile[]": [""],
        "tax_code": "ARCHIVEDTAX12345", "vat_number": "IT11122233344",
    })
    assert created.status_code == 302

    duplicate = user_client.post("/clients/new", data={
        "_csrf": csrf(user_client), "kind": "company", "company_name": "Duplicato",
        "contact_first_name[]": ["Anna"], "contact_last_name[]": ["Bianchi"],
        "contact_role[]": ["Amministrazione"], "contact_email[]": [""],
        "contact_phone[]": [""], "contact_mobile[]": [""],
        "tax_code": "ARCHIVEDTAX12345", "vat_number": "IT99887766554",
    }, follow_redirects=True)
    assert "codice fiscale o partita IVA" in duplicate.get_data(as_text=True)


def test_accepted_quote_stores_signed_copy_and_links_receipt_with_supplier_branding(user_client):
    client_id = create_client(user_client, tax_code="ZZZYYY1234567890")
    logo = BytesIO()
    Image.new("RGB", (48, 24), (23, 75, 113)).save(logo, format="PNG")
    signature = BytesIO()
    Image.new("RGBA", (80, 32), (23, 75, 113, 180)).save(signature, format="PNG")
    with user_client.application.app_context():
        from app.db import get_db
        settings = {
            "provider_name": "Professionista Demo",
            "provider_tax_code": "PRV1234567890",
            "provider_vat": "IT12345678901",
            "provider_address": "Via Roma 2",
            "provider_postal_code": "00100",
            "provider_city": "Roma",
            "provider_province": "RM",
            "provider_country": "Italia",
            "provider_email": "fornitore@example.test",
            "provider_phone": "+39 061234567",
            "provider_pec": "fornitore@pec.example.test",
            "provider_iban": "IT00TESTBANK0000000000000000",
            "provider_account_holder": "Professionista Demo",
            "provider_logo": base64.b64encode(logo.getvalue()).decode("ascii"),
            "provider_signature": base64.b64encode(signature.getvalue()).decode("ascii"),
        }
        get_db().executemany(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            settings.items(),
        )

    quote_response = user_client.post("/quotes/new", data={
        "_csrf": csrf(user_client), "client_id": str(client_id), "issue_date": "2026-10-16",
        "withholding_rate": "20", "description[]": ["Analisi tecnica"], "quantity[]": ["2"],
        "unit_price[]": ["100"], "discount[]": ["0"], "vat[]": ["22"], "withholding[]": ["1"],
        "payment_terms": "30 giorni", "notes": "", "commercial_terms": "",
    })
    quote_id = int(quote_response.location.rsplit("/", 1)[-1])
    accepted_pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
    accept_response = user_client.post(
        f"/quotes/{quote_id}/status",
        data={
            "_csrf": csrf(user_client),
            "status": "accepted",
            "signed_copy": (BytesIO(accepted_pdf), "preventivo-firmato.pdf"),
        },
        follow_redirects=True,
    )
    assert accept_response.status_code == 200
    assert "preventivo-firmato.pdf" in accept_response.get_data(as_text=True)
    saved_copy = user_client.get(f"/quotes/{quote_id}/signed-copy")
    assert saved_copy.status_code == 200 and saved_copy.data == accepted_pdf

    quote_pdf_response = user_client.get(f"/quotes/{quote_id}/pdf")
    quote_text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(quote_pdf_response.data)).pages)
    assert "Professionista Demo" in quote_text
    assert "fornitore@pec.example.test" in quote_text
    assert "IT00TESTBANK0000000000000000" not in quote_text
    assert "Prezzo unit. netto" in quote_text
    assert PdfReader(BytesIO(quote_pdf_response.data)).pages[0].images

    receipt_response = user_client.post("/receipts/new", data={
        "_csrf": csrf(user_client), "client_id": str(client_id), "quote_id": str(quote_id),
        "issue_date": "2026-10-16", "subject": "Prestazione collegata",
        "description[]": ["Analisi tecnica"], "amount[]": ["166.40"], "kind[]": ["service"],
        "amount_mode": "net", "withholding_rate": "20", "contribution_rate": "4",
        "stamp_amount": "2", "stamp_threshold": "77.47",
    })
    assert receipt_response.status_code == 302
    receipt_page = user_client.get(receipt_response.location)
    assert "2026-001" in receipt_page.get_data(as_text=True)
    assert "€ 168.40" in receipt_page.get_data(as_text=True)
    receipt_pdf_response = user_client.get(receipt_response.location + "/pdf")
    receipt_reader = PdfReader(BytesIO(receipt_pdf_response.data))
    receipt_text = "\n".join(page.extract_text() or "" for page in receipt_reader.pages)
    assert "Professionista Demo" in receipt_text
    assert "fornitore@pec.example.test" in receipt_text
    assert "IT00TESTBANK0000000000000000" in receipt_text
    assert "Firma" in receipt_text
    assert len(receipt_reader.pages[0].images) >= 2
    assert "Importo inserito (netto)" in receipt_text


def test_issued_receipt_stays_immutable_but_can_be_deleted_with_linked_quote(user_client):
    client_id = create_client(user_client, tax_code="ABCDEF1234500001")
    quote_response = user_client.post("/quotes/new", data={
        "_csrf": csrf(user_client), "client_id": str(client_id), "issue_date": "2026-10-16",
        "withholding_rate": "20", "description[]": ["Lavoro"], "quantity[]": ["1"],
        "unit_price[]": ["100"], "discount[]": ["0"], "vat[]": ["0"], "withholding[]": ["1"],
        "payment_terms": "", "notes": "", "commercial_terms": "",
    })
    quote_id = int(quote_response.location.rsplit("/", 1)[-1])
    accepted = user_client.post(f"/quotes/{quote_id}/status", data={
        "_csrf": csrf(user_client), "status": "accepted",
    })
    assert accepted.status_code == 302

    receipt_response = user_client.post("/receipts/new", data={
        "_csrf": csrf(user_client), "client_id": str(client_id), "quote_id": str(quote_id),
        "issue_date": "2026-10-16", "subject": "Prestazione",
        "description[]": ["Lavoro"], "amount[]": ["100"], "kind[]": ["service"],
        "withholding_rate": "20", "contribution_rate": "0", "stamp_amount": "2", "stamp_threshold": "77.47",
    })
    receipt_id = int(receipt_response.location.rsplit("/", 1)[-1])
    user_client.post(f"/receipts/{receipt_id}/issue", data={"_csrf": csrf(user_client)})
    assert user_client.get(f"/receipts/{receipt_id}/edit").status_code == 409
    with user_client.application.app_context():
        from app.db import get_db
        get_db().execute(
            """INSERT INTO email_logs(user_id,document_kind,document_id,recipient,outcome)
               SELECT id,'quote',?,'client@example.test','sent' FROM users WHERE username='operatore'""",
            (quote_id,),
        )
        get_db().execute(
            """INSERT INTO email_logs(user_id,document_kind,document_id,recipient,outcome)
               SELECT id,'receipt',?,'client@example.test','sent' FROM users WHERE username='operatore'""",
            (receipt_id,),
        )

    deleted_quote = user_client.post(
        f"/quotes/{quote_id}/delete", data={"_csrf": csrf(user_client)}
    )
    assert deleted_quote.status_code == 302
    assert user_client.get(f"/quotes/{quote_id}").status_code == 404
    with user_client.application.app_context():
        from app.db import get_db
        receipt = get_db().execute("SELECT status,quote_id FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        assert receipt["status"] == "issued"
        assert receipt["quote_id"] is None
        assert get_db().execute(
            "SELECT COUNT(*) FROM email_logs WHERE document_kind='quote' AND document_id=?",
            (quote_id,),
        ).fetchone()[0] == 0

    deleted_receipt = user_client.post(
        f"/receipts/{receipt_id}/delete", data={"_csrf": csrf(user_client)}
    )
    assert deleted_receipt.status_code == 302
    assert user_client.get(f"/receipts/{receipt_id}").status_code == 404
    with user_client.application.app_context():
        from app.db import get_db
        assert get_db().execute("SELECT COUNT(*) FROM receipt_lines WHERE receipt_id=?", (receipt_id,)).fetchone()[0] == 0
        assert get_db().execute(
            "SELECT COUNT(*) FROM email_logs WHERE document_kind='receipt' AND document_id=?",
            (receipt_id,),
        ).fetchone()[0] == 0
