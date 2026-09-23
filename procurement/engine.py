"""Расчёт рекомендованных заказов поставщику.

Методика (для каждого артикула):
 1. Спрос по месяцам берётся из месячного отчёта 1С. Текущий неполный месяц в историю не входит.
 2. Разовые крупные заказы. По построчным накладным для артикула считается типичный размер строки
    (медиана) и разброс (MAD). Строка считается разовым крупным заказом, если одновременно:
      - больше медиана + k * 1.4826 * MAD (и не меньше 3 медиан),
      - больше 1.5 * медианы ненулевых месячных продаж (одна накладная больше обычного месяца),
      - даёт не меньше OUTLIER_MONTH_SHARE продаж своего месяца.
    Из спроса месяца вычитается превышение над типичной строкой (qty - медиана).
    Второй уровень — всплески в месячном отчёте без крупной накладной (фильтр Хампеля): месяц,
    который после снятия сезонности больше медиана + MONTH_SPIKE_K * MAD ненулевых месяцев и больше
    MONTH_SPIKE_RATIO медиан, заменяется типичным месяцем (медиана x сезонность).
 3. Упущенный спрос. Месяц считается месяцем дефицита, если остаток на начало месяца или на начало
    следующего месяца <= 0 (товар кончился в течение месяца). Спрос такого месяца заменяется на
    max(факт, ожидаемый), где ожидаемый = базовый спрос по месяцам без дефицита x сезонный индекс.
    Не восстанавливается, если товара нет LONG_STOCKOUT_MONTHS+ месяцев подряд до текущей даты
    (похоже, позиция не закупается) или в названии пометка «!!!» — такие позиции помечаются «проверить».
 4. Сезонность. Индекс по месяцу года: по поставщику — отношение продаж к центрированной
    12-месячной скользящей средней; по артикулу — средние очищенные продажи по месяцам года,
    если истории достаточно; по группе товаров — скользящая средняя на суммарных
    продажах группы. Итог — смесь: артикул 50%, группа 25%, поставщик 25% (без своей истории —
    группа и поставщик поровну).
 5. Рост. Отношение последних 12 месяцев к предыдущим 12 — смесь артикула, группы и поставщика
    в тех же долях, либо плановый прирост, заданный менеджером.
 6. База = среднее десезонализированного очищенного спроса за HISTORY_MONTHS месяцев.
 7. Прогноз на горизонт (срок поставки + период между заказами) = база x сезонность x рост по дням.
    Страховой запас = z x sigma x sqrt(горизонт / 30), sigma = max(1.4826*MAD, sqrt(база)) —
    устойчива к выбросам.
 8. К заказу = прогноз + страховой запас - остаток - поставки в горизонте, вверх до кратности.
 9. Срочность: первая нехватка при дневном списании спроса и поступлениях в их даты.
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
    lead_time_days: int | None = None          # None = из справочника / данных поставщика
    lead_times: dict[str, int] = field(default_factory=dict)  # срок по поставщику {key: дней}
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
    test_orders: list[dict] = field(default_factory=list)   # [{code, qty, date, doc, monthly_only}]
    overrides: dict[str, dict] = field(default_factory=dict)  # {code: {stock_now, in_transit}}

    def validate(self):
        for label, value, low, high in [
            ("Период между заказами", self.review_days, 0, 365),
            ("История", self.history_months, 1, 36),
            ("Страховой коэффициент", self.service_z, 0, 4),
            ("Порог выбросов", self.outlier_k, 1, 20),
            ("Доля крупного заказа", self.outlier_share, 0, 1),
        ]:
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{label}: допустимо от {low} до {high}.")
        for value in [self.lead_time_days, *self.lead_times.values()]:
            if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)
                                      or not 1 <= value <= 365 or value != int(value)):
                raise ValueError("Срок поставки: целое число от 1 до 365 дней.")
        if self.growth_plan_pct is not None and (not math.isfinite(self.growth_plan_pct)
                                               or not -100 <= self.growth_plan_pct <= 300):
            raise ValueError("Прирост: число от −100 до 300 процентов.")
        for ov in self.overrides.values():
            for key, value in ov.items():
                if key != "category" and (not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                    raise ValueError("Остаток и количество в пути должны быть конечными неотрицательными числами.")

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["test_orders"] = [{**o, "date": str(o.get("date"))} for o in self.test_orders]
        return d


def prepare_lines(tx: pd.DataFrame, months: list[pd.Period]) -> dict[str, pd.DataFrame]:
    """Строки накладных по артикулам (doc+дата), с суммой артикула за месяц — один раз на поставщика."""
    if tx.empty:
        return {}
    t = tx.assign(month=tx["date"].dt.to_period("M"))
    t = t[t["month"].isin(months)]
    if "client_id" in t.columns:
        # Join split invoices for the same anonymous customer on the same day.
        t = t.copy()
        t["doc"] = [f"клиент {c}" if pd.notna(c) and str(c).strip() else d
                    for c, d in zip(t["client_id"], t["doc"])]
        t["date"] = t["date"].dt.normalize()
    lines = t.groupby(["code", "doc", "date", "month"], as_index=False, sort=False, observed=True)["qty"].sum()
    lines["month_tot"] = lines.groupby(["code", "month"], observed=True)["qty"].transform("sum")
    return {c: g for c, g in lines.groupby("code", sort=False)}


def detect_one_offs_fast(lines: pd.DataFrame | None, monthly_sales: np.ndarray, k: float, share: float) -> list[dict]:
    """То же, что detect_one_offs, но на заранее сгруппированных строках (numpy, без groupby)."""
    if lines is None or len(lines) < 5:
        return []
    q = lines["qty"].to_numpy(dtype=float)
    med = float(np.median(q))
    mad = float(np.median(np.abs(q - med))) * 1.4826
    doc_thr = max(med + k * max(mad, 1.0), 3 * med)
    nz = monthly_sales[monthly_sales > 0]
    month_thr = 1.5 * (float(np.median(nz)) if len(nz) else 0.0)
    flag = (q > doc_thr) & (q > month_thr) & (q >= share * lines["month_tot"].to_numpy(dtype=float))
    if not flag.any():
        return []
    sel = lines[flag]
    return [{"doc": r.doc, "date": r.date, "month": r.month, "qty": float(r.qty), "excess": float(r.qty) - med,
             "typical": med} for r in sel.itertuples(index=False)]


# ---------- сезонность и рост на уровне поставщика ----------

def seasonal_index_from_series(s: pd.Series) -> np.ndarray:
    """s: значения по месяцам (index = Period[M]). Индекс по 12 месяцам (среднее = 1)
    как отношение к центрированной 12-мес. скользящей средней (2x12 MA)."""
    s = s.sort_index()
    s = s.reindex(pd.period_range(s.index.min(), s.index.max(), freq="M")) if len(s) else s
    # Keep calendar gaps: dropping zero months would shift the seasonal calendar.
    s = s.clip(lower=0)
    if len(s) < 18:
        return np.ones(12)
    ma = s.rolling(12, center=True).mean().rolling(2).mean().shift(-1)  # 2x12 MA
    ratio = (s / ma.where(ma > 0)).dropna()
    idx = np.ones(12)
    for m in range(1, 13):
        vals = ratio[[p.month == m for p in ratio.index]]
        if len(vals):
            idx[m - 1] = vals.mean()
    return idx / idx.mean()


def supplier_seasonal_index(season: pd.DataFrame) -> np.ndarray:
    s = season.stack().sort_index()
    s.index = [pd.Period(year=y, month=m, freq="M") for y, m in s.index]
    return seasonal_index_from_series(s)


def category_profiles(data: SupplierData, last_full: pd.Period, min_sku: int = 5) -> dict[str, tuple[np.ndarray, float]]:
    """Сезонный индекс и рост г/г по группам товаров (на суммарных продажах группы в штуках)."""
    m = data.monthly[data.monthly["month"] <= last_full]
    cat = data.items["category"]
    m = m.assign(category=m["code"].map(cat)).dropna(subset=["category"])
    active = m[m["sales_1c"] > 0].groupby("category")["code"].nunique()
    out = {}
    for c, g in m.groupby("category"):
        if active.get(c, 0) < min_sku:
            continue
        ser = g.groupby("month")["sales_1c"].sum()
        idx = seasonal_index_from_series(ser)
        ser = ser.sort_index()
        growth = 1.0
        if len(ser) >= 24 and ser.iloc[-24:-12].sum() > 0:
            growth = float(np.clip(ser.iloc[-12:].sum() / ser.iloc[-24:-12].sum(), 0.7, 1.4))
        out[str(c)] = (np.clip(idx, 0.3, 3.0), growth)
    return out


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


def _n(v: float) -> str:
    """Число для человека: 54.7 -> «55», 3.4 -> «3,4»."""
    return f"{v:.0f}" if abs(v) >= 10 else f"{v:.1f}".replace(".", ",").replace(",0", "")


def _days_word(n: int) -> str:
    n = int(n)
    w = "день" if n % 10 == 1 and n % 100 != 11 else (
        "дня" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "дней")
    return f"{n} {w}"


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


def cap_month_spikes(clean: np.ndarray, season: np.ndarray, k: float, ratio: float) -> list[tuple[int, float, float]]:
    """Фильтр Хампеля по месяцам: всплеск, не объяснённый крупной накладной, заменяется типичным
    месяцем (медиана ненулевых месяцев x сезонность) — так же, как у разовой накладной оставляется
    типичная строка. Меняет clean на месте, возвращает [(индекс месяца, было, стало)]."""
    ds = clean / season
    nz = ds[ds > 0]
    if len(nz) < 4:
        return []
    med = float(np.median(nz))
    mad = float(np.median(np.abs(nz - med))) * 1.4826
    thr = max(med + k * mad, ratio * med)
    out = []
    for i in np.where(ds > thr)[0]:
        new = med * season[i]
        out.append((int(i), float(clean[i]), float(new)))
        clean[i] = new
    return out


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


def days_of_supply(qty: float, daily_base: float, start: date, idx: np.ndarray, growth: float, limit: int = 730) -> float:
    """На сколько дней хватит qty с учётом сезонности (помесячно, внутри месяца — линейно)."""
    if daily_base <= 0 or growth <= 0:
        return 9999.0
    left, d, days = qty, start, 0.0
    while days < limit:
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        n = (nxt - d).days
        per_day = daily_base * idx[d.month - 1] * growth
        if per_day * n >= left:
            return days + (left / per_day if per_day > 0 else n)
        left -= per_day * n
        days += n
        d = nxt
    return 9999.0


def supply_schedule(data, code, item, overrides, today, lead, horizon, shipments_by_code):
    """Keep each shipment's date; late/overdue quantities cannot cover this horizon."""
    total = max(float(item["in_transit"] or 0), 0.0)
    flags, schedule = [], []
    if code in shipments_by_code and "in_transit" not in overrides.get(code, {}):
        records = shipments_by_code[code]
    else:
        arrival = item.get("next_arrival")
        records = [{"qty": total, "arrival_date": arrival}] if total else []
    for r in records:
        qty, arrival = max(float(r["qty"]), 0), r["arrival_date"]
        if pd.isna(arrival):
            flags.append("дата товара в пути неизвестна; в сценарии принято прибытие через срок поставки")
            arrival = today + timedelta(days=lead)
        else:
            arrival = pd.Timestamp(arrival).date()
        if arrival < today:
            flags.append("есть просроченная поставка; её количество не вычтено до подтверждения новой даты")
            continue
        schedule.append((arrival, qty))
    end = today + timedelta(days=horizon)
    eligible = sum(q for d, q in schedule if d < end)
    late = sum(q for d, q in schedule if d >= end)
    return schedule, eligible, late, list(dict.fromkeys(flags))


