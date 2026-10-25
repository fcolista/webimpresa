import re

from flask import g

from .db import get_db

STATUS_LABELS = {
    "draft": "Bozza",
    "sent": "Inviato",
    "accepted": "Accettato",
    "rejected": "Rifiutato",
    "expired": "Scaduto",
    "issued": "Emessa",
}
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def get_settings():
    rows = get_db().execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def valid_color(value: str) -> bool:
    return bool(COLOR_RE.fullmatch(value or ""))


def contrast_ratio(foreground: str, background: str) -> float:
    def luminance(color):
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    first, second = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (first + 0.05) / (second + 0.05)


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def current_user_id():
    user = g.get("user")
    return user["id"] if user else None


def status_label(value):
    return STATUS_LABELS.get(value, value)
