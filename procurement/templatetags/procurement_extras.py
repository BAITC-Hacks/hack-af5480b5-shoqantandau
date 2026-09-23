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