def first_shortage(stock, schedule, daily_base, start, days, idx, growth):
    """First fractional day with unmet demand, including receipts on their actual day."""
    receipts = {}
    for arrival, q in schedule:
        receipts[arrival] = receipts.get(arrival, 0) + q
    available = stock
    for offset in range(days):
        d = start + timedelta(days=offset)
        available += receipts.get(d, 0)
        demand = daily_base * idx[d.month - 1] * growth
        if demand > available and demand > 0:
            return offset + available / demand
        available -= demand
    return None


# ---------- основной расчёт ----------

def _memo(data: SupplierData, key, fn):
    """Кэш тяжёлых промежуточных таблиц на объекте данных (страница проверки считает 10 сценариев)."""
    store = data.__dict__.setdefault("_memo", {})
    if key not in store:
        store[key] = fn()
    return store[key]


def _build_matrix(data: SupplierData, months: list[pd.Period], col: str) -> pd.DataFrame:
    def build():
        m = data.monthly[data.monthly["month"].isin(months)]
        return m.pivot_table(index="code", columns="month", values=col, aggfunc="sum").reindex(columns=months).fillna(0.0)
    return _memo(data, ("matrix", col, tuple(months)), build).copy()


def calculate(data: SupplierData, params: Params | None = None) -> pd.DataFrame:
    p = params or Params()
    p.validate()
    today = data.data_date
    cur = pd.Period(today, freq="M")
    last_full = cur - 1
    months = list(pd.period_range(last_full - (max(p.history_months, 24) - 1), last_full, freq="M"))
    hist = months[-p.history_months:]
    lead = int(p.lead_times.get(data.key) or p.lead_time_days or data.lead_time_days)
    horizon = lead + int(p.review_days)

    tx = data.transactions
    sales = _build_matrix(data, months, "sales_1c")
    stock_open = _build_matrix(data, months + [cur], "stock_open")

    # тестовые разовые заказы (для проверки): добавляются и в накладные, и в месячный спрос
    if p.test_orders:
        extra = pd.DataFrame([{"date": pd.Timestamp(o.get("date") or pd.Timestamp(last_full.start_time) + pd.Timedelta(days=14)),
                               "doc": o.get("doc", "ТЕСТ"), "code": o["code"], "name": "",
                               "warehouse": "", "qty": float(o["qty"])} for o in p.test_orders])
        extra["monthly_only"] = [bool(o.get("monthly_only")) for o in p.test_orders]
        tx = pd.concat([tx, extra[~extra["monthly_only"]].drop(columns="monthly_only")], ignore_index=True)
        for _, o in extra.iterrows():
            mo = o["date"].to_period("M")
            if o["code"] in sales.index and mo in sales.columns:
                sales.loc[o["code"], mo] += o["qty"]

    items = data.items.copy()
    for code, ov in p.overrides.items():
        for k_, v in ov.items():
            if code in items.index and v is not None:
                items.loc[code, k_] = str(v) if k_ == "category" else float(v)
    if p.category:
        items = items[items["category"].astype(str).str.startswith(p.category)]
    if p.codes:
        items = items[items.index.isin(p.codes)]

    completed_season = data.seasonality.copy()
    for year in completed_season.index:
        for month in completed_season.columns:
            if pd.Period(year=int(year), month=int(month), freq="M") > last_full:
                completed_season.loc[year, month] = np.nan
    sup_idx = supplier_seasonal_index(completed_season) if p.use_seasonality else np.ones(12)
    sup_growth = supplier_growth(data.seasonality, last_full) if p.use_growth else 1.0
    cat_prof = _memo(data, ("cat", last_full), lambda: category_profiles(data, last_full)) \
        if (p.use_seasonality or p.use_growth) else {}
    if not p.use_outliers:
        lines_by_code = {}
    elif p.test_orders:
        lines_by_code = prepare_lines(tx, months)
    else:
        lines_by_code = _memo(data, ("lines", tuple(months)), lambda: prepare_lines(tx, months))
    cal = np.array([m.month for m in months])
    hist_mask = np.isin(np.arange(len(months)), np.arange(len(months) - len(hist), len(months)))

    rows = []
    exact_stockouts = data.monthly.pivot_table(index="code", columns="month", values="stockout_days", aggfunc="max") \
        .reindex(columns=months) if "stockout_days" in data.monthly else pd.DataFrame()
    shipments_by_code = {code: g.to_dict("records") for code, g in data.shipments.groupby("code")} \
        if not data.shipments.empty else {}
    for code, it in items.iterrows():
        raw = sales.loc[code].to_numpy() if code in sales.index else np.zeros(len(months))
        if raw[hist_mask].sum() <= 0:
            continue  # нет продаж за окно — не заказываем
        clean = raw.copy()
        reasons, details = [], {"months": [str(m) for m in months], "raw": raw.tolist()}

        # 2. разовые крупные заказы
        one_offs = []
        if p.use_outliers:
            for r in detect_one_offs_fast(lines_by_code.get(code), raw, p.outlier_k, p.outlier_share):
                i = months.index(r["month"])
                removed = min(r["excess"], clean[i])
                clean[i] -= removed
                one_offs.append({"doc": r["doc"], "date": r["date"].strftime("%d.%m.%Y"), "qty": r["qty"],
                                 "removed": float(removed), "month": str(r["month"]), "in_window": bool(hist_mask[i])})

        # 2б. всплески в месячном отчёте без крупной накладной
        spikes = []
        if p.use_outliers:
            for i, was, now in cap_month_spikes(clean, sup_idx[cal - 1], constants.MONTH_SPIKE_K, constants.MONTH_SPIKE_RATIO):
                spikes.append({"month": str(months[i]), "was": was, "now": now, "in_window": bool(hist_mask[i]),
                               "label": f"{MONTH_NAMES[months[i].month - 1]} {months[i].year}"})

        # 4. сезонность: артикул (если хватает истории) + группа + поставщик
        cat_key = str(it["category"])
        cat_idx, cat_g = cat_prof.get(cat_key, (None, None))
        parts = [(sup_idx, 1.0)]
        if p.use_seasonality and cat_idx is not None:
            parts.append((cat_idx, 1.0))
        season_src = ["поставщик"] + (["группа"] if len(parts) > 1 else [])
        if p.use_seasonality and clean.sum() >= 100 and (clean > 0).sum() >= 12 and len(months) >= 24:
            by_m = np.array([clean[cal == m].mean() for m in range(1, 13)])
            if by_m.mean() > 0:
                parts.append((np.clip(by_m / by_m.mean(), 0.3, 3.0), 2.0))
                season_src.insert(0, "артикул")
        idx = sum(w * v for v, w in parts) / sum(w for _, w in parts)
        idx = idx / idx.mean()
        season = idx[cal - 1]

        # 3. упущенный спрос в периоды дефицита
        stockout_months, added = [], 0.0
        so_open = stock_open.loc[code].to_numpy() if code in stock_open.index else np.ones(len(months) + 1)
        is_so = (so_open[:-1] <= 0) | (so_open[1:] <= 0)
        exact_days = exact_stockouts.loc[code].to_numpy() if code in exact_stockouts.index else None
        if exact_days is not None:
            known = np.isfinite(exact_days)
            is_so[known] = exact_days[known] > 0
        ok = ~is_so & (clean >= 0)
        deseason = clean / season
        base_ok = deseason[ok & hist_mask].mean() if (ok & hist_mask).any() else (
            deseason[ok].mean() if ok.any() else deseason.mean())
        streak = 0
        for v in is_so[::-1]:
            if not v:
                break
            streak += 1
        long_so = streak >= constants.LONG_STOCKOUT_MONTHS
        marked = bool(it.get("marked", False))
        restore = is_so.copy()
        if long_so:
            restore[len(restore) - streak:] = False
        if marked:
            restore[:] = False
        if p.use_stockout and ok.any():
            for i in np.where(restore)[0]:
                expected = base_ok * season[i]
                if exact_days is not None and np.isfinite(exact_days[i]):
                    expected = clean[i] + base_ok * season[i] * exact_days[i] / months[i].days_in_month
                if expected > clean[i]:
                    added_i = expected - clean[i]
                    clean[i] = expected
                    if hist_mask[i]:
                        added += added_i
                        stockout_months.append(f"{MONTH_NAMES[months[i].month - 1]} {months[i].year}")
        flags = []
        if bool(it.get("stock_estimated", False)):
            flags.append("остаток оценён без поступлений текущего месяца; сверить с 1С")
        if bool(it.get("moq_missing", False)):
            flags.append("кратность не найдена в справочнике; временно принята 1")
        if it.get("unit") == "м":
            flags.append("расчёт в метрах; проверить перевод в бухты или упаковки перед заказом")
        if marked:
            flags.append(f"в названии пометка «{constants.DISCONTINUED_MARK}» — возможно, товар выводится из "
                         f"ассортимента")
        if long_so and p.use_stockout:
            flags.append(f"товара нет на складе {streak} мес. подряд — проверьте, закупается ли он ещё; "
                         f"недополученные продажи за это время не добавляем")

        # 6. база и разброс
        des_h = (clean / season)[hist_mask]
        base = float(des_h.mean())
        sigma = robust_sigma(des_h, base)

        # 5. рост
        if p.growth_plan_pct is not None:
            g_year, g_src = 1 + p.growth_plan_pct / 100, "плановый"
        elif p.use_growth:
            gp = [(sup_growth, 1.0)]
            if cat_g is not None:
                gp.append((cat_g, 1.0))
            if len(months) >= 24 and clean[:12].sum() > 0:
                gp.append((float(np.clip(clean[-12:].sum() / clean[:12].sum(), 0.5, 1.5)), 2.0))
            g_year, g_src = sum(v * w for v, w in gp) / sum(w for _, w in gp), "по истории"
        else:
            g_year, g_src = 1.0, "не учитывается"
        # база — средний уровень за окно (середина окна ~ history/2 мес. назад), прогноз — середина горизонта
        lag_months = p.history_months / 2 + horizon / 30 / 2
        growth = g_year ** (lag_months / 12) if g_year > 0 else 0.0

        # 7. прогноз и страховой запас
        forecast, season_h = horizon_forecast(base / 30.0, today, horizon, idx, growth)
        safety = p.service_z * sigma * math.sqrt(horizon / 30.0) if growth > 0 else 0.0

        # 8. потребность
        stock_now = max(float(it["stock_now"] or 0), 0.0)
        in_transit = max(float(it["in_transit"] or 0), 0.0)
        schedule, eligible_transit, late_transit, supply_flags = supply_schedule(
            data, code, it, p.overrides, today, lead, horizon, shipments_by_code)
        flags.extend(supply_flags)
        need = forecast + safety - stock_now - eligible_transit
        moq = max(float(it["moq"] or 1), 1.0)
        qty = int(math.ceil(need / moq) * moq) if need > 0 else 0
        qty_calc = qty
        if marked:
            qty = 0  # помеченные позиции не предлагаем автоматически — решение за менеджером

        # 9. срочность
        cover = days_of_supply(stock_now, base / 30.0, today, idx, growth)
        shortage = first_shortage(stock_now, schedule, base / 30.0, today, horizon, idx, growth)
        urgency = "high" if shortage is not None and shortage < lead else (
            "medium" if qty > 0 or shortage is not None else "low")

        # тот же расчёт по «сырым» продажам — без исключения разовых заказов и без учёта дефицита
        des_raw = (raw / season)[hist_mask]
        f_raw, _ = horizon_forecast(float(des_raw.mean()) / 30.0, today, horizon, idx, growth)
        s_raw = p.service_z * robust_sigma(des_raw, float(des_raw.mean())) * math.sqrt(horizon / 30.0) if growth > 0 else 0.0
        need_raw = f_raw + s_raw - stock_now - eligible_transit
        naive_qty = int(math.ceil(need_raw / moq) * moq) if need_raw > 0 else 0

        # обоснование — простым языком, с цифрами (для менеджера, не для программиста)
        oo_in = [o for o in one_offs if o["in_window"]]
        u = it["unit"]
        if flags:
            reasons.append("Проверьте вручную: " + "; ".join(flags) + ".")
        if shortage is not None and shortage < lead:
            reasons.append(f"Риск дефицита через {shortage:.1f} дн., раньше новой поставки. "
                           "Нужно ускорить поставку или согласовать перемещение со склада.")
        if late_transit:
            reasons.append(f"{late_transit:.0f} {it['unit']} в пути придут за пределами горизонта и не уменьшают заказ.")
        now_m = base * idx[today.month - 1]
        reasons.append(f"Обычно продаётся около {_n(base)} {u} в месяц (в среднем за год)"
                       + (f", сейчас по сезону — около {_n(now_m)}." if abs(now_m - base) >= max(1, 0.1 * base) else "."))
        if oo_in:
            tot = sum(o["removed"] for o in oo_in)
            docs = "; ".join(f"накл. {o['doc']} от {o['date']} — {o['qty']:.0f} {u}" for o in oo_in[:3])
            reasons.append(f"Разовые крупные продажи не считаем обычным спросом ({docs}): "
                           f"из расчёта убрано {tot:.0f} {u} сверх обычной покупки.")
        sp_in = [x for x in spikes if x["in_window"]]
        if sp_in:
            reasons.append("Нетипичный всплеск в отчёте продаж сглажен: " + ", ".join(
                f"{x['label']} (было {x['was']:.0f}, считаем {x['now']:.0f})" for x in sp_in[:3]) + ".")
        if stockout_months:
            evidence = "по переданным периодам" if exact_days is not None else "по оценке месячных остатков"
            reasons.append(f"Товара не было на складе {evidence} ({', '.join(stockout_months)}) — "
                           f"к спросу добавлена оценка упущенных продаж {_n(added)} {u}.")
        if p.use_seasonality and abs(season_h - 1) >= 0.03:
            reasons.append(f"Сезон: в ближайшие недели продажи обычно на {abs(season_h - 1) * 100:.0f}% "
                           f"{'выше' if season_h > 1 else 'ниже'} среднего.")
        if g_src == "плановый":
            reasons.append(f"Заложен плановый рост продаж {(g_year - 1) * 100:+.0f}% в год.")
        elif g_src == "по истории" and abs(g_year - 1) >= 0.03:
            reasons.append(f"За последний год продажи {'выросли' if g_year > 1 else 'снизились'} "
                           f"на {abs(g_year - 1) * 100:.0f}% — это учтено.")
        pack = f" (с округлением до упаковки по {moq:.0f} {u})" if moq > 1 else ""
        head = (f"На {_days_word(horizon)} ({_days_word(lead)} на поставку + {_days_word(p.review_days)} до следующего "
                f"заказа) нужно около {forecast:.0f} {u} и {safety:.0f} {u} запаса на случай всплеска. "
                f"На складе {stock_now:.0f}, в пути до конца горизонта {eligible_transit:.0f} из {in_transit:.0f}")
        if qty_calc > 0:
            reasons.append(f"{head} → заказать {qty_calc} {u}{pack}.")
        else:
            reasons.append(f"{head} — этого хватает, заказывать не нужно.")
        if qty_calc != qty:
            reasons.append(f"По расчёту нужно {qty_calc} {u}, но из-за пометки «{constants.DISCONTINUED_MARK}» "
                           f"автоматически не предлагаем — впишите количество вручную, если товар ещё закупается.")
        if naive_qty != qty and (oo_in or sp_in or stockout_months):
            reasons.append(f"Если не чистить данные, получилось бы {naive_qty} {u}.")

        details.update({"clean": clean.tolist(), "season_idx": idx.tolist(), "one_offs": one_offs,
                        "spikes": spikes, "flags": flags, "category": cat_key,
                        "stockout_months": stockout_months, "growth_year": g_year, "lead": lead,
                        "horizon": horizon, "naive_qty": float(naive_qty), "sigma": sigma})
        details.update({"eligible_transit": eligible_transit, "late_transit": late_transit,
                        "stock_estimated": bool(it.get("stock_estimated", False)),
                        "first_shortage_day": shortage,
                        "shipments": [{"date": str(d), "qty": q} for d, q in schedule]})
        rows.append({
            "supplier": data.key, "supplier_name": data.name, "code_1c": code, "article": it["article"],
            "name": it["name"], "category": it["category"], "abc_class": it.get("abc_class", ""),
            "unit": it["unit"], "stock": stock_now, "in_transit": in_transit,
            "regular_demand": base * idx[today.month - 1], "forecast": forecast, "safety_stock": safety,
            "moq": moq, "qty_recommended": qty, "qty_raw": naive_qty,
            "cost": float(it["cost"]) if pd.notna(it.get("cost")) else None,
            "order_value": float(it["cost"]) * qty if pd.notna(it.get("cost")) else None,
            "raw_value": float(it["cost"]) * naive_qty if pd.notna(it.get("cost")) else None, "urgency": urgency, "days_of_cover": min(cover, 9999.0),
            "reason": " ".join(reasons), "details": details,
            "one_off_removed": sum(o["removed"] for o in oo_in) + sum(x["was"] - x["now"] for x in sp_in),
            "stockout_added": added, "needs_review": bool(flags),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    urg_order = {"high": 0, "medium": 1, "low": 2}
    df["_u"] = df["urgency"].map(urg_order)
    return df.sort_values(["supplier", "_u", "days_of_cover"]).drop(columns="_u").reset_index(drop=True)
