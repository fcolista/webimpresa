import pytest

from app.finance import quote_totals, receipt_totals


def test_quote_amounts_use_half_up_integer_cents():
    totals = quote_totals([{
        "gross_cents": 15002,
        "discount_cents": 0,
        "net_cents": 15002,
        "vat_bp": 2200,
        "withholding": 1,
    }], "20")

    assert totals == {
        "subtotal_cents": 15002,
        "discount_cents": 0,
        "taxable_cents": 15002,
        "vat_cents": 3300,
        "withholding_base_cents": 15002,
        "withholding_cents": 3000,
        "total_cents": 18302,
        "net_due_cents": 15302,
        "withholding_rate_bp": 2000,
    }


def test_receipt_calculates_configured_contribution_stamp_and_reimbursement():
    totals = receipt_totals([
        {"amount_cents": 12345, "kind": "service"},
        {"amount_cents": 5000, "kind": "expense"},
    ], "20", "4", "2", "77.47")

    assert totals["contribution_cents"] == 494
    assert totals["withholding_base_cents"] == 12839
    assert totals["withholding_cents"] == 2568
    assert totals["stamp_cents"] == 200
    assert totals["net_due_cents"] == 15471


def test_receipt_stamp_uses_strictly_greater_threshold():
    totals = receipt_totals([{"amount_cents": 7747, "kind": "service"}], "20", "0", "2", "77.47")
    assert totals["stamp_cents"] == 0


def test_net_service_amount_is_grossed_up_before_contribution_and_withholding():
    totals = receipt_totals([
        {"amount_cents": 10000, "kind": "service"},
        {"amount_cents": 5000, "kind": "expense"},
    ], "20", "4", "2", "77.47", "net")

    assert totals["entered_services_cents"] == 10000
    assert totals["services_cents"] == 12019
    assert totals["contribution_cents"] == 481
    assert totals["withholding_cents"] == 2500
    assert totals["net_due_cents"] == 15200
    assert totals["amount_mode"] == "net"


def test_net_amount_is_rejected_when_full_withholding_makes_gross_up_impossible():
    with pytest.raises(ValueError, match="ritenuta del 100%"):
        receipt_totals([{"amount_cents": 10000, "kind": "service"}], "100", "0", "2", "77.47", "net")
