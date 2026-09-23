"""Расчёт рекомендованных заказов поставщику.

Методика (для каждого артикула):
 1. Спрос по месяцам берётся из месячного отчёта 1С. Текущий неполный месяц в историю не входит.
 2. Разовые крупные заказы. По построчным накладным для артикула считается типичный размер строки
    (медиана) и разброс (MAD). Строка считается разовым крупным заказом, если одновременно:
      - больше медиана + k * 1.4826 * MAD (и не меньше 3 медиан),
      - больше 1.5 * медианы ненулевых месячных продаж (одна накладная больше обычного месяца),
      - даёт не меньше OUTLIER_MONTH_SHARE продаж своего месяца.
    Из спроса месяца вычитается превышение над типичной строкой (qty - медиана).
 3. Упущенный спрос. Месяц считается месяцем дефицита, если остаток на начало месяца или на начало
    следующего месяца <= 0 (товар кончился в течение месяца). Спрос такого месяца заменяется на
    max(факт, ожидаемый), где ожидаемый = базовый спрос по месяцам без дефицита x сезонный индекс.
 4. Сезонность. Индекс по месяцу года: по поставщику — отношение продаж к центрированной
    12-месячной скользящей средней (так тренд не искажает сезонность); по артикулу — то же на его
    очищенном спросе, если истории достаточно. Итоговый индекс — смесь артикула и поставщика.
 5. Рост. Отношение последних 12 месяцев к предыдущим 12 (артикул, смешанный с поставщиком),
    либо плановый прирост, заданный менеджером.
 6. База = среднее десезонализированного очищенного спроса за HISTORY_MONTHS месяцев.
 7. Прогноз на горизонт (срок поставки + период между заказами) = база x сезонность x рост по дням.
    Страховой запас = z x sigma x sqrt(срок поставки / 30), sigma = max(1.4826*MAD, sqrt(база)) —
    устойчива к выбросам.
 8. К заказу = прогноз + страховой запас - остаток - в пути, округлено вверх до кратности.
 9. Срочность: сколько дней хватит остатка (+ товара, который придёт до новой поставки)
    в сравнении со сроком поставки.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

import constants

from .loaders import SupplierData

MONTH_NAMES = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


@dataclass
class Params:
    lead_time_days: int | None = None          # None = из данных поставщика
    review_days: int = constants.REVIEW_PERIOD_DAYS
    service_z: float = constants.SERVICE_LEVEL_Z
    history_months: int = constants.HISTORY_MONTHS
    outlier_k: float = constants.OUTLIER_MAD_K
    outlier_share: float = constants.OUTLIER_MONTH_SHARE
    use_outliers: bool = True
    use_stockout: bool = True
    use_seasonality: bool = True
    use_growth: bool = True
    growth_plan_pct: float | None = None       # плановый прирост, % г/г; None = по истории
    category: str = ""                         # фильтр по группе (первые цифры кода 1С)
    codes: list[str] = field(default_factory=list)  # расчёт только по этим артикулам
    # для проверки экспертами: подмешать тестовые продажи и переопределить остатки/в пути
    test_orders: list[dict] = field(default_factory=list)   # [{code, qty, date, doc}]
    overrides: dict[str, dict] = field(default_factory=dict)  # {code: {stock_now, in_transit}}

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["test_orders"] = [{**o, "date": str(o.get("date"))} for o in self.test_orders]
        return d


# ---------- сезонность и рост на уровне поставщика ----------

def supplier_seasonal_index(season: pd.DataFrame) -> np.ndarray:
    """Индекс по 12 месяцам (среднее = 1) по отношению к центрированной 12-мес. скользящей."""
    s = season.stack().sort_index()
    s.index = [pd.Period(year=y, month=m, freq="M") for y, m in s.index]
    s = s[s > 0]
    if len(s) < 18:
        return np.ones(12)
    ma = s.rolling(12, center=True).mean().rolling(2).mean().shift(-1)  # 2x12 MA
    ratio = (s / ma).dropna()
    idx = np.ones(12)
    for m in range(1, 13):
        vals = ratio[[p.month == m for p in ratio.index]]
        if len(vals):
            idx[m - 1] = vals.mean()
    return idx / idx.mean()


def supplier_growth(season: pd.DataFrame, last_full: pd.Period) -> float:
    s = season.stack().sort_index()
    s.index = [pd.Period(year=y, month=m, freq="M") for y, m in s.index]
    s = s[s.index <= last_full]
    if len(s) < 24:
        return 1.0
    return float(np.clip(s.iloc[-12:].sum() / max(s.iloc[-24:-12].sum(), 1), 0.8, 1.25))


# ---------- разовые крупные заказы ----------

def detect_one_offs(tx: pd.DataFrame, monthly_sales: pd.Series, k: float, share: float) -> pd.DataFrame:
    """tx — строки одного артикула. Возвращает строки-выбросы с колонкой excess."""
    if tx.empty:
        return tx.assign(excess=[])
    lines = tx.groupby(["doc", "date"], as_index=False)["qty"].sum()
    if len(lines) < 5:
        return lines.iloc[0:0].assign(excess=[])
    med = lines["qty"].median()
    mad = (lines["qty"] - med).abs().median() * 1.4826
    doc_thr = max(med + k * max(mad, 1.0), 3 * med)
    nz = monthly_sales[monthly_sales > 0]
    month_thr = 1.5 * (nz.median() if len(nz) else 0)
    lines["month"] = lines["date"].dt.to_period("M")
    month_tot = lines.groupby("month")["qty"].transform("sum")
    flag = (lines["qty"] > doc_thr) & (lines["qty"] > month_thr) & (lines["qty"] >= share * month_tot)
    out = lines[flag].copy()
    out["excess"] = out["qty"] - med
    out["typical"] = med
    return out


def _fmt_need(need: float) -> str:
    need = max(need, 0.0)
    return f"{need:.1f}" if 0 < need < 10 else f"{need:.0f}"


def robust_sigma(x: np.ndarray, level: float) -> float:
    """Разброс месячного спроса, устойчивый к выбросам: 1.4826*MAD, но не меньше sqrt(уровня)
    (пуассоновский минимум — у штучного спроса разброс не бывает нулевым)."""
    if len(x) == 0:
        return 0.0
    mad = float(np.median(np.abs(x - np.median(x)))) * 1.4826
    return max(mad, math.sqrt(max(level, 0.0)))


# ---------- прогноз по дням ----------

def horizon_forecast(daily_base: float, start: date, days: int, idx: np.ndarray, growth: float) -> tuple[float, float]:
    """Сумма спроса на [start, start+days) и средний сезонный множитель горизонта."""
    total, weight = 0.0, 0.0
    d = start
    end = start + timedelta(days=days)
    while d < end:
        nxt = min(date(d.year + (d.month == 12), d.month % 12 + 1, 1), end)
        n = (nxt - d).days
        total += daily_base * idx[d.month - 1] * n
        weight += idx[d.month - 1] * n
        d = nxt
    return total * growth, weight / max(days, 1)


# ---------- основной расчёт ----------

def _build_matrix(data: SupplierData, months: list[pd.Period], col: str) -> pd.DataFrame:
    m = data.monthly[data.monthly["month"].isin(months)]
    return m.pivot_table(index="code", columns="month", values=col, aggfunc="sum").reindex(columns=months).fillna(0.0)


def calculate(data: SupplierData, params: Params | None = None) -> pd.DataFrame:
    p = params or Params()
    today = data.data_date
    cur = pd.Period(today, freq="M")
    last_full = cur - 1
    months = list(pd.period_range(last_full - (max(p.history_months, 24) - 1), last_full, freq="M"))
    hist = months[-p.history_months:]
    lead = int(p.lead_time_days or data.lead_time_days)
    horizon = lead + int(p.review_days)

    tx = data.transactions
    sales = _build_matrix(data, months, "sales_1c")
    stock_open = _build_matrix(data, months + [cur], "stock_open")

    # тестовые разовые заказы (для проверки): добавляются и в накладные, и в месячный спрос
    if p.test_orders:
        extra = pd.DataFrame([{"date": pd.Timestamp(o.get("date") or pd.Timestamp(last_full.start_time) + pd.Timedelta(days=14)),
                               "doc": o.get("doc", "ТЕСТ"), "code": o["code"], "name": "",
                               "warehouse": "", "qty": float(o["qty"])} for o in p.test_orders])
        tx = pd.concat([tx, extra], ignore_index=True)
        for _, o in extra.iterrows():
            mo = o["date"].to_period("M")
            if o["code"] in sales.index and mo in sales.columns:
                sales.loc[o["code"], mo] += o["qty"]

    items = data.items.copy()
    for code, ov in p.overrides.items():
        for k_, v in ov.items():
            if code in items.index and v is not None:
                items.loc[code, k_] = float(v)
    if p.category:
        items = items[items["category"].astype(str).str.startswith(p.category)]
    if p.codes:
        items = items[items.index.isin(p.codes)]

    sup_idx = supplier_seasonal_index(data.seasonality) if p.use_seasonality else np.ones(12)
    sup_growth = supplier_growth(data.seasonality, last_full) if p.use_growth else 1.0
    tx_by_code = {c: g for c, g in tx.groupby("code")} if not tx.empty else {}
    cal = np.array([m.month for m in months])
    hist_mask = np.isin(np.arange(len(months)), np.arange(len(months) - len(hist), len(months)))

    rows = []
    for code, it in items.iterrows():
        raw = sales.loc[code].to_numpy() if code in sales.index else np.zeros(len(months))
        if raw[hist_mask].sum() <= 0:
            continue  # нет продаж за окно — не заказываем
        clean = raw.copy()
        reasons, details = [], {"months": [str(m) for m in months], "raw": raw.tolist()}

        # 2. разовые крупные заказы
        one_offs = []
        if p.use_outliers and code in tx_by_code:
            oo = detect_one_offs(tx_by_code[code][tx_by_code[code]["date"].dt.to_period("M").isin(months)],
                                 pd.Series(raw), p.outlier_k, p.outlier_share)
            for _, r in oo.iterrows():
                i = months.index(r["month"])
                removed = min(r["excess"], clean[i])
                clean[i] -= removed
                one_offs.append({"doc": r["doc"], "date": r["date"].strftime("%d.%m.%Y"), "qty": float(r["qty"]),
                                 "removed": float(removed), "month": str(r["month"]), "in_window": bool(hist_mask[i])})

        # сезонный индекс артикула (на очищенном спросе), смешанный с индексом поставщика
        idx = sup_idx.copy()
        if p.use_seasonality and clean.sum() >= 100 and (clean > 0).sum() >= 12 and len(months) >= 24:
            by_m = np.array([clean[cal == m].mean() for m in range(1, 13)])
            if by_m.mean() > 0:
                sku_idx = np.clip(by_m / by_m.mean(), 0.3, 3.0)
                idx = 0.5 * sku_idx + 0.5 * sup_idx
                idx = idx / idx.mean()
        season = idx[cal - 1]

        # 3. упущенный спрос в периоды дефицита
        stockout_months, added = [], 0.0
        so_open = stock_open.loc[code].to_numpy() if code in stock_open.index else np.ones(len(months) + 1)
        is_so = (so_open[:-1] <= 0) | (so_open[1:] <= 0)
        ok = ~is_so & (clean >= 0)
        deseason = clean / season
        base_ok = deseason[ok & hist_mask].mean() if (ok & hist_mask).any() else (
            deseason[ok].mean() if ok.any() else deseason.mean())
        if p.use_stockout and ok.any():
            for i in np.where(is_so)[0]:
                expected = base_ok * season[i]
                if expected > clean[i]:
                    added_i = expected - clean[i]
                    clean[i] = expected
                    if hist_mask[i]:
                        added += added_i
                        stockout_months.append(f"{MONTH_NAMES[months[i].month - 1]} {months[i].year}")

        # 6. база и разброс
        des_h = (clean / season)[hist_mask]
        base = float(des_h.mean())
        sigma = robust_sigma(des_h, base)

        # 5. рост
        if p.growth_plan_pct is not None:
            g_year, g_src = 1 + p.growth_plan_pct / 100, "плановый"
        elif p.use_growth and len(months) >= 24 and clean[:12].sum() > 0:
            g_sku = float(np.clip(clean[-12:].sum() / clean[:12].sum(), 0.5, 1.5))
            g_year, g_src = 0.5 * g_sku + 0.5 * sup_growth, "по истории"
        else:
            g_year, g_src = 1.0, "не учитывается"
        # база — средний уровень за окно (середина окна ~ history/2 мес. назад), прогноз — середина горизонта
        lag_months = p.history_months / 2 + horizon / 30 / 2
        growth = g_year ** (lag_months / 12) if g_year > 0 else 1.0

        # 7. прогноз и страховой запас
        forecast, season_h = horizon_forecast(base / 30.0, today, horizon, idx, growth)
        safety = p.service_z * sigma * math.sqrt(lead / 30.0)

        # 8. потребность
        stock_now = max(float(it["stock_now"] or 0), 0.0)
        in_transit = max(float(it["in_transit"] or 0), 0.0)
        need = forecast + safety - stock_now - in_transit
        moq = max(float(it["moq"] or 1), 1.0)
        qty = int(math.ceil(need / moq) * moq) if need > 0 else 0

        # 9. срочность
        daily_now = base / 30.0 * idx[today.month - 1] * growth
        cover = stock_now / daily_now if daily_now > 0 else 9999.0
        arrival = it.get("next_arrival")
        cover_tr = cover + (in_transit / daily_now if daily_now > 0 and pd.notna(arrival)
                            and pd.Timestamp(arrival).date() <= today + timedelta(days=lead) else 0)
        urgency = "high" if cover_tr < lead else ("medium" if cover_tr < horizon else "low")

        # тот же расчёт по «сырым» продажам — без исключения разовых заказов и без учёта дефицита
        des_raw = (raw / season)[hist_mask]
        f_raw, _ = horizon_forecast(float(des_raw.mean()) / 30.0, today, horizon, idx, growth)
        s_raw = p.service_z * robust_sigma(des_raw, float(des_raw.mean())) * math.sqrt(lead / 30.0)
        need_raw = f_raw + s_raw - stock_now - in_transit
        naive_qty = int(math.ceil(need_raw / moq) * moq) if need_raw > 0 else 0

        # обоснование
        oo_in = [o for o in one_offs if o["in_window"]]
        reasons.append(f"Регулярный спрос {base:.1f} {it['unit']}/мес в среднем за {p.history_months} мес. "
                       f"(с учётом сезона сейчас ≈ {base * idx[today.month - 1]:.1f}).")
        if oo_in:
            tot = sum(o["removed"] for o in oo_in)
            docs = "; ".join(f"накл. {o['doc']} от {o['date']} — {o['qty']:.0f}" for o in oo_in[:3])
            reasons.append(f"Исключены разовые крупные заказы ({docs}): из спроса убрано {tot:.0f} сверх обычной строки.")
        if stockout_months:
            add_txt = f"{added:.0f}" if added >= 1 else f"{added:.1f}"
            reasons.append(f"Товара не было: {', '.join(stockout_months)} — упущенный спрос восстановлен на +{add_txt}.")
        if p.use_seasonality:
            reasons.append(f"Сезонность на горизонте ×{season_h:.2f}.")
        if g_src != "не учитывается":
            reasons.append(f"Рост {g_src}: {(g_year - 1) * 100:+.0f}% г/г (×{growth:.2f} к базе).")
        reasons.append(f"Прогноз на {horizon} дн. (поставка {lead} + цикл заказа {p.review_days}) = {forecast:.0f}; "
                       f"страховой запас {safety:.0f}; остаток {stock_now:.0f}; в пути {in_transit:.0f} "
                       f"→ потребность {_fmt_need(need)}" + (f", округлено до кратности {moq:.0f} → {qty}." if moq > 1 else f" → {qty}."))
        if naive_qty != qty and (oo_in or stockout_months):
            reasons.append(f"Без очистки данных рекомендация была бы {naive_qty}.")

        details.update({"clean": clean.tolist(), "season_idx": idx.tolist(), "one_offs": one_offs,
                        "stockout_months": stockout_months, "growth_year": g_year, "lead": lead,
                        "horizon": horizon, "naive_qty": float(naive_qty), "sigma": sigma})
        rows.append({
            "supplier": data.key, "supplier_name": data.name, "code_1c": code, "article": it["article"],
            "name": it["name"], "category": it["category"], "abc_class": it.get("abc_class", ""),
            "unit": it["unit"], "stock": stock_now, "in_transit": in_transit,
            "regular_demand": base * idx[today.month - 1], "forecast": forecast, "safety_stock": safety,
            "moq": moq, "qty_recommended": qty, "qty_raw": naive_qty, "urgency": urgency, "days_of_cover": min(cover, 9999.0),
            "reason": " ".join(reasons), "details": details,
            "one_off_removed": sum(o["removed"] for o in oo_in), "stockout_added": added,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    urg_order = {"high": 0, "medium": 1, "low": 2}
    df["_u"] = df["urgency"].map(urg_order)
    return df.sort_values(["supplier", "_u", "days_of_cover"]).drop(columns="_u").reset_index(drop=True)
