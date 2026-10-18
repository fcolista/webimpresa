from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image, UnidentifiedImageError


PROVIDER_FIELDS = (
    "provider_name",
    "provider_tax_code",
    "provider_vat",
    "provider_address",
    "provider_postal_code",
    "provider_city",
    "provider_province",
    "provider_country",
    "provider_email",
    "provider_phone",
    "provider_pec",
    "provider_recipient_code",
    "provider_bank_name",
    "provider_account_holder",
    "provider_iban",
    "provider_bic",
)

PROVIDER_IMAGE_FIELDS = ("logo_data", "signature_data")
MAX_LOGO_BYTES = 1_000_000


def provider_snapshot(settings):
    return {
        **{key: settings.get(key, "") for key in PROVIDER_FIELDS},
        "logo_data": settings.get("provider_logo", ""),
        "signature_data": settings.get("provider_signature", ""),
    }


def clean_logo(upload):
    content = upload.read(MAX_LOGO_BYTES + 1)
    if len(content) > MAX_LOGO_BYTES:
        raise ValueError("Il logo non può superare 1 MB.")
    if not content:
        raise ValueError("Il file del logo è vuoto.")
    try:
        with Image.open(BytesIO(content)) as source:
            if source.format not in {"PNG", "JPEG"}:
                raise ValueError("Carica il logo in formato PNG o JPEG.")
            if source.width * source.height > 4_000_000:
                raise ValueError("Le dimensioni del logo sono troppo grandi.")
            source.verify()
        with Image.open(BytesIO(content)) as source:
            source.load()
            source.thumbnail((1400, 600), Image.Resampling.LANCZOS)
            has_alpha = "A" in source.getbands() or "transparency" in source.info
            image = source.convert("RGBA" if has_alpha else "RGB")
            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Il file del logo non è un'immagine PNG o JPEG valida.") from exc
    cleaned = output.getvalue()
    if len(cleaned) > 1_500_000:
        raise ValueError("Il logo ottimizzato supera il limite consentito.")
    return base64.b64encode(cleaned).decode("ascii")


def decode_logo(encoded):
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Il logo salvato non è valido.") from exc


def client_label(client):
    keys = client.keys()
    name = client["name"] if "name" in keys else ""
    kind = client["kind"] if "kind" in keys else ""
    surname = client["surname"] if "surname" in keys and kind != "company" else ""
    return " ".join(part for part in (name, surname) if part)
