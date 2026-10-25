from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


CENT = Decimal("0.01")


class LineError(ValueError):
    """Validation error tied to a specific line (0-based) and form field."""

    def __init__(self, message, row=None, field=None):
        super().__init__(message)
        self.row = row
        self.field = field


def decimal_value(value: str, field: str, *, minimum=None, maximum=None) -> Decimal:
    try:
        number = Decimal(value.replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"{field}: inserisci un numero valido.") from None
    if not number.is_finite():
        raise ValueError(f"{field}: numero non valido.")
    if minimum is not None and number < Decimal(str(minimum)):
        raise ValueError(f"{field}: valore troppo basso.")
    if maximum is not None and number > Decimal(str(maximum)):
        raise ValueError(f"{field}: valore troppo alto.")
    return number


def cents(value: Decimal) -> int:
    return int((value.quantize(CENT, rounding=ROUND_HALF_UP) * 100).to_integral_value())


def money(cents_value: int) -> Decimal:
    return (Decimal(cents_value) / 100).quantize(CENT)


def _rate_cents(base_cents: int, rate_bp: int) -> int:
    return int(
        (Decimal(base_cents) * Decimal(rate_bp) / Decimal(10000))
        .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def hydrate_quote_line(item):
    """Add gross, discount and net amounts (in cents) to a stored quote line."""
    gross = cents(Decimal(str(item["quantity"])) * money(item["unit_price_cents"]))
    discount = _rate_cents(gross, item["discount_bp"])
    item["gross_cents"] = gross
    item["discount_cents"] = discount
    item["net_cents"] = gross - discount
    return item


def _line_value(row, field, label, value, **limits):
    try:
        return decimal_value(value if value not in (None, "") else "0", f"{label} riga {row + 1}", **limits)
    except ValueError as exc:
        raise LineError(str(exc), row, field) from None


def parse_quote_lines(form, amount_mode: str = "gross", withholding_rate: str = "0"):
    if amount_mode not in {"gross", "net"}:
        raise ValueError("Scegli se i prezzi inseriti sono lordi o netti.")
    rate_bp = int(decimal_value(withholding_rate or "0", "Ritenuta", minimum=0, maximum=100) * 100)
    if amount_mode == "net" and rate_bp >= 10000:
        raise ValueError("Non è possibile calcolare un importo netto con una ritenuta del 100%.")
    descriptions = form.getlist("description[]")
    quantities = form.getlist("quantity[]")
    prices = form.getlist("unit_price[]")
    discounts = form.getlist("discount[]")
    vat_rates = form.getlist("vat[]")
    withholding_flags = form.getlist("withholding[]")
    if not descriptions or len(descriptions) > 100:
        raise ValueError("Inserisci da 1 a 100 righe.")
    if not (len(descriptions) == len(quantities) == len(prices) == len(discounts) == len(vat_rates) == len(withholding_flags)):
        raise ValueError("Le righe del preventivo non sono valide.")
    lines = []
    for position, (description, quantity, price, discount, vat, withholding) in enumerate(
        zip(descriptions, quantities, prices, discounts, vat_rates, withholding_flags)
    ):
        description = description.strip()
        if not description or len(description) > 500:
            raise LineError(f"Riga {position + 1}: descrizione obbligatoria (massimo 500 caratteri).", position, "description")
        qty = _line_value(position, "quantity", "Quantità", quantity, minimum="0.001", maximum="1000000")
        unit_price = cents(_line_value(position, "unit_price", "Prezzo", price, minimum=0, maximum="100000000"))
        entered_unit_price = unit_price
        if amount_mode == "net" and withholding == "1" and rate_bp:
            unit_price = int(
                (Decimal(unit_price) * Decimal(10000) / Decimal(10000 - rate_bp))
                .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
        discount_bp = int(_line_value(position, "discount", "Sconto", discount, minimum=0, maximum=100) * 100)
        vat_bp = int(_line_value(position, "vat", "IVA", vat, minimum=0, maximum=100) * 100)
        gross = cents(qty * money(unit_price))
        discount_cents = _rate_cents(gross, discount_bp)
        net = gross - discount_cents
        lines.append({
            "position": position,
            "description": description,
            "quantity": str(qty),
            "unit_price_cents": unit_price,
            "entered_unit_price_cents": entered_unit_price,
            "discount_bp": discount_bp,
            "vat_bp": vat_bp,
            "withholding": int(withholding == "1"),
            "gross_cents": gross,
            "discount_cents": discount_cents,
            "net_cents": net,
        })
    return lines


def quote_totals(lines, withholding_rate: str):
    rate_bp = int(decimal_value(withholding_rate, "Ritenuta", minimum=0, maximum=100) * 100)
    subtotal = sum(line["gross_cents"] for line in lines)
    discount = sum(line["discount_cents"] for line in lines)
    taxable = sum(line["net_cents"] for line in lines)
    vat = sum(_rate_cents(line["net_cents"], line["vat_bp"]) for line in lines)
    withholding_base = sum(line["net_cents"] for line in lines if line["withholding"])
    withholding = _rate_cents(withholding_base, rate_bp)
    total = taxable + vat
    return {
        "subtotal_cents": subtotal,
        "discount_cents": discount,
        "taxable_cents": taxable,
        "vat_cents": vat,
        "withholding_base_cents": withholding_base,
        "withholding_cents": withholding,
        "total_cents": total,
        "net_due_cents": total - withholding,
        "withholding_rate_bp": rate_bp,
    }


def parse_receipt_lines(form):
    descriptions = form.getlist("description[]")
    amounts = form.getlist("amount[]")
    kinds = form.getlist("kind[]")
    if not descriptions or len(descriptions) > 100 or not (len(descriptions) == len(amounts) == len(kinds)):
        raise ValueError("Inserisci da 1 a 100 righe valide.")
    lines = []
    for pos, (description, amount, kind) in enumerate(zip(descriptions, amounts, kinds)):
        description = description.strip()
        if not description or len(description) > 500:
            raise LineError(f"Riga {pos + 1}: descrizione obbligatoria (massimo 500 caratteri).", pos, "description")
        if kind not in {"service", "expense"}:
            raise LineError(f"Riga {pos + 1}: tipo di riga non valido.", pos, "kind")
        amount_cents = cents(_line_value(pos, "amount", "Importo", amount, minimum=0, maximum="100000000"))
        lines.append({"position": pos, "description": description, "amount_cents": amount_cents, "kind": kind})
    return lines


def receipt_totals(
    lines,
    withholding_rate: str,
    contribution_rate: str,
    stamp_amount: str,
    stamp_threshold: str,
    amount_mode: str = "gross",
):
    withholding_bp = int(decimal_value(withholding_rate, "Ritenuta", minimum=0, maximum=100) * 100)
    contribution_bp = int(decimal_value(contribution_rate, "Contributo", minimum=0, maximum=100) * 100)
    if amount_mode not in {"gross", "net"}:
        raise ValueError("Scegli se gli importi inseriti sono lordi o netti.")
    entered_service = sum(line["amount_cents"] for line in lines if line["kind"] == "service")
    service = entered_service
    if amount_mode == "net":
        service = _gross_up_net(entered_service, contribution_bp, withholding_bp)
    expenses = sum(line["amount_cents"] for line in lines if line["kind"] == "expense")
    contribution = _rate_cents(service, contribution_bp)
    base = service + contribution
    withholding = _rate_cents(base, withholding_bp)
    stamp = cents(decimal_value(stamp_amount, "Bollo", minimum=0, maximum=1000)) if money(service) > decimal_value(stamp_threshold, "Soglia bollo", minimum=0, maximum=1000000) else 0
    return {
        "services_cents": service,
        "entered_services_cents": entered_service,
        "expenses_cents": expenses,
        "contribution_cents": contribution,
        "withholding_base_cents": base,
        "withholding_cents": withholding,
        "stamp_cents": stamp,
        "gross_cents": service + expenses + contribution + stamp,
        "net_due_cents": service + expenses + contribution + stamp - withholding,
        "withholding_rate_bp": withholding_bp,
        "contribution_rate_bp": contribution_bp,
        "amount_mode": amount_mode,
    }


def _gross_up_net(net_cents: int, contribution_bp: int, withholding_bp: int) -> int:
    if net_cents == 0:
        return 0
    net_factor = Decimal(10000 + contribution_bp) * Decimal(10000 - withholding_bp) / Decimal(100000000)
    if net_factor <= 0:
        raise ValueError("Non è possibile calcolare un importo netto con una ritenuta del 100%.")
    estimate = int(
        (Decimal(net_cents) / net_factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    if estimate > 10_000_000_000:
        raise ValueError("Il compenso lordo calcolato supera il limite consentito.")

    def payable(gross_cents):
        contribution = _rate_cents(gross_cents, contribution_bp)
        return gross_cents + contribution - _rate_cents(gross_cents + contribution, withholding_bp)

    candidates = range(max(0, estimate - 4), estimate + 5)
    return min(candidates, key=lambda value: (abs(payable(value) - net_cents), value))
