import re

from django import template

register = template.Library()


@register.filter
def pct(v):
    try:
        return f"{float(v) * 100:.1f}%".replace(".", ",")
    except (TypeError, ValueError):
        return "—"


@register.filter
def pct_signed(v):
    try:
        return f"{float(v) * 100:+.1f}%".replace(".", ",").replace("-", "−")
    except (TypeError, ValueError):
        return "—"


@register.filter
def num(v):
    """12345 -> «12 345»."""
    try:
        return f"{float(v):,.0f}".replace(",", "\u202f")
    except (TypeError, ValueError):
        return "—"


@register.filter
def money(v):
    """Сумма в тенге: 18699041 -> «18,7 млн ₸»."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f} млн ₸".replace(".", ",")
    if abs(v) >= 1_000:
        return f"{v / 1_000:.0f} тыс. ₸"
    return f"{v:.0f} ₸"


@register.filter
def days(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if v >= 9999:
        return "нет продаж"
    if v >= 365:
        return "больше года"
    if v < 1:
        return "уже нет"
    n = int(round(v))
    if n % 10 == 1 and n % 100 != 11:
        word = "день"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "дня"
    else:
        word = "дней"
    return f"{n} {word}"


@register.filter
def sentences(text):
    """Обоснование одной строкой -> список предложений (не режем «накл. 2000…», «шт/мес.» и т.п.)."""
    if not text:
        return []
    parts = re.split(r"(?<=[.!])\s+(?=[А-ЯЁA-Z])", str(text).strip())
    return [p for p in (x.strip() for x in parts) if p]


@register.filter
def get_item(d, key):
    try:
        return d.get(key)
    except AttributeError:
        return None
