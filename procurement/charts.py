"""Простой SVG-график спроса без внешних библиотек: столбцы — продажи из 1С,
линия — очищенный спрос, заштрихованы месяцы без товара, точки — исключённые разовые заказы."""
from __future__ import annotations

from html import escape

MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def demand_chart(months: list[str], raw: list[float], clean: list[float], stockout_idx: set[int],
                 one_off_idx: set[int], width: int = 820, height: int = 260) -> str:
    n = len(months)
    if n == 0:
        return ""
    pad_l, pad_r, pad_t, pad_b = 44, 12, 14, 40
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    top = max(max(raw or [0]), max(clean or [0]), 1) * 1.08
    step = w / n
    bw = step * 0.62

    def y(v):
        return pad_t + h - (v / top) * h

    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Спрос по месяцам">']
    # сетка
    for k in range(5):
        v = top * k / 4
        out.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="c-grid"/>')
        out.append(f'<text x="{pad_l - 6}" y="{y(v) + 4:.1f}" class="c-ax" text-anchor="end">{v:.0f}</text>')
    for i in range(n):
        x0 = pad_l + i * step
        if i in stockout_idx:
            out.append(f'<rect x="{x0:.1f}" y="{pad_t}" width="{step:.1f}" height="{h}" class="c-stockout"><title>Товара не было</title></rect>')
        bx = x0 + (step - bw) / 2
        cls = "c-bar c-bar-oneoff" if i in one_off_idx else "c-bar"
        out.append(f'<rect x="{bx:.1f}" y="{y(raw[i]):.1f}" width="{bw:.1f}" height="{max(y(0) - y(raw[i]), 0):.1f}" class="{cls}">'
                   f'<title>{escape(months[i])}: продажи {raw[i]:.0f}, очищенный спрос {clean[i]:.1f}</title></rect>')
        y_, m_ = months[i].split("-")
        if n <= 24 or i % 2 == 0:
            label = MONTHS[int(m_) - 1] + (f" {y_[2:]}" if m_ == "01" or i == 0 else "")
            out.append(f'<text x="{x0 + step / 2:.1f}" y="{height - pad_b + 16}" class="c-ax" text-anchor="middle">{label}</text>')
    pts = " ".join(f"{pad_l + i * step + step / 2:.1f},{y(v):.1f}" for i, v in enumerate(clean))
    out.append(f'<polyline points="{pts}" class="c-line"/>')
    out.append("</svg>")
    return "".join(out)
