from tests.conftest import csrf
from tests.test_operations import create_client


def test_net_quote_grosses_up_and_edit_keeps_single_record(user_client, app):
    cid = create_client(user_client)
    def data(price):
        return {"_csrf": csrf(user_client), "client_id": str(cid), "issue_date": "2026-10-16", "withholding_rate": "20",
                "amount_mode": "net", "description[]": ["x"], "quantity[]": ["1"], "unit_price[]": [price],
                "discount[]": ["0"], "vat[]": ["0"], "withholding[]": ["1"]}
    r = user_client.post("/quotes/new", data=data("100"))
    import json
    from app.db import get_db
    with app.app_context():
        totals = json.loads(get_db().execute("SELECT totals FROM quotes").fetchone()[0])
    assert totals["net_due_cents"] == 10000 and totals["taxable_cents"] == 12500
    page = user_client.get(r.location + "/edit").data.decode()
    assert 'value="100.00"' in page
    assert user_client.post(r.location + "/edit", data=data("200")).status_code == 302
    with app.app_context():
        rows = get_db().execute("SELECT number,totals FROM quotes").fetchall()
    assert len(rows) == 1 and json.loads(rows[0][1])["net_due_cents"] == 20000
