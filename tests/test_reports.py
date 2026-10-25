from io import BytesIO

from pypdf import PdfReader

from tests.conftest import csrf
from tests.test_operations import create_client


def make_receipt(client, client_id, amount, issue_date, *, issue=True, subject="Consulenza"):
    response = client.post("/receipts/new", data={
        "_csrf": csrf(client), "client_id": str(client_id), "issue_date": issue_date, "subject": subject,
        "withholding_rate": "20", "contribution_rate": "0", "stamp_amount": "0", "stamp_threshold": "77.47",
        "amount_mode": "gross", "number": "", "description[]": ["Prestazione"], "amount[]": [amount], "kind[]": ["service"],
    })
    assert response.status_code == 302
    if issue:
        assert client.post(response.location + "/issue", data={"_csrf": csrf(client)}).status_code == 302
    return response.location


def test_withholding_report_and_csv_exports(user_client):
    first = create_client(user_client, name="=Cliente Uno", tax_code="AAAAAA11A11A111A")
    second = create_client(user_client, name="Cliente Due", tax_code="BBBBBB22B22B222B", vat_number="IT99999999999")
    make_receipt(user_client, first, "1000", "2026-03-10")
    make_receipt(user_client, first, "500", "2026-03-20")
    make_receipt(user_client, second, "200", "2026-04-02")
    make_receipt(user_client, second, "999", "2026-04-03", issue=False)
    make_receipt(user_client, second, "300", "2025-12-30")

    page = user_client.get("/reports?year=2026").data.decode()
    assert "Report ritenute 2026" in page
    assert "€ 1.700,00" in page and "€ 340,00" in page
    assert "Marzo" in page and "Aprile" in page and "Dicembre" not in page

    months = user_client.get("/reports/withholding-months.csv?year=2026")
    assert months.headers["Content-Type"].startswith("text/csv")
    text = months.data.decode("utf-8-sig")
    assert "Marzo;2;1500,00;1500,00;300,00;1200,00" in text
    assert "Aprile;1;200,00;200,00;40,00;160,00" in text

    clients_csv = user_client.get("/reports/withholding-clients.csv?year=2026").data.decode("utf-8-sig")
    assert "'=Cliente Uno;AAAAAA11A11A111A" in clients_csv
    assert ";2;1500,00;1500,00;300,00;1200,00" in clients_csv

    receipts_csv = user_client.get("/reports/receipts.csv?year=2026").data.decode("utf-8-sig").splitlines()
    assert len(receipts_csv) == 1 + 3 + 1
    assert receipts_csv[-1].startswith("Totale;")
    assert user_client.get("/reports/unknown.csv?year=2026").status_code == 404
    assert user_client.get("/reports/receipts.csv?year=abc").status_code == 400


def test_statuses_are_shown_in_italian(user_client):
    client_id = create_client(user_client)
    make_receipt(user_client, client_id, "100", "2026-03-10")
    listing = user_client.get("/receipts").data.decode()
    assert "Emessa" in listing and ">issued<" not in listing
    quote = user_client.post("/quotes/new", data={
        "_csrf": csrf(user_client), "client_id": str(client_id), "issue_date": "2026-03-10", "withholding_rate": "0",
        "description[]": ["x"], "quantity[]": ["1"], "unit_price[]": ["10"], "discount[]": ["0"], "vat[]": ["0"], "withholding[]": ["0"],
    })
    listing = user_client.get("/quotes").data.decode()
    assert ">Bozza<" in listing and ">draft<" not in listing
    assert "Preventivo · Bozza" in user_client.get(quote.location).data.decode()


def test_invalid_quote_keeps_entered_data_and_marks_row(user_client):
    client_id = create_client(user_client)
    response = user_client.post("/quotes/new", data={
        "_csrf": csrf(user_client), "client_id": str(client_id), "issue_date": "2026-03-10", "withholding_rate": "0",
        "payment_terms": "60 giorni",
        "description[]": ["Prima voce", "Seconda voce"], "quantity[]": ["1", "2"], "unit_price[]": ["10", "20"],
        "discount[]": ["0", "150"], "vat[]": ["0", "22"], "withholding[]": ["0", "1"],
    })
    page = response.data.decode()
    assert response.status_code == 200
    assert "Sconto riga 2" in page
    assert 'value="Prima voce"' in page and 'value="Seconda voce"' in page and 'value="60 giorni"' in page
    assert page.count("has-error") == 1
    assert 'class="invalid" aria-invalid="true"' in page
    assert f'<option value="{client_id}" selected>' in page


def test_invalid_receipt_keeps_entered_data_and_marks_row(user_client):
    client_id = create_client(user_client)
    response = user_client.post("/receipts/new", data={
        "_csrf": csrf(user_client), "client_id": str(client_id), "issue_date": "2026-03-10", "subject": "Oggetto mio",
        "withholding_rate": "20", "contribution_rate": "0", "stamp_amount": "2", "stamp_threshold": "77.47",
        "amount_mode": "gross", "number": "", "description[]": ["Riga uno", ""], "amount[]": ["10", "5"], "kind[]": ["service", "expense"],
    })
    page = response.data.decode()
    assert "Riga 2" in page
    assert 'value="Oggetto mio"' in page and 'value="Riga uno"' in page
    assert page.count("has-error") == 1


def test_net_quote_pdf_explains_gross_amounts(user_client):
    client_id = create_client(user_client)
    response = user_client.post("/quotes/new", data={
        "_csrf": csrf(user_client), "client_id": str(client_id), "issue_date": "2026-03-10", "withholding_rate": "20",
        "amount_mode": "net", "description[]": ["x"], "quantity[]": ["1"], "unit_price[]": ["100"],
        "discount[]": ["0"], "vat[]": ["0"], "withholding[]": ["1"],
    })
    pdf = user_client.get(response.location + "/pdf")
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf.data)).pages)
    assert "Compenso lordo unit." in text and "€ 125.00" in text
    assert "importo netto da corrispondere sia quello concordato" in " ".join(text.split())


def test_invalid_client_keeps_contacts(user_client):
    response = user_client.post("/clients/new", data={
        "_csrf": csrf(user_client), "kind": "company", "company_name": "Azienda", "email": "non-valida@",
        "contact_first_name[]": ["Maria"], "contact_last_name[]": ["Rossi"], "contact_role[]": ["Amministrazione"],
        "contact_email[]": [""], "contact_phone[]": [""], "contact_mobile[]": [""],
    })
    page = response.data.decode()
    assert response.status_code == 200
    assert 'value="Maria"' in page and 'value="Rossi"' in page and 'value="Azienda"' in page
